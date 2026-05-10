from __future__ import annotations

import json
import os
import subprocess
import time
import traceback
from queue import Empty, Queue
from pathlib import Path
from threading import Thread
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

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
        self.pricing = self._load_pricing("workflow/pricing.yaml")
        self.current_branch: str | None = None
        self.task_counter = 0
        self._registered_agents_cache: dict[str, dict[str, Any]] | None = None
        self._models_list_cache: set[str] | None = None
        self._models_list_attempted = False
        self._models_list_status = "not_attempted"
        self._agent_cli_capabilities: dict[str, bool] | None = None
        self._global_registry_models = self._load_global_registry_models()

    def run_full_cycle(self) -> bool:
        self.logger.info("Запуск полного цикла agents-pipeline")
        try:
            for phase_key in self._get_phase_order():
                if not self._preflight_runtime(phase_key):
                    return False
                if not self.run_phase(phase_key):
                    return False
            return True
        finally:
            summary = self.logger.save_summary()
            self.logger.info(f"Сводка сохранена: {summary}")

    def run_research_phase(self) -> bool:
        if not self._preflight_runtime("research"):
            return False
        return self.run_phase("research")

    def run_implementation_phase(self) -> bool:
        if not self._preflight_runtime("implementation"):
            return False
        return self.run_phase("implementation")

    def run_deployment_phase(self) -> bool:
        if not self._preflight_runtime("deployment"):
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
            self.logger.save_phase_summary(phase_key, phase["name"])
            self.logger.phase_end(phase["name"], "failed")
            return False

        approval_prompt = phase.get("approval_prompt") or f"Подтвердить результаты фазы {phase['name']}?"
        if phase.get("requires_approval") and not self._wait_for_user(approval_prompt):
            self.logger.save_phase_summary(phase_key, phase["name"])
            self.logger.phase_end(phase["name"], "rejected")
            return False

        self.logger.save_phase_summary(phase_key, phase["name"])
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
                    self.logger.save_phase_summary("implementation", phase["name"])
                    self.logger.phase_end(phase["name"], "failed")
                    return False
                self.logger.save_phase_summary("implementation", phase["name"])
                self.logger.phase_end(phase["name"], "success")
                return True

            self._save_feedback(task_id, "qa", f"Попытка {attempt} завершилась ошибкой. Проверь логи и исправь регрессии.")
            if self.config["git"]["enabled"] and self.config["git"]["auto_rollback"]:
                self._rollback_git(f"attempt {attempt} failed")
                if attempt < max_retries:
                    self._create_git_branch(task_id)

        self.logger.save_phase_summary("implementation", phase["name"])
        self.logger.phase_end(phase["name"], "failed")
        return False

    def _run_phase_agents(self, phase: dict[str, Any], phase_key: str) -> bool:
        total = len(phase["agents"])
        fail_fast = bool(phase.get("fail_fast", False))
        had_failures = False
        for index, agent in enumerate(phase["agents"], start=1):
            if not self._wait_for_user(f"Запустить агента {agent['name']} ({index}/{total})?"):
                self.logger.warning(f"Агент пропущен: {agent['name']}")
                continue
            if not self._run_agent(agent, phase_key, index=index, total=total):
                had_failures = True
                if fail_fast:
                    return False
        return not had_failures

    def _run_agent(self, agent_config: dict[str, Any], phase: str, index: int | None = None, total: int | None = None) -> bool:
        agent_name = agent_config["name"]
        timeout = agent_config.get("timeout", 600)
        agent_dir = self._get_agents_root() / phase / agent_name
        prompt_file = agent_dir / "prompt.md"
        agent_runtime = self._resolve_agent_runtime(agent_config)
        executor = self.runtime.executor

        self.logger.agent_start(agent_name, agent_config.get("description", ""))
        if index and total:
            self.logger.agent_progress(agent_name, f"Порядок в фазе: {index}/{total}")
        self.logger.agent_progress(agent_name, f"Фаза: {phase}")
        self.logger.agent_progress(agent_name, f"Провайдер: {self.runtime.provider}")
        self.logger.agent_progress(agent_name, f"Модель: {self.runtime.model}")
        self.logger.agent_progress(agent_name, f"Режим запуска: {self.runtime.run_mode}")

        self.logger.agent_progress(
            agent_name,
            f"Runtime override: provider={agent_runtime['provider']} model={agent_runtime['model']} thinking={agent_runtime['thinking']}",
        )
        self.logger.agent_progress(agent_name, f"Diagnostic agent={agent_name}")
        self.logger.agent_progress(agent_name, f"Diagnostic provider={agent_runtime['provider']}")
        self.logger.agent_progress(agent_name, f"Diagnostic model={agent_runtime['model']}")
        self.logger.agent_progress(agent_name, f"Diagnostic thinking={agent_runtime['thinking']}")
        self.logger.agent_progress(agent_name, f"Diagnostic source={agent_runtime['model_source']}")
        self.logger.agent_progress(agent_name, f"Diagnostic executor={executor}")

        if not agent_dir.exists():
            self.logger.error(f"Каталог агента не найден: {agent_dir}")
            self.logger.agent_end(agent_name, "failed", "missing agent directory")
            return False
        if not prompt_file.exists():
            self.logger.error(f"Файл prompt.md не найден: {prompt_file}")
            self.logger.agent_end(agent_name, "failed", "missing agent prompt")
            return False

        message_bundle = self._build_agent_message_bundle(agent_name, agent_config, prompt_file, phase)
        message = message_bundle["combined_message"]
        prompt_stats = message_bundle["prompt_stats"]

        def save_agent_report(
            status: str,
            result: str,
            elapsed_s: float,
            stdout: str,
            stderr: str,
            parsed_output: str,
            command: str,
            returncode: int | None,
        ) -> None:
            usage = self._extract_usage(stdout, agent_runtime)
            self.logger.save_agent_report(
                phase,
                agent_name,
                {
                    "phase": phase,
                    "agent": agent_name,
                    "agent_name": agent_name,
                    "status": status,
                    "result": result,
                    "elapsed_s": round(elapsed_s, 2),
                    "returncode": returncode,
                    "runtime": agent_runtime,
                    "command": command,
                    "message": message,
                    "prompt_stats": prompt_stats,
                    "stdout": stdout,
                    "stderr": stderr,
                    "parsed_output": parsed_output,
                    "usage": usage,
                },
            )
            self._log_usage_totals(agent_name, phase, usage)

        if executor == "direct_api":
            return self._run_direct_api_agent(
                agent_name,
                phase,
                agent_runtime,
                message_bundle,
                timeout,
                save_agent_report,
            )

        runner = resolve_runner_path(self.runtime.runner_bin) or self.runtime.runner_bin
        runtime_application = self._evaluate_runtime_application(runner, agent_name, agent_runtime)
        self.logger.agent_progress(agent_name, f"Diagnostic requested workflow model={runtime_application['requested_model']}")
        self.logger.agent_progress(agent_name, f"Diagnostic registered agent model={runtime_application['registered_model']}")
        self.logger.agent_progress(agent_name, f"Diagnostic actual command model={runtime_application['actual_command_model']}")
        self.logger.agent_progress(agent_name, f"Diagnostic actual command provider={runtime_application['actual_command_provider']}")
        if runtime_application["warning"]:
            self.logger.warning(runtime_application["warning"])
        if runtime_application["error"]:
            self.logger.error(runtime_application["error"])
            self.logger.agent_end(agent_name, "failed", runtime_application["error"])
            self.logger.save_agent_report(
                phase,
                agent_name,
                {
                    "phase": phase,
                    "agent": agent_name,
                    "agent_name": agent_name,
                    "status": "failed",
                    "result": runtime_application["error"],
                    "elapsed_s": 0.0,
                    "returncode": None,
                    "runtime": agent_runtime,
                    "command": "",
                    "message": message,
                    "prompt_stats": prompt_stats,
                    "stdout": "",
                    "stderr": "",
                    "parsed_output": "",
                },
            )
            return False

        validation = self._verify_model_available(runner, agent_name, agent_runtime)
        self.logger.agent_progress(agent_name, f"Diagnostic validation={validation['method']}")
        if validation["warning"]:
            self.logger.warning(validation["warning"])
        if validation["error"]:
            self.logger.error(validation["error"])
            self.logger.agent_end(agent_name, "failed", validation["error"])
            self.logger.save_agent_report(
                phase,
                agent_name,
                {
                    "phase": phase,
                    "agent": agent_name,
                    "agent_name": agent_name,
                    "status": "failed",
                    "result": validation["error"],
                    "elapsed_s": 0.0,
                    "returncode": None,
                    "runtime": agent_runtime,
                    "command": "",
                    "message": message,
                    "prompt_stats": prompt_stats,
                    "stdout": "",
                    "stderr": "",
                    "parsed_output": "",
                },
            )
            return False
        cmd, message, prompt_stats = self._build_agent_command(
            runner,
            agent_name,
            agent_config,
            prompt_file,
            timeout,
            phase,
            runtime_application,
        )

        self.logger.agent_progress(agent_name, "Полная команда OpenClaw:")
        self.logger.agent_progress(agent_name, " ".join(cmd))
        self.logger.agent_progress(agent_name, "")
        self.logger.agent_progress(
            agent_name,
            (
                "Диагностика prompt/message: "
                f"prompt_chars={prompt_stats['prompt_chars']}, "
                f"prompt_lines={prompt_stats['prompt_lines']}, "
                f"message_chars={prompt_stats['message_chars']}, "
                f"message_lines={prompt_stats['message_lines']}, "
                f"timeout_s={timeout}"
            ),
        )
        self.logger.agent_progress(agent_name, "Полный промпт агента:")
        for line in message.splitlines():
            self.logger.agent_progress(agent_name, line)
        self.logger.agent_progress(agent_name, "")
        self.logger.agent_progress(agent_name, "Ожидание ответа агента...")

        started_at = time.monotonic()
        heartbeat_interval = 15.0
        last_heartbeat = started_at
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=self._build_agent_env(agent_config),
                bufsize=1,
            )
            output_queue: Queue[tuple[str, str]] = Queue()
            stdout_thread = self._start_stream_reader(process.stdout, "stdout", output_queue)
            stderr_thread = self._start_stream_reader(process.stderr, "stderr", output_queue)
            while True:
                self._drain_output_queue(agent_name, output_queue, stdout_lines, stderr_lines)
                if process.poll() is not None:
                    break
                now = time.monotonic()
                elapsed = now - started_at
                if elapsed >= timeout:
                    process.kill()
                    stdout_thread.join(timeout=2)
                    stderr_thread.join(timeout=2)
                    self._drain_output_queue(agent_name, output_queue, stdout_lines, stderr_lines)
                    stdout = "\n".join(stdout_lines)
                    stderr = "\n".join(stderr_lines)
                    timeout_details = [
                        f"timeout_s={timeout}",
                        f"elapsed_s={elapsed:.2f}",
                        f"prompt_chars={prompt_stats['prompt_chars']}",
                        f"message_chars={prompt_stats['message_chars']}",
                    ]
                    tail = self._tail_text(stdout or stderr or "")
                    if tail:
                        timeout_details.append(f"last_output_tail={tail}")
                    self.logger.error(f"Агент превысил таймаут: {agent_name}", " | ".join(timeout_details))
                    save_agent_report("timeout", "timeout", elapsed, stdout, stderr, "", " ".join(cmd), None)
                    self.logger.agent_end(agent_name, "timeout", "timeout")
                    return False
                if now - last_heartbeat >= heartbeat_interval:
                    self.logger.agent_progress(
                        agent_name,
                        f"Все еще выполняется: elapsed_s={elapsed:.0f}/{timeout}",
                    )
                    last_heartbeat = now
                time.sleep(1)
            stdout_thread.join(timeout=2)
            stderr_thread.join(timeout=2)
            self._drain_output_queue(agent_name, output_queue, stdout_lines, stderr_lines)
            stdout = "\n".join(stdout_lines)
            stderr = "\n".join(stderr_lines)
        except FileNotFoundError:
            self.logger.error("Команда openclaw не найдена", "Проверь установку OpenClaw или переменную OPENCLAW_BIN.")
            save_agent_report("failed", "openclaw missing", 0.0, "", "", "", " ".join(cmd), None)
            self.logger.agent_end(agent_name, "failed", "openclaw missing")
            return False

        elapsed = time.monotonic() - started_at
        self.logger.agent_progress(
            agent_name,
            (
                "Диагностика выполнения: "
                f"elapsed_s={elapsed:.2f}, "
                f"returncode={process.returncode}, "
                f"stdout_chars={len(stdout or '')}, "
                f"stderr_chars={len(stderr or '')}"
            ),
        )

        parsed_output = ""
        if stdout:
            parsed_output = self._extract_agent_output(stdout)
            self.logger.agent_progress(agent_name, "")
            self.logger.agent_progress(agent_name, "Ответ агента:")
            for line in parsed_output.splitlines():
                self.logger.agent_progress(agent_name, line)

        failure_reason = self._detect_agent_failure(stdout, stderr, parsed_output)
        if failure_reason:
            details = (
                f"elapsed_s={elapsed:.2f}"
                f" | detected_failure={failure_reason}"
                f" | stdout_tail={self._tail_text(stdout)}"
            )
            stderr_tail = self._tail_text(stderr)
            if stderr_tail:
                details += f" | stderr_tail={stderr_tail}"
            self.logger.error(f"Агент вернул некорректный результат: {agent_name}", details)
            failure_status = self._classify_failure_status(failure_reason)
            save_agent_report(failure_status, failure_reason, elapsed, stdout, stderr, parsed_output, " ".join(cmd), process.returncode)
            self.logger.agent_end(agent_name, failure_status, failure_reason)
            return False

        if process.returncode != 0:
            stderr_tail = self._tail_text(stderr)
            details = f"elapsed_s={elapsed:.2f}"
            if stderr_tail:
                details += f" | stderr_tail={stderr_tail}"
            self.logger.error(f"Агент завершился с ошибкой: {agent_name}", details)
            save_agent_report("failed", stderr.strip() or "non-zero exit", elapsed, stdout, stderr, parsed_output, " ".join(cmd), process.returncode)
            self.logger.agent_end(agent_name, "failed", stderr.strip())
            return False

        save_agent_report("success", "completed", elapsed, stdout, stderr, parsed_output, " ".join(cmd), process.returncode)
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

    @staticmethod
    def _load_pricing(pricing_path: str) -> dict[str, dict[str, float]]:
        pricing_file = Path(pricing_path)
        if not pricing_file.exists():
            return {}
        with pricing_file.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
        return payload if isinstance(payload, dict) else {}

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

    def _preflight_runtime(self, phase_key: str | None = None) -> bool:
        if not self.runtime.preflight_enabled:
            return True

        configured_agents = self._iter_configured_agents_for_preflight(phase_key)
        missing_local_agents = self._get_missing_local_agents(configured_agents)
        if missing_local_agents:
            self.logger.error(
                "Local agent directories are missing",
                "Missing: " + ", ".join(sorted(missing_local_agents)),
            )
            return False

        for configured_phase, agent in configured_agents:
            runtime = self._resolve_agent_runtime(agent)
            self.logger.info(
                "Startup diagnostic: "
                f"phase={configured_phase} "
                f"agent={agent['name']} "
                f"effective provider={runtime['provider']} "
                f"effective model={runtime['model']} "
                f"effective source={runtime['model_source']} "
                f"effective thinking level={runtime['thinking']}"
            )

        if self.runtime.executor == "direct_api":
            return self._preflight_direct_api(configured_agents)

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

        registry_check = self._inspect_registered_agent_records(runner_path)
        registered_agents_map = registry_check["records"]
        self._registered_agents_cache = registered_agents_map
        registered_agents = set(registered_agents_map)
        missing_agents = (
            self._get_missing_registered_agents(configured_agents, registered_agents)
            if registry_check["status"] == "ok"
            else []
        )
        if registry_check["status"] == "timeout":
            self.logger.warning(
                "openclaw agents list --json timed out after 60 seconds; using local agent directories for preflight"
            )
        elif registry_check["status"] != "ok":
            if self.runtime.require_registry_preflight:
                self.logger.error(
                    "Unable to verify the OpenClaw agent registry",
                    "Run `run.bat python manage_agents.py register-all` or disable workflow.require_registry_preflight.",
                )
                return False
            self.logger.warning(
                "openclaw agents list --json did not return usable registry data; using local agent directories because "
                "workflow.require_registry_preflight=false"
            )
        if missing_agents and self.runtime.require_registry_preflight:
            self.logger.error(
                "Агенты не зарегистрированы в OpenClaw",
                "Выполни `run.bat python manage_agents.py register-all`. Не найдены: " + ", ".join(sorted(missing_agents)),
            )
            return False
        if missing_agents:
            self.logger.warning(
                "Some configured agents are missing from `openclaw agents list --json`; continuing because "
                "workflow.require_registry_preflight=false. Missing: " + ", ".join(sorted(missing_agents))
            )

        for model, runtime in self._get_required_startup_models().items():
            validation = self._verify_required_startup_model(runner_path, runtime)
            self.logger.info(f"Startup diagnostic: model={model} validation={validation['method']}")
            if validation["warning"]:
                self.logger.warning(validation["warning"])
            if validation["error"]:
                self.logger.error(validation["error"])
                return False

        self.logger.info(
            f"Проверка runtime пройдена: runner={runner_path}, provider={self.runtime.provider}, model={self.runtime.model}"
        )
        return True

    def _build_agent_env(self, agent_config: dict[str, Any] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.runtime.env_overrides)
        if agent_config:
            runtime = self._resolve_agent_runtime(agent_config)
            if runtime["provider"]:
                env["OPENCLAW_PROVIDER"] = runtime["provider"]
            if runtime["model"]:
                env["OPENCLAW_MODEL"] = runtime["model"]
            if runtime["profile"]:
                env["OPENCLAW_PROFILE"] = runtime["profile"]
        return env

    def _iter_configured_agents(self) -> list[tuple[str, dict[str, Any]]]:
        configured: list[tuple[str, dict[str, Any]]] = []
        for phase_key, phase in self.config.get("phases", {}).items():
            for agent in phase.get("agents", []):
                configured.append((phase_key, agent))
        return configured

    def _iter_configured_agents_for_preflight(self, phase_key: str | None = None) -> list[tuple[str, dict[str, Any]]]:
        if phase_key is None:
            return self._iter_configured_agents()
        phase = self.config.get("phases", {}).get(phase_key, {})
        return [(phase_key, agent) for agent in phase.get("agents", [])]

    def _preflight_direct_api(self, configured_agents: list[tuple[str, dict[str, Any]]]) -> bool:
        env = self._build_agent_env()
        for _phase_key, agent in configured_agents:
            runtime = self._resolve_agent_runtime(agent)
            provider = str(runtime.get("provider") or "").strip().lower()
            if provider != "openrouter":
                self.logger.error(
                    "direct_api executor currently supports only provider=openrouter",
                    f"agent={agent['name']} provider={runtime['provider']}",
                )
                return False
            api_key = env.get("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY", "")
            if not api_key:
                self.logger.error(
                    "OPENROUTER_API_KEY is required for direct_api executor",
                    f"agent={agent['name']} provider={runtime['provider']}",
                )
                return False

        self.logger.info(
            f"Runtime preflight passed for direct_api: provider={self.runtime.provider}, model={self.runtime.model}"
        )
        return True

    def _preflight_openclaw(self, configured_agents: list[tuple[str, dict[str, Any]]]) -> bool:
        runner_path = resolve_runner_path(self.runtime.runner_bin)
        if not runner_path:
            self.logger.error(
                "РќРµ РЅР°Р№РґРµРЅ РёСЃРїРѕР»РЅСЏРµРјС‹Р№ С„Р°Р№Р» OpenClaw",
                f"configured bin={self.runtime.runner_bin}. РћР±РЅРѕРІРё .openclaw/config/settings.yaml РёР»Рё OPENCLAW_BIN.",
            )
            return False

        required_key = required_key_env(self.runtime.provider)
        env = self._build_agent_env()
        if required_key and not has_provider_credentials(self.runtime.provider) and required_key not in env:
            self.logger.error(
                "РќРµ РЅР°Р№РґРµРЅ РєР»СЋС‡ РїСЂРѕРІР°Р№РґРµСЂР°",
                f"provider={self.runtime.provider} С‚СЂРµР±СѓРµС‚ {required_key} РёР»Рё СЃРѕРѕС‚РІРµС‚СЃС‚РІСѓСЋС‰РёР№ РїСЂРѕС„РёР»СЊ РІ ~/.openclaw.",
            )
            return False

        registry_check = self._inspect_registered_agent_records(runner_path)
        registered_agents_map = registry_check["records"]
        self._registered_agents_cache = registered_agents_map
        registered_agents = set(registered_agents_map)
        missing_agents = (
            self._get_missing_registered_agents(configured_agents, registered_agents)
            if registry_check["status"] == "ok"
            else []
        )
        if registry_check["status"] == "timeout":
            self.logger.warning(
                "openclaw agents list --json timed out after 60 seconds; using local agent directories for preflight"
            )
        elif registry_check["status"] != "ok":
            if self.runtime.require_registry_preflight:
                self.logger.error(
                    "Unable to verify the OpenClaw agent registry",
                    "Run `run.bat python manage_agents.py register-all` or disable workflow.require_registry_preflight.",
                )
                return False
            self.logger.warning(
                "openclaw agents list --json did not return usable registry data; using local agent directories because "
                "workflow.require_registry_preflight=false"
            )
        if missing_agents and self.runtime.require_registry_preflight:
            self.logger.error(
                "РђРіРµРЅС‚С‹ РЅРµ Р·Р°СЂРµРіРёСЃС‚СЂРёСЂРѕРІР°РЅС‹ РІ OpenClaw",
                "Р’С‹РїРѕР»РЅРё `run.bat python manage_agents.py register-all`. РќРµ РЅР°Р№РґРµРЅС‹: " + ", ".join(sorted(missing_agents)),
            )
            return False
        if missing_agents:
            self.logger.warning(
                "Some configured agents are missing from `openclaw agents list --json`; continuing because "
                "workflow.require_registry_preflight=false. Missing: " + ", ".join(sorted(missing_agents))
            )

        for model, runtime in self._get_required_startup_models().items():
            validation = self._verify_required_startup_model(runner_path, runtime)
            self.logger.info(f"Startup diagnostic: model={model} validation={validation['method']}")
            if validation["warning"]:
                self.logger.warning(validation["warning"])
            if validation["error"]:
                self.logger.error(validation["error"])
                return False

        self.logger.info(
            f"РџСЂРѕРІРµСЂРєР° runtime РїСЂРѕР№РґРµРЅР°: runner={runner_path}, provider={self.runtime.provider}, model={self.runtime.model}"
        )
        return True

    def _get_agents_root(self) -> Path:
        configured_root = str(self.config.get("paths", {}).get("agents_dir", ".openclaw/agents"))
        return Path(configured_root)

    def _get_missing_local_agents(self, configured_agents: list[tuple[str, dict[str, Any]]]) -> list[str]:
        missing: list[str] = []
        agents_root = self._get_agents_root()
        for phase_key, agent in configured_agents:
            agent_name = str(agent.get("name") or "").strip()
            if not agent_name:
                continue
            agent_dir = agents_root / phase_key / agent_name
            if not agent_dir.is_dir():
                missing.append(f"{phase_key}/{agent_name}")
        return missing

    @staticmethod
    def _get_missing_registered_agents(
        configured_agents: list[tuple[str, dict[str, Any]]],
        registered_agents: set[str],
    ) -> list[str]:
        missing: list[str] = []
        for _phase_key, agent in configured_agents:
            agent_name = str(agent.get("name") or "").strip()
            if agent_name and agent_name not in registered_agents:
                missing.append(agent_name)
        return missing

    def _get_required_startup_models(self) -> dict[str, dict[str, str]]:
        required: dict[str, dict[str, str]] = {}
        required[self.runtime.model] = {
            "provider": self.runtime.provider,
            "model": self.runtime.model,
            "profile": self.runtime.profile,
            "thinking": self.runtime.thinking,
            "model_source": "runtime config",
        }
        for _phase_key, agent in self._iter_configured_agents():
            runtime = self._resolve_agent_runtime(agent)
            required.setdefault(runtime["model"], runtime)
        return required

    def _resolve_agent_runtime(self, agent_config: dict[str, Any]) -> dict[str, str]:
        agent_name = str(agent_config.get("name") or "")
        provider_override = agent_config.get("provider")
        model_override = agent_config.get("model")
        profile_override = agent_config.get("profile")
        thinking_override = agent_config.get("thinking")
        registry_model = self._global_registry_models.get(agent_name, "")

        if provider_override or model_override or profile_override:
            provider = str(provider_override or self.runtime.provider)
            model = str(model_override or registry_model or self.runtime.model)
            model_source = "agent override"
        elif registry_model:
            provider = self._infer_provider_from_model(registry_model) or self.runtime.provider
            model = registry_model
            model_source = "global registry"
        else:
            provider = str(self.runtime.provider)
            model = str(self.runtime.model)
            model_source = "workflow config"

        return {
            "provider": provider,
            "model": model,
            "profile": str(profile_override or self.runtime.profile),
            "thinking": str(thinking_override or self.runtime.thinking),
            "model_source": model_source,
        }

    def _build_agent_message_bundle(
        self,
        agent_name: str,
        agent_config: dict[str, Any],
        prompt_file: Path,
        phase: str,
    ) -> dict[str, Any]:
        prompt_text = prompt_file.read_text(encoding="utf-8").strip()
        task = str(agent_config.get("description", "") or "").strip()
        previous_context = self._build_previous_agent_context(phase, agent_name)
        translation_instruction = (
            "Output format is mandatory. Write the full primary answer in English first. "
            "Then add a second section titled exactly 'Russian translation' with a clear Russian translation "
            "of the full answer. Keep both sections aligned in meaning. "
            "Do not omit the Russian translation section. Do not end the answer before that section appears."
        )

        combined_parts: list[str] = []
        if task:
            combined_parts.append(f"Task: {task}")
        combined_parts.append(prompt_text)
        repository_context = ""
        if self.runtime.executor == "direct_api" and agent_name == "project-analyst":
            repository_context = self._build_direct_api_repository_context(limit=12000)
            if repository_context:
                combined_parts.append(f"Repository context collected locally:\n{repository_context}")
        if previous_context:
            combined_parts.append(f"Previous agent context:\n{previous_context}")
        combined_parts.append(translation_instruction)
        combined_message = "\n\n".join(combined_parts)

        system_parts = [prompt_text]
        if repository_context:
            system_parts.append(f"Repository context collected locally:\n{repository_context}")
        if previous_context:
            system_parts.append(f"Previous agent context:\n{previous_context}")
        system_parts.append(translation_instruction)
        system_message = "\n\n".join(part for part in system_parts if part)
        user_message = task or "Follow the system instructions and produce the requested output format."

        prompt_stats = {
            "prompt_chars": len(prompt_text),
            "prompt_lines": len(prompt_text.splitlines()) if prompt_text else 0,
            "message_chars": len(combined_message),
            "message_lines": len(combined_message.splitlines()) if combined_message else 0,
        }
        return {
            "prompt_text": prompt_text,
            "task": task,
            "previous_context": previous_context,
            "translation_instruction": translation_instruction,
            "system_message": system_message,
            "user_message": user_message,
            "combined_message": combined_message,
            "prompt_stats": prompt_stats,
        }

    def _build_direct_api_repository_context(self, limit: int = 12000) -> str:
        sections = [
            ("Current working directory", str(Path.cwd())),
            ("Git status --short", self._run_local_capture(["git", "status", "--short"])),
            ("Git log --oneline -5", self._run_local_capture(["git", "log", "--oneline", "-5"])),
            ("README.md", self._read_file_excerpt(Path("README.md"), 2000)),
            ("workflow/config.yaml", self._read_file_excerpt(Path("workflow/config.yaml"), 3000)),
            ("workflow/orchestrator.py outline", self._build_python_outline(Path("workflow/orchestrator.py"))),
            ("workflow/runtime.py", self._read_file_excerpt(Path("workflow/runtime.py"), 2000)),
            ("manage_agents.py outline", self._build_python_outline(Path("manage_agents.py"))),
            ("requirements.txt", self._read_file_excerpt(Path("requirements.txt"), 2000)),
            ("pyproject.toml", self._read_file_excerpt(Path("pyproject.toml"), 2000)),
            ("Top-level file tree up to depth 3", self._build_top_level_tree(depth=3)),
            ("Tests file list", self._build_tests_file_list()),
        ]

        chunks: list[str] = []
        total = 0
        for title, content in sections:
            if not content:
                continue
            chunk = f"## {title}\n{content.strip()}"
            remaining = limit - total
            if remaining <= 0:
                break
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            chunks.append(chunk)
            total += len(chunk) + 2
            if total >= limit:
                break

        context = "\n\n".join(chunks)
        if len(context) <= limit:
            return context
        return context[:limit]

    def _run_local_capture(self, command: list[str], timeout: int = 10) -> str:
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=Path.cwd(),
            )
        except Exception:
            return ""
        output = (process.stdout or "").strip()
        if output:
            return output
        return (process.stderr or "").strip()

    def _build_top_level_tree(self, depth: int = 3) -> str:
        root = Path.cwd()
        lines: list[str] = []
        for path in sorted(root.rglob("*")):
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            if len(relative.parts) > depth:
                continue
            if any(part.startswith(".git") for part in relative.parts):
                continue
            lines.append(str(relative).replace("\\", "/"))
        return "\n".join(lines)

    def _read_file_excerpt(self, path: Path, limit: int) -> str:
        if not path.exists() or not path.is_file():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        return text[:limit]

    def _build_python_outline(self, path: Path) -> str:
        if not path.exists() or not path.is_file():
            return ""
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""
        outline: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                outline.append(stripped)
            elif stripped.startswith("class "):
                outline.append(stripped.split(":", 1)[0])
            elif stripped.startswith("def "):
                outline.append(stripped.split(":", 1)[0])
        return "\n".join(outline)

    def _build_tests_file_list(self) -> str:
        tests_dir = Path("tests")
        if not tests_dir.exists():
            return ""
        files = sorted(
            str(path).replace("\\", "/")
            for path in tests_dir.rglob("*")
            if path.is_file()
        )
        return "\n".join(files)

    def _build_agent_command(
        self,
        runner: str,
        agent_name: str,
        agent_config: dict[str, Any],
        prompt_file: Path,
        timeout: int,
        phase: str,
        runtime_application: dict[str, Any] | None = None,
    ) -> tuple[list[str], str, dict[str, int]]:
        message_bundle = self._build_agent_message_bundle(agent_name, agent_config, prompt_file, phase)
        message = str(message_bundle["combined_message"])
        prompt_stats = dict(message_bundle["prompt_stats"])

        runtime = self._resolve_agent_runtime(agent_config)
        cmd = [runner, "agent", "--agent", agent_name, "--message", message, "--timeout", str(timeout), "--json"]
        if self.runtime.run_mode == "local":
            cmd.append("--local")
        if runtime_application and runtime_application.get("supports_provider_override") and runtime["provider"]:
            cmd.extend(["--provider", runtime["provider"]])
        if runtime_application and runtime_application.get("supports_model_override") and runtime["model"]:
            cmd.extend(["--model", runtime["model"]])
        if runtime["thinking"]:
            cmd.extend(["--thinking", runtime["thinking"]])
        return cmd, message, prompt_stats

    @staticmethod
    def _normalize_openrouter_model(model: str) -> str:
        normalized = str(model or "").strip()
        if normalized.startswith("openrouter/"):
            return normalized[len("openrouter/") :]
        return normalized

    @staticmethod
    def _extract_direct_api_text(payload: dict[str, Any]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            return "\n".join(parts).strip()
        return ""

    @staticmethod
    def _classify_direct_api_error(status_code: int, body: str) -> str:
        normalized = body.lower()
        if status_code == 401:
            return "auth_failed"
        if status_code == 429:
            return "rate_limited"
        if "model" in normalized and ("not found" in normalized or "does not exist" in normalized):
            return "model_not_found"
        return "failed"

    def _run_direct_api_agent(
        self,
        agent_name: str,
        phase: str,
        agent_runtime: dict[str, str],
        message_bundle: dict[str, Any],
        timeout: int,
        save_agent_report: Any,
    ) -> bool:
        provider = str(agent_runtime.get("provider") or "").strip().lower()
        if provider != "openrouter":
            error_message = "direct_api executor currently supports only provider=openrouter"
            self.logger.error(error_message)
            save_agent_report("failed", error_message, 0.0, "", error_message, "", "direct_api", 1)
            self.logger.agent_end(agent_name, "failed", error_message)
            return False

        api_key = self._build_agent_env().get("OPENROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY", "")
        if not api_key:
            error_message = "OPENROUTER_API_KEY is required for direct_api executor"
            self.logger.error(error_message)
            save_agent_report("failed", error_message, 0.0, "", error_message, "", "direct_api", 1)
            self.logger.agent_end(agent_name, "failed", error_message)
            return False

        normalized_model = self._normalize_openrouter_model(str(agent_runtime.get("model") or ""))
        command = (
            "direct_api POST https://openrouter.ai/api/v1/chat/completions "
            f"--model {normalized_model}"
        )
        request_payload = {
            "model": normalized_model,
            "messages": [
                {"role": "system", "content": str(message_bundle["system_message"])},
                {"role": "user", "content": str(message_bundle["user_message"])},
            ],
            "temperature": 0.2,
        }
        body = json.dumps(request_payload).encode("utf-8")
        request = urllib_request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost/agents-pipeline",
                "X-Title": "agents-pipeline",
            },
        )

        self.logger.agent_progress(agent_name, "Executor command:")
        self.logger.agent_progress(agent_name, command)
        self.logger.agent_progress(agent_name, "")
        self.logger.agent_progress(
            agent_name,
            (
                "Direct API prompt/message: "
                f"prompt_chars={message_bundle['prompt_stats']['prompt_chars']}, "
                f"prompt_lines={message_bundle['prompt_stats']['prompt_lines']}, "
                f"message_chars={message_bundle['prompt_stats']['message_chars']}, "
                f"message_lines={message_bundle['prompt_stats']['message_lines']}, "
                f"timeout_s={timeout}"
            ),
        )
        self.logger.agent_progress(agent_name, "Full prompt message:")
        for line in str(message_bundle["combined_message"]).splitlines():
            self.logger.agent_progress(agent_name, line)
        self.logger.agent_progress(agent_name, "")
        self.logger.agent_progress(agent_name, "Waiting for direct_api response...")

        started_at = time.monotonic()
        try:
            with urllib_request.urlopen(request, timeout=timeout) as response:
                raw_body = response.read().decode("utf-8", errors="replace")
        except urllib_error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            status = self._classify_direct_api_error(exc.code, response_body)
            elapsed = time.monotonic() - started_at
            self.logger.error(
                f"Direct API request failed: {agent_name}",
                f"status_code={exc.code} | body={self._tail_text(response_body)}",
            )
            save_agent_report(status, status, elapsed, "", response_body, "", command, 1)
            self.logger.agent_end(agent_name, status, status)
            return False
        except Exception:
            elapsed = time.monotonic() - started_at
            error_text = traceback.format_exc()
            self.logger.error(f"Direct API request failed: {agent_name}", error_text)
            save_agent_report("failed", "direct_api request failed", elapsed, "", error_text, "", command, 1)
            self.logger.agent_end(agent_name, "failed", "direct_api request failed")
            return False

        elapsed = time.monotonic() - started_at
        try:
            response_payload = json.loads(raw_body)
        except json.JSONDecodeError:
            self.logger.error(f"Direct API returned invalid JSON: {agent_name}", self._tail_text(raw_body))
            save_agent_report("failed", "invalid direct_api response", elapsed, raw_body, "", "", command, 1)
            self.logger.agent_end(agent_name, "failed", "invalid direct_api response")
            return False

        output_text = self._extract_direct_api_text(response_payload)
        stdout_payload = {
            "output_text": output_text,
            "model": str(response_payload.get("model") or normalized_model),
            "provider": "openrouter",
        }
        usage = response_payload.get("usage")
        if isinstance(usage, dict):
            stdout_payload["usage"] = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
        stdout = json.dumps(stdout_payload, ensure_ascii=False)
        parsed_output = self._extract_agent_output(stdout)
        if parsed_output:
            self.logger.agent_progress(agent_name, "")
            self.logger.agent_progress(agent_name, "Agent response:")
            for line in parsed_output.splitlines():
                self.logger.agent_progress(agent_name, line)

        failure_reason = self._detect_agent_failure(stdout, "", parsed_output)
        if failure_reason:
            failure_status = self._classify_failure_status(failure_reason)
            self.logger.error(
                f"Agent returned invalid direct_api output: {agent_name}",
                f"elapsed_s={elapsed:.2f} | detected_failure={failure_reason} | stdout_tail={self._tail_text(stdout)}",
            )
            save_agent_report(failure_status, failure_reason, elapsed, stdout, "", parsed_output, command, 0)
            self.logger.agent_end(agent_name, failure_status, failure_reason)
            return False

        save_agent_report("success", "completed", elapsed, stdout, "", parsed_output, command, 0)
        self.logger.agent_end(agent_name, "success", "completed")
        return True

    def _build_previous_agent_context(self, phase: str, agent_name: str, limit: int = 4000) -> str:
        agent_dir = self.logger.run_dir / "agents" / phase
        if not agent_dir.exists():
            return ""

        chunks: list[str] = []
        total = 0
        current_safe_name = self.logger._safe_name(agent_name)

        for json_path in sorted(agent_dir.glob("*.json")):
            if json_path.stem == current_safe_name:
                continue
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                continue

            if payload.get("status") != "success":
                continue

            source_text = str(payload.get("parsed_output") or payload.get("stdout") or "").strip()
            if not source_text:
                continue

            heading = f"[{payload.get('agent_name') or payload.get('agent') or json_path.stem}]\n"
            remaining = limit - total
            if remaining <= len(heading):
                break

            body_limit = remaining - len(heading)
            if len(source_text) > body_limit:
                source_text = source_text[-body_limit:]

            chunk = heading + source_text
            chunks.append(chunk)
            total += len(chunk) + 2

            if total >= limit:
                break

        context = "\n\n".join(chunks)
        if len(context) <= limit:
            return context
        return context[-limit:]

    def _load_global_registry_models(self) -> dict[str, str]:
        registry_path = Path.home() / ".openclaw" / "openclaw.json"
        if not registry_path.exists():
            return {}

        try:
            payload = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

        models: dict[str, str] = {}
        for item in payload.get("agents", {}).get("list", []):
            if not isinstance(item, dict):
                continue
            agent_id = item.get("id")
            model = item.get("model")
            if isinstance(agent_id, str) and isinstance(model, str) and model.strip():
                models[agent_id] = model.strip()
        return models

    @staticmethod
    def _infer_provider_from_model(model: str) -> str:
        normalized = model.strip()
        if normalized.startswith("openrouter/"):
            return "openrouter"
        if normalized.startswith("anthropic/"):
            return "anthropic"
        return ""

    def _build_model_list_env(self, agent_runtime: dict[str, str]) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.runtime.env_overrides)
        env.pop("OPENCLAW_MODEL", None)
        if agent_runtime.get("provider"):
            env["OPENCLAW_PROVIDER"] = agent_runtime["provider"]
        if agent_runtime.get("profile"):
            env["OPENCLAW_PROFILE"] = agent_runtime["profile"]
        return env

    def _get_agent_cli_capabilities(self, runner: str) -> dict[str, bool]:
        if self._agent_cli_capabilities is not None:
            return self._agent_cli_capabilities

        process = self._run_openclaw_subprocess(
            [runner, "agent", "--help"],
            env=dict(os.environ),
            timeout=20,
            purpose="openclaw agent help",
        )
        stdout = process.stdout if process else ""
        self._agent_cli_capabilities = {
            "supports_model_override": "--model" in stdout,
            "supports_provider_override": "--provider" in stdout,
        }
        return self._agent_cli_capabilities

    def _evaluate_runtime_application(self, runner: str, agent_name: str, agent_runtime: dict[str, str]) -> dict[str, Any]:
        requested_model = str(agent_runtime.get("model") or "").strip()
        requested_provider = str(agent_runtime.get("provider") or "").strip()
        registered_agents = self._ensure_registered_agents_cache(runner)
        registry_record = registered_agents.get(agent_name, {})
        registered_model = str(registry_record.get("model") or "").strip()
        capabilities = self._get_agent_cli_capabilities(runner)
        supports_model = bool(capabilities.get("supports_model_override"))
        supports_provider = bool(capabilities.get("supports_provider_override"))
        model_source = str(agent_runtime.get("model_source") or "")

        if model_source == "global registry":
            actual_model = registered_model or requested_model
            actual_provider = self._infer_provider_from_model(actual_model) or requested_provider
            return {
                "requested_model": requested_model,
                "registered_model": registered_model,
                "actual_command_model": actual_model,
                "actual_command_provider": actual_provider,
                "supports_model_override": supports_model,
                "supports_provider_override": supports_provider,
                "warning": "",
                "error": "",
            }

        if registered_model and registered_model != requested_model and not (supports_model and supports_provider):
            return {
                "requested_model": requested_model,
                "registered_model": registered_model,
                "actual_command_model": registered_model,
                "actual_command_provider": self._infer_provider_from_model(registered_model) or requested_provider,
                "supports_model_override": supports_model,
                "supports_provider_override": supports_provider,
                "warning": "",
                "error": "Runtime override differs from registered agent model and cannot be applied",
            }

        return {
            "requested_model": requested_model,
            "registered_model": registered_model,
            "actual_command_model": requested_model,
            "actual_command_provider": requested_provider,
            "supports_model_override": supports_model,
            "supports_provider_override": supports_provider,
            "warning": "",
            "error": "",
        }

    @staticmethod
    def _prepare_command_for_windows(command: list[str]) -> list[str]:
        if os.name != "nt" or not command:
            return command
        runner = command[0].lower()
        if not (runner.endswith(".cmd") or runner.endswith(".bat")):
            return command
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/d", "/c", *command]

    def _run_openclaw_subprocess(
        self,
        command: list[str],
        env: dict[str, str],
        timeout: int,
        purpose: str,
    ) -> subprocess.CompletedProcess[str] | None:
        prepared = self._prepare_command_for_windows(command)
        self.logger.info(f"{purpose}: subprocess command={' '.join(prepared)}")
        try:
            process = subprocess.run(
                prepared,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            self.logger.warning(f"{purpose}: subprocess timed out after {timeout}s")
            return None
        except Exception:
            self.logger.error(
                f"{purpose}: subprocess exception",
                traceback.format_exc(),
            )
            return None

        self.logger.info(f"{purpose}: returncode={process.returncode}")
        self.logger.info(f"{purpose}: stdout={process.stdout!r}")
        self.logger.info(f"{purpose}: stderr={process.stderr!r}")
        if not process.stdout.strip():
            self.logger.warning(f"{purpose}: command completed but stdout is empty")
        return process

    def _get_available_models(self, runner: str, agent_runtime: dict[str, str]) -> set[str] | None:
        if self._models_list_attempted:
            return self._models_list_cache

        self._models_list_attempted = True
        self._models_list_status = "running"
        process = self._run_openclaw_subprocess(
            [runner, "models", "list"],
            env=self._build_model_list_env(agent_runtime),
            timeout=20,
            purpose="openclaw models list",
        )
        if process is None:
            self._models_list_status = "timeout"
            return None

        if process.returncode != 0 or not process.stdout.strip():
            self._models_list_status = "failed"
            return None

        available: set[str] = set()
        for line in process.stdout.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("["):
                continue
            available.add(stripped)
            for token in stripped.replace(",", " ").split():
                if "/" in token:
                    available.add(token.strip())

        self._models_list_cache = available
        self._models_list_status = "ok"
        return available

    def _verify_required_startup_model(self, runner: str, agent_runtime: dict[str, str]) -> dict[str, str]:
        model = str(agent_runtime.get("model") or "").strip()
        available_models = self._get_available_models(runner, agent_runtime)
        if available_models is None:
            if not self.runtime.require_model_list_preflight:
                return {
                    "error": "",
                    "method": f"startup models-list {self._models_list_status} ignored",
                    "warning": "OpenClaw model list check timed out; continuing with configured effective models.",
                }
            return {
                "error": "Unable to verify configured model via `openclaw models list`.",
                "method": f"startup models-list {self._models_list_status}",
                "warning": "",
            }
        if model not in available_models:
            return {
                "error": f"Configured startup model is not available: {model}",
                "method": "startup models-list missing",
                "warning": "",
            }
        return {"error": "", "method": "startup models-list", "warning": ""}

    def _verify_model_available(self, runner: str, agent_name: str, agent_runtime: dict[str, str]) -> dict[str, str]:
        model = str(agent_runtime.get("model") or "").strip()
        if not model:
            return {"error": "Configured model is not available: ", "method": "missing model", "warning": ""}

        registered_agents = self._ensure_registered_agents_cache(runner)
        registry_record = registered_agents.get(agent_name, {})
        registry_model = str(registry_record.get("model") or "").strip()
        model_source = str(agent_runtime.get("model_source") or "")

        if model_source == "global registry" and registry_model:
            if model == registry_model:
                return {"error": "", "method": "agents-list cache", "warning": ""}
            return {
                "error": f"Configured model is not available: {model}",
                "method": "agents-list cache mismatch",
                "warning": "",
            }

        available_models = self._get_available_models(runner, agent_runtime)
        if available_models is None:
            if registry_model:
                return {
                    "error": "",
                    "method": "registry fallback after models-list timeout",
                    "warning": (
                        "openclaw models list verification unavailable; continuing because registry already defines "
                        f"model {registry_model} for {agent_name}"
                    ),
                }
            if not self.runtime.require_model_list_preflight:
                return {
                    "error": "",
                    "method": f"configured model fallback after models-list {self._models_list_status}",
                    "warning": "OpenClaw model list check timed out; continuing with configured effective models.",
                }
            return {
                "error": "Unable to verify configured model via `openclaw models list`.",
                "method": "models-list fallback unavailable",
                "warning": "",
            }
        if model not in available_models:
            return {
                "error": f"Configured model is not available: {model}",
                "method": "models-list fallback",
                "warning": "",
            }
        return {"error": "", "method": "models-list fallback", "warning": ""}

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

    @staticmethod
    def _extract_agent_payload(stdout: str) -> dict[str, Any] | None:
        text = stdout.strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _extract_usage(self, stdout: str, agent_runtime: dict[str, str]) -> dict[str, Any]:
        payload = self._extract_agent_payload(stdout)
        runtime_model = str(agent_runtime.get("model") or "")
        runtime_provider = str(agent_runtime.get("provider") or "")
        usage = self._find_usage_payload(payload) if payload else None
        if not usage:
            return {
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "estimated_cost_usd": None,
                "usage_status": "unavailable",
                "model": runtime_model,
                "provider": runtime_provider,
            }

        input_tokens = self._coerce_int(
            usage.get("input_tokens") or usage.get("prompt_tokens") or usage.get("inputTokens")
        )
        output_tokens = self._coerce_int(
            usage.get("output_tokens") or usage.get("completion_tokens") or usage.get("outputTokens")
        )
        total_tokens = self._coerce_int(usage.get("total_tokens") or usage.get("totalTokens"))
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens

        if input_tokens is None and output_tokens is None and total_tokens is None:
            return {
                "input_tokens": None,
                "output_tokens": None,
                "total_tokens": None,
                "estimated_cost_usd": None,
                "usage_status": "unavailable",
                "model": runtime_model,
                "provider": runtime_provider,
            }

        model = str(
            payload.get("model")
            or usage.get("model")
            or runtime_model
        )
        provider = str(
            payload.get("provider")
            or usage.get("provider")
            or runtime_provider
        )
        estimated_cost = self._estimate_cost_usd(model, input_tokens, output_tokens)
        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": estimated_cost,
            "usage_status": "captured",
            "model": model,
            "provider": provider,
        }

    @staticmethod
    def _find_usage_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
        candidates = [
            payload.get("usage"),
            payload.get("token_usage"),
            payload.get("metrics", {}).get("usage") if isinstance(payload.get("metrics"), dict) else None,
        ]
        for candidate in candidates:
            if isinstance(candidate, dict):
                return candidate
        return None

    def _estimate_cost_usd(self, model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
        pricing = self.pricing.get(model)
        if not pricing or input_tokens is None or output_tokens is None:
            return None
        input_rate = float(pricing.get("input_per_1m_usd") or 0.0)
        output_rate = float(pricing.get("output_per_1m_usd") or 0.0)
        cost = (input_tokens / 1_000_000) * input_rate + (output_tokens / 1_000_000) * output_rate
        return round(cost, 6)

    @staticmethod
    def _coerce_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _log_usage_totals(self, agent_name: str, phase: str, usage: dict[str, Any]) -> None:
        agent_cost = usage.get("estimated_cost_usd")
        phase_totals = self.logger.get_phase_totals(phase)
        run_totals = self.logger.get_run_totals()
        self.logger.agent_progress(agent_name, f"Agent cost: {self._format_cost(agent_cost)}")
        self.logger.agent_progress(agent_name, f"Phase total so far: {self._format_cost(phase_totals['estimated_cost_usd'])}")
        self.logger.agent_progress(agent_name, f"Run total so far: {self._format_cost(run_totals['estimated_cost_usd'])}")

    @staticmethod
    def _format_cost(value: Any) -> str:
        if value is None:
            return "unavailable"
        return f"${float(value):.6f}"

    def _start_stream_reader(
        self,
        stream: Any,
        stream_name: str,
        output_queue: Queue[tuple[str, str]],
    ) -> Thread:
        def reader() -> None:
            if stream is None:
                return
            try:
                for line in iter(stream.readline, ""):
                    output_queue.put((stream_name, line.rstrip("\r\n")))
            finally:
                stream.close()

        thread = Thread(target=reader, daemon=True)
        thread.start()
        return thread

    def _drain_output_queue(
        self,
        agent_name: str,
        output_queue: Queue[tuple[str, str]],
        stdout_lines: list[str],
        stderr_lines: list[str],
    ) -> None:
        while True:
            try:
                stream_name, line = output_queue.get_nowait()
            except Empty:
                return

            if stream_name == "stdout":
                stdout_lines.append(line)
                if line.strip():
                    self.logger.agent_progress(agent_name, f"[stdout] {line}")
            else:
                stderr_lines.append(line)
                if line.strip():
                    self.logger.agent_progress(agent_name, f"[stderr] {line}")

    @staticmethod
    def _tail_text(text: str, limit: int = 400) -> str:
        if not text:
            return ""
        normalized = " ".join(text.strip().split())
        if len(normalized) <= limit:
            return normalized
        return normalized[-limit:]

    @staticmethod
    def _detect_agent_failure(stdout: str, stderr: str, parsed_output: str) -> str:
        combined = "\n".join(part for part in (stdout or "", stderr or "", parsed_output or "") if part).lower()
        if not combined:
            return ""
        if "llm request timed out" in combined:
            return "llm request timeout"
        if "the model did not produce a response before the llm idle timeout" in combined:
            return "llm idle timeout"
        if "idle timeout" in combined and "model did not produce a response" in combined:
            return "llm idle timeout"
        if "did not produce a response" in combined:
            return "llm produced no response"
        if parsed_output and "russian translation" not in parsed_output.lower():
            return "missing russian translation section"
        return ""

    @staticmethod
    def _classify_failure_status(failure_reason: str) -> str:
        normalized = failure_reason.strip().lower()
        if "timeout" in normalized:
            return "timeout"
        if normalized:
            return "invalid_output"
        return "failed"

    def _ensure_registered_agents_cache(self, runner_path: str) -> dict[str, dict[str, Any]]:
        if self._registered_agents_cache is None:
            self._registered_agents_cache = self._get_registered_agent_records(runner_path)
        return self._registered_agents_cache

    def _inspect_registered_agent_records(self, runner_path: str) -> dict[str, Any]:
        command = [runner_path, "agents", "list", "--json"]
        prepared = self._prepare_command_for_windows(command)
        self.logger.info(f"openclaw agents list: subprocess command={' '.join(prepared)}")
        try:
            process = subprocess.run(
                prepared,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
                env=self._build_agent_env(),
            )
        except subprocess.TimeoutExpired:
            return {"records": {}, "status": "timeout"}
        except Exception:
            self.logger.error(
                "openclaw agents list: subprocess exception",
                traceback.format_exc(),
            )
            return {"records": {}, "status": "error"}

        self.logger.info(f"openclaw agents list: returncode={process.returncode}")
        self.logger.info(f"openclaw agents list: stdout={process.stdout!r}")
        self.logger.info(f"openclaw agents list: stderr={process.stderr!r}")

        if process.returncode != 0 or not process.stdout.strip():
            return {"records": {}, "status": "unavailable"}
        try:
            payload = json.loads(process.stdout)
        except json.JSONDecodeError:
            return {"records": {}, "status": "invalid_json"}
        if not isinstance(payload, list):
            return {"records": {}, "status": "invalid_payload"}

        result: dict[str, dict[str, Any]] = {}
        for item in payload:
            agent_id = item.get("id") if isinstance(item, dict) else None
            if isinstance(agent_id, str):
                result[agent_id] = item
        return {"records": result, "status": "ok"}

    def _get_registered_agent_records(self, runner_path: str) -> dict[str, dict[str, Any]]:
        return self._inspect_registered_agent_records(runner_path)["records"]
