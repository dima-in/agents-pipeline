from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import time
import traceback
from fnmatch import fnmatch
from queue import Empty, Queue
from pathlib import Path
from threading import Thread
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlparse

import git
import yaml

from workflow.logger import WorkflowLogger
from workflow.runtime import has_provider_credentials, load_runtime_config, required_key_env, resolve_runner_path


class WorkflowOrchestrator:
    def __init__(
        self,
        config_path: str = "workflow/config.yaml",
        *,
        engine_root: str | None = None,
        launch_cwd: str | None = None,
        workspace: str | None = None,
        project_id: str | None = None,
        no_memory: bool = False,
        fresh_run: bool = False,
        task_scope: str | None = None,
        research_run: str | None = None,
        allow_scope_expansion: bool = False,
    ) -> None:
        self.engine_root = Path(engine_root).resolve() if engine_root else Path(__file__).resolve().parent.parent
        self.launch_cwd = Path(launch_cwd).resolve() if launch_cwd else Path(os.environ.get("AGENTS_PIPELINE_LAUNCH_CWD") or os.getcwd()).resolve()
        self.config_path = self._resolve_engine_path(config_path)
        self.config = self._load_config(self.config_path)
        self.target_workspace = self._resolve_target_workspace(workspace)
        self.context_mode = self._resolve_context_mode()
        self.repo = self._load_target_repo()
        self.git_remote = self._detect_git_remote()
        self.project_id = self._resolve_project_id(project_id)
        self.project_state_dir = self._ensure_project_state_dirs()
        self.repository_context_root = self.engine_root if self.context_mode == "engine_self_analysis" else self.target_workspace
        self.retrieval_root = self.target_workspace
        logs_root = self._engine_path(self.config.get("paths", {}).get("logs_dir", ".openclaw/logs"))
        self.logger = WorkflowLogger(log_dir=str(logs_root / self.project_id))
        self.handoff_summary_root = self.logger.run_dir / "agents" / "research"
        self.logs_root = self.logger.log_dir
        self.runtime = load_runtime_config(
            engine_root=self.engine_root,
            workflow_settings=self.config,
            workspace=self.target_workspace,
        )
        self.pricing = self._load_pricing(self._engine_path("workflow/pricing.yaml"))
        self.max_phase_cost_usd = self._coerce_float(self.config.get("workflow", {}).get("max_phase_cost_usd"))
        self.no_memory = no_memory
        self.fresh_run = fresh_run
        self.task_scope_override = str(task_scope or "").strip()
        self.research_run_id = str(research_run or "").strip()
        self.allow_scope_expansion = allow_scope_expansion
        self.implementation_scope_policy = self._get_implementation_scope_policy()
        self.current_branch: str | None = None
        self.task_counter = 0
        self._phase_failure_status: str | None = None
        self._agent_report_extras: dict[tuple[str, str], dict[str, Any]] = {}
        self._registered_agents_cache: dict[str, dict[str, Any]] | None = None
        self._models_list_cache: set[str] | None = None
        self._models_list_attempted = False
        self._models_list_status = "not_attempted"
        self._agent_cli_capabilities: dict[str, bool] | None = None
        self._global_registry_models = self._load_global_registry_models()
        self._log_startup_diagnostics()

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
        self._phase_failure_status = None
        self.logger.phase_start(phase["name"])
        implementation_context = self._build_implementation_phase_context(limit=12000)
        if implementation_context["context_chars"] == 0:
            self.logger.error(
                f"No research handoff found for project_id={self.project_id}. Run research phase first or specify --research-run."
            )
            self.logger.phase_end(phase["name"], "failed")
            return False
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
            if not ok and self._phase_failure_status in {"scope_violation", "no_changes"}:
                if self.config["git"]["enabled"] and self.config["git"]["auto_rollback"]:
                    self._rollback_git(self._phase_failure_status.replace("_", " "))
                self.logger.save_phase_summary("implementation", phase["name"])
                self.logger.phase_end(phase["name"], self._phase_failure_status)
                return False
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
        self.logger.phase_end(phase["name"], self._phase_failure_status or "failed")
        return False

    def _run_phase_agents(self, phase: dict[str, Any], phase_key: str) -> bool:
        total = len(phase["agents"])
        fail_fast = bool(phase.get("fail_fast", False))
        had_failures = False
        for index, agent in enumerate(phase["agents"], start=1):
            if self._phase_cost_limit_exceeded(phase_key):
                had_failures = True
                break
            if not self._wait_for_user(f"Запустить агента {agent['name']} ({index}/{total})?"):
                self.logger.warning(f"Агент пропущен: {agent['name']}")
                continue
            if phase_key == "implementation" and agent["name"] == "developer":
                if not self._enforce_implementation_scope_plan():
                    had_failures = True
                    return False
            if not self._run_agent(agent, phase_key, index=index, total=total):
                had_failures = True
                if fail_fast:
                    return False
            if phase_key == "implementation" and agent["name"] == "developer":
                if not self._enforce_implementation_scope_diff():
                    had_failures = True
                    return False
            if self._phase_cost_limit_exceeded(phase_key):
                had_failures = True
                break
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
        self.logger.agent_progress(agent_name, f"Diagnostic engine_root={self.engine_root}")
        self.logger.agent_progress(agent_name, f"Diagnostic target_workspace={self.target_workspace}")
        self.logger.agent_progress(agent_name, f"Diagnostic launch_cwd={self.launch_cwd}")
        self.logger.agent_progress(agent_name, f"Diagnostic project_id={self.project_id}")
        self.logger.agent_progress(agent_name, f"Diagnostic git_remote={self.git_remote or 'unavailable'}")
        self.logger.agent_progress(agent_name, f"Diagnostic context_mode={self.context_mode}")
        self.logger.agent_progress(agent_name, f"Diagnostic repository_context_root={self.repository_context_root}")
        self.logger.agent_progress(agent_name, f"Diagnostic retrieval_root={self.retrieval_root}")
        self.logger.agent_progress(agent_name, f"Diagnostic handoff_summary_root={self.handoff_summary_root}")
        self.logger.agent_progress(agent_name, f"Diagnostic logs_root={self.logs_root}")

        if not agent_dir.exists():
            self.logger.error(f"Каталог агента не найден: {agent_dir}")
            self.logger.agent_end(agent_name, "failed", "missing agent directory")
            return False
        if not prompt_file.exists():
            self.logger.error(f"Файл prompt.md не найден: {prompt_file}")
            self.logger.agent_end(agent_name, "failed", "missing agent prompt")
            return False
        if phase == "implementation" and agent_name == "qa":
            qa_diff = self._collect_scope_watchdog_diff_diagnostics()
            if qa_diff["changed_files_count"] == 0:
                self.logger.save_agent_report(
                    phase,
                    agent_name,
                    {
                        "phase": phase,
                        "agent": agent_name,
                        "agent_name": agent_name,
                        "status": "no_changes",
                        "result": "QA cannot run because there is no git diff to inspect.",
                        "elapsed_s": 0.0,
                        "returncode": 0,
                        "runtime": agent_runtime,
                        "command": "",
                        "message": "",
                        "prompt_stats": {},
                        "stdout": "",
                        "stderr": "",
                        "parsed_output": "",
                        "usage": {},
                        "developer_changed_files": [],
                        "developer_diff_lines": 0,
                        "write_tools_used": [],
                        "no_changes_detected": True,
                    },
                )
                self.logger.agent_end(agent_name, "no_changes", "QA cannot run because there is no git diff to inspect.")
                self._phase_failure_status = "no_changes"
                return False

        message_bundle = self._build_agent_message_bundle(agent_name, agent_config, prompt_file, phase)
        message = message_bundle["combined_message"]
        prompt_stats = message_bundle["prompt_stats"]
        self.logger.agent_progress(agent_name, f"Diagnostic context_profile={message_bundle['context_profile']}")
        self.logger.agent_progress(agent_name, f"Diagnostic repository_context_chars={message_bundle['repository_context_chars']}")
        self.logger.agent_progress(agent_name, f"Diagnostic handoff_summary_chars={message_bundle['handoff_summary_chars']}")
        self.logger.agent_progress(agent_name, f"Diagnostic retrieval_enabled={message_bundle['retrieval_enabled']}")
        self.logger.agent_progress(agent_name, f"Diagnostic retrieval_rounds={message_bundle['retrieval_rounds']}")
        self.logger.agent_progress(agent_name, f"Diagnostic target_workspace={self.target_workspace}")
        self.logger.agent_progress(agent_name, f"Diagnostic implementation_context_chars={message_bundle['implementation_context_chars']}")
        self.logger.agent_progress(agent_name, f"Diagnostic research_handoff_sources={', '.join(message_bundle['research_handoff_sources'])}")
        self.logger.agent_progress(agent_name, f"Diagnostic selected_task_scope={message_bundle['selected_task_scope']}")
        self.logger.agent_progress(agent_name, f"Diagnostic implementation_retrieval_enabled={message_bundle['implementation_retrieval_enabled']}")

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
            extra_fields = self._get_agent_report_extras(phase, agent_name)
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
                    "context_profile": message_bundle["context_profile"],
                    "repository_context_chars": message_bundle["repository_context_chars"],
                    "handoff_summary_chars": message_bundle["handoff_summary_chars"],
                    "context_chars": message_bundle["repository_context_chars"],
                    "previous_summary_chars": message_bundle["handoff_summary_chars"],
                    "retrieval_enabled": message_bundle["retrieval_enabled"],
                    "retrieval_rounds": message_bundle["retrieval_rounds"],
                    "implementation_context_chars": message_bundle["implementation_context_chars"],
                    "research_handoff_sources": message_bundle["research_handoff_sources"],
                    "selected_task_scope": message_bundle["selected_task_scope"],
                    "implementation_retrieval_enabled": message_bundle["implementation_retrieval_enabled"],
                    "target_workspace": str(self.target_workspace),
                    "project_id": self.project_id,
                    "git_remote": self.git_remote,
                    "context_mode": self.context_mode,
                    "repository_context_root": str(self.repository_context_root),
                    "retrieval_root": str(self.retrieval_root),
                    "handoff_summary_root": str(self.handoff_summary_root),
                    "logs_root": str(self.logs_root),
                    "stdout": stdout,
                    "stderr": stderr,
                    "parsed_output": parsed_output,
                    "handoff_summary": (
                        self._build_research_handoff_summary(agent_name, parsed_output)
                        if phase == "research" and status == "success" and parsed_output
                        else ""
                    ),
                    "usage": usage,
                    **extra_fields,
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
                cwd=self.target_workspace,
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
        if self.repo is None:
            self.logger.error("РќРµ СѓРґР°Р»РѕСЃСЊ СЃРѕР·РґР°С‚СЊ РІРµС‚РєСѓ", f"Git repository not found under {self.target_workspace}")
            return False
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
        if self.repo is None:
            self.logger.error("РќРµ СѓРґР°Р»РѕСЃСЊ СЃРјРµСЂР¶РёС‚СЊ РІРµС‚РєСѓ", f"Git repository not found under {self.target_workspace}")
            return False
        try:
            self.logger.git_operation("merge", self.current_branch)
            self.repo.git.checkout(self._default_branch())
            self.repo.git.merge(self.current_branch)
            self.repo.delete_head(self.current_branch, force=True)
            self.current_branch = None
            return True
        except Exception as exc:  # pragma: no cover
            self.logger.error("Не удалось смержить ветку", str(exc))
            return False

    def _rollback_git(self, reason: str = "") -> bool:
        if self.repo is None:
            self.logger.error("РќРµ СѓРґР°Р»РѕСЃСЊ РѕС‚РєР°С‚РёС‚СЊ РІРµС‚РєСѓ", f"Git repository not found under {self.target_workspace}")
            return False
        try:
            self.logger.git_operation("rollback", reason)
            self.repo.git.checkout(self._default_branch())
            if self.current_branch:
                self.repo.delete_head(self.current_branch, force=True)
            self.current_branch = None
            return True
        except Exception as exc:  # pragma: no cover
            self.logger.error("Не удалось откатить ветку", str(exc))
            return False

    @staticmethod
    def _load_config(config_path: Path) -> dict[str, Any]:
        with config_path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    @staticmethod
    def _load_pricing(pricing_path: Path) -> dict[str, dict[str, float]]:
        pricing_file = pricing_path
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
        feedback_dir = self._engine_path(self.config.get("paths", {}).get("feedback_dir", ".openclaw/feedback"))
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
        return self._engine_path(configured_root)

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
        translation_instruction = (
            "Output format is mandatory. Write the full primary answer in English first. "
            "Then add a second section titled exactly 'Russian translation' with a clear Russian translation "
            "of the full answer. Keep both sections aligned in meaning. "
            "Do not omit the Russian translation section. Do not end the answer before that section appears."
        )
        context_profile = "default"
        repository_context = ""
        handoff_sources: list[str] = []
        selected_task_scope = ""
        implementation_retrieval_enabled = False
        implementation_context_chars = 0
        retrieval_enabled = self.runtime.executor == "direct_api" and phase == "research"
        if retrieval_enabled:
            context_profile = self._get_research_context_profile(agent_name)
            repository_context = self._build_direct_api_repository_context(agent_name=agent_name, limit=12000)
            previous_context = self._build_research_summary_context(agent_name, limit=4000)
            retrieval_enabled = self._is_retrieval_enabled_for_research_agent(agent_name)
        elif phase == "implementation":
            context_profile = "implementation_delivery"
            implementation_context = self._build_implementation_phase_context(agent_name=agent_name, limit=12000)
            repository_context = implementation_context["repository_context"]
            previous_context = implementation_context["previous_context"]
            handoff_sources = implementation_context["research_handoff_sources"]
            selected_task_scope = implementation_context["selected_task_scope"]
            implementation_context_chars = implementation_context["context_chars"]
            implementation_retrieval_enabled = self.runtime.executor == "direct_api" and self._is_implementation_retrieval_enabled(agent_name)
            retrieval_enabled = implementation_retrieval_enabled
        else:
            previous_context = self._build_previous_agent_context(phase, agent_name)
            implementation_context_chars = 0

        combined_parts: list[str] = []
        if task:
            combined_parts.append(f"Task: {task}")
        combined_parts.append(prompt_text)
        if repository_context:
            combined_parts.append(f"Repository context collected locally:\n{repository_context}")
        if previous_context:
            combined_parts.append(f"Previous agent context:\n{previous_context}")
        if phase == "implementation":
            combined_parts.append(self._build_implementation_scope_instruction(selected_task_scope))
        combined_parts.append(translation_instruction)
        combined_message = "\n\n".join(combined_parts)

        system_parts = [prompt_text]
        if repository_context:
            system_parts.append(f"Repository context collected locally:\n{repository_context}")
        if retrieval_enabled:
            system_parts.append(self._build_direct_api_retrieval_instruction(phase=phase, agent_name=agent_name))
        if previous_context:
            system_parts.append(f"Previous agent context:\n{previous_context}")
        if phase == "implementation":
            system_parts.append(self._build_implementation_scope_instruction(selected_task_scope))
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
            "context_profile": context_profile,
            "repository_context_chars": len(repository_context),
            "handoff_summary_chars": len(previous_context),
            "context_chars": len(repository_context),
            "previous_summary_chars": len(previous_context),
            "retrieval_enabled": retrieval_enabled,
            "retrieval_rounds": 0,
            "implementation_context_chars": implementation_context_chars if phase == "implementation" else 0,
            "research_handoff_sources": handoff_sources,
            "selected_task_scope": selected_task_scope,
            "implementation_retrieval_enabled": implementation_retrieval_enabled,
            "translation_instruction": translation_instruction,
            "system_message": system_message,
            "user_message": user_message,
            "combined_message": combined_message,
            "prompt_stats": prompt_stats,
        }

    @staticmethod
    def _get_research_context_profile(agent_name: str) -> str:
        profiles = {
            "project-analyst": "repo_overview_full",
            "competitor-analyst": "external_comparison",
            "market-analyst": "market_positioning",
            "tech-analyst": "technical_architecture",
            "innovation-scout": "external_innovation",
            "product-manager": "research_synthesis",
        }
        return profiles.get(agent_name, "research_default")

    @staticmethod
    def _is_retrieval_enabled_for_research_agent(agent_name: str) -> bool:
        return agent_name in {"project-analyst", "tech-analyst"}

    @staticmethod
    def _is_implementation_retrieval_enabled(agent_name: str) -> bool:
        return agent_name in {"architect", "developer", "qa", "template-validator"}

    @staticmethod
    def _default_implementation_scope_policy() -> dict[str, Any]:
        return {
            "allowed_paths": [
                "gateway-v4/app/services/monitoring.py",
                "gateway-v4/app/services/routing.py",
                "gateway-v4/app/services/proxy.py",
                "gateway-v4/app/routers/chat.py",
                "gateway-v4/app/routers/admin.py",
                "gateway-v4/app/models.py",
                "gateway-v4/app/database.py",
                "gateway-v4/alembic/versions/*",
                "gateway-v4/tests/*",
                "tests/*",
                "docs/*",
                "README.md",
                "gateway-v4/README.md",
            ],
            "forbidden_paths": [
                "frontend/*",
                "gateway-v4/app/routers/billing.py",
                "gateway-v4/app/routers/auth.py",
                "gateway-v4/app/services/auth.py",
                "gateway-v4/app/services/billing.py",
                "gateway-v4/app/services/marketplace.py",
                ".github/*",
                "docker-compose.yml",
                "docker-compose.prod.yml",
                "gateway-v4/docker-compose.yml",
                "gateway-v4/docker-compose.prod.yml",
                "gateway-v4/Dockerfile",
                "gateway-v4/Dockerfile.prod",
            ],
            "forbidden_keywords": ["stripe", "billing", "subscription", "payment", "checkout", "marketplace"],
            "max_changed_files": 8,
            "max_diff_lines": 320,
        }

    def _get_implementation_scope_policy(self) -> dict[str, Any]:
        base = self._default_implementation_scope_policy()
        overrides = self.config.get("workflow", {}).get("implementation_scope_policy", {}) or {}
        for key in ("allowed_paths", "forbidden_paths", "forbidden_keywords"):
            value = overrides.get(key)
            if isinstance(value, list) and value:
                base[key] = [str(item).replace("\\", "/").strip() for item in value if str(item).strip()]
        for key in ("max_changed_files", "max_diff_lines"):
            value = overrides.get(key)
            if value is not None:
                try:
                    base[key] = int(value)
                except (TypeError, ValueError):
                    pass
        return base

    def _get_agent_report_extras(self, phase: str, agent_name: str) -> dict[str, Any]:
        return dict(self._agent_report_extras.get((phase, agent_name), {}))

    def _set_agent_report_extras(self, phase: str, agent_name: str, extras: dict[str, Any]) -> None:
        current = dict(self._agent_report_extras.get((phase, agent_name), {}))
        current.update(extras)
        self._agent_report_extras[(phase, agent_name)] = current

    def _build_direct_api_repository_context(self, agent_name: str = "project-analyst", limit: int = 12000) -> str:
        if self.context_mode == "external_project_analysis":
            return self._build_external_project_repository_context(agent_name=agent_name, limit=limit)
        return self._build_engine_self_repository_context(agent_name=agent_name, limit=limit)

    def _build_engine_self_repository_context(self, agent_name: str = "project-analyst", limit: int = 12000) -> str:
        profile = self._get_research_context_profile(agent_name)
        if profile == "repo_overview_full":
            sections = [
                ("Git status --short", self._run_local_capture(["git", "status", "--short"], cwd=self.target_workspace)),
                ("Git log --oneline -5", self._run_local_capture(["git", "log", "--oneline", "-5"], cwd=self.target_workspace)),
                ("README.md", self._read_file_excerpt(self.target_workspace / "README.md", 2000)),
                ("workflow/config.yaml", self._read_file_excerpt(self.engine_root / "workflow/config.yaml", 3000)),
                ("workflow/orchestrator.py outline", self._build_python_outline(self.engine_root / "workflow/orchestrator.py")),
                ("workflow/runtime.py", self._read_file_excerpt(self.engine_root / "workflow/runtime.py", 2000)),
                ("manage_agents.py outline", self._build_python_outline(self.engine_root / "manage_agents.py")),
                ("Top-level file tree up to depth 3", self._build_top_level_tree(root=self.target_workspace, depth=3)),
                ("Tests summary/list", self._build_tests_file_list()),
            ]
        elif profile == "external_comparison":
            sections = [
                ("Compressed project-analyst summary", self._build_fallback_project_summary()),
                ("README.md", self._read_file_excerpt(self.target_workspace / "README.md", 1400)),
                ("Compact architecture summary", self._build_compact_architecture_summary()),
                ("Default competitors", "LangGraph, CrewAI, AutoGen, OpenHands, Claude Code, Codex CLI, OpenClaw, Dify, n8n"),
                ("External research instruction", "Use Perplexity/Sonar for external comparison. Do not ask clarification."),
            ]
        elif profile == "market_positioning":
            sections = [
                ("Positioning", self._build_positioning_summary()),
                ("Workflow goals", self._build_workflow_goals_summary()),
                ("Target users and use cases", self._build_target_users_summary()),
            ]
        elif profile == "technical_architecture":
            sections = [
                ("Execution architecture", self._build_execution_architecture_summary()),
                ("workflow/orchestrator.py outline", self._build_python_outline(self.engine_root / "workflow/orchestrator.py")),
                (
                    "direct_api implementation",
                    self._read_python_sections(
                        self.engine_root / "workflow/orchestrator.py",
                        ["def _perform_direct_api_request(", "def _normalize_openrouter_model(", "def _run_direct_api_agent("],
                        4000,
                    ),
                ),
                (
                    "retrieval-loop implementation",
                    self._read_python_sections(
                        self.engine_root / "workflow/orchestrator.py",
                        [
                            "def _build_direct_api_retrieval_instruction(",
                            "def _parse_direct_api_retrieval_request(",
                            "def _execute_direct_api_retrieval_request(",
                            "def _direct_api_read_files(",
                            "def _direct_api_search_text(",
                            "def _direct_api_list_files(",
                        ],
                        4000,
                    ),
                ),
                ("workflow/runtime.py", self._read_file_excerpt(self.engine_root / "workflow/runtime.py", 2200)),
                ("Relevant orchestration tests", self._build_tests_subset(["agent_reports", "workflow", "orchestrator"], limit=2000)),
            ]
        elif profile == "external_innovation":
            sections = [
                ("Compressed project summary", self._build_fallback_project_summary()),
                ("Compact architecture summary", self._build_compact_architecture_summary()),
                ("Known constraints and problems", self._build_known_constraints_summary()),
                ("Roadmap context", self._build_workflow_goals_summary()),
                ("External inspiration focus", "Look at agent/workflow tools for product, UX, and automation ideas."),
                ("External research instruction", "Use Perplexity/Sonar for external inspiration. Do not ask clarification."),
            ]
        elif profile == "research_synthesis":
            sections = []
        else:
            sections = [("README.md", self._read_file_excerpt(self.target_workspace / "README.md", 1500))]

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

    def _build_external_project_repository_context(self, agent_name: str = "project-analyst", limit: int = 12000) -> str:
        profile = self._get_research_context_profile(agent_name)
        if profile == "repo_overview_full":
            sections = [
                ("Target workspace", str(self.target_workspace)),
                ("Target git remote", self.git_remote or "unavailable"),
                ("Git status --short", self._run_local_capture(["git", "status", "--short"], cwd=self.target_workspace)),
                ("Git log --oneline -5", self._run_local_capture(["git", "log", "--oneline", "-5"], cwd=self.target_workspace)),
                ("Target docs excerpts", self._build_target_docs_excerpts(limit=3200)),
                ("Target dependency and config files", self._build_target_dependency_context(limit=4200)),
                ("Target top-level tree up to depth 3", self._build_top_level_tree(root=self.target_workspace, depth=3)),
            ]
        elif profile == "external_comparison":
            sections = [
                ("Compressed target project summary", self._build_fallback_project_summary()),
                ("Target README excerpt", self._read_target_repo_file("README.md", 1600)),
                ("Target product and architecture summary", self._build_target_product_architecture_summary()),
                ("Default competitors", "LangGraph, CrewAI, AutoGen, OpenHands, Claude Code, Codex CLI, OpenClaw, Dify, n8n"),
                ("External research instruction", "Use Perplexity/Sonar for external comparison. Do not ask clarification."),
            ]
        elif profile == "market_positioning":
            sections = [
                ("Target project summary", self._build_fallback_project_summary()),
                ("Target product positioning", self._build_target_positioning_summary()),
                ("Target workflow and business goals", self._build_target_goals_summary()),
                ("Target users and use cases", self._build_target_users_inferred_summary()),
            ]
        elif profile == "technical_architecture":
            sections = [
                ("Target backend/frontend structure", self._build_target_backend_frontend_summary()),
                ("Target dependency and config files", self._build_target_dependency_context(limit=4200)),
                ("Target docker and deployment files", self._build_target_deployment_context(limit=2400)),
                ("Target top-level tree up to depth 4", self._build_top_level_tree(root=self.target_workspace, depth=4)),
                ("Target tests list", self._build_target_tests_file_list(limit=2200)),
            ]
        elif profile == "external_innovation":
            sections = [
                ("Target project summary", self._build_fallback_project_summary()),
                ("Target constraints and problems", self._build_target_constraints_summary()),
                ("External inspiration focus", "Look for product, UX, workflow, and automation ideas relevant to this target repository."),
                ("External research instruction", "Use Perplexity/Sonar for external inspiration. Do not ask clarification."),
            ]
        elif profile == "research_synthesis":
            sections = [
                ("Target docs excerpts", self._build_target_docs_excerpts(limit=2200)),
                ("Target dependency and config files", self._build_target_dependency_context(limit=2200)),
            ]
        else:
            sections = [("Target README excerpt", self._read_target_repo_file("README.md", 1500))]

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

    def _run_local_capture(self, command: list[str], timeout: int = 10, cwd: Path | None = None) -> str:
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=cwd or self.target_workspace,
            )
        except Exception:
            return ""
        output = (process.stdout or "").strip()
        if output:
            return output
        return (process.stderr or "").strip()

    def _build_compact_architecture_summary(self) -> str:
        return "\n".join(
            [
                f"Workflow executor: {self.runtime.executor}",
                f"Phase order: {', '.join(self._get_phase_order())}",
                "Primary flow: research -> implementation -> deployment.",
                "Core orchestration lives in workflow/orchestrator.py.",
                "Runtime settings live in workflow/config.yaml and workflow/runtime.py.",
                "Reports are written under .openclaw/logs/run_*/agents/<phase>/<agent>.json.",
            ]
        )

    @staticmethod
    def _build_positioning_summary() -> str:
        return "\n".join(
            [
                "agents-pipeline is a local-first multi-agent workflow for staged research, implementation, and deployment readiness.",
                "It aims to make agent work reproducible, inspectable, and resilient on developer machines, including Windows.",
            ]
        )

    @staticmethod
    def _build_workflow_goals_summary() -> str:
        return "\n".join(
            [
                "Run research agents before implementation to surface constraints and decisions.",
                "Feed implementation with structured summaries instead of ad hoc chat context.",
                "Keep startup and model execution robust when OpenClaw is unreliable.",
            ]
        )

    @staticmethod
    def _build_target_users_summary() -> str:
        return "\n".join(
            [
                "Target users: developers, founders, and operators building or testing agent-driven coding workflows.",
                "Use cases: repo analysis, market research, technical planning, implementation orchestration, release readiness.",
            ]
        )

    def _build_execution_architecture_summary(self) -> str:
        return "\n".join(
            [
                f"Executor mode: {self.runtime.executor}",
                "WorkflowOrchestrator handles preflight, prompt assembly, phase sequencing, runtime selection, and report persistence.",
                "direct_api uses OpenRouter chat completions and normalizes output into the same report format.",
                "Research retrieval uses a bounded JSON tool loop for local file reads, search, and file listing.",
            ]
        )

    @staticmethod
    def _build_known_constraints_summary() -> str:
        return "\n".join(
            [
                "direct_api models do not directly read local files, so the orchestrator injects local context and retrieval.",
                "Windows/OpenClaw setup can time out on registry and model listing preflight.",
                "Research handoff must stay compact enough for downstream prompts.",
            ]
        )

    def _read_target_repo_file(self, relative_path: str, limit: int) -> str:
        return self._read_file_excerpt(self.target_workspace / relative_path, limit)

    def _build_target_docs_excerpts(self, limit: int = 3200) -> str:
        doc_candidates = [
            "README.md",
            "README.txt",
            "docs/README.md",
            "docs/overview.md",
            "docs/architecture.md",
            ".env.example",
        ]
        return self._build_target_file_excerpts(doc_candidates, per_file_limit=900, total_limit=limit)

    def _build_target_dependency_context(self, limit: int = 3200) -> str:
        candidates = [
            "package.json",
            "pyproject.toml",
            "requirements.txt",
            "docker-compose.yml",
            "Dockerfile",
            ".env.example",
            "frontend/package.json",
            "backend/package.json",
            "gateway-v4/pyproject.toml",
            "gateway-v4/requirements.txt",
        ]
        return self._build_target_file_excerpts(candidates, per_file_limit=900, total_limit=limit)

    def _build_target_deployment_context(self, limit: int = 2400) -> str:
        candidates = [
            "docker-compose.yml",
            "Dockerfile",
            "frontend/Dockerfile",
            "backend/Dockerfile",
            "gateway-v4/Dockerfile",
            ".github/workflows/deploy.yml",
            ".github/workflows/ci.yml",
        ]
        return self._build_target_file_excerpts(candidates, per_file_limit=800, total_limit=limit)

    def _build_target_file_excerpts(self, relative_paths: list[str], per_file_limit: int = 900, total_limit: int = 3200) -> str:
        chunks: list[str] = []
        total = 0
        seen: set[str] = set()
        for relative_path in relative_paths:
            normalized = relative_path.replace("\\", "/")
            if normalized in seen:
                continue
            seen.add(normalized)
            content = self._read_target_repo_file(normalized, per_file_limit)
            if not content:
                continue
            chunk = f"### {normalized}\n{content.strip()}"
            remaining = total_limit - total
            if remaining <= 0:
                break
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            chunks.append(chunk)
            total += len(chunk) + 2
        return "\n\n".join(chunks)

    def _build_target_tests_file_list(self, limit: int = 2200) -> str:
        patterns = ("test", "spec")
        files = sorted(
            str(path.relative_to(self.target_workspace)).replace("\\", "/")
            for path in self.target_workspace.rglob("*")
            if path.is_file()
            and ".git" not in path.parts
            and any(token in path.name.lower() for token in patterns)
        )
        return "\n".join(files)[:limit]

    def _build_target_git_diff_excerpt(self, limit: int = 3200) -> str:
        diff_text = self._run_local_capture(["git", "diff", "--"], timeout=10, cwd=self.target_workspace)
        return diff_text[:limit]

    def _build_target_product_architecture_summary(self) -> str:
        lines = [
            f"Target workspace: {self.target_workspace}",
        ]
        if self.git_remote:
            lines.append(f"Git remote: {self.git_remote}")
        readme = self._read_target_repo_file("README.md", 900)
        if readme:
            lines.append("README summary excerpt available.")
        package_markers = []
        for candidate in ("package.json", "frontend/package.json", "backend/package.json"):
            if (self.target_workspace / candidate).exists():
                package_markers.append(candidate)
        if package_markers:
            lines.append("JavaScript package files: " + ", ".join(package_markers))
        python_markers = []
        for candidate in ("pyproject.toml", "requirements.txt", "gateway-v4/pyproject.toml", "gateway-v4/requirements.txt"):
            if (self.target_workspace / candidate).exists():
                python_markers.append(candidate)
        if python_markers:
            lines.append("Python dependency files: " + ", ".join(python_markers))
        deployment_markers = []
        for candidate in ("docker-compose.yml", "Dockerfile", "frontend", "backend", "gateway-v4"):
            if (self.target_workspace / candidate).exists():
                deployment_markers.append(candidate)
        if deployment_markers:
            lines.append("Visible runtime/deployment structure: " + ", ".join(deployment_markers))
        return "\n".join(lines)

    def _build_target_positioning_summary(self) -> str:
        readme = self._read_target_repo_file("README.md", 1200)
        if readme:
            return readme[:1200]
        return "\n".join(
            [
                f"Repository name: {self.target_workspace.name}",
                "Positioning should be inferred from target docs, dependency files, and top-level structure.",
            ]
        )

    def _build_target_goals_summary(self) -> str:
        readme = self._read_target_repo_file("README.md", 800)
        if readme:
            return readme[:800]
        return "Infer product and workflow goals from the target repository structure and config files."

    def _build_target_users_inferred_summary(self) -> str:
        package_files = self._build_target_dependency_context(limit=800)
        if package_files:
            return "Infer likely users and deployment shape from the target dependency/config files and README excerpts."
        return "Target users are not explicit; infer from repository naming, docs, and file structure."

    def _build_target_backend_frontend_summary(self) -> str:
        sections: list[str] = []
        for directory in ("frontend", "backend", "gateway-v4", "src", "app"):
            path = self.target_workspace / directory
            if path.exists():
                tree = self._build_top_level_tree(root=path, depth=2)
                if tree:
                    sections.append(f"## {directory}\n{tree}")
        return "\n\n".join(sections)

    def _build_target_constraints_summary(self) -> str:
        lines = [
            "Constraints should be inferred from target docs, dependency files, and repository structure.",
        ]
        if (self.target_workspace / "docker-compose.yml").exists():
            lines.append("Deployment orchestration is present via docker-compose.yml.")
        if (self.target_workspace / "requirements.txt").exists() or (self.target_workspace / "gateway-v4/requirements.txt").exists():
            lines.append("Python runtime dependencies are present.")
        if (self.target_workspace / "package.json").exists() or (self.target_workspace / "frontend/package.json").exists():
            lines.append("Node/npm dependencies are present.")
        tests_list = self._build_target_tests_file_list(limit=400)
        if not tests_list:
            lines.append("Automated tests are not obvious from the top-level target tree.")
        return "\n".join(lines)

    @staticmethod
    def _build_acceptance_criteria_template() -> str:
        return "\n".join(
            [
                "1. User-visible behavior",
                "2. Config/default expectations",
                "3. Error handling and fallback behavior",
                "4. Tests and verification",
            ]
        )

    @staticmethod
    def _build_implementation_task_template() -> str:
        return "\n".join(
            [
                "Task",
                "Files likely affected",
                "Behavioral change",
                "Risks and regressions",
                "Required tests",
            ]
        )

    def _build_top_level_tree(self, root: Path | None = None, depth: int = 3) -> str:
        root = (root or self.target_workspace).resolve()
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
        tests_dir = self.engine_root / "tests"
        if not tests_dir.exists():
            return ""
        files = sorted(
            str(path).replace("\\", "/")
            for path in tests_dir.rglob("*")
            if path.is_file()
        )
        return "\n".join(files)

    def _build_tests_subset(self, keywords: list[str], limit: int = 2000) -> str:
        tests_dir = self.engine_root / "tests"
        if not tests_dir.exists():
            return ""
        files = sorted(
            str(path).replace("\\", "/")
            for path in tests_dir.rglob("*")
            if path.is_file() and any(keyword in path.name for keyword in keywords)
        )
        return "\n".join(files)[:limit]

    def _read_python_sections(self, path: Path, markers: list[str], limit: int = 4000) -> str:
        if not path.exists() or not path.is_file():
            return ""
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return ""

        chunks: list[str] = []
        for marker in markers:
            normalized_marker = marker.strip()
            start_index = next((idx for idx, line in enumerate(lines) if line.lstrip().startswith(normalized_marker)), None)
            if start_index is None:
                continue
            end_index = len(lines)
            for idx in range(start_index + 1, len(lines)):
                stripped = lines[idx].lstrip()
                if stripped.startswith("def ") or stripped.startswith("class "):
                    end_index = idx
                    break
            section = "\n".join(lines[start_index:end_index]).strip()
            if section:
                chunks.append(section)
        return "\n\n".join(chunks)[:limit]

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
        if "no endpoints found" in normalized:
            return "model_not_found"
        if "model" in normalized and ("not found" in normalized or "does not exist" in normalized):
            return "model_not_found"
        return "failed"

    @staticmethod
    def _build_direct_api_retrieval_instruction(phase: str = "research", agent_name: str = "") -> str:
        base = (
            "If you need more local repository data, do not guess. "
            "Instead respond with only one JSON object and no surrounding prose. "
            "Supported requests are: "
            '{"tool":"read_file","path":"relative/path.py"}, '
            '{"tool":"read_files","paths":["relative/path.py"]}, '
            '{"tool":"search_text","pattern":"text","limit":20}, '
            '{"tool":"list_files","directory":"workflow","max_depth":3}. '
            "Use relative workspace paths only. "
        )
        if phase == "implementation":
            if agent_name == "developer":
                return base + (
                    'You may also request write operations with '
                    '{"tool":"write_file","path":"gateway-v4/app/services/monitoring.py","content":"..."} '
                    'or {"tool":"apply_patch","path":"gateway-v4/app/services/proxy.py","search":"old","replace":"new"}. '
                    "Before writing, inspect the target files first. "
                    "Make the smallest viable backend-only change. "
                    "Do not return narrative-only output when a safe edit is required. "
                    "If you cannot safely edit within scope, return normal text with status=no_changes and a clear reason. "
                    "When you have enough information, return the final answer normally instead of JSON."
                )
            if agent_name == "qa":
                return base + (
                    "Use retrieval for local inspection only; inspect the actual git diff before approving. "
                    "Test execution is handled outside this JSON retrieval loop. "
                    "When you have enough information, return the final answer normally instead of JSON."
                )
            return base + "When you have enough information, return the final answer normally instead of JSON."
        return base + "When you have enough information, return the final answer normally instead of JSON."

    def _perform_direct_api_request(
        self,
        request_payload: dict[str, Any],
        api_key: str,
        timeout: int,
    ) -> tuple[int, str]:
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
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = urllib_request.urlopen(request, timeout=timeout)
                if hasattr(response, "__enter__") and hasattr(response, "__exit__"):
                    with response:
                        return 200, response.read().decode("utf-8", errors="replace")
                return 200, response.read().decode("utf-8", errors="replace")
            except http.client.IncompleteRead as exc:
                last_error = exc
                if attempt >= 3:
                    raise
                time.sleep(min(1.5, 0.5 * attempt))
            except urllib_error.URLError as exc:
                last_error = exc
                if attempt >= 3:
                    raise
                time.sleep(min(1.5, 0.5 * attempt))
        if last_error is not None:
            raise last_error
        raise RuntimeError("direct_api request failed without response")

    @staticmethod
    def _parse_direct_api_retrieval_request(text: str) -> dict[str, Any] | None:
        stripped = text.strip()
        if not stripped.startswith("{") or not stripped.endswith("}"):
            return None
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        tool = payload.get("tool")
        return payload if isinstance(tool, str) else None

    def _execute_direct_api_retrieval_request(self, payload: dict[str, Any], phase: str = "research", agent_name: str = "") -> str:
        tool = str(payload.get("tool") or "").strip()
        if tool == "read_file":
            path = str(payload.get("path") or "").strip()
            if not path:
                return "Invalid read_file request."
            return self._direct_api_read_files([path], limit=8000)
        if tool == "read_files":
            paths = payload.get("paths")
            if not isinstance(paths, list):
                return "Invalid read_files request."
            return self._direct_api_read_files([str(path) for path in paths], limit=8000)
        if tool == "search_text":
            pattern = str(payload.get("pattern") or "").strip()
            limit = self._coerce_int(payload.get("limit")) or 20
            if not pattern:
                return "Invalid search_text request."
            return self._direct_api_search_text(pattern, limit=max(1, min(limit, 50)))
        if tool == "list_files":
            directory = str(payload.get("directory") or ".").strip() or "."
            max_depth = self._coerce_int(payload.get("max_depth")) or 3
            return self._direct_api_list_files(directory, max_depth=max(1, min(max_depth, 6)))
        if tool == "write_file":
            if not (phase == "implementation" and agent_name == "developer"):
                return "write_file is not allowed for this agent."
            path = str(payload.get("path") or "").strip()
            content = payload.get("content")
            if not path or not isinstance(content, str):
                return "Invalid write_file request."
            return self._direct_api_write_file(path, content)
        if tool == "apply_patch":
            if not (phase == "implementation" and agent_name == "developer"):
                return "apply_patch is not allowed for this agent."
            path = str(payload.get("path") or "").strip()
            search = payload.get("search")
            replace = payload.get("replace")
            if not path or not isinstance(search, str) or not isinstance(replace, str):
                return "Invalid apply_patch request."
            return self._direct_api_apply_patch(path, search, replace)
        return f"Unsupported retrieval tool: {tool}"

    def _direct_api_read_files(self, paths: list[str], limit: int = 8000) -> str:
        chunks: list[str] = []
        total = 0
        workspace_root = self.target_workspace.resolve()
        for raw_path in paths[:12]:
            candidate = (workspace_root / raw_path).resolve()
            try:
                candidate.relative_to(workspace_root)
            except ValueError:
                continue
            if not candidate.exists() or not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            chunk = f"## {raw_path}\n{text[:2500]}"
            remaining = limit - total
            if remaining <= 0:
                break
            if len(chunk) > remaining:
                chunk = chunk[:remaining]
            chunks.append(chunk)
            total += len(chunk) + 2
        return "\n\n".join(chunks)

    def _direct_api_search_text(self, pattern: str, limit: int = 20) -> str:
        results: list[str] = []
        workspace_root = self.target_workspace.resolve()
        for path in sorted(workspace_root.rglob("*")):
            if not path.is_file():
                continue
            if ".git" in path.parts or "__pycache__" in path.parts:
                continue
            try:
                relative = path.relative_to(workspace_root)
                relative_text = str(relative).replace("\\", "/")
                text = path.read_text(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if pattern.lower() in line.lower():
                    results.append(f"{relative_text}:{lineno}: {line.strip()}")
                    if len(results) >= limit:
                        return "\n".join(results)
        return "\n".join(results)

    def _direct_api_list_files(self, directory: str, max_depth: int = 3) -> str:
        base = (self.target_workspace / directory).resolve()
        try:
            base.relative_to(self.target_workspace.resolve())
        except ValueError:
            return ""
        if not base.exists():
            return ""
        lines: list[str] = []
        for path in sorted(base.rglob("*")):
            if ".git" in path.parts:
                continue
            try:
                relative = path.relative_to(base)
            except ValueError:
                continue
            if len(relative.parts) > max_depth:
                continue
            lines.append(str(path.relative_to(self.target_workspace)).replace("\\", "/"))
        return "\n".join(lines)

    def _resolve_target_relative_file(self, raw_path: str) -> tuple[Path | None, str]:
        relative_path = self._normalize_repo_relative_path(raw_path)
        if not relative_path:
            return None, ""
        candidate = (self.target_workspace / relative_path).resolve()
        try:
            candidate.relative_to(self.target_workspace.resolve())
        except ValueError:
            return None, relative_path
        return candidate, relative_path

    def _validate_direct_api_write_request(self, raw_path: str, text_parts: list[str]) -> tuple[bool, str, Path | None]:
        candidate, relative_path = self._resolve_target_relative_file(raw_path)
        if candidate is None:
            return False, "Write request escapes target_workspace.", None
        scope_check = self._evaluate_scope_paths([relative_path])
        if not scope_check["allowed"]:
            return False, "Write request violates implementation scope policy for path: " + relative_path, None
        lower_blob = "\n".join([relative_path, *text_parts]).lower()
        hits = [keyword for keyword in self.implementation_scope_policy["forbidden_keywords"] if keyword.lower() in lower_blob]
        if hits:
            return False, "Write request contains forbidden keywords: " + ", ".join(sorted(set(hits))), None
        return True, relative_path, candidate

    def _record_write_tool_usage(self, tool_name: str) -> None:
        current = self._get_agent_report_extras("implementation", "developer")
        used = list(current.get("write_tools_used") or [])
        used.append(tool_name)
        self._set_agent_report_extras("implementation", "developer", {"write_tools_used": used})

    def _direct_api_write_file(self, path: str, content: str) -> str:
        allowed, detail, candidate = self._validate_direct_api_write_request(path, [content])
        if not allowed or candidate is None:
            return detail
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(content, encoding="utf-8")
        self._record_write_tool_usage("write_file")
        return f"Wrote file: {detail}"

    def _direct_api_apply_patch(self, path: str, search: str, replace: str) -> str:
        allowed, detail, candidate = self._validate_direct_api_write_request(path, [search, replace])
        if not allowed or candidate is None:
            return detail
        if not candidate.exists() or not candidate.is_file():
            return f"Target file does not exist: {detail}"
        try:
            original = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return f"Unable to read target file: {detail}"
        if search not in original:
            return f"Search block not found in {detail}"
        updated = original.replace(search, replace, 1)
        candidate.write_text(updated, encoding="utf-8")
        self._record_write_tool_usage("apply_patch")
        return f"Patched file: {detail}"

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
        messages: list[dict[str, str]] = [
            {"role": "system", "content": str(message_bundle["system_message"])},
            {"role": "user", "content": str(message_bundle["user_message"])},
        ]
        max_turns = 1
        if message_bundle.get("retrieval_enabled"):
            max_turns = 6 if phase == "implementation" and agent_name == "developer" else 3

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
        last_raw_body = ""
        response_payload: dict[str, Any] | None = None
        retrieval_rounds = 0
        if phase == "implementation" and agent_name == "developer":
            self._set_agent_report_extras(
                "implementation",
                "developer",
                {
                    "write_tools_used": [],
                    "developer_changed_files": [],
                    "developer_diff_lines": 0,
                    "no_changes_detected": False,
                },
            )
        try:
            for turn in range(1, max_turns + 1):
                request_payload = {
                    "model": normalized_model,
                    "messages": messages,
                    "temperature": 0.2,
                }
                _status_code, raw_body = self._perform_direct_api_request(request_payload, api_key, timeout)
                last_raw_body = raw_body
                response_payload = json.loads(raw_body)
                output_text = self._extract_direct_api_text(response_payload)
                retrieval_request = self._parse_direct_api_retrieval_request(output_text)
                if not retrieval_request:
                    break
                retrieval_output = self._execute_direct_api_retrieval_request(retrieval_request, phase=phase, agent_name=agent_name)
                retrieval_rounds = turn
                message_bundle["retrieval_rounds"] = retrieval_rounds
                self.logger.agent_progress(
                    agent_name,
                    f"Direct API retrieval turn {turn}: {retrieval_request.get('tool')}",
                )
                messages.append({"role": "assistant", "content": output_text})
                messages.append(
                    {
                        "role": "user",
                        "content": "Local retrieval result:\n" + (retrieval_output or "No matching local results."),
                    }
                )
            else:
                response_payload = {
                    "choices": [{"message": {"content": "Exceeded retrieval rounds.\n\nRussian translation\nПревышен лимит раундов retrieval."}}],
                    "model": normalized_model,
                }
                message_bundle["retrieval_rounds"] = max_turns
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
        except json.JSONDecodeError:
            elapsed = time.monotonic() - started_at
            self.logger.error(f"Direct API returned invalid JSON: {agent_name}", self._tail_text(last_raw_body))
            save_agent_report("failed", "invalid direct_api response", elapsed, last_raw_body, "", "", command, 1)
            self.logger.agent_end(agent_name, "failed", "invalid direct_api response")
            return False
        except Exception:
            elapsed = time.monotonic() - started_at
            error_text = traceback.format_exc()
            self.logger.error(f"Direct API request failed: {agent_name}", error_text)
            save_agent_report("failed", "direct_api request failed", elapsed, "", error_text, "", command, 1)
            self.logger.agent_end(agent_name, "failed", "direct_api request failed")
            return False

        elapsed = time.monotonic() - started_at
        if response_payload is None:
            self.logger.error(f"Direct API returned invalid JSON: {agent_name}", self._tail_text(last_raw_body))
            save_agent_report("failed", "invalid direct_api response", elapsed, last_raw_body, "", "", command, 1)
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
        prioritized_stem = self.logger._safe_name("project-analyst") if phase == "research" else None

        json_paths = sorted(
            agent_dir.glob("*.json"),
            key=lambda path: (
                0 if prioritized_stem and path.stem == prioritized_stem else 1,
                path.stem,
            ),
        )
        for json_path in json_paths:
            if json_path.stem == current_safe_name:
                continue
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                continue

            if payload.get("status") != "success":
                continue

            if phase == "research":
                source_text = str(payload.get("handoff_summary") or payload.get("parsed_output") or payload.get("stdout") or "").strip()
            else:
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

    def _build_research_summary_context(self, agent_name: str, limit: int = 4000) -> str:
        if agent_name == "project-analyst":
            return ""
        if agent_name == "product-manager":
            summaries = self._load_research_handoff_summaries()
        elif agent_name in {"competitor-analyst", "market-analyst", "innovation-scout", "tech-analyst"}:
            summaries = self._load_research_handoff_summaries(["project-analyst"])
        else:
            summaries = self._load_research_handoff_summaries()

        chunks: list[str] = []
        total = 0
        for summary in summaries:
            heading = f"[{summary['agent_name']}]\n"
            body = summary["handoff_summary"]
            remaining = limit - total
            if not body or remaining <= len(heading):
                break
            body_limit = remaining - len(heading)
            if len(body) > body_limit:
                body = body[:body_limit]
            chunk = heading + body
            chunks.append(chunk)
            total += len(chunk) + 2
            if total >= limit:
                break
        return "\n\n".join(chunks)

    def _load_research_handoff_summaries(self, agent_names: list[str] | None = None) -> list[dict[str, str]]:
        agent_dir = self.logger.run_dir / "agents" / "research"
        if not agent_dir.exists():
            return []
        wanted = set(agent_names or [])
        summaries: list[dict[str, str]] = []
        for report_path in sorted(agent_dir.glob("*.json")):
            try:
                payload = json.loads(report_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if payload.get("status") != "success":
                continue
            agent_name = str(payload.get("agent_name") or payload.get("agent") or report_path.stem)
            if wanted and agent_name not in wanted:
                continue
            handoff_summary = str(payload.get("handoff_summary") or "").strip()
            if not handoff_summary:
                continue
            summaries.append({"agent_name": agent_name, "handoff_summary": handoff_summary})
        return summaries

    def _build_fallback_project_summary(self) -> str:
        summaries = self._load_research_handoff_summaries(["project-analyst"])
        if summaries:
            return summaries[0]["handoff_summary"]
        if self.context_mode == "external_project_analysis":
            parts = [
                self._read_target_repo_file("README.md", 1200),
                self._build_target_positioning_summary(),
                self._build_target_goals_summary(),
            ]
        else:
            parts = [
                self._read_file_excerpt(self.target_workspace / "README.md", 1200),
                self._build_positioning_summary(),
                self._build_workflow_goals_summary(),
            ]
        text = "\n\n".join(part.strip() for part in parts if part.strip())
        return text[:2000]

    @staticmethod
    def _build_implementation_scope_instruction(selected_scope: str) -> str:
        lines = [
            "Selected implementation scope:",
            selected_scope,
            "",
            "Implementation guardrails:",
            "- Do not implement marketplace.",
            "- Do not change Stripe or billing flows.",
            "- Do not make broad frontend changes.",
            "- Prefer small backend-first changes.",
            "- Frontend changes are limited to API client stubs only if required.",
            "- Inspect target files before writing.",
            "- Developer must produce real file edits via write_file/apply_patch when a safe scoped change is possible.",
            "- If no safe edit is possible, return status=no_changes with a reason.",
            "- If the task requires wider scope, stop and report that scope expansion is needed.",
        ]
        return "\n".join(lines)

    def _load_saved_agent_report(self, phase: str, agent_name: str, run_dir: Path | None = None) -> dict[str, Any] | None:
        base_dir = run_dir or self.logger.run_dir
        report_path = base_dir / "agents" / phase / f"{agent_name}.json"
        if not report_path.exists():
            return None
        try:
            return json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _overwrite_agent_report(self, phase: str, agent_name: str, payload: dict[str, Any]) -> None:
        normalized = dict(payload)
        normalized["phase"] = phase
        normalized["agent"] = agent_name
        normalized["agent_name"] = agent_name
        normalized.setdefault("message", "")
        normalized.setdefault("stdout", "")
        normalized.setdefault("stderr", "")
        normalized.setdefault("parsed_output", "")
        normalized.setdefault("command", "")
        normalized.setdefault("usage", {})
        self.logger.save_agent_report(phase, agent_name, normalized)

    @staticmethod
    def _normalize_repo_relative_path(value: str) -> str:
        cleaned = str(value or "").strip().strip("`'\"()[]{}:;,")
        cleaned = cleaned.replace("\\", "/")
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        return cleaned.strip("/")

    @staticmethod
    def _path_matches_any(path: str, patterns: list[str]) -> bool:
        normalized = WorkflowOrchestrator._normalize_repo_relative_path(path)
        if not normalized:
            return False
        return any(fnmatch(normalized, pattern) for pattern in patterns)

    def _extract_planned_files_from_text(self, text: str) -> list[str]:
        planned: set[str] = set()
        pattern = re.compile(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.*\-\[\]]+")
        for raw_match in pattern.findall(text or ""):
            if "://" in raw_match:
                continue
            normalized = self._normalize_repo_relative_path(raw_match)
            if normalized:
                planned.add(normalized)
        return sorted(planned)

    def _evaluate_scope_paths(self, paths: list[str]) -> dict[str, Any]:
        normalized_paths = [
            self._normalize_repo_relative_path(path)
            for path in paths
            if self._normalize_repo_relative_path(path)
        ]
        allowed_paths_matched: list[str] = []
        forbidden_hits: list[str] = []
        violations: list[str] = []
        for path in normalized_paths:
            if self._path_matches_any(path, self.implementation_scope_policy["forbidden_paths"]):
                forbidden_hits.append(f"path:{path}")
                violations.append(path)
                continue
            if self._path_matches_any(path, self.implementation_scope_policy["allowed_paths"]):
                allowed_paths_matched.append(path)
                continue
            forbidden_hits.append(f"out_of_scope:{path}")
            violations.append(path)
        return {
            "allowed": not violations,
            "violations": violations,
            "forbidden_hits": forbidden_hits,
            "allowed_paths_matched": sorted(set(allowed_paths_matched)),
        }

    def _enforce_implementation_scope_plan(self) -> bool:
        architect_report = self._load_saved_agent_report("implementation", "architect")
        if not architect_report:
            diagnostics = {
                "scope_policy_result": "blocked",
                "changed_files_count": 0,
                "diff_lines_count": 0,
                "forbidden_hits": ["missing_architect_output"],
                "allowed_paths_matched": [],
            }
            return self._handle_scope_violation(
                "developer",
                "Architect output is missing; cannot validate implementation plan scope.",
                diagnostics,
            )

        planned_files = self._extract_planned_files_from_text(
            str(architect_report.get("parsed_output") or architect_report.get("stdout") or "")
        )
        path_check = self._evaluate_scope_paths(planned_files)
        diagnostics = {
            "scope_policy_result": "allowed" if path_check["allowed"] else "blocked",
            "changed_files_count": len(planned_files),
            "diff_lines_count": 0,
            "forbidden_hits": path_check["forbidden_hits"],
            "allowed_paths_matched": path_check["allowed_paths_matched"],
        }
        self._set_agent_report_extras("implementation", "architect", diagnostics)
        self.logger.agent_progress("architect", f"Diagnostic scope_policy_result={diagnostics['scope_policy_result']}")
        self.logger.agent_progress("architect", f"Diagnostic changed_files_count={diagnostics['changed_files_count']}")
        self.logger.agent_progress("architect", f"Diagnostic diff_lines_count={diagnostics['diff_lines_count']}")
        self.logger.agent_progress("architect", f"Diagnostic forbidden_hits={diagnostics['forbidden_hits']}")
        self.logger.agent_progress("architect", f"Diagnostic allowed_paths_matched={diagnostics['allowed_paths_matched']}")
        existing_architect = self._load_saved_agent_report("implementation", "architect")
        if existing_architect:
            self._overwrite_agent_report("implementation", "architect", {**existing_architect, **diagnostics})
        if path_check["allowed"]:
            self._set_agent_report_extras("implementation", "developer", diagnostics)
            return True
        return self._handle_scope_violation(
            "developer",
            "Architect planned files outside the allowed implementation scope: " + ", ".join(path_check["violations"]),
            diagnostics,
            warning_only=self.allow_scope_expansion,
        )

    def _collect_scope_watchdog_diff_diagnostics(self) -> dict[str, Any]:
        name_only_output = self._run_local_capture(["git", "diff", "--name-only"], timeout=10, cwd=self.target_workspace)
        status_output = self._run_local_capture(["git", "status", "--porcelain"], timeout=10, cwd=self.target_workspace)
        git_error_markers = (
            "not a git repository",
            "fatal:",
            "unknown revision",
            "ambiguous argument",
        )
        if any(marker in name_only_output.lower() for marker in git_error_markers):
            name_only_output = ""
        if any(marker in status_output.lower() for marker in git_error_markers):
            status_output = ""
        changed_files: list[str] = []
        for line in name_only_output.splitlines():
            normalized = self._normalize_repo_relative_path(line)
            if normalized:
                changed_files.append(normalized)
        for line in status_output.splitlines():
            if len(line) < 3:
                continue
            path_text = line[2:].strip()
            if " -> " in path_text:
                path_text = path_text.split(" -> ", 1)[1].strip()
            normalized = self._normalize_repo_relative_path(path_text)
            if normalized:
                changed_files.append(normalized)
        changed_files = sorted(set(changed_files))

        path_check = self._evaluate_scope_paths(changed_files)
        numstat_output = self._run_local_capture(["git", "diff", "--numstat"], timeout=10, cwd=self.target_workspace)
        diff_stat_output = self._run_local_capture(["git", "diff", "--stat"], timeout=10, cwd=self.target_workspace)
        diff_text = self._run_local_capture(["git", "diff", "--"], timeout=10, cwd=self.target_workspace)
        if any(marker in numstat_output.lower() for marker in git_error_markers):
            numstat_output = ""
        if any(marker in diff_stat_output.lower() for marker in git_error_markers):
            diff_stat_output = ""
        if any(marker in diff_text.lower() for marker in git_error_markers):
            diff_text = ""
        diff_lines_count = 0
        for line in numstat_output.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            try:
                added = 0 if parts[0] == "-" else int(parts[0])
                deleted = 0 if parts[1] == "-" else int(parts[1])
            except ValueError:
                continue
            diff_lines_count += added + deleted

        lower_blob = "\n".join([diff_text, diff_stat_output, "\n".join(changed_files)]).lower()
        keyword_hits = [
            f"keyword:{keyword}"
            for keyword in self.implementation_scope_policy["forbidden_keywords"]
            if keyword.lower() in lower_blob
        ]
        forbidden_hits = path_check["forbidden_hits"] + keyword_hits
        if len(changed_files) > int(self.implementation_scope_policy["max_changed_files"]):
            forbidden_hits.append(
                f"max_changed_files:{len(changed_files)}>{self.implementation_scope_policy['max_changed_files']}"
            )
        if diff_lines_count > int(self.implementation_scope_policy["max_diff_lines"]):
            forbidden_hits.append(
                f"max_diff_lines:{diff_lines_count}>{self.implementation_scope_policy['max_diff_lines']}"
            )
        allowed = (
            path_check["allowed"]
            and not keyword_hits
            and len(changed_files) <= int(self.implementation_scope_policy["max_changed_files"])
            and diff_lines_count <= int(self.implementation_scope_policy["max_diff_lines"])
        )
        return {
            "allowed": allowed,
            "changed_files": changed_files,
            "changed_files_count": len(changed_files),
            "diff_lines_count": diff_lines_count,
            "forbidden_hits": forbidden_hits,
            "allowed_paths_matched": path_check["allowed_paths_matched"],
            "scope_policy_result": "allowed" if allowed else "blocked",
        }

    def _mark_developer_no_changes(self, reason: str) -> bool:
        diagnostics = self._collect_scope_watchdog_diff_diagnostics()
        payload = {
            **diagnostics,
            "developer_changed_files": diagnostics["changed_files"],
            "developer_diff_lines": diagnostics["diff_lines_count"],
            "no_changes_detected": True,
            "scope_policy_result": diagnostics.get("scope_policy_result", "allowed"),
        }
        self._set_agent_report_extras("implementation", "developer", payload)
        existing = self._load_saved_agent_report("implementation", "developer") or {}
        self._overwrite_agent_report(
            "implementation",
            "developer",
            {
                **existing,
                **payload,
                "status": "no_changes",
                "result": reason,
            },
        )
        self.logger.error("Implementation produced no file changes", reason)
        self.logger.agent_end("developer", "no_changes", reason)
        self._phase_failure_status = "no_changes"
        return False

    def _enforce_implementation_scope_diff(self) -> bool:
        diagnostics = self._collect_scope_watchdog_diff_diagnostics()
        diagnostics["developer_changed_files"] = diagnostics["changed_files"]
        diagnostics["developer_diff_lines"] = diagnostics["diff_lines_count"]
        diagnostics["no_changes_detected"] = diagnostics["changed_files_count"] == 0
        self._set_agent_report_extras("implementation", "developer", diagnostics)
        self.logger.agent_progress("developer", f"Diagnostic scope_policy_result={diagnostics['scope_policy_result']}")
        self.logger.agent_progress("developer", f"Diagnostic changed_files_count={diagnostics['changed_files_count']}")
        self.logger.agent_progress("developer", f"Diagnostic diff_lines_count={diagnostics['diff_lines_count']}")
        self.logger.agent_progress("developer", f"Diagnostic forbidden_hits={diagnostics['forbidden_hits']}")
        self.logger.agent_progress("developer", f"Diagnostic allowed_paths_matched={diagnostics['allowed_paths_matched']}")
        self.logger.agent_progress("developer", f"Diagnostic developer_changed_files={diagnostics['developer_changed_files']}")
        self.logger.agent_progress("developer", f"Diagnostic developer_diff_lines={diagnostics['developer_diff_lines']}")
        self.logger.agent_progress("developer", f"Diagnostic write_tools_used={diagnostics.get('write_tools_used', self._get_agent_report_extras('implementation', 'developer').get('write_tools_used', []))}")
        self.logger.agent_progress("developer", f"Diagnostic no_changes_detected={diagnostics['no_changes_detected']}")
        existing_developer = self._load_saved_agent_report("implementation", "developer")
        if existing_developer:
            self._overwrite_agent_report("implementation", "developer", {**existing_developer, **diagnostics})
        if diagnostics["changed_files_count"] == 0:
            return self._mark_developer_no_changes(
                "Developer completed without modifying target files. Return status=no_changes or make a scoped file edit."
            )
        if diagnostics["allowed"]:
            return True
        offending = diagnostics["forbidden_hits"] or diagnostics["changed_files"]
        return self._handle_scope_violation(
            "developer",
            "Developer changes violated implementation scope policy: " + ", ".join(str(item) for item in offending),
            diagnostics,
            warning_only=self.allow_scope_expansion,
        )

    def _handle_scope_violation(
        self,
        agent_name: str,
        reason: str,
        diagnostics: dict[str, Any],
        *,
        warning_only: bool = False,
    ) -> bool:
        payload = dict(diagnostics)
        if warning_only:
            payload["scope_policy_result"] = "bypassed"
            self._set_agent_report_extras("implementation", agent_name, payload)
            self.logger.warning(f"Scope watchdog bypassed: {reason}")
            return True

        payload["scope_policy_result"] = "blocked"
        self._set_agent_report_extras("implementation", agent_name, payload)
        existing = self._load_saved_agent_report("implementation", agent_name) or {}
        self._overwrite_agent_report(
            "implementation",
            agent_name,
            {
                **existing,
                **payload,
                "status": "scope_violation",
                "result": reason,
            },
        )
        self.logger.error("Implementation scope violation detected", reason)
        self.logger.agent_end(agent_name, "scope_violation", reason)
        self._phase_failure_status = "scope_violation"
        return False

    def _build_implementation_phase_context(self, agent_name: str = "architect", limit: int = 12000) -> dict[str, Any]:
        research_reports, run_dir = self._load_latest_project_research_reports()
        selected_scope = self._select_implementation_scope(research_reports)
        same_phase_context = self._build_previous_agent_context("implementation", agent_name, limit=4000)
        research_context = self._build_implementation_research_context(research_reports, agent_name, limit=5000)
        previous_parts = [part for part in [research_context, same_phase_context] if part.strip()]
        previous_context = "\n\n".join(previous_parts)

        sections = [
            ("Selected implementation scope", selected_scope),
            ("Target README and docs", self._build_target_docs_excerpts(limit=2000)),
            ("Target dependency and config files", self._build_target_dependency_context(limit=2200)),
            ("Target top-level tree up to depth 4", self._build_top_level_tree(root=self.target_workspace, depth=4)),
        ]
        if agent_name in {"architect", "qa", "template-validator"}:
            sections.append(("Target tests list", self._build_target_tests_file_list(limit=1500)))
        if agent_name == "qa":
            diff_excerpt = self._build_target_git_diff_excerpt(limit=4000)
            if diff_excerpt:
                sections.append(("Target git diff", diff_excerpt))

        repository_context = self._join_context_sections(sections, limit=limit)
        sources = [
            f"{run_dir.name}:{report.get('agent_name') or report.get('agent')}"
            for report in research_reports
            if report.get("status") == "success"
        ]
        return {
            "repository_context": repository_context,
            "previous_context": previous_context,
            "research_handoff_sources": sources,
            "selected_task_scope": selected_scope,
            "context_chars": (len(repository_context) + len(previous_context)) if sources else 0,
        }

    def _load_latest_project_research_reports(self) -> tuple[list[dict[str, Any]], Path | None]:
        candidate_run_dirs: list[Path] = []
        current_research_dir = self.logger.run_dir / "agents" / "research"
        if current_research_dir.exists():
            candidate_run_dirs.append(self.logger.run_dir)

        if self.research_run_id:
            run_name = self.research_run_id if self.research_run_id.startswith("run_") else f"run_{self.research_run_id}"
            explicit_dir = self.logger.log_dir / run_name
            if explicit_dir.exists() and explicit_dir not in candidate_run_dirs:
                candidate_run_dirs.insert(0, explicit_dir)
        else:
            for run_dir in sorted(self.logger.log_dir.glob("run_*"), reverse=True):
                if run_dir not in candidate_run_dirs:
                    candidate_run_dirs.append(run_dir)

        for run_dir in candidate_run_dirs:
            research_dir = run_dir / "agents" / "research"
            if not research_dir.exists():
                continue
            reports: list[dict[str, Any]] = []
            for report_path in sorted(research_dir.glob("*.json")):
                try:
                    payload = json.loads(report_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if payload.get("status") == "success":
                    reports.append(payload)
            if reports:
                return reports, run_dir
        return [], None

    def _build_implementation_research_context(self, reports: list[dict[str, Any]], agent_name: str, limit: int = 5000) -> str:
        wanted_order = ["product-manager", "project-analyst", "tech-analyst", "competitor-analyst", "market-analyst", "innovation-scout"]
        by_name = {
            str(report.get("agent_name") or report.get("agent") or ""): report
            for report in reports
            if report.get("status") == "success"
        }
        if agent_name == "architect":
            selected_names = ["product-manager", "project-analyst", "tech-analyst"]
        else:
            selected_names = wanted_order

        chunks: list[str] = []
        total = 0
        for selected_name in selected_names:
            report = by_name.get(selected_name)
            if not report:
                continue
            source_text = str(report.get("handoff_summary") or report.get("parsed_output") or "").strip()
            if not source_text:
                continue
            heading = f"[{selected_name}]\n"
            remaining = limit - total
            if remaining <= len(heading):
                break
            body_limit = remaining - len(heading)
            if len(source_text) > body_limit:
                source_text = source_text[:body_limit]
            chunk = heading + source_text
            chunks.append(chunk)
            total += len(chunk) + 2
        return "\n\n".join(chunks)

    def _select_implementation_scope(self, reports: list[dict[str, Any]]) -> str:
        if self.task_scope_override:
            return self.task_scope_override
        return str(
            self.config.get("workflow", {}).get(
                "default_implementation_scope",
                "Implement backend-only MVP provider performance monitoring and smart routing foundation. "
                "No marketplace, no Stripe changes, no frontend changes except API client stubs if required.",
            )
        )

    @staticmethod
    def _join_context_sections(sections: list[tuple[str, str]], limit: int) -> str:
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
        return "\n\n".join(chunks)

    def _resolve_engine_path(self, path_value: str | Path) -> Path:
        path = Path(path_value)
        if path.is_absolute():
            return path.resolve()
        return (self.engine_root / path).resolve()

    def _engine_path(self, path_value: str | Path) -> Path:
        return self._resolve_engine_path(path_value)

    def _resolve_target_workspace(self, workspace: str | None) -> Path:
        if workspace:
            candidate = Path(workspace)
            if not candidate.is_absolute():
                candidate = self.launch_cwd / candidate
            return candidate.resolve()
        if self.launch_cwd != self.engine_root:
            return self.launch_cwd
        configured_workspace = str(self.config.get("project", {}).get("workspace", "."))
        candidate = Path(configured_workspace)
        if not candidate.is_absolute():
            candidate = self.engine_root / candidate
        return candidate.resolve()

    def _resolve_context_mode(self) -> str:
        if self.target_workspace.resolve() == self.engine_root.resolve():
            return "engine_self_analysis"
        return "external_project_analysis"

    def _load_target_repo(self) -> git.Repo | None:
        try:
            return git.Repo(self.target_workspace, search_parent_directories=True)
        except git.InvalidGitRepositoryError:
            return None
        except git.NoSuchPathError:
            return None

    def _default_branch(self) -> str:
        return str(self.config.get("project", {}).get("default_branch", "main"))

    def _detect_git_remote(self) -> str:
        if self.repo is None:
            return ""
        try:
            return str(self.repo.remotes.origin.url or "").strip()
        except Exception:
            return ""

    def _resolve_project_id(self, override: str | None) -> str:
        candidate = str(override or "").strip()
        if not candidate:
            candidate = self._derive_project_id_source()
        return self._sanitize_project_id(candidate)

    def _derive_project_id_source(self) -> str:
        if self.git_remote:
            remote = self.git_remote.strip()
            if remote.startswith("git@"):
                without_prefix = remote[4:]
                host, _, path = without_prefix.partition(":")
                normalized_path = path.removesuffix(".git").strip("/")
                return f"{host}/{normalized_path}".strip("/")
            parsed = urlparse(remote)
            host = parsed.netloc or parsed.path
            path = parsed.path.removesuffix(".git").strip("/")
            if host and path:
                return f"{host}/{path}"
            return remote.removesuffix(".git")
        return self.target_workspace.name or "project"

    @staticmethod
    def _sanitize_project_id(value: str) -> str:
        sanitized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip().lower()).strip("._-")
        return sanitized or "project"

    def _ensure_project_state_dirs(self) -> Path:
        base = self.engine_root / ".agents-pipeline" / "projects" / self.project_id
        for name in ("context", "memory", "logs", "summaries"):
            (base / name).mkdir(parents=True, exist_ok=True)
        settings_path = base / "settings.yaml"
        settings_payload = {
            "project_id": self.project_id,
            "git_remote": self.git_remote,
            "target_workspace": str(self.target_workspace),
            "engine_root": str(self.engine_root),
        }
        settings_path.write_text(yaml.safe_dump(settings_payload, sort_keys=False), encoding="utf-8")
        return base

    def _log_startup_diagnostics(self) -> None:
        self.logger.info(f"Startup diagnostic: engine_root={self.engine_root}")
        self.logger.info(f"Startup diagnostic: launch_cwd={self.launch_cwd}")
        self.logger.info(f"Startup diagnostic: target_workspace={self.target_workspace}")
        self.logger.info(f"Startup diagnostic: project_id={self.project_id}")
        self.logger.info(f"Startup diagnostic: git_remote={self.git_remote or 'unavailable'}")
        self.logger.info(f"Startup diagnostic: context_mode={self.context_mode}")
        self.logger.info(f"Startup diagnostic: repository_context_root={self.repository_context_root}")
        self.logger.info(f"Startup diagnostic: retrieval_root={self.retrieval_root}")
        self.logger.info(f"Startup diagnostic: handoff_summary_root={self.handoff_summary_root}")
        self.logger.info(f"Startup diagnostic: logs_root={self.logs_root}")

    @staticmethod
    def _build_research_handoff_summary(agent_name: str, parsed_output: str, limit: int = 2000) -> str:
        text = str(parsed_output or "").strip()
        primary = text.split("Russian translation", 1)[0].strip() if text else ""
        normalized = primary or text
        if not normalized:
            return ""

        sections = WorkflowOrchestrator._extract_handoff_sections(normalized)
        lines = [f"agent: {agent_name}"]
        for section_name in ("findings", "risks", "decisions", "recommended_next_tasks"):
            lines.append(f"{section_name}:")
            for value in sections[section_name] or ["none"]:
                compact = " ".join(str(value).split())
                lines.append(f"- {compact[:320]}")

        summary = "\n".join(lines)
        if len(summary) <= limit:
            return summary
        return summary[:limit].rstrip()

    @staticmethod
    def _extract_handoff_sections(text: str) -> dict[str, list[str]]:
        canonical_sections = {
            "findings": ["findings", "key findings", "observations", "summary"],
            "risks": ["risks", "issues", "concerns", "constraints"],
            "decisions": ["decisions", "recommendations", "recommended decisions"],
            "recommended_next_tasks": ["recommended next tasks", "next steps", "next tasks", "actions", "action items"],
        }
        heading_lookup = {
            WorkflowOrchestrator._normalize_handoff_heading(alias): key
            for key, aliases in canonical_sections.items()
            for alias in aliases
        }
        sections = {key: [] for key in canonical_sections}
        current_section = "findings"

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            normalized_heading = WorkflowOrchestrator._normalize_handoff_heading(line.rstrip(":"))
            if normalized_heading in heading_lookup:
                current_section = heading_lookup[normalized_heading]
                continue

            cleaned = re.sub(r"^[-*]\s*", "", line).strip()
            if cleaned:
                sections[current_section].append(cleaned)

        if all(not values for values in sections.values()):
            for sentence in WorkflowOrchestrator._split_handoff_sentences(text):
                sections[WorkflowOrchestrator._classify_handoff_sentence(sentence)].append(sentence)

        if not any(sections["findings"]):
            fallback = next(
                (
                    item
                    for key in ("decisions", "recommended_next_tasks", "risks")
                    for item in sections[key]
                ),
                "",
            )
            if fallback:
                sections["findings"].append(fallback)

        for key, values in sections.items():
            deduped: list[str] = []
            for value in values:
                compact = " ".join(value.split())
                if compact and compact not in deduped:
                    deduped.append(compact)
                if len(deduped) >= 4:
                    break
            sections[key] = deduped
        return sections

    @staticmethod
    def _normalize_handoff_heading(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()

    @staticmethod
    def _split_handoff_sentences(text: str) -> list[str]:
        normalized = re.sub(r"\s+", " ", text).strip()
        if not normalized:
            return []
        return [part.strip(" -") for part in re.split(r"(?<=[.!?;])\s+", normalized) if part.strip(" -")]

    @staticmethod
    def _classify_handoff_sentence(sentence: str) -> str:
        lowered = sentence.lower()
        if any(token in lowered for token in ("risk", "constraint", "issue", "blocker", "uncertain", "failure")):
            return "risks"
        if any(token in lowered for token in ("recommend", "should", "decision", "priority", "must")):
            return "decisions"
        if any(token in lowered for token in ("next", "implement", "add", "build", "test", "validate", "investigate", "fix", "update")):
            return "recommended_next_tasks"
        return "findings"

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

    @staticmethod
    def _coerce_float(value: Any) -> float | None:
        if value is None or value == "":
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _log_usage_totals(self, agent_name: str, phase: str, usage: dict[str, Any]) -> None:
        agent_cost = usage.get("estimated_cost_usd")
        phase_totals = self.logger.get_phase_totals(phase)
        run_totals = self.logger.get_run_totals()
        self.logger.agent_progress(agent_name, f"Agent cost: {self._format_cost(agent_cost)}")
        self.logger.agent_progress(agent_name, f"Phase total so far: {self._format_cost(phase_totals['estimated_cost_usd'])}")
        self.logger.agent_progress(agent_name, f"Run total so far: {self._format_cost(run_totals['estimated_cost_usd'])}")
        if self.max_phase_cost_usd is not None:
            self.logger.agent_progress(agent_name, f"Phase cost limit: {self._format_cost(self.max_phase_cost_usd)}")

    def _phase_cost_limit_exceeded(self, phase: str) -> bool:
        if self.max_phase_cost_usd is None:
            return False
        phase_total = float(self.logger.get_phase_totals(phase)["estimated_cost_usd"] or 0.0)
        if phase_total <= self.max_phase_cost_usd:
            return False
        self.logger.warning(
            "Phase cost limit exceeded: "
            f"phase={phase} total={self._format_cost(phase_total)} limit={self._format_cost(self.max_phase_cost_usd)}"
        )
        return True

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
