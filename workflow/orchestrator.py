from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import git
import yaml

from workflow.logger import WorkflowLogger
from workflow.runtime import has_provider_credentials, load_runtime_config, required_key_env, resolve_runner_path


class WorkflowOrchestrator:
    def __init__(self, config_path: str = "workflow/config.yaml") -> None:
        self.config = self._load_config(config_path)
        self.logger = WorkflowLogger()
        self.repo = git.Repo(".", search_parent_directories=True)
        self.runtime = load_runtime_config()
        self.current_branch: str | None = None
        self.task_counter = 0

    def run_full_cycle(self) -> bool:
        self.logger.info("Запуск полного цикла agents-pipeline")
        try:
            if not self._preflight_runtime():
                return False
            for phase_key in self._get_phase_order():
                if not self.run_phase(phase_key):
                    return False
            return True
        finally:
            summary = self.logger.save_summary()
            self.logger.info(f"Сводка сохранена: {summary}")

    def run_research_phase(self) -> bool:
        if not self._preflight_runtime():
            return False
        return self.run_phase("research")

    def run_implementation_phase(self) -> bool:
        if not self._preflight_runtime():
            return False
        return self.run_phase("implementation")

    def run_deployment_phase(self) -> bool:
        if not self._preflight_runtime():
            return False
        return self.run_phase("deployment")

    def run_phase(self, phase_key: str) -> bool:
        if phase_key == "implementation":
            return self._run_implementation_phase()
        return self._run_standard_phase(phase_key)

    def _run_standard_phase(self, phase_key: str) -> bool:
        phase = self.config["phases"][phase_key]
        self.logger.phase_start(phase["name"])
        ok = self._run_phase_agents(phase, phase_key)
        if not ok:
            self.logger.phase_end(phase["name"], "failed")
            return False

        approval_prompt = phase.get("approval_prompt") or f"Подтвердить результаты фазы {phase['name']}?"
        if phase.get("requires_approval") and not self._wait_for_user(approval_prompt):
            self.logger.phase_end(phase["name"], "rejected")
            return False

        self.logger.phase_end(phase["name"], "success")
        return True

    def _run_implementation_phase(self) -> bool:
        phase = self.config["phases"]["implementation"]
        self.logger.phase_start(phase["name"])
        self.task_counter += 1
        task_id = self.task_counter

        if self.config["git"]["enabled"]:
            if not self._create_git_branch(task_id):
                self.logger.phase_end(phase["name"], "failed")
                return False

        max_retries = next(
            (agent.get("max_retries", 3) for agent in phase["agents"] if agent["name"] == "developer"),
            3,
        )
        for attempt in range(1, max_retries + 1):
            self.logger.info(f"Попытка реализации {attempt}/{max_retries}")
            ok = self._run_phase_agents(phase, "implementation")
            if ok:
                if self.config["git"]["enabled"] and not self._merge_git():
                    self.logger.phase_end(phase["name"], "failed")
                    return False
                self.logger.phase_end(phase["name"], "success")
                return True

            self._save_feedback(task_id, "qa", f"Попытка {attempt} завершилась ошибкой. Проверь логи и исправь регрессии.")
            if self.config["git"]["enabled"] and self.config["git"]["auto_rollback"]:
                self._rollback_git(f"attempt {attempt} failed")
                if attempt < max_retries:
                    self._create_git_branch(task_id)

        self.logger.phase_end(phase["name"], "failed")
        return False

    def _run_phase_agents(self, phase: dict[str, Any], phase_key: str) -> bool:
        total = len(phase["agents"])
        for index, agent in enumerate(phase["agents"], start=1):
            if not self._wait_for_user(f"Запустить агента {agent['name']} ({index}/{total})?"):
                self.logger.warning(f"Агент пропущен: {agent['name']}")
                continue
            if not self._run_agent(agent, phase_key, index=index, total=total):
                return False
        return True

    def _run_agent(self, agent_config: dict[str, Any], phase: str, index: int | None = None, total: int | None = None) -> bool:
        agent_name = agent_config["name"]
        timeout = agent_config.get("timeout", 600)
        agent_dir = Path(".openclaw/agents") / phase / agent_name
        prompt_file = agent_dir / "prompt.md"

        self.logger.agent_start(agent_name, agent_config.get("description", ""))
        if index and total:
            self.logger.agent_progress(agent_name, f"Порядок в фазе: {index}/{total}")
        self.logger.agent_progress(agent_name, f"Фаза: {phase}")
        self.logger.agent_progress(agent_name, f"Провайдер: {self.runtime.provider}")
        self.logger.agent_progress(agent_name, f"Модель: {self.runtime.model}")
        self.logger.agent_progress(agent_name, f"Режим запуска: {self.runtime.run_mode}")

        if not agent_dir.exists():
            self.logger.error(f"Каталог агента не найден: {agent_dir}")
            self.logger.agent_end(agent_name, "failed", "missing agent directory")
            return False
        if not prompt_file.exists():
            self.logger.error(f"Файл prompt.md не найден: {prompt_file}")
            self.logger.agent_end(agent_name, "failed", "missing agent prompt")
            return False

        runner = resolve_runner_path(self.runtime.runner_bin) or self.runtime.runner_bin
        cmd, message = self._build_agent_command(runner, agent_name, agent_config, prompt_file, timeout)

        self.logger.agent_progress(agent_name, "Полная команда OpenClaw:")
        self.logger.agent_progress(agent_name, " ".join(cmd))
        self.logger.agent_progress(agent_name, "")
        self.logger.agent_progress(agent_name, "Полный промпт агента:")
        for line in message.splitlines():
            self.logger.agent_progress(agent_name, line)
        self.logger.agent_progress(agent_name, "")
        self.logger.agent_progress(agent_name, "Ожидание ответа агента...")

        try:
            process = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=self._build_agent_env(),
            )
        except FileNotFoundError:
            self.logger.error("Команда openclaw не найдена", "Проверь установку OpenClaw или переменную OPENCLAW_BIN.")
            self.logger.agent_end(agent_name, "failed", "openclaw missing")
            return False
        except subprocess.TimeoutExpired:
            self.logger.error(f"Агент превысил таймаут: {agent_name}", f"timeout={timeout}")
            self.logger.agent_end(agent_name, "failed", "timeout")
            return False

        if process.stdout:
            parsed_output = self._extract_agent_output(process.stdout)
            self.logger.agent_progress(agent_name, "")
            self.logger.agent_progress(agent_name, "Ответ агента:")
            for line in parsed_output.splitlines():
                self.logger.agent_progress(agent_name, line)
        if process.returncode != 0:
            self.logger.error(f"Агент завершился с ошибкой: {agent_name}", process.stderr.strip())
            self.logger.agent_end(agent_name, "failed", process.stderr.strip())
            return False

        self.logger.agent_end(agent_name, "success", "completed")
        return True

    def _wait_for_user(self, prompt: str) -> bool:
        mode = self.config["workflow"]["mode"]
        if mode == "auto":
            delay = self.config["workflow"].get("auto_continue_delay", 3)
            self.logger.info(f"Автопродолжение через {delay}с: {prompt}")
            time.sleep(delay)
            return True

        answer = input(f"{prompt} [y/n/auto]: ").strip().lower()
        if answer == "auto":
            self.config["workflow"]["mode"] = "auto"
            return True
        return answer in {"y", "yes", ""}

    def _create_git_branch(self, task_id: int) -> bool:
        branch_name = f"{self.config['git']['branch_prefix']}task_{task_id}"
        try:
            self.logger.git_operation("create-branch", branch_name)
            if self.repo.is_dirty(untracked_files=True):
                self.repo.git.add(A=True)
                self.repo.index.commit(f"Auto-commit before {branch_name}")
            self.repo.git.checkout("-b", branch_name)
            self.current_branch = branch_name
            return True
        except Exception as exc:  # pragma: no cover
            self.logger.error("Не удалось создать ветку", str(exc))
            return False

    def _merge_git(self) -> bool:
        if not self.current_branch:
            return True
        try:
            self.logger.git_operation("merge", self.current_branch)
            self.repo.git.checkout("main")
            self.repo.git.merge(self.current_branch)
            self.repo.delete_head(self.current_branch, force=True)
            self.current_branch = None
            return True
        except Exception as exc:  # pragma: no cover
            self.logger.error("Не удалось смержить ветку", str(exc))
            return False

    def _rollback_git(self, reason: str = "") -> bool:
        try:
            self.logger.git_operation("rollback", reason)
            self.repo.git.checkout("main")
            if self.current_branch:
                self.repo.delete_head(self.current_branch, force=True)
            self.current_branch = None
            return True
        except Exception as exc:  # pragma: no cover
            self.logger.error("Не удалось откатить ветку", str(exc))
            return False

    @staticmethod
    def _load_config(config_path: str) -> dict[str, Any]:
        with Path(config_path).open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    def _get_phase_order(self) -> list[str]:
        configured_order = self.config["workflow"].get("phase_order")
        if configured_order:
            return [phase_key for phase_key in configured_order if phase_key in self.config["phases"]]
        return list(self.config["phases"].keys())

    def _save_feedback(self, task_id: int, agent: str, feedback: str) -> Path:
        feedback_dir = Path(".openclaw/feedback")
        feedback_dir.mkdir(parents=True, exist_ok=True)
        feedback_file = feedback_dir / f"task_{task_id}_{agent}.md"
        feedback_file.write_text(
            "\n".join(
                [
                    f"# Feedback from {agent}",
                    "",
                    feedback,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        self.logger.info(f"Файл обратной связи сохранен: {feedback_file}")
        return feedback_file

    def _preflight_runtime(self) -> bool:
        if not self.runtime.preflight_enabled:
            return True

        runner_path = resolve_runner_path(self.runtime.runner_bin)
        if not runner_path:
            self.logger.error(
                "Не найден исполняемый файл OpenClaw",
                f"configured bin={self.runtime.runner_bin}. Обнови .openclaw/config/settings.yaml или OPENCLAW_BIN.",
            )
            return False

        required_key = required_key_env(self.runtime.provider)
        env = self._build_agent_env()
        if required_key and not has_provider_credentials(self.runtime.provider) and required_key not in env:
            self.logger.error(
                "Не найден ключ провайдера",
                f"provider={self.runtime.provider} требует {required_key} или соответствующий профиль в ~/.openclaw.",
            )
            return False

        registered_agents = self._get_registered_agents(runner_path)
        missing_agents = []
        for phase in self.config["phases"].values():
            for agent in phase["agents"]:
                if agent["name"] not in registered_agents:
                    missing_agents.append(agent["name"])
        if missing_agents:
            self.logger.error(
                "Агенты не зарегистрированы в OpenClaw",
                "Выполни `run.bat python manage_agents.py register-all`. Не найдены: " + ", ".join(sorted(missing_agents)),
            )
            return False

        self.logger.info(
            f"Проверка runtime пройдена: runner={runner_path}, provider={self.runtime.provider}, model={self.runtime.model}"
        )
        return True

    def _build_agent_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.runtime.env_overrides)
        return env

    def _build_agent_command(
        self,
        runner: str,
        agent_name: str,
        agent_config: dict[str, Any],
        prompt_file: Path,
        timeout: int,
    ) -> tuple[list[str], str]:
        prompt_text = prompt_file.read_text(encoding="utf-8").strip()
        task = agent_config.get("description", "").strip()
        message_parts = []
        if task:
            message_parts.append(f"Task: {task}")
        message_parts.append(prompt_text)
        message = "\n\n".join(message_parts)

        cmd = [runner, "agent", "--agent", agent_name, "--message", message, "--timeout", str(timeout), "--json"]
        if self.runtime.run_mode == "local":
            cmd.append("--local")
        if self.runtime.thinking:
            cmd.extend(["--thinking", self.runtime.thinking])
        return cmd, message

    @staticmethod
    def _extract_agent_output(stdout: str) -> str:
        text = stdout.strip()
        if not text:
            return ""
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return text

        if isinstance(payload, dict):
            for key in ("output_text", "text", "message", "result"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return text

    def _get_registered_agents(self, runner_path: str) -> set[str]:
        try:
            process = subprocess.run(
                [runner_path, "agents", "list", "--json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                env=self._build_agent_env(),
            )
        except Exception:
            return set()
        if process.returncode != 0 or not process.stdout.strip():
            return set()
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError:
            return set()
        if not isinstance(payload, list):
            return set()
        result = set()
        for item in payload:
            agent_id = item.get("id") if isinstance(item, dict) else None
            if isinstance(agent_id, str):
                result.add(agent_id)
        return result
