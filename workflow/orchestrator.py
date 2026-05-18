from __future__ import annotations

import http.client
import json
import os
import re
import subprocess
import time
import traceback
from datetime import datetime
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

from tools.repo_map import compare_repo_maps, generate_repo_map, validate_agent_paths
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
        selected_task_ref: str | None = None,
        next_task: bool = False,
        research_run: str | None = None,
        allow_scope_expansion: bool = False,
        retry_agent: str | None = None,
        from_agent: str | None = None,
        reuse_architect: bool = False,
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
        self.project_settings_path = self.project_state_dir / "settings.yaml"
        self.project_settings = self._load_project_settings()
        self.repo_map_path = self.project_state_dir / "context" / "repo_map.json"
        self.repository_context_root = self.engine_root if self.context_mode == "engine_self_analysis" else self.target_workspace
        self.retrieval_root = self.target_workspace
        logs_root = self._engine_path(self.config.get("paths", {}).get("logs_dir", ".openclaw/logs"))
        self.logger = WorkflowLogger(log_dir=str(logs_root / self.project_id))
        self.handoff_summary_root = self.logger.run_dir / "agents" / "research"
        self.logs_root = self.logger.log_dir
        self.repo_map_before_path = self.logger.run_dir / "repo_map_before.json"
        self.repo_map_after_path = self.logger.run_dir / "repo_map_after.json"
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
        self.selected_task_ref = str(selected_task_ref or "").strip()
        self.next_task_requested = next_task
        self.research_run_id = str(research_run or "").strip()
        self.allow_scope_expansion = allow_scope_expansion
        self.retry_agent_name = str(retry_agent or "").strip()
        self.from_agent_name = str(from_agent or "").strip()
        self.reuse_architect = reuse_architect
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
        self._implementation_backlog_cache: list[dict[str, Any]] | None = None
        self._implementation_backlog_source = ""
        self._selected_implementation_item: dict[str, Any] | None = None
        self._implementation_planner_output_chars = 0
        self._planner_invalid_paths: list[str] = []
        self._planner_repair_attempted = False
        self._validated_backlog_task_count = 0
        self._planner_missing_directories: list[str] = []
        self._planner_missing_tests: list[str] = []
        self._planner_conflicting_forbidden_paths: list[str] = []
        self._generic_root_dirs_rejected: list[str] = []
        self._planner_dependency_graph: dict[str, Any] = {}
        self._planner_future_known_paths: dict[str, Any] = {}
        self._planner_dependency_validation_errors: list[str] = []
        self._planner_rejection_reason = ""
        self._planner_feedback_file = ""
        self._planner_feedback_payload: dict[str, Any] = {}
        self._planner_feedback_source = ""
        self._planner_feedback_chars = 0
        self._planner_parse_error = ""
        self._planner_schema_errors: list[str] = []
        self._planner_raw_output_excerpt = ""
        self._planner_extracted_payload_excerpt = ""
        self._planner_validation_stage = ""
        self._reused_architect_output = False
        self._architect_output_source = ""
        self._planner_retry_count = 0
        self._planner_retry_reason = ""
        self._task_designer_validation_errors: list[str] = []
        self._task_designer_feedback_file = ""
        self._task_designer_feedback_source = ""
        self._task_designer_rejection_reason = ""
        self._implementation_attempt = 0
        self._repo_map_cache: dict[str, Any] | None = None
        self._repo_map_before: dict[str, Any] | None = None
        self._repo_map_after: dict[str, Any] | None = None
        self._repo_map_delta: dict[str, list[str]] = {
            "new_files_created": [],
            "files_modified": [],
            "removed_files": [],
        }
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
        if phase_key == "research" and not self._refresh_repo_map():
            return False
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
        self._selected_implementation_item = None
        self._implementation_backlog_cache = None
        self._implementation_backlog_source = ""
        self._implementation_planner_output_chars = 0
        self._planner_invalid_paths = []
        self._planner_repair_attempted = False
        self._validated_backlog_task_count = 0
        self._planner_missing_directories = []
        self._planner_missing_tests = []
        self._planner_conflicting_forbidden_paths = []
        self._generic_root_dirs_rejected = []
        self._planner_dependency_graph = {}
        self._planner_future_known_paths = {}
        self._planner_dependency_validation_errors = []
        self._planner_rejection_reason = ""
        self._planner_feedback_file = ""
        self._planner_feedback_payload = {}
        self._planner_feedback_source = ""
        self._planner_feedback_chars = 0
        self._planner_parse_error = ""
        self._planner_schema_errors = []
        self._planner_raw_output_excerpt = ""
        self._planner_extracted_payload_excerpt = ""
        self._planner_validation_stage = ""
        self._reused_architect_output = False
        self._architect_output_source = ""
        self._planner_retry_count = 0
        self._planner_retry_reason = ""
        self._task_designer_validation_errors = []
        self._task_designer_feedback_file = ""
        self._task_designer_feedback_source = ""
        self._task_designer_rejection_reason = ""
        self._implementation_attempt = 0
        self.logger.phase_start(phase["name"])
        research_reports, _run_dir = self._load_latest_project_research_reports()
        if not research_reports:
            self.logger.error(
                f"No research handoff found for project_id={self.project_id}. Run research phase first or specify --research-run."
            )
            self.logger.phase_end(phase["name"], "failed")
            return False
        if not self._refresh_repo_map(snapshot_path=self.repo_map_before_path):
            self.logger.phase_end(phase["name"], "failed")
            return False
        self._repo_map_before = self._load_repo_map()
        self._repo_map_after = None
        self._repo_map_delta = {"new_files_created": [], "files_modified": [], "removed_files": []}
        if self._should_reuse_architect_for_implementation():
            if not self._reuse_architect_output_for_current_run():
                self.logger.phase_end(phase["name"], "failed")
                return False
        if self._should_reuse_planner_for_implementation():
            if not self._reuse_planner_output_for_current_run():
                self.logger.phase_end(phase["name"], "failed")
                return False
        if self._should_reuse_task_designer_for_implementation():
            if not self._reuse_task_designer_output_for_current_run():
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
            self._implementation_attempt = attempt
            self.logger.info(f"Попытка реализации {attempt}/{max_retries}")
            ok = self._run_phase_agents(phase, "implementation")
            if not ok and self._phase_failure_status in {"scope_violation", "no_changes", "planner_invalid", "task_designer_invalid"}:
                if self.config["git"]["enabled"] and self.config["git"]["auto_rollback"]:
                    self._rollback_git(self._phase_failure_status.replace("_", " "))
                self.logger.save_phase_summary("implementation", phase["name"])
                self.logger.phase_end(phase["name"], self._phase_failure_status)
                return False
            if ok:
                if self._implementation_resume_stops_before_delivery():
                    self.logger.save_phase_summary("implementation", phase["name"])
                    self.logger.phase_end(phase["name"], "planner_ready")
                    return True
                self._mark_implementation_task_completed()
                if self.config["git"]["enabled"] and not self._merge_git():
                    self.logger.save_phase_summary("implementation", phase["name"])
                    self.logger.phase_end(phase["name"], "failed")
                    return False
                self.logger.save_phase_summary("implementation", phase["name"])
                self.logger.phase_end(phase["name"], "success")
                next_action = self._prompt_post_implementation_action()
                if next_action == "next":
                    self.next_task_requested = True
                    self.selected_task_ref = ""
                    self._selected_implementation_item = None
                    return self._run_implementation_phase()
                if next_action == "deployment":
                    return self.run_deployment_phase()
                return True

            self._save_feedback(task_id, "qa", f"Попытка {attempt} завершилась ошибкой. Проверь логи и исправь регрессии.")
            if self.config["git"]["enabled"] and self.config["git"]["auto_rollback"]:
                self._rollback_git(f"attempt {attempt} failed")
                if attempt < max_retries:
                    self._create_git_branch(task_id)

        self.logger.save_phase_summary("implementation", phase["name"])
        self.logger.phase_end(phase["name"], self._phase_failure_status or "failed")
        return False

    def _implementation_resume_stops_before_delivery(self) -> bool:
        return self.retry_agent_name == "implementation-planner" or self.from_agent_name == "implementation-planner"

    def _run_phase_agents(self, phase: dict[str, Any], phase_key: str) -> bool:
        total = len(phase["agents"])
        fail_fast = bool(phase.get("fail_fast", False))
        had_failures = False
        for index, agent in enumerate(phase["agents"], start=1):
            if phase_key == "implementation" and self._should_skip_implementation_agent(agent["name"]):
                self.logger.info(f"Skipping implementation agent due to retry/resume mode: {agent['name']}")
                continue
            if self._phase_cost_limit_exceeded(phase_key):
                had_failures = True
                break
            if not self._wait_for_user(f"Запустить агента {agent['name']} ({index}/{total})?"):
                self.logger.warning(f"Агент пропущен: {agent['name']}")
                continue
            if phase_key == "implementation" and agent["name"] in {"implementation-planner", "task-designer", "developer"}:
                if not self._ensure_repo_map_workspace_consistency(agent["name"]):
                    had_failures = True
                    return False
            if phase_key == "implementation" and agent["name"] == "task-designer":
                selection = self._prepare_implementation_backlog_selection(require_backlog=True)
                if selection["error"]:
                    self.logger.error(selection["error"])
                    had_failures = True
                    return False
            if phase_key == "implementation" and agent["name"] == "developer":
                selection = self._prepare_implementation_backlog_selection(require_backlog=True)
                if selection["error"]:
                    self.logger.error(selection["error"])
                    had_failures = True
                    return False
                task_designer_required = any(str(candidate.get("name") or "") == "task-designer" for candidate in phase.get("agents", []))
                if task_designer_required and str((self._selected_implementation_item or {}).get("contract_source") or "") != "task-designer":
                    self.logger.error("Task designer output is missing. Run task-designer before developer.")
                    self._phase_failure_status = "task_designer_invalid"
                    had_failures = True
                    return False
                if not self._enforce_implementation_scope_plan():
                    had_failures = True
                    return False
            if not self._run_agent(agent, phase_key, index=index, total=total):
                had_failures = True
                if fail_fast:
                    return False
            if phase_key == "implementation" and agent["name"] == "implementation-planner":
                if not self._validate_or_repair_implementation_planner(agent, phase_key, index=index, total=total):
                    had_failures = True
                    return False
            if phase_key == "implementation" and agent["name"] == "task-designer":
                task_designer_report = self._load_saved_agent_report("implementation", "task-designer") or {}
                if not self._apply_task_designer_contract_from_report(task_designer_report):
                    had_failures = True
                    return False
            if phase_key == "implementation" and agent["name"] == "developer":
                self._capture_repo_map_after_developer()
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
        self.logger.agent_progress(agent_name, f"Diagnostic implementation_planner_output_chars={message_bundle['implementation_planner_output_chars']}")
        self.logger.agent_progress(agent_name, f"Diagnostic backlog_task_count={message_bundle['backlog_task_count']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_model={message_bundle['planner_model']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_invalid_paths={message_bundle['planner_invalid_paths']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_repair_attempted={message_bundle['planner_repair_attempted']}")
        self.logger.agent_progress(agent_name, f"Diagnostic validated_backlog_task_count={message_bundle['validated_backlog_task_count']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_missing_directories={message_bundle['planner_missing_directories']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_missing_tests={message_bundle['planner_missing_tests']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_conflicting_forbidden_paths={message_bundle['planner_conflicting_forbidden_paths']}")
        self.logger.agent_progress(agent_name, f"Diagnostic generic_root_dirs_rejected={message_bundle['generic_root_dirs_rejected']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_dependency_graph={message_bundle['planner_dependency_graph']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_future_known_paths={message_bundle['planner_future_known_paths']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_dependency_validation_errors={message_bundle['planner_dependency_validation_errors']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_rejection_reason={message_bundle['planner_rejection_reason']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_feedback_file={message_bundle['planner_feedback_file']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_feedback_source={message_bundle['planner_feedback_source']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_feedback_chars={message_bundle['planner_feedback_chars']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_parse_error={message_bundle['planner_parse_error']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_schema_errors={message_bundle['planner_schema_errors']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_validation_stage={message_bundle['planner_validation_stage']}")
        self.logger.agent_progress(agent_name, f"Diagnostic reused_architect_output={message_bundle['reused_architect_output']}")
        self.logger.agent_progress(agent_name, f"Diagnostic architect_output_source={message_bundle['architect_output_source']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_retry_count={message_bundle['planner_retry_count']}")
        self.logger.agent_progress(agent_name, f"Diagnostic planner_retry_reason={message_bundle['planner_retry_reason']}")
        self.logger.agent_progress(agent_name, f"Diagnostic repo_map_path={message_bundle['repo_map_path']}")
        self.logger.agent_progress(agent_name, f"Diagnostic repo_map_file_count={message_bundle['repo_map_file_count']}")
        self.logger.agent_progress(agent_name, f"Diagnostic repo_map_directory_count={message_bundle['repo_map_directory_count']}")
        self.logger.agent_progress(agent_name, f"Diagnostic backlog_source={message_bundle['backlog_source']}")
        self.logger.agent_progress(agent_name, f"Diagnostic selected_task_id={message_bundle['selected_task_id']}")
        self.logger.agent_progress(agent_name, f"Diagnostic research_handoff_sources={', '.join(message_bundle['research_handoff_sources'])}")
        self.logger.agent_progress(agent_name, f"Diagnostic selected_task_scope={message_bundle['selected_task_scope']}")
        self.logger.agent_progress(agent_name, f"Diagnostic selected_task_allowed_paths={message_bundle['selected_task_allowed_paths']}")
        self.logger.agent_progress(agent_name, f"Diagnostic implementation_retrieval_enabled={message_bundle['implementation_retrieval_enabled']}")
        self.logger.agent_progress(agent_name, f"Diagnostic contract_completeness={message_bundle['contract_completeness']}")
        self.logger.agent_progress(agent_name, f"Diagnostic contract_compliance={message_bundle['contract_compliance']}")
        self.logger.agent_progress(agent_name, f"Diagnostic missing_must_contain={message_bundle['missing_must_contain']}")
        self.logger.agent_progress(agent_name, f"Diagnostic missing_test_file={message_bundle['missing_test_file']}")

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
                    "implementation_planner_output_chars": message_bundle["implementation_planner_output_chars"],
                    "backlog_task_count": message_bundle["backlog_task_count"],
                    "planner_model": message_bundle["planner_model"],
                    "planner_invalid_paths": message_bundle["planner_invalid_paths"],
                    "planner_repair_attempted": message_bundle["planner_repair_attempted"],
                    "validated_backlog_task_count": message_bundle["validated_backlog_task_count"],
                    "planner_missing_directories": message_bundle["planner_missing_directories"],
                    "planner_missing_tests": message_bundle["planner_missing_tests"],
                    "planner_conflicting_forbidden_paths": message_bundle["planner_conflicting_forbidden_paths"],
                    "generic_root_dirs_rejected": message_bundle["generic_root_dirs_rejected"],
                    "planner_dependency_graph": message_bundle["planner_dependency_graph"],
                    "planner_future_known_paths": message_bundle["planner_future_known_paths"],
                    "planner_dependency_validation_errors": message_bundle["planner_dependency_validation_errors"],
                    "planner_rejection_reason": message_bundle["planner_rejection_reason"],
                    "planner_feedback_file": message_bundle["planner_feedback_file"],
                    "planner_feedback_source": message_bundle["planner_feedback_source"],
                    "planner_feedback_chars": message_bundle["planner_feedback_chars"],
                    "planner_parse_error": message_bundle["planner_parse_error"],
                    "planner_schema_errors": message_bundle["planner_schema_errors"],
                    "planner_raw_output_excerpt": message_bundle["planner_raw_output_excerpt"],
                    "planner_extracted_payload_excerpt": message_bundle["planner_extracted_payload_excerpt"],
                    "planner_validation_stage": message_bundle["planner_validation_stage"],
                    "reused_architect_output": message_bundle["reused_architect_output"],
                    "architect_output_source": message_bundle["architect_output_source"],
                    "planner_retry_count": message_bundle["planner_retry_count"],
                    "planner_retry_reason": message_bundle["planner_retry_reason"],
                    "repo_map_path": message_bundle["repo_map_path"],
                    "repo_map_file_count": message_bundle["repo_map_file_count"],
                    "repo_map_directory_count": message_bundle["repo_map_directory_count"],
                    "research_handoff_sources": message_bundle["research_handoff_sources"],
                    "selected_task_scope": message_bundle["selected_task_scope"],
                    "selected_task_allowed_paths": message_bundle["selected_task_allowed_paths"],
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
                    "backlog_source": message_bundle["backlog_source"],
                    "selected_task_id": message_bundle["selected_task_id"],
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

        require_translation = not (phase == "implementation" and agent_name == "developer")
        failure_reason = self._detect_agent_failure(stdout, stderr, parsed_output, require_translation=require_translation)
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
            if self.repo.is_dirty(untracked_files=True):
                self.repo.git.add(A=True)
                self.repo.index.commit(f"Auto-commit before {branch_name}")
            active_branch = None
            try:
                active_branch = self.repo.active_branch.name
            except (TypeError, AttributeError):
                active_branch = None
            existing_branches = {head.name for head in self.repo.heads}
            if active_branch == branch_name:
                self.logger.info(f"Git: reuse-branch {branch_name} (already checked out)")
            elif branch_name in existing_branches:
                self.logger.info(f"Git: reuse-branch {branch_name}")
                self.repo.git.checkout(branch_name)
            else:
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
        if phase == "implementation" and agent_name == "developer":
            translation_instruction = (
                "Output format is mandatory. Do not write narrative text, explanations, plans, or translations. "
                "During the edit loop, respond only with one JSON tool request and no surrounding prose. "
                "If you complete at least one file edit, the final non-JSON response must be exactly "
                "`status=implemented`. If you cannot safely edit within scope, the final non-JSON response must be "
                "`status=no_changes: <reason>`."
            )
        elif phase == "implementation" and agent_name == "task-designer":
            translation_instruction = (
                "Output format is mandatory. Return only structured YAML or JSON for the selected task contract. "
                "Do not add translations, explanations, markdown fences, or commentary."
            )
        context_profile = "default"
        repository_context = ""
        handoff_sources: list[str] = []
        selected_task_scope = ""
        selected_task_id = ""
        selected_task_allowed_paths: list[str] = []
        backlog_source = ""
        backlog_task_count = 0
        implementation_planner_output_chars = 0
        planner_model = ""
        planner_invalid_paths: list[str] = []
        planner_repair_attempted = False
        validated_backlog_task_count = 0
        planner_missing_directories: list[str] = []
        planner_missing_tests: list[str] = []
        planner_conflicting_forbidden_paths: list[str] = []
        generic_root_dirs_rejected: list[str] = []
        planner_dependency_graph: dict[str, Any] = {}
        planner_future_known_paths: dict[str, Any] = {}
        planner_dependency_validation_errors: list[str] = []
        planner_rejection_reason = ""
        planner_feedback_file = ""
        planner_feedback_source = ""
        planner_feedback_chars = 0
        planner_parse_error = ""
        planner_schema_errors: list[str] = []
        planner_raw_output_excerpt = ""
        planner_extracted_payload_excerpt = ""
        planner_validation_stage = ""
        reused_architect_output = False
        architect_output_source = ""
        planner_retry_count = 0
        planner_retry_reason = ""
        repo_map_path = ""
        repo_map_file_count = 0
        repo_map_directory_count = 0
        contract_completeness = False
        contract_compliance = False
        missing_must_contain: list[str] = []
        missing_test_file = False
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
            selected_task_id = implementation_context["selected_task_id"]
            selected_task_allowed_paths = implementation_context["selected_task_allowed_paths"]
            backlog_source = implementation_context["backlog_source"]
            backlog_task_count = implementation_context["backlog_task_count"]
            implementation_planner_output_chars = implementation_context["implementation_planner_output_chars"]
            planner_model = implementation_context["planner_model"]
            planner_invalid_paths = implementation_context["planner_invalid_paths"]
            planner_repair_attempted = implementation_context["planner_repair_attempted"]
            validated_backlog_task_count = implementation_context["validated_backlog_task_count"]
            planner_missing_directories = implementation_context["planner_missing_directories"]
            planner_missing_tests = implementation_context["planner_missing_tests"]
            planner_conflicting_forbidden_paths = implementation_context["planner_conflicting_forbidden_paths"]
            generic_root_dirs_rejected = implementation_context["generic_root_dirs_rejected"]
            planner_dependency_graph = implementation_context["planner_dependency_graph"]
            planner_future_known_paths = implementation_context["planner_future_known_paths"]
            planner_dependency_validation_errors = implementation_context["planner_dependency_validation_errors"]
            planner_rejection_reason = implementation_context["planner_rejection_reason"]
            planner_feedback_file = implementation_context["planner_feedback_file"]
            planner_feedback_source = implementation_context["planner_feedback_source"]
            planner_feedback_chars = implementation_context["planner_feedback_chars"]
            planner_parse_error = implementation_context["planner_parse_error"]
            planner_schema_errors = implementation_context["planner_schema_errors"]
            planner_raw_output_excerpt = implementation_context["planner_raw_output_excerpt"]
            planner_extracted_payload_excerpt = implementation_context["planner_extracted_payload_excerpt"]
            planner_validation_stage = implementation_context["planner_validation_stage"]
            reused_architect_output = implementation_context["reused_architect_output"]
            architect_output_source = implementation_context["architect_output_source"]
            planner_retry_count = implementation_context["planner_retry_count"]
            planner_retry_reason = implementation_context["planner_retry_reason"]
            repo_map_path = implementation_context["repo_map_path"]
            repo_map_file_count = implementation_context["repo_map_file_count"]
            repo_map_directory_count = implementation_context["repo_map_directory_count"]
            contract_completeness = implementation_context["contract_completeness"]
            contract_compliance = implementation_context["contract_compliance"]
            missing_must_contain = implementation_context["missing_must_contain"]
            missing_test_file = implementation_context["missing_test_file"]
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
            "implementation_planner_output_chars": implementation_planner_output_chars if phase == "implementation" else 0,
            "backlog_task_count": backlog_task_count if phase == "implementation" else 0,
            "planner_model": planner_model if phase == "implementation" else "",
            "planner_invalid_paths": planner_invalid_paths if phase == "implementation" else [],
            "planner_repair_attempted": planner_repair_attempted if phase == "implementation" else False,
            "validated_backlog_task_count": validated_backlog_task_count if phase == "implementation" else 0,
            "planner_missing_directories": planner_missing_directories if phase == "implementation" else [],
            "planner_missing_tests": planner_missing_tests if phase == "implementation" else [],
            "planner_conflicting_forbidden_paths": planner_conflicting_forbidden_paths if phase == "implementation" else [],
            "generic_root_dirs_rejected": generic_root_dirs_rejected if phase == "implementation" else [],
            "planner_dependency_graph": planner_dependency_graph if phase == "implementation" else {},
            "planner_future_known_paths": planner_future_known_paths if phase == "implementation" else {},
            "planner_dependency_validation_errors": planner_dependency_validation_errors if phase == "implementation" else [],
            "planner_rejection_reason": planner_rejection_reason if phase == "implementation" else "",
            "planner_feedback_file": planner_feedback_file if phase == "implementation" else "",
            "planner_feedback_source": planner_feedback_source if phase == "implementation" else "",
            "planner_feedback_chars": planner_feedback_chars if phase == "implementation" else 0,
            "planner_parse_error": planner_parse_error if phase == "implementation" else "",
            "planner_schema_errors": planner_schema_errors if phase == "implementation" else [],
            "planner_raw_output_excerpt": planner_raw_output_excerpt if phase == "implementation" else "",
            "planner_extracted_payload_excerpt": planner_extracted_payload_excerpt if phase == "implementation" else "",
            "planner_validation_stage": planner_validation_stage if phase == "implementation" else "",
            "reused_architect_output": reused_architect_output if phase == "implementation" else False,
            "architect_output_source": architect_output_source if phase == "implementation" else "",
            "planner_retry_count": planner_retry_count if phase == "implementation" else 0,
            "planner_retry_reason": planner_retry_reason if phase == "implementation" else "",
            "repo_map_path": repo_map_path if phase == "implementation" else "",
            "repo_map_file_count": repo_map_file_count if phase == "implementation" else 0,
            "repo_map_directory_count": repo_map_directory_count if phase == "implementation" else 0,
            "contract_completeness": contract_completeness if phase == "implementation" else False,
            "contract_compliance": contract_compliance if phase == "implementation" else False,
            "missing_must_contain": missing_must_contain if phase == "implementation" else [],
            "missing_test_file": missing_test_file if phase == "implementation" else False,
            "backlog_source": backlog_source,
            "selected_task_id": selected_task_id,
            "research_handoff_sources": handoff_sources,
            "selected_task_scope": selected_task_scope,
            "selected_task_allowed_paths": selected_task_allowed_paths,
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
                ("workflow/orchestrator.py outline", self._build_python_outline(self.engine_root / "workflow/orchestrator.py")),
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
                    "Before writing, inspect the exact target files first. "
                    "If the selected task contract already provides exact file paths, start with read_file/read_files for those exact paths instead of list_files. "
                    "Use list_files only when the contract does not already provide the concrete file paths you need. "
                    "Make the smallest viable backend-only change. "
                    "Do not return narrative-only output when a safe edit is required. "
                    "Keep using JSON tool requests until you have either completed a real file edit or determined that no safe scoped edit is possible. "
                    "If you complete at least one file edit, your final non-JSON response must be exactly status=implemented. "
                    "If you cannot safely edit within scope, your final non-JSON response must be status=no_changes: <reason>."
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
        candidates: list[str] = []
        if stripped:
            candidates.append(stripped)
            fenced_matches = re.findall(r"```(?:json)?\s*([\s\S]*?)```", stripped, flags=re.IGNORECASE)
            candidates.extend(match.strip() for match in fenced_matches if match.strip())

        seen: set[str] = set()
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            tool = payload.get("tool")
            if isinstance(tool, str):
                return payload
        if stripped:
            tag_matches = list(
                re.finditer(
                    r"<(?P<tool>[a-z_][a-z0-9_-]*)\s+(?P<attrs>[^<>]*?)/>",
                    stripped,
                    flags=re.IGNORECASE,
                )
            )
            xml_payloads: list[dict[str, Any]] = []
            for match in tag_matches:
                tool = str(match.group("tool") or "").strip().lower()
                attrs_raw = str(match.group("attrs") or "")
                attrs = {
                    key.lower(): value
                    for key, _quote, value in re.findall(
                        r"([a-zA-Z_][a-zA-Z0-9_-]*)\s*=\s*(['\"])(.*?)\2",
                        attrs_raw,
                    )
                }
                payload: dict[str, Any] = {"tool": tool}
                if tool == "read_file" and attrs.get("path"):
                    payload["path"] = attrs["path"]
                elif tool == "read_files":
                    raw_paths = attrs.get("paths", "")
                    if raw_paths:
                        payload["paths"] = [part.strip() for part in re.split(r"[,;\n]+", raw_paths) if part.strip()]
                elif tool == "search_text" and attrs.get("pattern"):
                    payload["pattern"] = attrs["pattern"]
                    if attrs.get("limit"):
                        payload["limit"] = attrs["limit"]
                elif tool == "list_files":
                    if attrs.get("directory"):
                        payload["directory"] = attrs["directory"]
                    if attrs.get("max_depth"):
                        payload["max_depth"] = attrs["max_depth"]
                elif tool == "write_file" and attrs.get("path"):
                    payload["path"] = attrs["path"]
                    if attrs.get("content") is not None:
                        payload["content"] = attrs["content"]
                elif tool == "apply_patch" and attrs.get("path"):
                    payload["path"] = attrs["path"]
                    if attrs.get("search") is not None:
                        payload["search"] = attrs["search"]
                    if attrs.get("replace") is not None:
                        payload["replace"] = attrs["replace"]
                else:
                    continue
                xml_payloads.append(payload)
            if xml_payloads:
                if all(payload.get("tool") == "read_file" and payload.get("path") for payload in xml_payloads):
                    return {
                        "tool": "read_files",
                        "paths": [str(payload["path"]) for payload in xml_payloads],
                    }
                return xml_payloads[0]
            call_matches = list(
                re.finditer(
                    r"(?P<tool>read_file|read_files|search_text|list_files|write_file|apply_patch)\s*\(\s*(?P<args>\{[\s\S]*?\})\s*\)",
                    stripped,
                    flags=re.IGNORECASE,
                )
            )
            call_payloads: list[dict[str, Any]] = []
            for match in call_matches:
                tool = str(match.group("tool") or "").strip().lower()
                args_text = str(match.group("args") or "").strip()
                try:
                    args_payload = json.loads(args_text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(args_payload, dict):
                    continue
                payload = {"tool": tool, **args_payload}
                call_payloads.append(payload)
            if call_payloads:
                if all(payload.get("tool") == "read_file" and payload.get("path") for payload in call_payloads):
                    return {
                        "tool": "read_files",
                        "paths": [str(payload["path"]) for payload in call_payloads],
                    }
                leading_read_files: list[str] = []
                for payload in call_payloads:
                    if payload.get("tool") == "read_file" and payload.get("path"):
                        leading_read_files.append(str(payload["path"]))
                        continue
                    break
                if len(leading_read_files) >= 2:
                    return {
                        "tool": "read_files",
                        "paths": leading_read_files,
                    }
                return call_payloads[0]
            tool_call_matches = list(
                re.finditer(
                    r"(?:<tool_call>\s*)?(?P<tool>read_file|read_files|search_text|list_files|write_file|apply_patch)\s*\(\s*(?P<args>[^()]*)\s*\)",
                    stripped,
                    flags=re.IGNORECASE,
                )
            )
            tool_call_payloads: list[dict[str, Any]] = []
            for match in tool_call_matches:
                tool = str(match.group("tool") or "").strip().lower()
                args_text = str(match.group("args") or "").strip()
                attrs = {
                    key.lower(): value
                    for key, _quote, value in re.findall(
                        r"([a-zA-Z_][a-zA-Z0-9_-]*)\s*=\s*(['\"])(.*?)\2",
                        args_text,
                    )
                }
                payload: dict[str, Any] = {"tool": tool}
                if tool == "read_file" and attrs.get("path"):
                    payload["path"] = attrs["path"]
                elif tool == "read_files":
                    raw_paths = attrs.get("paths", "")
                    if raw_paths:
                        payload["paths"] = [part.strip() for part in re.split(r"[,;\n]+", raw_paths) if part.strip()]
                elif tool == "search_text" and attrs.get("pattern"):
                    payload["pattern"] = attrs["pattern"]
                    if attrs.get("limit"):
                        payload["limit"] = attrs["limit"]
                elif tool == "list_files":
                    if attrs.get("directory"):
                        payload["directory"] = attrs["directory"]
                    if attrs.get("max_depth"):
                        payload["max_depth"] = attrs["max_depth"]
                elif tool == "write_file" and attrs.get("path"):
                    payload["path"] = attrs["path"]
                    if attrs.get("content") is not None:
                        payload["content"] = attrs["content"]
                elif tool == "apply_patch" and attrs.get("path"):
                    payload["path"] = attrs["path"]
                    if attrs.get("search") is not None:
                        payload["search"] = attrs["search"]
                    if attrs.get("replace") is not None:
                        payload["replace"] = attrs["replace"]
                else:
                    continue
                tool_call_payloads.append(payload)
            if tool_call_payloads:
                if all(payload.get("tool") == "read_file" and payload.get("path") for payload in tool_call_payloads):
                    return {
                        "tool": "read_files",
                        "paths": [str(payload["path"]) for payload in tool_call_payloads],
                    }
                leading_read_files = []
                for payload in tool_call_payloads:
                    if payload.get("tool") == "read_file" and payload.get("path"):
                        leading_read_files.append(str(payload["path"]))
                        continue
                    break
                if len(leading_read_files) >= 2:
                    return {
                        "tool": "read_files",
                        "paths": leading_read_files,
                    }
                return tool_call_payloads[0]
            object_matches = re.findall(r"(\{[\s\S]*\})", stripped)
            object_candidates = [match.strip() for match in object_matches if match.strip()]
            seen_objects: set[str] = set()
            for candidate in object_candidates:
                if candidate in seen_objects:
                    continue
                seen_objects.add(candidate)
                try:
                    payload = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                tool = payload.get("tool")
                if isinstance(tool, str):
                    return payload
        return None

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
            if phase == "implementation" and agent_name == "developer":
                return (
                    "search_text is disabled for developer in implementation mode. "
                    "Use read_file/read_files for the exact contract paths, then either write the scoped change or return status=no_changes."
                )
            return self._direct_api_search_text(pattern, limit=max(1, min(limit, 50)))
        if tool == "list_files":
            directory = str(payload.get("directory") or ".").strip() or "."
            max_depth = self._coerce_int(payload.get("max_depth")) or 3
            if phase == "implementation" and agent_name == "developer":
                return (
                    "list_files is disabled for developer in implementation mode. "
                    "Use read_file/read_files for the exact contract paths, then either write the scoped change or return status=no_changes."
                )
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

    def _build_developer_force_write_instruction(self) -> str:
        item = self._selected_implementation_item or {}
        allowed_paths = ", ".join(item.get("allowed_paths") or []) or "none"
        existing_paths = ", ".join(item.get("existing_paths") or []) or "none"
        new_files = ", ".join(item.get("new_files") or []) or "none"
        return (
            "You now have enough local context for this scoped implementation task. "
            "Do not request more broad retrieval. "
            "On your next response, do exactly one of the following: "
            '1) emit a write tool request using {"tool":"write_file", ...} or {"tool":"apply_patch", ...} within the allowed paths; '
            "or 2) return normal text with status=no_changes and a concrete missing-scope reason. "
            f"Allowed paths: {allowed_paths}. Existing reference files: {existing_paths}. New files allowed: {new_files}."
        )

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
        selected_item = self._selected_implementation_item or {}
        existing_paths = {
            self._normalize_repo_relative_path(path)
            for path in (selected_item.get("existing_paths") or [])
            if self._normalize_repo_relative_path(path)
        }
        new_files = {
            self._normalize_repo_relative_path(path)
            for path in (selected_item.get("new_files") or [])
            if self._normalize_repo_relative_path(path)
        }
        new_directories = {
            self._normalize_repo_relative_path(path)
            for path in (selected_item.get("new_directories") or [])
            if self._normalize_repo_relative_path(path)
        }
        contract_paths: set[str] = set()
        target_file = selected_item.get("target_file") or {}
        test_file = selected_item.get("test_file") or {}
        if isinstance(target_file, dict):
            normalized = self._normalize_repo_relative_path(target_file.get("path"))
            if normalized:
                contract_paths.add(normalized)
        if isinstance(test_file, dict):
            normalized = self._normalize_repo_relative_path(test_file.get("path"))
            if normalized:
                contract_paths.add(normalized)
        repo_map = self._load_repo_map()
        repo_file_paths = {
            str(item.get("path") or "").strip()
            for item in (repo_map.get("files") or [])
            if str(item.get("path") or "").strip()
        }
        repo_directories = {
            str(path).strip()
            for path in (repo_map.get("directories") or [])
            if str(path).strip()
        }
        if not selected_item:
            if relative_path in repo_file_paths:
                existing_paths = {relative_path}
                new_files = set()
            elif self._normalize_repo_relative_path(str(Path(relative_path).parent)) in repo_directories:
                existing_paths = set()
                new_files = {relative_path}
            else:
                return False, "Write request targets a path outside repo_map and there is no selected task contract: " + relative_path, None
        elif contract_paths and relative_path not in contract_paths:
            return False, "Write request violates developer contract target/test file scope: " + relative_path, None
        elif relative_path not in existing_paths and relative_path not in new_files:
            return False, "Write request targets a path outside the selected task contract: " + relative_path, None
        validation = validate_agent_paths(
            paths=[relative_path],
            repo_map=repo_map,
            existing_paths=[relative_path] if relative_path in existing_paths else [],
            new_directories=sorted(new_directories),
            new_files=[relative_path] if relative_path in new_files else [],
            allowed_paths=[relative_path],
        )
        if not validation["valid"]:
            detail = ", ".join(validation["invalid_paths"])
            return False, "Write request violates repo_map path validation for path: " + relative_path + (f" ({detail})" if detail else ""), None
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
        blocked_retrieval_count = 0
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
                if (
                    phase == "implementation"
                    and agent_name == "developer"
                    and str(retrieval_request.get("tool") or "") in {"search_text", "list_files"}
                ):
                    blocked_retrieval_count += 1
                messages.append({"role": "assistant", "content": output_text})
                messages.append(
                    {
                        "role": "user",
                        "content": "Local retrieval result:\n" + (retrieval_output or "No matching local results."),
                    }
                )
                if (
                    phase == "implementation"
                    and agent_name == "developer"
                    and turn >= 3
                    and not self._get_agent_report_extras("implementation", "developer").get("write_tools_used")
                ):
                    messages.append(
                        {
                            "role": "user",
                            "content": self._build_developer_force_write_instruction(),
                        }
                    )
                if (
                    phase == "implementation"
                    and agent_name == "developer"
                    and not self._get_agent_report_extras("implementation", "developer").get("write_tools_used")
                    and (blocked_retrieval_count >= 2 or turn >= 4)
                ):
                    response_payload = {
                        "choices": [
                            {
                                "message": {
                                    "content": (
                                        "status=no_changes\n"
                                        "Developer exceeded the allowed retrieval policy without making a scoped file edit. "
                                        "Use only read_file/read_files for exact contract paths, then write the target/test file or report a concrete scope gap.\n\n"
                                        "Russian translation\n"
                                        "status=no_changes\n"
                                        "Разработчик превысил допустимую политику retrieval, не выполнив scoped-изменение файла. "
                                        "Используй только read_file/read_files для точных путей из контракта, затем запиши target/test файл или сообщи конкретную нехватку scope."
                                    )
                                }
                            }
                        ],
                        "model": normalized_model,
                    }
                    break
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

        require_translation = not (phase == "implementation" and agent_name == "developer")
        failure_reason = self._detect_agent_failure(stdout, "", parsed_output, require_translation=require_translation)
        if failure_reason:
            failure_status = self._classify_failure_status(failure_reason)
            self.logger.error(
                f"Agent returned invalid direct_api output: {agent_name}",
                f"elapsed_s={elapsed:.2f} | detected_failure={failure_reason} | stdout_tail={self._tail_text(stdout)}",
            )
            save_agent_report(failure_status, failure_reason, elapsed, stdout, "", parsed_output, command, 0)
            self.logger.agent_end(agent_name, failure_status, failure_reason)
            return False

        if phase == "implementation" and agent_name == "developer":
            developer_extras = self._get_agent_report_extras("implementation", "developer")
            write_tools_used = developer_extras.get("write_tools_used", [])
            normalized_output = str(parsed_output or "").strip()
            normalized_output_lower = normalized_output.lower()
            if write_tools_used:
                if not normalized_output_lower.startswith("status=implemented"):
                    parsed_output = "status=implemented"
                    stdout_payload["output_text"] = parsed_output
                    stdout = json.dumps(stdout_payload, ensure_ascii=False)
            elif not normalized_output_lower.startswith("status=no_changes"):
                reason = "Developer must use write_file/apply_patch or return status=no_changes: <reason>."
                self.logger.error(f"Agent returned invalid direct_api output: {agent_name}", reason)
                save_agent_report("no_changes", reason, elapsed, stdout, "", "status=no_changes: protocol_violation", command, 0)
                self.logger.agent_end(agent_name, "no_changes", reason)
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

    def _build_implementation_scope_instruction(self, selected_scope: str) -> str:
        lines = [
            "Selected implementation scope:",
            selected_scope,
            "",
        ]
        if self._selected_implementation_item and self._selected_implementation_item.get("allowed_paths"):
            lines.append("Allowed files for the selected task:")
            lines.extend(f"- {path}" for path in self._selected_implementation_item["allowed_paths"])
            lines.append("")
        lines.extend(
            [
            "Implementation guardrails:",
            "- Use repo_map as the source of truth for real repository paths.",
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
        )
        if self._is_agents_pipeline_self_analysis():
            lines.extend(
                [
                    "- For agents-pipeline self-analysis, prefer workflow/, tools/, tests/, start.py, run.bat, and README files.",
                    "- Do not propose src/, api/, services/, models/, or config/ root directories unless they already exist.",
                ]
            )
        if self._selected_implementation_item:
            lines.append("- Only inspect changed files during QA unless broader inspection is explicitly requested.")
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
        allowed_patterns = list(self.implementation_scope_policy["allowed_paths"])
        if self._selected_implementation_item and self._selected_implementation_item.get("allowed_paths"):
            allowed_patterns = list(self._selected_implementation_item["allowed_paths"])
        allowed_paths_matched: list[str] = []
        forbidden_hits: list[str] = []
        violations: list[str] = []
        for path in normalized_paths:
            if self._path_matches_any(path, self.implementation_scope_policy["forbidden_paths"]):
                forbidden_hits.append(f"path:{path}")
                violations.append(path)
                continue
            if self._path_matches_any(path, allowed_patterns):
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

    def _build_selected_task_planned_edit_paths(self) -> list[str]:
        selected_item = self._selected_implementation_item or {}
        candidates = [
            *list(selected_item.get("allowed_paths") or []),
            *list(selected_item.get("new_files") or []),
            *list(selected_item.get("existing_paths") or []),
        ]
        normalized: list[str] = []
        for path in candidates:
            value = self._normalize_repo_relative_path(path)
            if value:
                normalized.append(value)
        return sorted(set(normalized))

    def _enforce_implementation_scope_plan(self) -> bool:
        planner_report = self._load_saved_agent_report("implementation", "implementation-planner")
        if not planner_report or not self._selected_implementation_item:
            diagnostics = {
                "scope_policy_result": "blocked",
                "changed_files_count": 0,
                "diff_lines_count": 0,
                "forbidden_hits": ["missing_implementation_planner_output"],
                "allowed_paths_matched": [],
                "selected_task_forbidden_paths": [],
                "planned_edit_paths": [],
                "forbidden_hits_source": "precheck",
            }
            return self._handle_scope_violation(
                "developer",
                "Implementation planner output is missing; cannot validate implementation task scope.",
                diagnostics,
            )

        planned_files = self._build_selected_task_planned_edit_paths()
        path_check = self._evaluate_scope_paths(planned_files)
        forbidden_hits = list(path_check["forbidden_hits"])
        diagnostics = {
            "scope_policy_result": "allowed" if path_check["allowed"] else "blocked",
            "changed_files_count": len(planned_files),
            "diff_lines_count": 0,
            "forbidden_hits": forbidden_hits,
            "allowed_paths_matched": path_check["allowed_paths_matched"],
            "selected_task_forbidden_paths": [
                self._normalize_repo_relative_path(path)
                for path in (self._selected_implementation_item.get("forbidden_paths") or [])
                if self._normalize_repo_relative_path(path)
            ],
            "planned_edit_paths": planned_files,
            "forbidden_hits_source": "precheck",
        }
        self._set_agent_report_extras("implementation", "implementation-planner", diagnostics)
        self.logger.agent_progress("implementation-planner", f"Diagnostic scope_policy_result={diagnostics['scope_policy_result']}")
        self.logger.agent_progress("implementation-planner", f"Diagnostic changed_files_count={diagnostics['changed_files_count']}")
        self.logger.agent_progress("implementation-planner", f"Diagnostic diff_lines_count={diagnostics['diff_lines_count']}")
        self.logger.agent_progress("implementation-planner", f"Diagnostic forbidden_hits={diagnostics['forbidden_hits']}")
        self.logger.agent_progress("implementation-planner", f"Diagnostic allowed_paths_matched={diagnostics['allowed_paths_matched']}")
        self.logger.agent_progress("implementation-planner", f"Diagnostic selected_task_forbidden_paths={diagnostics['selected_task_forbidden_paths']}")
        self.logger.agent_progress("implementation-planner", f"Diagnostic planned_edit_paths={diagnostics['planned_edit_paths']}")
        self.logger.agent_progress("implementation-planner", f"Diagnostic forbidden_hits_source={diagnostics['forbidden_hits_source']}")
        existing_planner = self._load_saved_agent_report("implementation", "implementation-planner")
        if existing_planner:
            self._overwrite_agent_report("implementation", "implementation-planner", {**existing_planner, **diagnostics})
        if path_check["allowed"]:
            self._set_agent_report_extras("implementation", "developer", diagnostics)
            return True
        return self._handle_scope_violation(
            "developer",
            "Implementation planner selected task is outside the allowed implementation scope: "
            + ", ".join(path_check["violations"]),
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
            "selected_task_forbidden_paths": [
                self._normalize_repo_relative_path(path)
                for path in ((self._selected_implementation_item or {}).get("forbidden_paths") or [])
                if self._normalize_repo_relative_path(path)
            ],
            "planned_edit_paths": self._build_selected_task_planned_edit_paths(),
            "forbidden_hits_source": "diff" if forbidden_hits else "",
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
        selection = self._prepare_implementation_backlog_selection(reports=research_reports, require_backlog=False)
        selected_item = selection["selected_item"] or {}
        selected_scope = self._select_implementation_scope(research_reports)
        same_phase_context = self._build_implementation_same_phase_context(agent_name=agent_name, limit=4000)
        research_context = self._build_implementation_research_context(research_reports, agent_name, limit=5000)
        previous_parts = [part for part in [research_context, same_phase_context] if part.strip()]
        previous_context = "\n\n".join(previous_parts)
        repo_map = self._load_repo_map()

        sections = [
            ("Selected implementation scope", selected_scope),
            ("Repo map summary", self._build_repo_map_summary(repo_map=repo_map, agent_name=agent_name, limit=2600)),
            ("Target README and docs", self._build_target_docs_excerpts(limit=2000)),
            ("Target dependency and config files", self._build_target_dependency_context(limit=2200)),
            ("Target top-level tree up to depth 4", self._build_top_level_tree(root=self.target_workspace, depth=4)),
        ]
        if agent_name in {"task-designer", "developer", "qa", "template-validator"}:
            scoped_excerpts = self._build_selected_task_file_excerpts(limit=4200)
            if scoped_excerpts:
                sections.append(("Selected task file excerpts", scoped_excerpts))
        if agent_name == "implementation-planner":
            sections.extend(
                [
                    ("Existing file list", self._build_target_existing_file_list(limit=3000)),
                    ("Relevant implementation files", self._build_relevant_implementation_file_list(limit=2500)),
                    ("Relevant implementation file excerpts", self._build_relevant_implementation_file_excerpts(limit=2600)),
                ]
            )
        if agent_name in {"architect", "qa", "template-validator"}:
            sections.append(("Target tests list", self._build_target_tests_file_list(limit=1500)))
        if agent_name == "qa":
            diff_excerpt = self._build_target_git_diff_excerpt(limit=4000)
            if diff_excerpt:
                sections.append(("Target git diff", diff_excerpt))
            sections.append(("Repo map before/after summary", self._build_repo_map_delta_summary(limit=2200)))

        repository_context = self._join_context_sections(sections, limit=limit)
        contract_diag = self._evaluate_selected_task_contract_compliance() if agent_name in {"qa", "template-validator"} else {}
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
            "selected_task_id": str(selected_item.get("id") or ""),
            "selected_task_allowed_paths": list(selected_item.get("allowed_paths") or []),
            "backlog_task_count": len(selection["backlog"]),
            "implementation_planner_output_chars": self._implementation_planner_output_chars,
            "planner_model": self._resolve_agent_runtime(self._find_agent_config("implementation", "implementation-planner")).get("model", ""),
            "planner_invalid_paths": list(self._planner_invalid_paths),
            "planner_repair_attempted": self._planner_repair_attempted,
            "validated_backlog_task_count": self._validated_backlog_task_count,
            "planner_missing_directories": list(self._planner_missing_directories),
            "planner_missing_tests": list(self._planner_missing_tests),
            "planner_conflicting_forbidden_paths": list(self._planner_conflicting_forbidden_paths),
            "generic_root_dirs_rejected": list(self._generic_root_dirs_rejected),
            "planner_dependency_graph": dict(self._planner_dependency_graph),
            "planner_future_known_paths": dict(self._planner_future_known_paths),
            "planner_dependency_validation_errors": list(self._planner_dependency_validation_errors),
            "planner_rejection_reason": self._planner_rejection_reason,
            "planner_feedback_file": self._planner_feedback_file,
            "planner_feedback_source": self._planner_feedback_source,
            "planner_feedback_chars": self._planner_feedback_chars,
            "planner_parse_error": self._planner_parse_error,
            "planner_schema_errors": list(self._planner_schema_errors),
            "planner_raw_output_excerpt": self._planner_raw_output_excerpt,
            "planner_extracted_payload_excerpt": self._planner_extracted_payload_excerpt,
            "planner_validation_stage": self._planner_validation_stage,
            "reused_architect_output": self._reused_architect_output,
            "architect_output_source": self._architect_output_source,
            "planner_retry_count": self._planner_retry_count,
            "planner_retry_reason": self._planner_retry_reason,
            "repo_map_path": str(self.repo_map_path),
            "repo_map_file_count": len(repo_map.get("files") or []),
            "repo_map_directory_count": len(repo_map.get("directories") or []),
            "backlog_source": selection["backlog_source"],
            "contract_completeness": bool(selected_item.get("contract_completeness", False)),
            "contract_compliance": bool(contract_diag.get("contract_compliance", False)),
            "missing_must_contain": list(contract_diag.get("missing_must_contain") or []),
            "missing_test_file": bool(contract_diag.get("missing_test_file", False)),
            "context_chars": (len(repository_context) + len(previous_context)) if sources else 0,
        }

    def _build_target_existing_file_list(self, limit: int = 3000) -> str:
        repo_map = self._load_repo_map()
        files = [str(item.get("path") or "").strip() for item in (repo_map.get("files") or []) if str(item.get("path") or "").strip()]
        text = "\n".join(files)
        return text[:limit]

    def _build_relevant_implementation_file_list(self, limit: int = 2500) -> str:
        candidates: list[str] = []
        repo_map = self._load_repo_map()
        candidates.extend([str(path) for path in (repo_map.get("agent_relevant_files") or []) if str(path).strip()])
        for pattern in list(self.implementation_scope_policy["allowed_paths"]) + list(self.implementation_scope_policy["forbidden_paths"]):
            normalized = self._normalize_repo_relative_path(pattern)
            if normalized:
                candidates.append(normalized)
        text = "\n".join(sorted(dict.fromkeys(candidates)))
        return text[:limit]

    def _build_relevant_implementation_file_excerpts(self, limit: int = 2600) -> str:
        repo_map = self._load_repo_map()
        candidates = [str(path).strip() for path in (repo_map.get("agent_relevant_files") or []) if str(path).strip()]
        if not candidates:
            return ""
        return self._direct_api_read_files(candidates[:4], limit=limit)

    def _build_selected_task_file_excerpts(self, limit: int = 4200) -> str:
        item = self._selected_implementation_item or {}
        sections: list[str] = []
        candidate_paths: list[str] = []
        for path in item.get("existing_paths") or []:
            normalized = self._normalize_repo_relative_path(path)
            if normalized:
                candidate_paths.append(normalized)
        for path in item.get("required_test_paths") or []:
            normalized = self._normalize_repo_relative_path(path)
            if normalized:
                candidate_paths.append(normalized)
        for path in item.get("reference_files") or []:
            normalized = self._normalize_repo_relative_path(path)
            if normalized:
                candidate_paths.append(normalized)
        ordered_paths = list(dict.fromkeys(candidate_paths))
        if ordered_paths:
            file_excerpt = self._direct_api_read_files(ordered_paths, limit=max(1200, limit // 2))
            if file_excerpt:
                sections.append(file_excerpt)

        parent_dirs: list[str] = []
        for path in item.get("new_files") or []:
            normalized = self._normalize_repo_relative_path(path)
            if not normalized:
                continue
            parent = Path(normalized).parent.as_posix()
            if parent and parent != ".":
                parent_dirs.append(parent)
        for directory in list(dict.fromkeys(parent_dirs))[:4]:
            listing = self._direct_api_list_files(directory, max_depth=1)
            if listing:
                sections.append(f"### sibling files in {directory}\n{listing[:1200]}")
            sibling_excerpt = self._build_directory_reference_file_excerpts(directory, limit=max(1200, limit // 3))
            if sibling_excerpt:
                sections.append(f"### reference file excerpts from {directory}\n{sibling_excerpt}")

        text = "\n\n".join(section for section in sections if section.strip())
        return text[:limit]

    def _build_directory_reference_file_excerpts(self, directory: str, limit: int = 1400) -> str:
        base = (self.target_workspace / directory).resolve()
        try:
            base.relative_to(self.target_workspace.resolve())
        except ValueError:
            return ""
        if not base.exists() or not base.is_dir():
            return ""
        candidates: list[str] = []
        for path in sorted(base.iterdir()):
            if not path.is_file():
                continue
            relative = str(path.relative_to(self.target_workspace)).replace("\\", "/")
            candidates.append(relative)
            if len(candidates) >= 2:
                break
        if not candidates:
            return ""
        return self._direct_api_read_files(candidates, limit=limit)

    def _selected_task_has_exact_file_context(self) -> bool:
        item = self._selected_implementation_item or {}
        return bool(item.get("existing_paths") or item.get("required_test_paths") or item.get("new_files"))

    def _build_implementation_same_phase_context(self, agent_name: str, limit: int = 4000) -> str:
        if agent_name == "task-designer":
            return self._build_selected_task_outline_context(limit=limit)
        if agent_name in {"developer", "qa", "template-validator"}:
            return self._build_selected_task_contract_context(limit=limit)
        return self._build_previous_agent_context("implementation", agent_name, limit=limit)

    def _evaluate_selected_task_contract_compliance(self) -> dict[str, Any]:
        item = self._selected_implementation_item or {}
        target_file = self._normalize_repo_relative_path((item.get("target_file") or {}).get("path")) if isinstance(item.get("target_file"), dict) else ""
        test_file = self._normalize_repo_relative_path((item.get("test_file") or {}).get("path")) if isinstance(item.get("test_file"), dict) else ""
        must_contain = [str(value).strip() for value in (item.get("must_contain") or []) if str(value).strip()]
        forbidden = [str(value).strip() for value in (item.get("forbidden") or []) if str(value).strip()]
        target_text = ""
        test_text = ""
        target_exists = False
        test_exists = False
        if target_file:
            candidate = self.target_workspace / target_file
            if candidate.exists() and candidate.is_file():
                target_exists = True
                try:
                    target_text = candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    target_text = ""
        if test_file:
            candidate = self.target_workspace / test_file
            if candidate.exists() and candidate.is_file():
                test_exists = True
                try:
                    test_text = candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    test_text = ""
        missing_must_contain = [value for value in must_contain if value not in target_text]
        forbidden_hits = [value for value in forbidden if value and (value in target_text or value in test_text)]
        return {
            "contract_completeness": bool(item.get("contract_completeness", False)),
            "contract_compliance": bool(target_exists and test_exists and not missing_must_contain and not forbidden_hits),
            "missing_must_contain": missing_must_contain,
            "missing_test_file": bool(test_file and not test_exists),
            "forbidden_contract_hits": forbidden_hits,
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
        elif agent_name == "implementation-planner":
            selected_names = ["product-manager", "project-analyst", "tech-analyst"]
        elif agent_name == "task-designer":
            selected_names = []
        elif agent_name in {"developer", "qa", "template-validator"}:
            selected_names = []
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
        if self._selected_implementation_item:
            return str(self._selected_implementation_item.get("scope") or "").strip()
        if self._is_agents_pipeline_self_analysis():
            return (
                "Implement one small backend-only improvement to agents-pipeline orchestration reliability. "
                "Prefer status/resume/doctor/repo-map/validation improvements. "
                "Do not implement provider marketplace, billing, frontend, or unrelated AI Gateway features."
            )
        return str(
            self.config.get("workflow", {}).get(
                "default_implementation_scope",
                "Implement backend-only MVP provider performance monitoring and smart routing foundation. "
                "No marketplace, no Stripe changes, no frontend changes except API client stubs if required.",
            )
        )

    def print_implementation_backlog(self) -> int:
        reports, _run_dir = self._load_latest_project_research_reports()
        backlog, backlog_source = self._build_implementation_backlog(reports, allow_research_fallback=True)
        if not backlog:
            print("No implementation backlog found. Run research first.")
            return 1
        print(self._format_implementation_backlog(backlog, backlog_source))
        return 0

    def _prepare_implementation_backlog_selection(
        self,
        reports: list[dict[str, Any]] | None = None,
        *,
        require_backlog: bool,
        allow_research_fallback: bool = False,
    ) -> dict[str, Any]:
        if self._selected_implementation_item is not None:
            return {
                "selected_item": self._selected_implementation_item,
                "backlog": self._implementation_backlog_cache or [],
                "backlog_source": self._implementation_backlog_source,
                "error": "",
            }
        available_reports = reports if reports is not None else self._load_latest_project_research_reports()[0]
        backlog, backlog_source = self._build_implementation_backlog(
            available_reports,
            allow_research_fallback=allow_research_fallback,
        )
        if backlog_source == "implementation-planner":
            planner_validation = self._validate_implementation_planner_output()
            if not planner_validation["valid"]:
                backlog = []
                backlog_source = ""
        self._implementation_backlog_cache = backlog
        self._implementation_backlog_source = backlog_source
        if not backlog:
            planner_error = self._implementation_planner_backlog_error()
            return {
                "selected_item": None,
                "backlog": [],
                "backlog_source": "",
                "error": planner_error if require_backlog and planner_error else ("No implementation backlog found. Run research first." if require_backlog else ""),
            }
        selected_item = self._resolve_selected_implementation_item(backlog)
        if selected_item is None and require_backlog:
            return {
                "selected_item": None,
                "backlog": backlog,
                "backlog_source": backlog_source,
                "error": "Selected implementation task was not found in the planner backlog.",
            }
        self._selected_implementation_item = selected_item
        return {
            "selected_item": selected_item,
            "backlog": backlog,
            "backlog_source": backlog_source,
            "error": "",
        }

    def _build_implementation_backlog(
        self,
        reports: list[dict[str, Any]],
        *,
        allow_research_fallback: bool = False,
    ) -> tuple[list[dict[str, Any]], str]:
        planner_report = self._load_saved_agent_report("implementation", "implementation-planner")
        parsed_items: list[dict[str, Any]] = []
        sources: list[str] = []
        self._implementation_planner_output_chars = 0

        if planner_report and planner_report.get("status") == "success":
            planner_text = str(planner_report.get("parsed_output") or planner_report.get("stdout") or "").strip()
            self._implementation_planner_output_chars = len(planner_text)
            parsed_items = self._parse_implementation_planner_output(planner_text)["items"]
            if parsed_items:
                sources.append("implementation-planner")
            elif not allow_research_fallback:
                return [], ""

        if not parsed_items and allow_research_fallback:
            parsed_items = self._build_fallback_backlog_from_product_manager(reports)
            if parsed_items:
                sources.append("product-manager-fallback")

        if not parsed_items:
            return [], ""
        unique: dict[str, dict[str, Any]] = {}
        for item in parsed_items:
            unique[str(item["id"])] = item
        backlog = sorted(unique.values(), key=self._implementation_backlog_sort_key)
        return backlog, ",".join(sources)

    def _implementation_planner_backlog_error(self) -> str:
        planner_report = self._load_saved_agent_report("implementation", "implementation-planner")
        if planner_report is None:
            return "Implementation planner output is missing. Run architect and implementation-planner before developer."
        if planner_report.get("status") != "success":
            return "Implementation planner did not complete successfully. Fix planner output before developer."
        planner_text = str(planner_report.get("parsed_output") or planner_report.get("stdout") or "").strip()
        if not planner_text:
            return "Implementation planner output is empty. Fix planner output before developer."
        validation = self._validate_implementation_planner_output()
        if not validation["valid"]:
            details = ", ".join(validation["invalid_paths"][:6])
            if not details:
                details = ", ".join(validation.get("dependency_validation_errors", [])[:6])
            suffix = f" Invalid paths: {details}" if details else ""
            return "Implementation planner output is invalid. Fix planner output before developer." + suffix
        return ""

    def _planner_allows_restructuring(self) -> bool:
        if self.allow_scope_expansion:
            return True
        selected_scope = str(self._selected_implementation_item.get("scope") or "") if self._selected_implementation_item else ""
        if "allow project restructuring" in selected_scope.lower():
            return True
        override_scope = str(self.task_scope_override or "")
        return "allow project restructuring" in override_scope.lower()

    def _planner_suggested_existing_directories(self, repo_map: dict[str, Any]) -> list[str]:
        directories = [str(path) for path in (repo_map.get("directories") or []) if str(path).strip()]
        preferred = []
        for candidate in ["workflow", "tools", "tests", "docs"]:
            if candidate in directories:
                preferred.append(candidate)
        return preferred or directories[:12]

    def _build_planner_rejection_payload(self, repo_map: dict[str, Any]) -> dict[str, Any]:
        suggested_existing_directories = self._planner_suggested_existing_directories(repo_map)
        if self._planner_validation_stage in {"parse", "schema", "repo_map"}:
            suggested_next_action = "Fix the structured planner payload first, then rerun validation."
        elif self._planner_dependency_validation_errors:
            suggested_next_action = "Fix depends_on ordering and ensure tasks only reference files or directories created by earlier tasks in the backlog."
        elif self._generic_root_dirs_rejected:
            suggested_next_action = "Replace generic root directories with existing repo_map paths under workflow/, tools/, tests/, start.py, run.bat, or README files."
        elif self._planner_missing_tests:
            suggested_next_action = "Add required_test_paths for backend tasks, or mark the task as docs-only/config-only when appropriate."
        elif self._planner_missing_directories:
            suggested_next_action = "Declare missing parent directories in new_directories, or move files under existing repo_map directories."
        else:
            suggested_next_action = "Rewrite planner tasks to use only repo_map paths or explicit new_directories/new_files."
        return {
            "invalid_paths": list(self._planner_invalid_paths),
            "missing_directories": list(self._planner_missing_directories),
            "missing_tests": list(self._planner_missing_tests),
            "conflicting_forbidden_paths": list(self._planner_conflicting_forbidden_paths),
            "generic_root_dirs_rejected": list(self._generic_root_dirs_rejected),
            "planner_dependency_graph": dict(self._planner_dependency_graph),
            "planner_future_known_paths": dict(self._planner_future_known_paths),
            "dependency_validation_errors": list(self._planner_dependency_validation_errors),
            "parse_error": self._planner_parse_error,
            "schema_errors": list(self._planner_schema_errors),
            "raw_output_excerpt": self._planner_raw_output_excerpt,
            "extracted_payload_excerpt": self._planner_extracted_payload_excerpt,
            "repo_map_path": str(self.repo_map_path),
            "repo_map_target_workspace": str(repo_map.get("target_workspace") or ""),
            "repo_map_top_directories": [entry.rstrip("/") for entry in (repo_map.get("top_level_tree") or []) if str(entry).endswith("/")][:12],
            "validation_stage": self._planner_validation_stage or "unknown",
            "suggested_existing_directories": suggested_existing_directories,
            "suggested_next_action": suggested_next_action,
        }

    def _format_planner_rejection_block(self, payload: dict[str, Any]) -> str:
        lines = ["Implementation planner rejection reasons:"]
        for key in (
            "parse_error",
            "schema_errors",
            "invalid_paths",
            "missing_directories",
            "missing_tests",
            "conflicting_forbidden_paths",
            "generic_root_dirs_rejected",
            "dependency_validation_errors",
            "raw_output_excerpt",
            "extracted_payload_excerpt",
            "repo_map_path",
            "repo_map_target_workspace",
            "repo_map_top_directories",
            "validation_stage",
            "suggested_existing_directories",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                lines.append(f"- {key}: " + (", ".join(str(item) for item in value) if value else "none"))
            else:
                lines.append(f"- {key}: {value or 'none'}")
        lines.append(f"- suggested_next_action: {payload.get('suggested_next_action') or 'Fix planner output.'}")
        return "\n".join(lines)

    def _prompt_implementation_planner_retry_action(self) -> str:
        if self.config["workflow"]["mode"] == "auto":
            if self._planner_retry_count >= 1:
                return "stop"
            return "retry"
        while True:
            answer = input("Implementation-planner failed [r retry / a architect / s stop]: ").strip().lower()
            if answer in {"r", "retry", ""}:
                return "retry"
            if answer in {"a", "architect"}:
                return "architect"
            if answer in {"s", "stop"}:
                return "stop"
            print("Invalid choice.")

    def _build_implementation_planner_retry_prompt(self, agent_config: dict[str, Any]) -> str:
        repo_map = self._load_repo_map()
        available_directories = ", ".join((repo_map.get("directories") or [])[:40]) or "."
        feedback_text, feedback_path = self._load_planner_feedback_for_retry()
        if feedback_text:
            self.logger.info(f"Injecting planner feedback from {feedback_path}")
        else:
            self.logger.warning("Planner feedback file is missing; retrying implementation-planner without injected feedback.")
        self.logger.info(f"planner_retry_count={self._planner_retry_count}")
        instruction_block = "\n".join(
            [
                "Previous validation feedback to repair",
                feedback_text or "No previous validation feedback file was available for this retry.",
                "",
                "Repair requirements",
                "- The planner must fix all listed validation errors.",
                "- Do not repeat invalid allowed_paths.",
                "- Every backend task must include required_test_paths.",
                "- Every allowed_path must be explicitly declared in existing_paths, new_files, or new_directories.",
                "- Do not place a file in allowed_paths unless that same file is also present in existing_paths or new_files.",
                "- If a required test file lives under a new directory such as gateway-v4/tests, declare that directory in new_directories.",
                "- If adding gateway-v4/tests/__init__.py and it does not already exist, declare it in new_files and include it in allowed_paths.",
                "- Return only corrected YAML/JSON.",
            ]
        )
        return (
            str(agent_config.get("description") or "").strip()
            + " Retry round: reuse the last successful architect output. "
            + "Previous rejection reasons: "
            + (self._planner_rejection_reason or "none")
            + ". "
            + instruction_block
            + ". Invalid paths: "
            + ", ".join(self._planner_invalid_paths[:20] or ["none"])
            + ". Missing parent directories: "
            + ", ".join(self._planner_missing_directories[:20] or ["none"])
            + ". Missing tests: "
            + ", ".join(self._planner_missing_tests[:20] or ["none"])
            + ". Conflicting forbidden paths: "
            + ", ".join(self._planner_conflicting_forbidden_paths[:20] or ["none"])
            + ". Dependency validation errors: "
            + ", ".join(self._planner_dependency_validation_errors[:20] or ["none"])
            + ". Available directories: "
            + available_directories
            + ". Repair rules: every path in allowed_paths must also appear in existing_paths or new_files; existing_paths must contain exact existing files only, never directories; never guess an existing filename from a directory name or naming pattern; if an exact migration or test file is not present in repo_map, do not place it in existing_paths and treat it as new_files or omit it; if a new file lives under a directory missing from repo_map, declare that parent in new_directories; if required_test_paths uses a new test package directory, include that directory in new_directories, add the package __init__.py to new_files, and include that __init__.py in allowed_paths."
            + ". Return corrected YAML/JSON using only paths from repo_map unless declaring a new file under an existing directory."
        ).strip()

    def _planner_feedback_path_for_current_attempt(self) -> Path:
        feedback_root = self._engine_path(self.config.get("paths", {}).get("feedback_dir", ".openclaw/feedback"))
        run_id = self.logger.run_dir.name
        attempt = self._implementation_attempt if self._implementation_attempt > 0 else 1
        return feedback_root / self.project_id / run_id / f"attempt_{attempt}" / "implementation-planner.md"

    @staticmethod
    def _extract_planner_feedback_rejection_block(feedback_text: str) -> str:
        text = str(feedback_text or "").strip()
        if not text:
            return ""
        fence_match = re.search(r"Implementation planner rejection reasons\s*```text\s*(.*?)\s*```", text, re.DOTALL)
        if fence_match:
            return fence_match.group(1).strip()
        return text[:2000].strip()

    def _load_planner_feedback_for_retry(self) -> tuple[str, str]:
        candidates: list[Path] = []
        current_attempt_path = self._planner_feedback_path_for_current_attempt()
        candidates.append(current_attempt_path)
        if self._planner_feedback_file:
            candidates.append(Path(self._planner_feedback_file))
        latest_path = self._engine_path(self.config.get("paths", {}).get("feedback_dir", ".openclaw/feedback")) / self.project_id / "latest" / "implementation-planner.md"
        candidates.append(latest_path)
        for candidate in candidates:
            try:
                if candidate.exists():
                    raw = candidate.read_text(encoding="utf-8")
                    extracted = self._extract_planner_feedback_rejection_block(raw)
                    self._planner_feedback_source = str(candidate)
                    self._planner_feedback_chars = len(extracted)
                    return extracted, str(candidate)
            except Exception:
                continue
        self._planner_feedback_source = ""
        self._planner_feedback_chars = 0
        return "", ""

    def _retry_implementation_planner(
        self,
        agent_config: dict[str, Any],
        phase_key: str,
        *,
        index: int | None = None,
        total: int | None = None,
        retry_reason: str,
    ) -> bool:
        if not self._refresh_repo_map():
            return False
        self._planner_retry_count += 1
        self._planner_retry_reason = retry_reason
        if retry_reason != "rerun_architect":
            self.logger.info("Reusing architect output")
        retry_agent_config = dict(agent_config)
        retry_agent_config["description"] = self._build_implementation_planner_retry_prompt(agent_config)
        self._set_agent_report_extras(
            "implementation",
            "implementation-planner",
            {
                "reused_architect_output": self._reused_architect_output,
                "architect_output_source": self._architect_output_source,
                "planner_retry_count": self._planner_retry_count,
                "planner_retry_reason": self._planner_retry_reason,
                "planner_feedback_source": self._planner_feedback_source,
                "planner_feedback_chars": self._planner_feedback_chars,
            },
        )
        return self._run_agent(retry_agent_config, phase_key, index=index, total=total)

    def _capture_planner_rejection_feedback(self) -> None:
        repo_map = self._load_repo_map()
        rejection_payload = self._build_planner_rejection_payload(repo_map)
        self._planner_rejection_reason = self._format_planner_rejection_block(rejection_payload)
        self._planner_feedback_payload = dict(rejection_payload)
        feedback_file = self._save_feedback(
            self.task_counter or 0,
            "implementation-planner",
            "\n\n".join(["Implementation planner rejection reasons", "```text", self._planner_rejection_reason, "```"]),
        )
        self._planner_feedback_file = str(feedback_file)
        self._planner_feedback_source = str(feedback_file)
        self._planner_feedback_chars = len(self._planner_rejection_reason)

    def _validate_or_repair_implementation_planner(
        self,
        agent_config: dict[str, Any],
        phase_key: str,
        *,
        index: int | None = None,
        total: int | None = None,
    ) -> bool:
        diagnostics = self._validate_implementation_planner_output()
        if diagnostics["valid"]:
            return True
        self._planner_repair_attempted = True
        self._capture_planner_rejection_feedback()
        if self._retry_implementation_planner(agent_config, phase_key, index=index, total=total, retry_reason="initial_repair_round"):
            diagnostics = self._validate_implementation_planner_output()
            if diagnostics["valid"]:
                return True
        self._capture_planner_rejection_feedback()
        while True:
            action = self._prompt_implementation_planner_retry_action()
            if action == "retry":
                if self._retry_implementation_planner(agent_config, phase_key, index=index, total=total, retry_reason=self._planner_rejection_reason or "manual_retry"):
                    diagnostics = self._validate_implementation_planner_output()
                    if diagnostics["valid"]:
                        return True
                self._capture_planner_rejection_feedback()
                continue
            if action == "architect":
                architect_config = self._find_agent_config("implementation", "architect")
                self._reused_architect_output = False
                self._architect_output_source = ""
                if not self._run_agent(architect_config, phase_key, index=index, total=total):
                    return False
                if self._retry_implementation_planner(agent_config, phase_key, index=index, total=total, retry_reason="rerun_architect"):
                    diagnostics = self._validate_implementation_planner_output()
                    if diagnostics["valid"]:
                        return True
                self._capture_planner_rejection_feedback()
                continue
            break
        repo_map = self._load_repo_map()
        planner_report = self._load_saved_agent_report("implementation", "implementation-planner") or {}
        rejection_payload = self._build_planner_rejection_payload(repo_map)
        reason_block = self._planner_rejection_reason or self._format_planner_rejection_block(rejection_payload)
        reason = "Implementation planner output is invalid after repair round"
        self._planner_rejection_reason = reason_block
        self.logger.error(reason, reason_block)
        self._overwrite_agent_report(
            "implementation",
            "implementation-planner",
            {
                **planner_report,
                "status": "invalid_output",
                "result": reason,
                "planner_rejection_reason": self._planner_rejection_reason,
                "planner_invalid_paths": list(self._planner_invalid_paths),
                "planner_repair_attempted": True,
                "validated_backlog_task_count": self._validated_backlog_task_count,
                "planner_missing_directories": list(self._planner_missing_directories),
                "planner_missing_tests": list(self._planner_missing_tests),
                "planner_conflicting_forbidden_paths": list(self._planner_conflicting_forbidden_paths),
                "generic_root_dirs_rejected": list(self._generic_root_dirs_rejected),
                "planner_dependency_graph": dict(self._planner_dependency_graph),
                "planner_future_known_paths": dict(self._planner_future_known_paths),
                "planner_dependency_validation_errors": list(self._planner_dependency_validation_errors),
                "planner_feedback_file": self._planner_feedback_file,
                "planner_feedback_source": self._planner_feedback_source,
                "planner_feedback_chars": self._planner_feedback_chars,
                "reused_architect_output": self._reused_architect_output,
                "architect_output_source": self._architect_output_source,
                "planner_retry_count": self._planner_retry_count,
                "planner_retry_reason": self._planner_retry_reason,
            },
        )
        self.logger.agent_end("implementation-planner", "invalid_output", reason)
        self._phase_failure_status = "planner_invalid"
        return False

    def _validate_implementation_planner_output(self) -> dict[str, Any]:
        planner_report = self._load_saved_agent_report("implementation", "implementation-planner")
        self._planner_invalid_paths = []
        self._validated_backlog_task_count = 0
        self._planner_missing_directories = []
        self._planner_missing_tests = []
        self._planner_conflicting_forbidden_paths = []
        self._generic_root_dirs_rejected = []
        self._planner_dependency_graph = {}
        self._planner_future_known_paths = {}
        self._planner_dependency_validation_errors = []
        self._planner_rejection_reason = ""
        self._planner_parse_error = ""
        self._planner_schema_errors = []
        self._planner_raw_output_excerpt = ""
        self._planner_extracted_payload_excerpt = ""
        self._planner_validation_stage = ""
        if not planner_report or planner_report.get("status") != "success":
            return {"valid": False, "invalid_paths": [], "task_count": 0}
        repo_map = self._load_repo_map()
        if not self._validate_repo_map_target(repo_map):
            self._planner_validation_stage = "repo_map"
            return {
                "valid": False,
                "invalid_paths": [],
                "parse_error": self._planner_parse_error,
                "schema_errors": list(self._planner_schema_errors),
                "task_count": 0,
            }
        planner_text = str(planner_report.get("parsed_output") or planner_report.get("stdout") or "").strip()
        self._planner_raw_output_excerpt = planner_text[:400]
        parsed = self._parse_implementation_planner_output(planner_text)
        items = parsed["items"]
        self._planner_parse_error = parsed["parse_error"]
        self._planner_schema_errors = list(parsed["schema_errors"])
        self._planner_extracted_payload_excerpt = parsed["extracted_payload_excerpt"]
        if self._planner_parse_error:
            self._planner_validation_stage = "parse"
            return {
                "valid": False,
                "invalid_paths": [],
                "parse_error": self._planner_parse_error,
                "schema_errors": list(self._planner_schema_errors),
                "task_count": 0,
            }
        if self._planner_schema_errors:
            self._planner_validation_stage = "schema"
            return {
                "valid": False,
                "invalid_paths": [],
                "parse_error": self._planner_parse_error,
                "schema_errors": list(self._planner_schema_errors),
                "task_count": 0,
            }
        if not items:
            self._planner_validation_stage = "schema"
            self._planner_schema_errors = ["planner_payload_contains_no_valid_tasks"]
            return {"valid": False, "invalid_paths": [], "schema_errors": list(self._planner_schema_errors), "task_count": 0}
        sequential = self._validate_planner_backlog_items_sequential(items, repo_map=repo_map)
        self._planner_invalid_paths = sorted(dict.fromkeys(sequential["invalid_paths"]))
        self._planner_missing_directories = sorted(dict.fromkeys(sequential["missing_directories"]))
        self._planner_missing_tests = sorted(dict.fromkeys(sequential["missing_tests"]))
        self._planner_conflicting_forbidden_paths = sorted(dict.fromkeys(sequential["conflicting_forbidden_paths"]))
        self._generic_root_dirs_rejected = sorted(dict.fromkeys(sequential["generic_root_dirs_rejected"]))
        self._planner_dependency_graph = dict(sequential["dependency_graph"])
        self._planner_future_known_paths = dict(sequential["future_known_paths"])
        self._planner_dependency_validation_errors = sorted(dict.fromkeys(sequential["dependency_validation_errors"]))
        self._validated_backlog_task_count = int(sequential["validated_count"])
        self._planner_validation_stage = "path/test-policy" if (
            self._planner_invalid_paths
            or self._planner_missing_directories
            or self._planner_missing_tests
            or self._planner_conflicting_forbidden_paths
            or self._generic_root_dirs_rejected
            or self._planner_dependency_validation_errors
        ) else ""
        self._set_agent_report_extras(
            "implementation",
            "implementation-planner",
            {
                "planner_invalid_paths": list(self._planner_invalid_paths),
                "planner_repair_attempted": self._planner_repair_attempted,
                "validated_backlog_task_count": self._validated_backlog_task_count,
                "planner_missing_directories": list(self._planner_missing_directories),
                "planner_missing_tests": list(self._planner_missing_tests),
                "planner_conflicting_forbidden_paths": list(self._planner_conflicting_forbidden_paths),
                "generic_root_dirs_rejected": list(self._generic_root_dirs_rejected),
                "planner_dependency_graph": dict(self._planner_dependency_graph),
                "planner_future_known_paths": dict(self._planner_future_known_paths),
                "planner_dependency_validation_errors": list(self._planner_dependency_validation_errors),
                "planner_rejection_reason": self._planner_rejection_reason,
                "planner_feedback_file": self._planner_feedback_file,
                "planner_parse_error": self._planner_parse_error,
                "planner_schema_errors": list(self._planner_schema_errors),
                "planner_raw_output_excerpt": self._planner_raw_output_excerpt,
                "planner_extracted_payload_excerpt": self._planner_extracted_payload_excerpt,
                "planner_validation_stage": self._planner_validation_stage,
                "repo_map_path": str(self.repo_map_path),
                "repo_map_file_count": len(repo_map.get("files") or []),
                "repo_map_directory_count": len(repo_map.get("directories") or []),
            },
        )
        return {
            "valid": not (
                self._planner_invalid_paths
                or self._planner_missing_directories
                or self._planner_missing_tests
                or self._planner_conflicting_forbidden_paths
                or self._generic_root_dirs_rejected
                or self._planner_dependency_validation_errors
                or self._planner_parse_error
                or self._planner_schema_errors
            ),
            "invalid_paths": list(self._planner_invalid_paths),
            "missing_directories": list(self._planner_missing_directories),
            "missing_tests": list(self._planner_missing_tests),
            "conflicting_forbidden_paths": list(self._planner_conflicting_forbidden_paths),
            "generic_root_dirs_rejected": list(self._generic_root_dirs_rejected),
            "dependency_validation_errors": list(self._planner_dependency_validation_errors),
            "parse_error": self._planner_parse_error,
            "schema_errors": list(self._planner_schema_errors),
            "task_count": self._validated_backlog_task_count,
        }

    def _validate_planner_backlog_item(
        self,
        item: dict[str, Any],
        *,
        repo_map: dict[str, Any],
        known_files: set[str] | None = None,
        known_directories: set[str] | None = None,
    ) -> tuple[list[str], list[str], list[str], list[str], list[str]]:
        invalid: list[str] = []
        existing_paths = [self._normalize_repo_relative_path(path) for path in item.get("existing_paths") or [] if self._normalize_repo_relative_path(path)]
        new_directories = [self._normalize_repo_relative_path(path) for path in item.get("new_directories") or [] if self._normalize_repo_relative_path(path)]
        new_files = [self._normalize_repo_relative_path(path) for path in item.get("new_files") or [] if self._normalize_repo_relative_path(path)]
        allowed_paths = [self._normalize_repo_relative_path(path) for path in item.get("allowed_paths") or [] if self._normalize_repo_relative_path(path)]
        required_test_paths = [self._normalize_repo_relative_path(path) for path in item.get("required_test_paths") or [] if self._normalize_repo_relative_path(path)]
        reasons = item.get("reason_each_path_is_needed") or {}
        missing_directories: list[str] = []
        missing_tests: list[str] = []
        conflicting_forbidden_paths: list[str] = []
        generic_root_dirs_rejected: list[str] = []
        if not allowed_paths:
            invalid.append(f"{item.get('id', 'task')}:missing_allowed_paths")
            return invalid, missing_directories, missing_tests, conflicting_forbidden_paths, generic_root_dirs_rejected
        allow_restructuring = self._planner_allows_restructuring()
        validation = validate_agent_paths(
            paths=allowed_paths,
            repo_map=repo_map,
            existing_paths=existing_paths,
            new_directories=new_directories,
            new_files=new_files,
            allowed_paths=allowed_paths,
            allow_restructuring=allow_restructuring,
            known_files=known_files,
            known_directories=known_directories,
        )
        invalid.extend(f"{item.get('id', 'task')}:{detail}" for detail in validation["invalid_paths"])
        for detail in validation["invalid_paths"]:
            if detail.endswith(":new_directory_parent_missing") or detail.endswith(":new_file_parent_missing"):
                missing_directories.append(f"{item.get('id', 'task')}:{detail}")
            if detail.endswith(":generic_nonexistent_directory"):
                generic_root_dirs_rejected.append(f"{item.get('id', 'task')}:{detail}")
        forbidden_paths = [self._normalize_repo_relative_path(path) for path in item.get("forbidden_paths") or [] if self._normalize_repo_relative_path(path)]
        for test_path in required_test_paths:
            if any(self._path_matches_any(test_path, [forbidden_path]) for forbidden_path in forbidden_paths):
                detail = f"{item.get('id', 'task')}:{test_path}:forbidden_test_path_conflict"
                invalid.append(detail)
                conflicting_forbidden_paths.append(detail)
        if self._task_requires_tests(item):
            if not required_test_paths:
                detail = f"{item.get('id', 'task')}:missing_required_test_paths"
                invalid.append(detail)
                missing_tests.append(detail)
            elif not any(self._is_test_path(path) for path in required_test_paths):
                detail = f"{item.get('id', 'task')}:required_test_paths_must_include_test_files"
                invalid.append(detail)
                missing_tests.append(detail)
        invalid.extend(self._validate_planner_task_outline(item, repo_map=repo_map, known_files=set(known_files or set())))
        for path in allowed_paths:
            if path not in existing_paths and path not in new_files and path not in new_directories:
                invalid.append(f"{item.get('id', 'task')}:{path}:allowed_path_not_declared")
            if not str(reasons.get(path) or "").strip():
                invalid.append(f"{item.get('id', 'task')}:{path}:missing_path_reason")
        return invalid, missing_directories, missing_tests, conflicting_forbidden_paths, generic_root_dirs_rejected

    def _validate_planner_backlog_items_sequential(
        self,
        items: list[dict[str, Any]],
        *,
        repo_map: dict[str, Any],
    ) -> dict[str, Any]:
        repo_files = {str(item.get("path") or "").strip() for item in (repo_map.get("files") or []) if str(item.get("path") or "").strip()}
        repo_directories = {str(path).strip() for path in (repo_map.get("directories") or []) if str(path).strip()}
        repo_directories.add("")
        task_index = {str(item.get("id") or ""): index for index, item in enumerate(items)}
        file_creators: dict[str, str] = {}
        directory_creators: dict[str, str] = {}
        dependency_graph: dict[str, dict[str, list[str]]] = {}
        dependency_validation_errors: list[str] = []

        for item in items:
            task_id = str(item.get("id") or "")
            dependency_graph[task_id] = {"declared": list(item.get("depends_on") or []), "inferred": [], "effective": []}
            for path in item.get("new_files") or []:
                normalized = self._normalize_repo_relative_path(path)
                if normalized and normalized not in file_creators:
                    file_creators[normalized] = task_id
            for path in item.get("new_directories") or []:
                normalized = self._normalize_repo_relative_path(path)
                if normalized and normalized not in directory_creators:
                    directory_creators[normalized] = task_id

        for item in items:
            task_id = str(item.get("id") or "")
            declared = [str(dep).strip() for dep in (item.get("depends_on") or []) if str(dep).strip()]
            for dep in declared:
                if dep not in task_index:
                    dependency_validation_errors.append(f"{task_id}:{dep}:missing_dependency")
                elif task_index[dep] >= task_index[task_id]:
                    dependency_validation_errors.append(f"{task_id}:{dep}:dependency_order_violation")

        known_files = set(repo_files)
        known_directories = set(repo_directories)
        future_known_paths: dict[str, dict[str, list[str]]] = {}
        invalid_paths: list[str] = []
        missing_directories: list[str] = []
        missing_tests: list[str] = []
        conflicting_forbidden_paths: list[str] = []
        generic_root_dirs_rejected: list[str] = []
        validated_count = 0

        for item in items:
            task_id = str(item.get("id") or "")
            existing_paths = [self._normalize_repo_relative_path(path) for path in item.get("existing_paths") or [] if self._normalize_repo_relative_path(path)]
            new_files = [self._normalize_repo_relative_path(path) for path in item.get("new_files") or [] if self._normalize_repo_relative_path(path)]
            new_directories = [self._normalize_repo_relative_path(path) for path in item.get("new_directories") or [] if self._normalize_repo_relative_path(path)]
            declared_dependencies = [str(dep).strip() for dep in (item.get("depends_on") or []) if str(dep).strip()]
            inferred_dependencies: set[str] = set()
            dependency_error_count_before = len(dependency_validation_errors)

            for path in existing_paths:
                if path in known_files:
                    continue
                creator = file_creators.get(path)
                if not creator:
                    continue
                creator_index = task_index.get(creator, -1)
                current_index = task_index.get(task_id, -1)
                if creator_index > current_index:
                    dependency_validation_errors.append(f"{task_id}:{path}:path_used_before_creation")
                    continue
                if creator not in declared_dependencies:
                    inferred_dependencies.add(creator)

            declared_directory_paths = set(known_directories)
            for directory in sorted(new_directories, key=lambda value: len(Path(value).parts)):
                declared_directory_paths.add(directory)
            for path in new_files:
                parent = self._normalize_repo_relative_path(str(Path(path).parent))
                if parent in known_directories or parent in declared_directory_paths:
                    continue
                creator = directory_creators.get(parent)
                if not creator:
                    continue
                creator_index = task_index.get(creator, -1)
                current_index = task_index.get(task_id, -1)
                if creator_index > current_index:
                    dependency_validation_errors.append(f"{task_id}:{path}:path_used_before_creation")
                    continue
                if creator not in declared_dependencies:
                    inferred_dependencies.add(creator)

            effective_dependencies = sorted(dict.fromkeys([*declared_dependencies, *sorted(inferred_dependencies)]))
            dependency_graph[task_id]["inferred"] = sorted(inferred_dependencies)
            dependency_graph[task_id]["effective"] = effective_dependencies

            item_invalid, item_missing_directories, item_missing_tests, item_conflicts, item_generic_roots = self._validate_planner_backlog_item(
                item,
                repo_map=repo_map,
                known_files=known_files,
                known_directories=known_directories,
            )
            if item_invalid:
                invalid_paths.extend(item_invalid)
                missing_directories.extend(item_missing_directories)
                missing_tests.extend(item_missing_tests)
                conflicting_forbidden_paths.extend(item_conflicts)
                generic_root_dirs_rejected.extend(item_generic_roots)
            else:
                validated_count += 1
            task_had_dependency_errors = len(dependency_validation_errors) > dependency_error_count_before
            if not item_invalid and not task_had_dependency_errors:
                known_directories.update(new_directories)
                known_files.update(new_files)
            future_known_paths[task_id] = {
                "new_directories": sorted(new_directories),
                "new_files": sorted(new_files),
                "known_directories_after_task": sorted(path for path in known_directories if path)[:200],
                "known_files_after_task": sorted(known_files)[:200],
            }

        cycle_graph = {task_id: list(node.get("effective") or []) for task_id, node in dependency_graph.items()}
        cycle_nodes = self._detect_dependency_cycles(cycle_graph)
        for task_id in cycle_nodes:
            dependency_validation_errors.append(f"{task_id}:cyclic_dependency")

        return {
            "invalid_paths": invalid_paths,
            "missing_directories": missing_directories,
            "missing_tests": missing_tests,
            "conflicting_forbidden_paths": conflicting_forbidden_paths,
            "generic_root_dirs_rejected": generic_root_dirs_rejected,
            "dependency_graph": dependency_graph,
            "future_known_paths": future_known_paths,
            "dependency_validation_errors": dependency_validation_errors,
            "validated_count": validated_count,
        }

    @staticmethod
    def _detect_dependency_cycles(graph: dict[str, list[str]]) -> set[str]:
        visiting: set[str] = set()
        visited: set[str] = set()
        cycle_nodes: set[str] = set()

        def visit(node: str, stack: list[str]) -> None:
            if node in visited:
                return
            if node in visiting:
                if node in stack:
                    cycle_nodes.update(stack[stack.index(node):])
                else:
                    cycle_nodes.add(node)
                return
            visiting.add(node)
            stack.append(node)
            for dep in graph.get(node, []):
                if dep in graph:
                    visit(dep, stack)
            stack.pop()
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node, [])
        return cycle_nodes

    def _validate_generic_nonexistent_path(self, path: str) -> str:
        normalized = self._normalize_repo_relative_path(path)
        if not normalized:
            return "invalid_path"
        first_segment = normalized.split("/", 1)[0]
        if first_segment not in {"api", "services", "storage"}:
            return ""
        candidate_dir = (self.target_workspace / first_segment).resolve()
        if candidate_dir.exists() and candidate_dir.is_dir():
            return ""
        return "generic_nonexistent_directory"

    def _is_valid_target_relative_path(self, path: str) -> bool:
        normalized = self._normalize_repo_relative_path(path)
        if not normalized or normalized.startswith(".."):
            return False
        try:
            resolved = (self.target_workspace / normalized).resolve()
        except Exception:
            return False
        return resolved == self.target_workspace or self.target_workspace in resolved.parents

    def _build_fallback_backlog_from_product_manager(self, reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
        product_manager_report = next(
            (
                report
                for report in reports
                if str(report.get("agent_name") or report.get("agent") or "").strip() == "product-manager"
                and report.get("status") == "success"
            ),
            None,
        )
        if not product_manager_report:
            return []
        source_text = str(
            product_manager_report.get("handoff_summary")
            or product_manager_report.get("parsed_output")
            or ""
        ).strip()
        if not source_text:
            return []
        fallback_items = self._parse_implementation_backlog_items(source_text, source_name="product-manager")
        normalized_items: list[dict[str, Any]] = []
        for item in fallback_items:
            fallback_required_tests = self._default_required_test_paths(item)
            normalized_items.append(
                {
                    "id": str(item.get("id") or "").strip(),
                    "title": str(item.get("title") or "").strip(),
                    "priority": str(item.get("priority") or "P1").upper(),
                    "scope": str(item.get("scope") or "").strip(),
                    "existing_paths": list(item.get("allowed_paths") or list(self.implementation_scope_policy["allowed_paths"])),
                    "new_directories": [],
                    "new_files": [],
                    "allowed_paths": list(item.get("allowed_paths") or list(self.implementation_scope_policy["allowed_paths"])),
                    "forbidden_paths": list(self.implementation_scope_policy["forbidden_paths"]),
                    "required_test_paths": fallback_required_tests,
                    "acceptance_criteria": [str(item.get("scope") or "").strip()],
                    "reason_each_path_is_needed": {
                        str(path): "Fallback path derived from product-manager summary."
                        for path in list(item.get("allowed_paths") or list(self.implementation_scope_policy["allowed_paths"]))
                    },
                    "target_file": {
                        "path": (list(item.get("allowed_paths") or list(self.implementation_scope_policy["allowed_paths"])) or [""])[0],
                        "action": "modify",
                        "purpose": str(item.get("scope") or "").strip(),
                    },
                    "must_contain": [],
                    "must_import": [],
                    "integration": [str(item.get("scope") or "").strip()],
                    "reference_files": list(item.get("allowed_paths") or list(self.implementation_scope_policy["allowed_paths"])),
                    "reference_excerpts": {},
                    "test_file": {"path": (fallback_required_tests or [""])[0]},
                    "must_test": list(fallback_required_tests),
                    "forbidden": list(self.implementation_scope_policy["forbidden_paths"]),
                    "contract_completeness": False,
                    "risk_level": str(item.get("risk_level") or "medium").lower(),
                    "estimated_effort": str(item.get("estimated_effort") or "M").upper(),
                }
            )
        return [item for item in normalized_items if item["id"] and item["title"] and item["scope"]]

    def _parse_implementation_planner_output(self, text: str) -> dict[str, Any]:
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return {
                "items": [],
                "parse_error": "planner_output_is_empty",
                "schema_errors": [],
                "raw_output_excerpt": "",
                "extracted_payload_excerpt": "",
            }
        candidates = self._planner_payload_candidates(normalized_text)
        parse_errors: list[str] = []
        for candidate in candidates:
            if not candidate.strip():
                continue
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                try:
                    payload = yaml.safe_load(candidate)
                except yaml.YAMLError as exc:
                    parse_errors.append(str(exc))
                    continue
            extracted_payload_excerpt = str(candidate).strip()[:400]
            if isinstance(payload, dict):
                for key in ("tasks", "backlog", "items"):
                    if isinstance(payload.get(key), list):
                        payload = payload[key]
                        break
            if not isinstance(payload, list):
                return {
                    "items": [],
                    "parse_error": "",
                    "schema_errors": ["planner_payload_must_be_a_list_or_tasks_object"],
                    "raw_output_excerpt": normalized_text[:400],
                    "extracted_payload_excerpt": extracted_payload_excerpt,
                }
            items: list[dict[str, Any]] = []
            schema_errors: list[str] = []
            for index, raw_item in enumerate(payload, start=1):
                normalized, item_errors = self._normalize_planner_task(raw_item, item_index=index)
                if item_errors:
                    schema_errors.extend(item_errors)
                elif normalized:
                    items.append(normalized)
            return {
                "items": items,
                "parse_error": "",
                "schema_errors": schema_errors,
                "raw_output_excerpt": normalized_text[:400],
                "extracted_payload_excerpt": extracted_payload_excerpt,
            }
        return {
            "items": [],
            "parse_error": "; ".join(parse_errors[:3]) or "unable_to_parse_planner_payload",
            "schema_errors": [],
            "raw_output_excerpt": normalized_text[:400],
            "extracted_payload_excerpt": "",
        }

    def _normalize_planner_task(self, raw_item: Any, *, item_index: int) -> tuple[dict[str, Any] | None, list[str]]:
        if not isinstance(raw_item, dict):
            return None, [f"task_{item_index}:task_must_be_object"]
        title = str(raw_item.get("title") or "").strip()
        task_id = str(raw_item.get("id") or "").strip()
        scope = str(raw_item.get("scope") or title).strip()
        schema_errors: list[str] = []
        if not task_id:
            schema_errors.append(f"task_{item_index}:missing_id")
        if not title:
            schema_errors.append(f"task_{item_index}:missing_title")
        if not scope:
            schema_errors.append(f"task_{item_index}:missing_scope")
        if schema_errors:
            return None, schema_errors
        existing_paths = [
            self._normalize_repo_relative_path(path)
            for path in (raw_item.get("existing_paths") or [])
            if self._normalize_repo_relative_path(path)
        ]
        new_directories = [
            self._normalize_repo_relative_path(path)
            for path in (raw_item.get("new_directories") or [])
            if self._normalize_repo_relative_path(path)
        ]
        new_files = [
            self._normalize_repo_relative_path(path)
            for path in (raw_item.get("new_files") or [])
            if self._normalize_repo_relative_path(path)
        ]
        allowed_paths = [
            self._normalize_repo_relative_path(path)
            for path in (raw_item.get("allowed_paths") or [])
            if self._normalize_repo_relative_path(path)
        ]
        forbidden_paths = [
            self._normalize_repo_relative_path(path)
            for path in (raw_item.get("forbidden_paths") or [])
            if self._normalize_repo_relative_path(path)
        ]
        required_test_paths = [
            self._normalize_repo_relative_path(path)
            for path in (raw_item.get("required_test_paths") or [])
            if self._normalize_repo_relative_path(path)
        ]
        raw_target_file = raw_item.get("target_file") or {}
        target_file_path = self._normalize_repo_relative_path(raw_target_file.get("path")) if isinstance(raw_target_file, dict) else ""
        target_file_action = str(raw_target_file.get("action") or "").strip().lower() if isinstance(raw_target_file, dict) else ""
        target_file_purpose = str(raw_target_file.get("purpose") or "").strip() if isinstance(raw_target_file, dict) else ""
        must_contain = [str(item).strip() for item in (raw_item.get("must_contain") or []) if str(item).strip()]
        must_import = [str(item).strip() for item in (raw_item.get("must_import") or []) if str(item).strip()]
        integration = [str(item).strip() for item in (raw_item.get("integration") or []) if str(item).strip()]
        reference_files = [
            self._normalize_repo_relative_path(path)
            for path in (raw_item.get("reference_files") or [])
            if self._normalize_repo_relative_path(path)
        ]
        raw_reference_excerpts = raw_item.get("reference_excerpts") or {}
        if isinstance(raw_reference_excerpts, dict):
            reference_excerpts = {
                self._normalize_repo_relative_path(path): str(snippet).strip()
                for path, snippet in raw_reference_excerpts.items()
                if self._normalize_repo_relative_path(path) and str(snippet).strip()
            }
        else:
            reference_excerpts = {}
        raw_test_file = raw_item.get("test_file") or {}
        test_file_path = self._normalize_repo_relative_path(raw_test_file.get("path")) if isinstance(raw_test_file, dict) else ""
        test_file_action = str(raw_test_file.get("action") or "").strip().lower() if isinstance(raw_test_file, dict) else ""
        must_test = [str(item).strip() for item in (raw_item.get("must_test") or []) if str(item).strip()]
        contract_forbidden = [str(item).strip() for item in (raw_item.get("forbidden") or []) if str(item).strip()]
        depends_on = [
            str(task_ref).strip()
            for task_ref in (raw_item.get("depends_on") or [])
            if str(task_ref).strip()
        ]
        acceptance_criteria = [str(item).strip() for item in (raw_item.get("acceptance_criteria") or []) if str(item).strip()]
        if not acceptance_criteria:
            acceptance_criteria = [scope]
        raw_reasons = raw_item.get("reason_each_path_is_needed") or {}
        if isinstance(raw_reasons, dict):
            reason_each_path_is_needed = {
                self._normalize_repo_relative_path(path): str(reason).strip()
                for path, reason in raw_reasons.items()
                if self._normalize_repo_relative_path(path) and str(reason).strip()
            }
        else:
            reason_each_path_is_needed = {}
        if not reason_each_path_is_needed:
            reason_each_path_is_needed = {path: "Planner selected this path for the scoped task." for path in allowed_paths}
        if not target_file_path:
            target_file_path = (new_files or allowed_paths or existing_paths or [""])[0]
        if not target_file_action:
            target_file_action = "create" if target_file_path in new_files else "update"
        if not target_file_purpose:
            target_file_purpose = scope
        if not test_file_path:
            test_file_path = (required_test_paths or [""])[0]
        if not test_file_action:
            test_file_action = "create" if test_file_path in new_files else "update"
        if not reference_files:
            reference_files = list(dict.fromkeys(existing_paths))
        if not must_test:
            must_test = list(required_test_paths)
        if not integration:
            integration = [scope]
        if not contract_forbidden:
            contract_forbidden = [str(path) for path in forbidden_paths]
        contract_completeness = all(
            [target_file_path, target_file_action in {"create", "update", "modify"}, bool(target_file_purpose), bool(test_file_path), test_file_action in {"create", "update", "modify"}]
        )
        return {
            "id": task_id,
            "title": title,
            "priority": str(raw_item.get("priority") or "P1").upper(),
            "scope": scope,
            "existing_paths": existing_paths,
            "new_directories": new_directories,
            "new_files": new_files,
            "allowed_paths": allowed_paths or list(self.implementation_scope_policy["allowed_paths"]),
            "forbidden_paths": forbidden_paths,
            "required_test_paths": required_test_paths,
            "depends_on": depends_on,
            "acceptance_criteria": acceptance_criteria,
            "reason_each_path_is_needed": reason_each_path_is_needed,
            "_target_file_declared": bool(target_file_path) if isinstance(raw_target_file, dict) else False,
            "_test_file_declared": bool(test_file_path) if isinstance(raw_test_file, dict) else False,
            "_must_contain_declared": "must_contain" in raw_item,
            "_must_test_declared": "must_test" in raw_item,
            "_depends_on_declared": "depends_on" in raw_item,
            "target_file": {"path": target_file_path, "action": target_file_action, "purpose": target_file_purpose},
            "must_contain": must_contain,
            "must_import": must_import,
            "integration": integration,
            "reference_files": reference_files,
            "reference_excerpts": reference_excerpts,
            "test_file": {"path": test_file_path, "action": test_file_action},
            "must_test": must_test,
            "forbidden": contract_forbidden,
            "contract_completeness": contract_completeness,
            "risk_level": str(raw_item.get("risk_level") or "medium").lower(),
            "estimated_effort": str(raw_item.get("estimated_effort") or "M").upper(),
        }, []

    def _parse_implementation_backlog_items(self, text: str, *, source_name: str) -> list[dict[str, Any]]:
        normalized_text = str(text or "").replace("\r\n", "\n")
        priority_pattern = re.compile(r"^\s*(?:[-*]\s*)?\[(P[0-2])\]\s*(.+?)\s*$", re.IGNORECASE)
        items: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for raw_line in normalized_text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            match = priority_pattern.match(line)
            if match:
                if current:
                    items.append(self._finalize_backlog_item(current, source_name))
                current = {
                    "priority": match.group(1).upper(),
                    "title": match.group(2).strip(),
                    "scope": "",
                    "expected_files": [],
                    "risk_level": "medium",
                    "estimated_effort": "M",
                }
                continue
            if current is None:
                continue
            lowered = line.lower()
            if lowered.startswith("scope:"):
                current["scope"] = line.split(":", 1)[1].strip()
            elif lowered.startswith("files:"):
                current["expected_files"] = [
                    self._normalize_repo_relative_path(part)
                    for part in line.split(":", 1)[1].split(",")
                    if self._normalize_repo_relative_path(part)
                ]
            elif lowered.startswith("risk:"):
                current["risk_level"] = line.split(":", 1)[1].strip().lower() or "medium"
            elif lowered.startswith("effort:"):
                current["estimated_effort"] = line.split(":", 1)[1].strip().upper() or "M"
        if current:
            items.append(self._finalize_backlog_item(current, source_name))
        if items:
            return items

        sections = self._extract_handoff_sections(normalized_text)
        fallback_items: list[dict[str, Any]] = []
        for index, task_text in enumerate(sections.get("recommended_next_tasks", []), start=1):
            cleaned = str(task_text).strip()
            if not cleaned or cleaned.lower() == "none":
                continue
            fallback_items.append(
                {
                    "id": f"{source_name}-next-{index}",
                    "title": cleaned[:80],
                    "priority": self._infer_priority_from_text(cleaned),
                    "scope": cleaned,
                    "expected_files": [],
                    "allowed_paths": list(self.implementation_scope_policy["allowed_paths"]),
                    "risk_level": self._infer_risk_from_text(cleaned),
                    "estimated_effort": self._infer_effort_from_text(cleaned),
                    "source_name": source_name,
                }
            )
        return fallback_items

    def _finalize_backlog_item(self, item: dict[str, Any], source_name: str) -> dict[str, Any]:
        title = str(item.get("title") or "").strip()
        scope = str(item.get("scope") or title).strip()
        expected_files = [path for path in item.get("expected_files", []) if path]
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "task"
        payload = {
            "id": f"{source_name}-{slug}",
            "title": title,
            "priority": str(item.get("priority") or "P1").upper(),
            "scope": scope,
            "expected_files": expected_files,
            "allowed_paths": expected_files or list(self.implementation_scope_policy["allowed_paths"]),
            "risk_level": str(item.get("risk_level") or "medium").lower(),
            "estimated_effort": str(item.get("estimated_effort") or "M").upper(),
            "source_name": source_name,
        }
        payload["required_test_paths"] = self._default_required_test_paths(payload)
        return payload

    def _implementation_backlog_sort_key(self, item: dict[str, Any]) -> tuple[int, int, int, str]:
        priority_rank = {"P0": 0, "P1": 1, "P2": 2}.get(str(item.get("priority") or "P2").upper(), 3)
        risk_rank = {"low": 0, "medium": 1, "high": 2}.get(str(item.get("risk_level") or "medium").lower(), 1)
        blob = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("scope") or ""),
                " ".join(item.get("allowed_paths") or []),
            ]
        ).lower()
        domain_rank = 0
        if self._is_agents_pipeline_self_analysis():
            if any(marker in blob for marker in ("status", "resume", "doctor", "repo_map", "repo-map", "validation", "retry", "recovery", "progress")):
                domain_rank = -1
            if any(marker in blob for marker in ("provider performance monitoring", "smart provider routing", "marketplace", "stripe", "billing")):
                domain_rank = 3
        if any(marker in blob for marker in ("frontend",)):
            domain_rank = 1
        if any(marker in blob for marker in ("billing", "stripe", "payment", "checkout", "marketplace")):
            domain_rank = 2
        return (priority_rank, risk_rank, domain_rank, str(item.get("title") or ""))

    @staticmethod
    def _infer_priority_from_text(text: str) -> str:
        lowered = text.lower()
        if "p0" in lowered or "critical" in lowered or "blocker" in lowered:
            return "P0"
        if "p2" in lowered or "later" in lowered or "optional" in lowered:
            return "P2"
        return "P1"

    @staticmethod
    def _infer_risk_from_text(text: str) -> str:
        lowered = text.lower()
        if any(marker in lowered for marker in ("low risk", "safe", "small", "contained")):
            return "low"
        if any(marker in lowered for marker in ("high risk", "broad", "migration", "dangerous")):
            return "high"
        return "medium"

    @staticmethod
    def _infer_effort_from_text(text: str) -> str:
        lowered = text.lower()
        if any(marker in lowered for marker in ("small", "minor", "single file", "quick")):
            return "S"
        if any(marker in lowered for marker in ("large", "broad", "migration", "multi-step")):
            return "L"
        return "M"

    @staticmethod
    def _is_test_path(path: str) -> bool:
        normalized = str(path or "").replace("\\", "/").strip()
        name = Path(normalized).name.lower()
        return normalized.startswith("tests/") or "/tests/" in normalized or name.startswith("test_")

    @staticmethod
    def _is_vague_contract_item(text: str) -> bool:
        normalized = str(text or "").strip().lower()
        if not normalized:
            return True
        vague_markers = (
            "implement ",
            "add functionality",
            "create system",
            "handle ",
            "support ",
            "improve ",
            "update logic",
            "monitoring functionality",
        )
        if any(marker in normalized for marker in vague_markers):
            return True
        if normalized.startswith(("return ", "yield ", "raise ")):
            return False
        if re.fullmatch(r"[a-z_][a-z0-9_\.]*", normalized):
            return False
        signal_markers = ("class ", "def ", "async def ", "mapped[", " = ", "assert ", "test_", "(", ":", "->")
        return not any(marker in normalized for marker in signal_markers)

    def _task_requires_tests(self, item: dict[str, Any]) -> bool:
        scope_blob = " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("scope") or ""),
                " ".join(item.get("allowed_paths") or []),
                " ".join(item.get("existing_paths") or []),
                " ".join(item.get("new_files") or []),
            ]
        ).lower()
        if any(marker in scope_blob for marker in ("docs-only", "doc-only", "documentation only", "docs/", "readme", ".md")):
            return False
        if any(marker in scope_blob for marker in ("config-only", "config only", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env")):
            return False
        if any(marker in scope_blob for marker in ("frontend", "ui", ".jsx", ".tsx", ".css")):
            return False
        return True

    def _validate_planner_task_outline(
        self,
        item: dict[str, Any],
        *,
        repo_map: dict[str, Any],
        known_files: set[str] | None = None,
    ) -> list[str]:
        task_id = str(item.get("id") or "task")
        allowed_paths = [self._normalize_repo_relative_path(path) for path in item.get("allowed_paths") or [] if self._normalize_repo_relative_path(path)]
        existing_paths = [self._normalize_repo_relative_path(path) for path in item.get("existing_paths") or [] if self._normalize_repo_relative_path(path)]
        new_files = [self._normalize_repo_relative_path(path) for path in item.get("new_files") or [] if self._normalize_repo_relative_path(path)]
        required_test_paths = [self._normalize_repo_relative_path(path) for path in item.get("required_test_paths") or [] if self._normalize_repo_relative_path(path)]
        target_file = item.get("target_file") or {}
        target_file_path = self._normalize_repo_relative_path(target_file.get("path")) if isinstance(target_file, dict) else ""
        target_file_action = str(target_file.get("action") or "").strip().lower() if isinstance(target_file, dict) else ""
        reference_files = [self._normalize_repo_relative_path(path) for path in item.get("reference_files") or [] if self._normalize_repo_relative_path(path)]
        repo_file_paths = {
            str(repo_item.get("path") or "").strip()
            for repo_item in (repo_map.get("files") or [])
            if str(repo_item.get("path") or "").strip()
        }
        effective_known_files = set(known_files or set()) | repo_file_paths
        errors: list[str] = []
        if not target_file_path:
            errors.append(f"{task_id}:missing_target_file_outline")
        elif target_file_path not in allowed_paths:
            errors.append(f"{task_id}:target_file_path_not_in_allowed_paths")
        elif target_file_path not in existing_paths and target_file_path not in new_files:
            errors.append(f"{task_id}:target_file_path_not_declared")
        if target_file_action not in {"create", "update", "modify"}:
            errors.append(f"{task_id}:invalid_target_file_action")
        for test_path in required_test_paths:
            if test_path not in existing_paths and test_path not in new_files:
                errors.append(f"{task_id}:{test_path}:required_test_path_not_declared")
        for ref_path in reference_files:
            if ref_path not in effective_known_files:
                errors.append(f"{task_id}:{ref_path}:reference_file_missing")
        return errors

    def _validate_backend_task_contract(self, item: dict[str, Any]) -> list[str]:
        task_id = str(item.get("id") or "task")
        allowed_paths = [self._normalize_repo_relative_path(path) for path in item.get("allowed_paths") or [] if self._normalize_repo_relative_path(path)]
        existing_paths = [self._normalize_repo_relative_path(path) for path in item.get("existing_paths") or [] if self._normalize_repo_relative_path(path)]
        new_files = [self._normalize_repo_relative_path(path) for path in item.get("new_files") or [] if self._normalize_repo_relative_path(path)]
        required_test_paths = [self._normalize_repo_relative_path(path) for path in item.get("required_test_paths") or [] if self._normalize_repo_relative_path(path)]
        target_file = item.get("target_file") or {}
        test_file = item.get("test_file") or {}
        target_file_path = self._normalize_repo_relative_path(target_file.get("path")) if isinstance(target_file, dict) else ""
        target_file_action = str(target_file.get("action") or "").strip().lower() if isinstance(target_file, dict) else ""
        test_file_path = self._normalize_repo_relative_path(test_file.get("path")) if isinstance(test_file, dict) else ""
        test_file_action = str(test_file.get("action") or "").strip().lower() if isinstance(test_file, dict) else ""
        must_contain = [str(value).strip() for value in (item.get("must_contain") or []) if str(value).strip()]
        must_test = [str(value).strip() for value in (item.get("must_test") or []) if str(value).strip()]
        errors: list[str] = []

        if not item.get("_target_file_declared", False):
            errors.append(f"{task_id}:missing_target_file")
        if not item.get("_test_file_declared", False):
            errors.append(f"{task_id}:missing_test_file_contract")
        if not item.get("_depends_on_declared", False):
            errors.append(f"{task_id}:missing_depends_on")
        if not item.get("_must_contain_declared", False):
            errors.append(f"{task_id}:missing_must_contain_contract")
        if not item.get("_must_test_declared", False):
            errors.append(f"{task_id}:missing_must_test_contract")

        if target_file_path and target_file_path not in allowed_paths:
            errors.append(f"{task_id}:target_file_path_not_in_allowed_paths")
        if target_file_path and target_file_path not in existing_paths and target_file_path not in new_files:
            errors.append(f"{task_id}:target_file_path_not_declared")
        if target_file_action not in {"create", "update", "modify"}:
            errors.append(f"{task_id}:invalid_target_file_action")

        if test_file_path and test_file_path not in required_test_paths:
            errors.append(f"{task_id}:test_file_path_not_in_required_test_paths")
        if test_file_path and test_file_path not in existing_paths and test_file_path not in new_files:
            errors.append(f"{task_id}:test_file_path_not_declared")
        if test_file_action not in {"create", "update", "modify"}:
            errors.append(f"{task_id}:invalid_test_file_action")

        if len(must_contain) < 2:
            errors.append(f"{task_id}:must_contain_too_short")
        elif len(must_contain) > 5:
            errors.append(f"{task_id}:must_contain_too_long")
        for value in must_contain:
            if self._is_vague_contract_item(value):
                errors.append(f"{task_id}:vague_must_contain:{value[:80]}")

        if len(must_test) < 1:
            errors.append(f"{task_id}:must_test_empty")
        elif len(must_test) > 3:
            errors.append(f"{task_id}:must_test_too_long")
        for value in must_test:
            if self._is_vague_contract_item(value):
                errors.append(f"{task_id}:vague_must_test:{value[:80]}")

        return errors

    def _parse_task_designer_output(self, text: str, base_item: dict[str, Any]) -> dict[str, Any]:
        normalized_text = str(text or "").strip()
        if not normalized_text:
            return {"item": None, "errors": ["task-designer:empty_output"]}
        candidates = self._planner_payload_candidates(normalized_text)
        parse_errors: list[str] = []
        repo_map = self._load_repo_map()
        repo_file_paths = {
            str(repo_item.get("path") or "").strip()
            for repo_item in (repo_map.get("files") or [])
            if str(repo_item.get("path") or "").strip()
        }
        for candidate in candidates:
            if not candidate.strip():
                continue
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                try:
                    payload = yaml.safe_load(candidate)
                except yaml.YAMLError as exc:
                    parse_errors.append(str(exc))
                    continue
            if not isinstance(payload, dict):
                return {"item": None, "errors": ["task-designer:payload_must_be_object"]}
            status = str(payload.get("status") or "").strip().lower()
            if status == "contract_invalid":
                reason = str(payload.get("reason") or "unspecified_contract_invalid").strip()
                return {"item": None, "errors": [f"task-designer:contract_invalid:{reason}"]}
            task_ref = str(payload.get("task_id") or payload.get("id") or "").strip()
            expected_id = str(base_item.get("id") or "").strip()
            if task_ref and expected_id and task_ref != expected_id:
                return {"item": None, "errors": [f"task-designer:task_id_mismatch:{task_ref}:{expected_id}"]}

            merged = dict(base_item)
            merged.update(
                {
                    "id": expected_id or task_ref,
                    "title": str(payload.get("title") or base_item.get("title") or "").strip(),
                    "depends_on": payload.get("depends_on", base_item.get("depends_on") or []),
                    "target_file": payload.get("target_file") or base_item.get("target_file") or {},
                    "must_contain": payload.get("must_contain") or [],
                    "must_import": payload.get("must_import") or [],
                    "integration": payload.get("integration") or [],
                    "reference_files": payload.get("reference_files") or base_item.get("reference_files") or [],
                    "reference_excerpts": payload.get("reference_excerpts") or {},
                    "test_file": payload.get("test_file") or {},
                    "must_test": payload.get("must_test") or [],
                    "forbidden": payload.get("forbidden") or [],
                }
            )
            normalized, schema_errors = self._normalize_planner_task(merged, item_index=1)
            if schema_errors or not normalized:
                return {"item": None, "errors": [f"task-designer:{error}" for error in schema_errors]}
            contract_errors = self._validate_backend_task_contract(normalized)
            for ref_path in normalized.get("reference_files") or []:
                if ref_path not in repo_file_paths:
                    contract_errors.append(f"{normalized.get('id') or 'task'}:{ref_path}:reference_file_missing")
            normalized["contract_source"] = "task-designer"
            normalized["task_designer_notes"] = [str(value).strip() for value in (payload.get("notes") or []) if str(value).strip()]
            return {"item": normalized, "errors": contract_errors}
        return {"item": None, "errors": [f"task-designer:parse_error:{'; '.join(parse_errors[:3]) or 'unable_to_parse'}"]}

    def _apply_task_designer_contract_from_report(self, report: dict[str, Any]) -> bool:
        if not self._selected_implementation_item:
            selection = self._prepare_implementation_backlog_selection(require_backlog=True)
            if selection["error"]:
                self.logger.error(selection["error"])
                return False
        planner_item = dict(self._selected_implementation_item or {})
        saved_contract = report.get("selected_task_contract")
        if isinstance(saved_contract, dict):
            normalized, schema_errors = self._normalize_planner_task(saved_contract, item_index=1)
            if normalized and not schema_errors:
                errors = self._validate_backend_task_contract(normalized)
                normalized["contract_source"] = "task-designer"
                if not errors:
                    self._selected_implementation_item = normalized
                    self._task_designer_validation_errors = []
                    return True
        parsed_text = str(report.get("parsed_output") or report.get("stdout") or "").strip()
        parsed = self._parse_task_designer_output(parsed_text, planner_item)
        contract_item = parsed["item"]
        errors = [str(error).strip() for error in (parsed.get("errors") or []) if str(error).strip()]
        self._task_designer_validation_errors = errors
        if not contract_item or errors:
            feedback = "Task designer validation errors\n\n```text\n" + ("\n".join(errors) or "unknown_error") + "\n```"
            feedback_file = self._save_feedback(self.task_counter or 0, "task-designer", feedback)
            self._task_designer_feedback_file = str(feedback_file)
            self._task_designer_feedback_source = str(feedback_file)
            self._task_designer_rejection_reason = "\n".join(errors)
            self.logger.error("Task designer output is invalid", self._task_designer_rejection_reason or "unknown_error")
            self._phase_failure_status = "task_designer_invalid"
            return False
        self._selected_implementation_item = contract_item
        self._set_agent_report_extras(
            "implementation",
            "task-designer",
            {
                "selected_task_id": str(contract_item.get("id") or ""),
                "selected_task_scope": str(contract_item.get("scope") or ""),
                "selected_task_allowed_paths": list(contract_item.get("allowed_paths") or []),
                "contract_completeness": bool(contract_item.get("contract_completeness", False)),
                "contract_source": "task-designer",
                "task_designer_validation_errors": [],
            },
        )
        task_designer_report = self._load_saved_agent_report("implementation", "task-designer") or report
        self._overwrite_agent_report(
            "implementation",
            "task-designer",
            {
                **task_designer_report,
                "selected_task_id": str(contract_item.get("id") or ""),
                "selected_task_scope": str(contract_item.get("scope") or ""),
                "selected_task_allowed_paths": list(contract_item.get("allowed_paths") or []),
                "selected_task_contract": contract_item,
                "contract_completeness": bool(contract_item.get("contract_completeness", False)),
                "contract_source": "task-designer",
                "task_designer_validation_errors": [],
            },
        )
        return True

    def _is_agents_pipeline_self_analysis(self) -> bool:
        if self.context_mode != "engine_self_analysis":
            return False
        project_name = str(self.config.get("project", {}).get("name") or "").strip().lower()
        return (
            project_name == "agents-pipeline"
            or self.target_workspace.name.lower() == "agents-pipeline"
            or "agents-pipeline" in self.project_id
        )

    def _default_required_test_paths(self, item: dict[str, Any]) -> list[str]:
        if not self._task_requires_tests(item):
            return []
        title = str(item.get("title") or item.get("id") or "implementation").lower()
        slug = re.sub(r"[^a-z0-9]+", "_", title).strip("_") or "implementation"
        return [f"tests/test_{slug}.py"]

    @staticmethod
    def _planner_payload_candidates(text: str) -> list[str]:
        normalized = str(text or "").strip()
        if not normalized:
            return []
        english_only = normalized.split("Russian translation", 1)[0].strip()
        candidates: list[str] = []
        for source in [english_only, normalized]:
            fenced_matches = re.findall(r"```(?:yaml|yml|json)?\s*([\s\S]*?)```", source, flags=re.IGNORECASE)
            candidates.extend(match.strip() for match in fenced_matches if match.strip())
            candidates.append(source.strip())
        deduped: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            trimmed = candidate.strip()
            if not trimmed or trimmed in seen:
                continue
            seen.add(trimmed)
            deduped.append(trimmed)
        return deduped

    def _should_reuse_architect_for_implementation(self) -> bool:
        if self.reuse_architect:
            return True
        if self.retry_agent_name == "implementation-planner":
            return True
        if self.from_agent_name == "implementation-planner":
            return True
        return self.from_agent_name in {"task-designer", "developer", "qa", "template-validator"} or self.retry_agent_name in {"task-designer", "developer", "qa", "template-validator"}

    def _should_reuse_planner_for_implementation(self) -> bool:
        if self.from_agent_name in {"task-designer", "developer", "qa", "template-validator"}:
            return True
        return self.retry_agent_name in {"task-designer", "developer", "qa", "template-validator"}

    def _should_reuse_task_designer_for_implementation(self) -> bool:
        if self.from_agent_name in {"developer", "qa", "template-validator"}:
            return True
        return self.retry_agent_name in {"developer", "qa", "template-validator"}

    def _should_skip_implementation_agent(self, agent_name: str) -> bool:
        if self._should_reuse_architect_for_implementation() and agent_name == "architect":
            return True
        if self._should_reuse_planner_for_implementation() and agent_name == "implementation-planner":
            return True
        if self._should_reuse_task_designer_for_implementation() and agent_name == "task-designer":
            return True
        if self.retry_agent_name == "implementation-planner" and agent_name in {"task-designer", "developer", "qa", "template-validator"}:
            return True
        if self.from_agent_name == "implementation-planner" and agent_name in {"task-designer", "developer", "qa", "template-validator"}:
            return True
        if self.retry_agent_name == "task-designer" and agent_name in {"developer", "qa", "template-validator"}:
            return True
        if self.from_agent_name == "task-designer" and agent_name in {"architect", "implementation-planner"}:
            return True
        if self.retry_agent_name == "developer" and agent_name in {"qa", "template-validator"}:
            return True
        if self.from_agent_name == "developer" and agent_name in {"architect", "implementation-planner", "task-designer"}:
            return True
        if self.from_agent_name == "qa" and agent_name in {"architect", "implementation-planner", "task-designer", "developer"}:
            return True
        if self.from_agent_name == "template-validator" and agent_name in {"architect", "implementation-planner", "task-designer", "developer", "qa"}:
            return True
        return False

    def _load_latest_successful_implementation_report(self, agent_name: str) -> tuple[dict[str, Any] | None, str]:
        current = self._load_saved_agent_report("implementation", agent_name)
        if current and current.get("status") == "success":
            return current, str(self.logger.run_dir / "agents" / "implementation" / f"{agent_name}.json")
        for run_dir in sorted(self.logger.log_dir.glob("run_*"), reverse=True):
            report = self._load_saved_agent_report("implementation", agent_name, run_dir=run_dir)
            if report and report.get("status") == "success":
                return report, str(run_dir / "agents" / "implementation" / f"{agent_name}.json")
        return None, ""

    def _load_latest_implementation_report_with_selected_task(self) -> tuple[dict[str, Any] | None, str]:
        candidate_agents = ("developer", "qa", "template-validator", "task-designer", "implementation-planner")
        current_base = self.logger.run_dir / "agents" / "implementation"
        for agent_name in candidate_agents:
            report = self._load_saved_agent_report("implementation", agent_name)
            if report and str(report.get("selected_task_id") or "").strip():
                return report, str(current_base / f"{agent_name}.json")
        for run_dir in sorted(self.logger.log_dir.glob("run_*"), reverse=True):
            for agent_name in candidate_agents:
                report = self._load_saved_agent_report("implementation", agent_name, run_dir=run_dir)
                if report and str(report.get("selected_task_id") or "").strip():
                    return report, str(run_dir / "agents" / "implementation" / f"{agent_name}.json")
        return None, ""

    def _reuse_architect_output_for_current_run(self) -> bool:
        report, source_path = self._load_latest_successful_implementation_report("architect")
        if not report:
            self.logger.error("Unable to reuse architect output", "No successful architect report found.")
            return False
        target_dir = self.logger.run_dir / "agents" / "implementation"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / "architect.json"
        target_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        self._reused_architect_output = True
        self._architect_output_source = source_path
        self.logger.info(f"Reusing architect output from {source_path}")
        self._set_agent_report_extras(
            "implementation",
            "implementation-planner",
            {
                "reused_architect_output": True,
                "architect_output_source": source_path,
            },
        )
        return True

    def _reuse_planner_output_for_current_run(self) -> bool:
        report, source_path = self._load_latest_successful_implementation_report("implementation-planner")
        if not report:
            self.logger.error("Unable to reuse implementation-planner output", "No successful implementation-planner report found.")
            return False
        target_dir = self.logger.run_dir / "agents" / "implementation"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / "implementation-planner.json"
        target_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        selected_task_id = str(report.get("selected_task_id") or "").strip()
        if not selected_task_id:
            selected_task_report, _selected_source = self._load_latest_implementation_report_with_selected_task()
            if selected_task_report:
                selected_task_id = str(selected_task_report.get("selected_task_id") or "").strip()
        if selected_task_id and not self.selected_task_ref:
            self.selected_task_ref = selected_task_id
        self._set_agent_report_extras(
            "implementation",
            "implementation-planner",
            {
                "reused_architect_output": self._reused_architect_output,
                "architect_output_source": self._architect_output_source,
                "planner_retry_count": self._planner_retry_count,
                "planner_retry_reason": self._planner_retry_reason,
            },
        )
        self.logger.info(f"Reusing implementation-planner output from {source_path}")
        return True

    def _reuse_task_designer_output_for_current_run(self) -> bool:
        report, source_path = self._load_latest_successful_implementation_report("task-designer")
        if not report:
            if not self._selected_implementation_item:
                selection = self._prepare_implementation_backlog_selection(require_backlog=True)
                if selection["error"]:
                    self.logger.error(selection["error"])
                    return False
            fallback_item = dict(self._selected_implementation_item or {})
            if fallback_item and not self._validate_backend_task_contract(fallback_item):
                fallback_item["contract_source"] = "task-designer"
                self._selected_implementation_item = fallback_item
                self.logger.info("No successful task-designer report found; reusing planner task contract as compatibility fallback.")
                return True
            self.logger.error("Unable to reuse task-designer output", "No successful task-designer report found.")
            return False
        if not self._selected_implementation_item:
            selection = self._prepare_implementation_backlog_selection(require_backlog=True)
            if selection["error"]:
                self.logger.error(selection["error"])
                return False
        target_dir = self.logger.run_dir / "agents" / "implementation"
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / "task-designer.json"
        target_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        selected_task_id = str(report.get("selected_task_id") or "").strip()
        if selected_task_id and not self.selected_task_ref:
            self.selected_task_ref = selected_task_id
        if not self._apply_task_designer_contract_from_report(report):
            return False
        self.logger.info(f"Reusing task-designer output from {source_path}")
        return True

    def _validate_repo_map_target(self, repo_map: dict[str, Any]) -> bool:
        repo_map_target = str(repo_map.get("target_workspace") or "").strip()
        if repo_map_target and Path(repo_map_target).resolve() != self.target_workspace.resolve():
            self._planner_parse_error = "repo_map_target_workspace_mismatch"
            self._planner_schema_errors = [
                f"repo_map_target_workspace={repo_map_target}",
                f"runtime_target_workspace={self.target_workspace}",
                f"repo_map_path={self.repo_map_path}",
            ]
            return False
        if self.target_workspace.name.lower() == "myai":
            gateway_path = self.target_workspace / "gateway-v4"
            top_directories = {str(entry).rstrip("/") for entry in (repo_map.get("top_level_tree") or []) if str(entry).endswith("/")}
            if gateway_path.exists() and "gateway-v4" not in top_directories:
                self._planner_parse_error = "repo_map_missing_gateway_v4_for_myai_target"
                self._planner_schema_errors = ["gateway-v4_exists_in_target_workspace_but_missing_from_repo_map"]
                return False
        return True

    def _resolve_selected_implementation_item(self, backlog: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not backlog:
            return None
        if self.next_task_requested:
            completed = set(self._completed_implementation_task_ids())
            for item in backlog:
                if str(item.get("id")) not in completed:
                    return item
        if self.selected_task_ref:
            ref = self.selected_task_ref.strip()
            if ref.isdigit():
                index = int(ref) - 1
                if 0 <= index < len(backlog):
                    return backlog[index]
            for item in backlog:
                if str(item.get("id")) == ref:
                    return item
            return None
        if self.config["workflow"]["mode"] == "auto":
            print(self._format_implementation_backlog(backlog, self._implementation_backlog_source or "research"))
            return backlog[0]
        print(self._format_implementation_backlog(backlog, self._implementation_backlog_source or "research"))
        while True:
            answer = input(self._console_text("choose_task_number")).strip()
            if answer.isdigit():
                index = int(answer) - 1
                if 0 <= index < len(backlog):
                    return backlog[index]
            print(self._console_text("invalid_task_number"))

    def _console_language(self) -> str:
        return str(self.config.get("workflow", {}).get("console_language", "ru") or "ru").strip().lower()

    def _console_text(self, key: str) -> str:
        ru = {
            "backlog_title": "Бэклог реализации",
            "source": "источник",
            "existing_files": "существующие файлы",
            "new_directories": "новые директории",
            "new_files": "новые файлы",
            "allowed_paths": "разрешённые пути",
            "required_tests": "обязательные тесты",
            "acceptance": "критерии приемки",
            "risk": "риск",
            "effort": "оценка трудозатрат",
            "choose_task_number": "Выберите номер задачи для реализации: ",
            "invalid_task_number": "Некорректный номер задачи.",
        }
        en = {
            "backlog_title": "Implementation backlog",
            "source": "source",
            "existing_files": "existing files",
            "new_directories": "new directories",
            "new_files": "new files",
            "allowed_paths": "allowed paths",
            "required_tests": "required tests",
            "acceptance": "acceptance",
            "risk": "risk",
            "effort": "effort",
            "choose_task_number": "Choose implementation task number: ",
            "invalid_task_number": "Invalid task number.",
        }
        catalog = en if self._console_language() == "en" else ru
        return catalog.get(key, key)

    def _format_implementation_backlog(self, backlog: list[dict[str, Any]], backlog_source: str) -> str:
        lines = [f"{self._console_text('backlog_title')} ({self._console_text('source')}={backlog_source or 'research'})"]
        for index, item in enumerate(backlog, start=1):
            files = ", ".join(item.get("allowed_paths") or []) or "policy default"
            existing_files = ", ".join(item.get("existing_paths") or []) or "none"
            new_directories = ", ".join(item.get("new_directories") or []) or "none"
            new_files = ", ".join(item.get("new_files") or []) or "none"
            required_tests = ", ".join(item.get("required_test_paths") or []) or "none"
            acceptance = "; ".join(item.get("acceptance_criteria") or []) or "n/a"
            lines.extend(
                [
                    f"{index}. {item['title']}",
                    f"   id: {item['id']}",
                    f"   priority: {item['priority']}",
                    f"   scope: {item['scope']}",
                    f"   {self._console_text('existing_files')}: {existing_files}",
                    f"   {self._console_text('new_directories')}: {new_directories}",
                    f"   {self._console_text('new_files')}: {new_files}",
                    f"   {self._console_text('allowed_paths')}: {files}",
                    f"   {self._console_text('required_tests')}: {required_tests}",
                    f"   {self._console_text('acceptance')}: {acceptance}",
                    f"   {self._console_text('risk')}: {item['risk_level']}",
                    f"   {self._console_text('effort')}: {item['estimated_effort']}",
                ]
            )
        return "\n".join(lines)

    def _build_selected_task_contract_context(self, limit: int = 4000) -> str:
        if not self._selected_implementation_item:
            return ""
        item = self._selected_implementation_item
        lines = [
            "[selected-task-contract]",
            f"id: {item.get('id', '')}",
            f"title: {item.get('title', '')}",
            f"priority: {item.get('priority', '')}",
            f"scope: {item.get('scope', '')}",
            "existing_paths:",
        ]
        lines.extend(f"- {path}" for path in (item.get("existing_paths") or []))
        lines.extend([
            "new_directories:",
        ])
        lines.extend(f"- {path}" for path in (item.get("new_directories") or []))
        lines.extend([
            "new_files:",
        ])
        lines.extend(f"- {path}" for path in (item.get("new_files") or []))
        lines.extend([
            "allowed_paths:",
        ])
        lines.extend(f"- {path}" for path in (item.get("allowed_paths") or []))
        lines.append("forbidden_paths:")
        lines.extend(f"- {path}" for path in (item.get("forbidden_paths") or []))
        lines.append("required_test_paths:")
        lines.extend(f"- {path}" for path in (item.get("required_test_paths") or []))
        lines.append("acceptance_criteria:")
        lines.extend(f"- {criterion}" for criterion in (item.get("acceptance_criteria") or []))
        lines.append("reason_each_path_is_needed:")
        for path, reason in (item.get("reason_each_path_is_needed") or {}).items():
            lines.append(f"- {path}: {reason}")
        lines.append("developer_contract:")
        lines.append("  depends_on:")
        lines.extend(f"  - {value}" for value in (item.get("depends_on") or []))
        target_file = item.get("target_file") or {}
        lines.append(f"  target_file.path: {target_file.get('path', '')}")
        lines.append(f"  target_file.action: {target_file.get('action', '')}")
        lines.append(f"  target_file.purpose: {target_file.get('purpose', '')}")
        lines.append("  must_contain:")
        lines.extend(f"  - {value}" for value in (item.get("must_contain") or []))
        lines.append("  must_import:")
        lines.extend(f"  - {value}" for value in (item.get("must_import") or []))
        lines.append("  integration:")
        lines.extend(f"  - {value}" for value in (item.get("integration") or []))
        lines.append("  reference_files:")
        lines.extend(f"  - {value}" for value in (item.get("reference_files") or []))
        lines.append("  reference_excerpts:")
        for path, snippet in (item.get("reference_excerpts") or {}).items():
            lines.append(f"  - {path}: {str(snippet)[:280]}")
        test_file = item.get("test_file") or {}
        lines.append(f"  test_file.path: {test_file.get('path', '')}")
        lines.append(f"  test_file.action: {test_file.get('action', '')}")
        lines.append("  must_test:")
        lines.extend(f"  - {value}" for value in (item.get("must_test") or []))
        lines.append("  forbidden:")
        lines.extend(f"  - {value}" for value in (item.get("forbidden") or []))
        lines.append(f"  contract_completeness: {item.get('contract_completeness', False)}")
        lines.append(f"risk_level: {item.get('risk_level', '')}")
        lines.append(f"estimated_effort: {item.get('estimated_effort', '')}")
        text = "\n".join(lines)
        return text[:limit]

    def _build_selected_task_outline_context(self, limit: int = 2800) -> str:
        if not self._selected_implementation_item:
            return ""
        item = self._selected_implementation_item
        target_file = item.get("target_file") or {}
        lines = [
            "[selected-task-outline]",
            f"id: {item.get('id', '')}",
            f"title: {item.get('title', '')}",
            f"priority: {item.get('priority', '')}",
            f"scope: {item.get('scope', '')}",
            "existing_paths:",
        ]
        lines.extend(f"- {path}" for path in (item.get("existing_paths") or []))
        lines.append("new_directories:")
        lines.extend(f"- {path}" for path in (item.get("new_directories") or []))
        lines.append("new_files:")
        lines.extend(f"- {path}" for path in (item.get("new_files") or []))
        lines.append("allowed_paths:")
        lines.extend(f"- {path}" for path in (item.get("allowed_paths") or []))
        lines.append("forbidden_paths:")
        lines.extend(f"- {path}" for path in (item.get("forbidden_paths") or []))
        lines.append("required_test_paths:")
        lines.extend(f"- {path}" for path in (item.get("required_test_paths") or []))
        lines.append("acceptance_criteria:")
        lines.extend(f"- {criterion}" for criterion in (item.get("acceptance_criteria") or []))
        lines.append("reason_each_path_is_needed:")
        for path, reason in (item.get("reason_each_path_is_needed") or {}).items():
            lines.append(f"- {path}: {reason}")
        lines.append("target_file:")
        lines.append(f"  path: {target_file.get('path', '')}")
        lines.append(f"  action: {target_file.get('action', '')}")
        lines.append(f"  purpose: {target_file.get('purpose', '')}")
        lines.append("reference_files:")
        lines.extend(f"- {value}" for value in (item.get("reference_files") or []))
        lines.append("depends_on:")
        lines.extend(f"- {value}" for value in (item.get("depends_on") or []))
        lines.append(f"risk_level: {item.get('risk_level', '')}")
        lines.append(f"estimated_effort: {item.get('estimated_effort', '')}")
        return "\n".join(lines)[:limit]

    def _refresh_repo_map(self, snapshot_path: Path | None = None) -> bool:
        try:
            repo_map = generate_repo_map(self.target_workspace, self.project_id, self.repo_map_path)
        except Exception as exc:
            self.logger.error("Failed to generate repo_map", str(exc))
            return False
        self._repo_map_cache = repo_map
        if snapshot_path is not None:
            snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            snapshot_path.write_text(json.dumps(repo_map, ensure_ascii=False, indent=2), encoding="utf-8")
        return True

    def _load_repo_map(self) -> dict[str, Any]:
        if self._repo_map_cache is not None:
            return self._repo_map_cache
        if not self.repo_map_path.exists() and not self._refresh_repo_map():
            return {
                "target_workspace": str(self.target_workspace),
                "project_id": self.project_id,
                "top_level_tree": [],
                "directories": [],
                "files": [],
                "entrypoints": [],
                "dependency_files": [],
                "config_files": [],
                "test_files": [],
                "docker_files": [],
                "agent_relevant_files": [],
            }
        try:
            self._repo_map_cache = json.loads(self.repo_map_path.read_text(encoding="utf-8"))
        except Exception:
            self._repo_map_cache = {
                "target_workspace": str(self.target_workspace),
                "project_id": self.project_id,
                "top_level_tree": [],
                "directories": [],
                "files": [],
                "entrypoints": [],
                "dependency_files": [],
                "config_files": [],
                "test_files": [],
                "docker_files": [],
                "agent_relevant_files": [],
            }
        return self._repo_map_cache

    def _save_feedback(self, task_id: int, agent: str, feedback: str) -> Path:
        feedback_root = self._engine_path(self.config.get("paths", {}).get("feedback_dir", ".openclaw/feedback"))
        repo_map = self._load_repo_map()
        repo_map_target_workspace = str(repo_map.get("target_workspace") or "").strip() or str(self.target_workspace)
        run_id = self.logger.run_dir.name
        attempt = self._implementation_attempt if self._implementation_attempt > 0 else max(int(task_id or 0), 1)
        run_feedback_dir = feedback_root / self.project_id / run_id / f"attempt_{attempt}"
        run_feedback_dir.mkdir(parents=True, exist_ok=True)
        latest_dir = feedback_root / self.project_id / "latest"
        latest_dir.mkdir(parents=True, exist_ok=True)
        feedback_file = run_feedback_dir / f"{agent}.md"
        latest_file = latest_dir / f"{agent}.md"
        content = "\n".join(
            [
                "---",
                f"project_id: {self.project_id}",
                f"run_id: {run_id}",
                f"attempt: {attempt}",
                "phase: implementation",
                f"agent: {agent}",
                f"target_workspace: {self.target_workspace}",
                f"repo_map_target_workspace: {repo_map_target_workspace}",
                f"context_mode: {self.context_mode}",
                f"created_at: {datetime.now().isoformat()}",
                "---",
                "",
                f"# Feedback from {agent}",
                "",
                feedback,
                "",
            ]
        )
        feedback_file.write_text(content, encoding="utf-8")
        latest_file.write_text(content, encoding="utf-8")
        self.logger.info(f"Diagnostic run_feedback_dir={run_feedback_dir}")
        self.logger.info(f"Diagnostic feedback_file={feedback_file}")
        self.logger.info(f"Р¤Р°Р№Р» РѕР±СЂР°С‚РЅРѕР№ СЃРІСЏР·Рё СЃРѕС…СЂР°РЅРµРЅ: {feedback_file}")
        return feedback_file

    def _ensure_repo_map_workspace_consistency(self, agent_name: str) -> bool:
        repo_map = self._load_repo_map()
        repo_map_target = str(repo_map.get("target_workspace") or "").strip()
        if not repo_map_target:
            return True
        resolved_repo_map_target = Path(repo_map_target).resolve()
        resolved_target_workspace = self.target_workspace.resolve()
        if resolved_repo_map_target == resolved_target_workspace:
            return True
        self._planner_parse_error = "repo_map_target_workspace_mismatch"
        self._planner_schema_errors = [
            f"repo_map_target_workspace={resolved_repo_map_target}",
            f"runtime_target_workspace={resolved_target_workspace}",
            f"repo_map_path={self.repo_map_path}",
        ]
        details = (
            "Repo map target workspace mismatch\n"
            f"repo_map_target_workspace={resolved_repo_map_target}\n"
            f"target_workspace={resolved_target_workspace}\n"
            f"repo_map_path={self.repo_map_path}"
        )
        self.logger.error("Repo map target workspace mismatch", details)
        self._phase_failure_status = "repo_map_target_workspace_mismatch"
        return False

    def _capture_repo_map_after_developer(self) -> None:
        if not self._refresh_repo_map(snapshot_path=self.repo_map_after_path):
            return
        self._repo_map_after = self._load_repo_map()
        before = self._repo_map_before or self._repo_map_after or {}
        self._repo_map_delta = compare_repo_maps(before, self._repo_map_after)
        self._set_agent_report_extras("implementation", "developer", self._repo_map_delta)
        developer_report = self._load_saved_agent_report("implementation", "developer")
        if developer_report:
            self._overwrite_agent_report("implementation", "developer", {**developer_report, **self._repo_map_delta})

    def _build_repo_map_summary(self, *, repo_map: dict[str, Any], agent_name: str, limit: int = 2600) -> str:
        files = repo_map.get("files") or []
        directories = [str(path) for path in (repo_map.get("directories") or []) if str(path).strip()]
        relevant_files = [str(path) for path in (repo_map.get("agent_relevant_files") or []) if str(path).strip()]
        if agent_name in {"task-designer", "developer", "qa", "template-validator"} and self._selected_implementation_item:
            relevant_files = [str(path) for path in (self._selected_implementation_item.get("allowed_paths") or []) if str(path).strip()]
        lines = [
            f"repo_map_path: {self.repo_map_path}",
            f"target_workspace: {repo_map.get('target_workspace', self.target_workspace)}",
            f"file_count: {len(files)}",
            f"directory_count: {len(directories)}",
            "top_level_tree:",
        ]
        lines.extend(f"- {entry}" for entry in (repo_map.get("top_level_tree") or [])[:20])
        lines.append("entrypoints:")
        lines.extend(f"- {path}" for path in (repo_map.get("entrypoints") or [])[:12])
        lines.append("relevant_directories:")
        lines.extend(f"- {path}" for path in directories[:24])
        lines.append("relevant_files:")
        lines.extend(f"- {path}" for path in relevant_files[:30])
        return "\n".join(lines)[:limit]

    def _build_repo_map_delta_summary(self, limit: int = 2200) -> str:
        before = self._repo_map_before
        after = self._repo_map_after
        if before is None and self.repo_map_before_path.exists():
            try:
                before = json.loads(self.repo_map_before_path.read_text(encoding="utf-8"))
            except Exception:
                before = None
        if after is None and self.repo_map_after_path.exists():
            try:
                after = json.loads(self.repo_map_after_path.read_text(encoding="utf-8"))
            except Exception:
                after = None
        delta = self._repo_map_delta
        if before is not None and after is not None and not any(delta.values()):
            delta = compare_repo_maps(before, after)
        lines = [
            f"repo_map_before: {self.repo_map_before_path}",
            f"repo_map_after: {self.repo_map_after_path}",
            "new_files_created:",
        ]
        lines.extend(f"- {path}" for path in delta.get("new_files_created", [])[:20])
        lines.append("files_modified:")
        lines.extend(f"- {path}" for path in delta.get("files_modified", [])[:20])
        lines.append("removed_files:")
        lines.extend(f"- {path}" for path in delta.get("removed_files", [])[:20])
        return "\n".join(lines)[:limit]

    def _completed_implementation_task_ids(self) -> list[str]:
        completed = self.project_settings.get("completed_implementation_tasks", [])
        if not isinstance(completed, list):
            return []
        return [str(item).strip() for item in completed if str(item).strip()]

    def _mark_implementation_task_completed(self) -> None:
        if not self._selected_implementation_item:
            return
        task_id = str(self._selected_implementation_item.get("id") or "").strip()
        if not task_id:
            return
        completed = self._completed_implementation_task_ids()
        if task_id not in completed:
            completed.append(task_id)
            self.project_settings["completed_implementation_tasks"] = completed
            self._save_project_settings()

    def _prompt_post_implementation_action(self) -> str:
        if self.config["workflow"]["mode"] == "auto":
            return "stop"
        backlog = self._implementation_backlog_cache or []
        completed = set(self._completed_implementation_task_ids())
        has_next = any(str(item.get("id")) not in completed for item in backlog)
        while True:
            answer = input("Next action [1 next item / 2 deployment / 3 stop]: ").strip().lower()
            if answer in {"1", "next"} and has_next:
                return "next"
            if answer in {"2", "deploy", "deployment"}:
                return "deployment"
            if answer in {"3", "stop", ""}:
                return "stop"
            print("Invalid choice.")

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

    def _find_agent_config(self, phase_key: str, agent_name: str) -> dict[str, Any]:
        phase = self.config.get("phases", {}).get(phase_key, {})
        for agent in phase.get("agents", []):
            if str(agent.get("name") or "") == agent_name:
                return dict(agent)
        return {"name": agent_name}

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
        settings_payload: dict[str, Any] = {}
        if settings_path.exists():
            settings_payload = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
        settings_payload.update(
            {
                "project_id": self.project_id,
                "git_remote": self.git_remote,
                "target_workspace": str(self.target_workspace),
                "engine_root": str(self.engine_root),
                "completed_implementation_tasks": settings_payload.get("completed_implementation_tasks", []),
            }
        )
        settings_path.write_text(yaml.safe_dump(settings_payload, sort_keys=False), encoding="utf-8")
        return base

    def _load_project_settings(self) -> dict[str, Any]:
        if not self.project_settings_path.exists():
            return {}
        return yaml.safe_load(self.project_settings_path.read_text(encoding="utf-8")) or {}

    def _save_project_settings(self) -> None:
        self.project_settings_path.write_text(yaml.safe_dump(self.project_settings, sort_keys=False), encoding="utf-8")

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
    def _detect_agent_failure(stdout: str, stderr: str, parsed_output: str, require_translation: bool = True) -> str:
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
        if require_translation and parsed_output and "russian translation" not in parsed_output.lower():
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
