from __future__ import annotations

import ast
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import textwrap
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

from tools.repo_map import compare_repo_maps, generate_repo_map, is_excluded_path, validate_agent_paths
from workflow.logger import WorkflowLogger
from workflow.multi_developer_dispatcher import filter_paths_for_agent, route_paths
from workflow.run_explainer import find_latest_implementation_run, generate_human_report
from workflow.runtime import has_provider_credentials, load_runtime_config, required_key_env, resolve_runner_path

PROJECT_CODEX_TEMPLATE = (
    "# Codex Project Context\n\n"
    "Add durable project notes here. This file is auto-loaded on every run across machines.\n"
)
PROJECT_CODEX_AUTO_START = "<!-- AUTO-GENERATED:RUN-CONTEXT START -->"
PROJECT_CODEX_AUTO_END = "<!-- AUTO-GENERATED:RUN-CONTEXT END -->"
PROJECT_RESUME_TEMPLATE = (
    "# Project Resume\n\n"
    "Add temporary handoff notes here if needed. This file is auto-loaded on every run across machines.\n"
)
PROJECT_RESUME_AUTO_START = "<!-- AUTO-GENERATED:RESUME-CONTEXT START -->"
PROJECT_RESUME_AUTO_END = "<!-- AUTO-GENERATED:RESUME-CONTEXT END -->"
DEFAULT_ALEMBIC_DOWN_REVISION = "0005"
TEST_DEVELOPER_FORBIDDEN_IMPORTS = ("sqlalchemy", "alembic", "pytest")
TEST_DEVELOPER_ALLOWED_IMPORTS = ("ast", "re", "pathlib", "importlib.util")


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
        user_goal: str | None = None,
        selected_task_ref: str | None = None,
        next_task: bool = False,
        research_run: str | None = None,
        allow_scope_expansion: bool = False,
        retry_agent: str | None = None,
        from_agent: str | None = None,
        reuse_architect: bool = False,
        rerun_completed: bool = False,
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
        self.project_local_settings_path = self.project_state_dir / "local.yaml"
        self.project_codex_context_path = self.project_state_dir / "codex.md"
        self.project_resume_context_path = self.project_state_dir / "resume.md"
        self.project_implementation_state_dir = self.project_state_dir / "state"
        self.canonical_backlog_path = self.project_implementation_state_dir / "implementation_backlog.json"
        self.completed_tasks_path = self.project_implementation_state_dir / "completed_implementation_tasks.json"
        self.project_settings = self._load_project_settings()
        self.project_local_settings = self._load_project_local_settings()
        self.project_codex_context = self._load_project_codex_context()
        self.project_resume_context = self._load_project_resume_context()
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
        self.user_goal_override = str(user_goal or "").strip()
        self.selected_task_ref = str(selected_task_ref or "").strip()
        self._selected_task_from_explicit_cli = bool(self.selected_task_ref)
        self._selected_task_from_resume = False
        self.next_task_requested = next_task
        self.research_run_id = str(research_run or "").strip()
        self.allow_scope_expansion = allow_scope_expansion
        self.retry_agent_name = str(retry_agent or "").strip()
        self.from_agent_name = str(from_agent or "").strip()
        self.reuse_architect = reuse_architect
        self.rerun_completed = rerun_completed
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
        self._canonical_backlog_loaded = False
        self._canonical_backlog_payload: dict[str, Any] = {}
        self._selected_task_source = ""
        self._backlog_selected_task_id = ""
        self._skipped_completed_task_ids: list[str] = []
        self._dependency_forced_task_id = ""
        self._completed_task_recorded = False
        self._completed_task_record_error = ""
        self._human_report_path = self.logger.run_dir / "human_report.md"
        self._human_report_updated = False
        self._human_report_error = ""
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
        self._task_designer_feedback_for_prompt = False
        self._developer_feedback_file = ""
        self._developer_feedback_source = ""
        self._developer_feedback_chars = 0
        self._implementation_retry_from_agent = ""
        self._implementation_attempt = 0
        self._repo_map_cache: dict[str, Any] | None = None
        self._db_architecture_cache: dict[str, Any] | None = None
        self._project_roots_cache: list[str] | None = None
        self._architecture_profile_cache: dict[str, Any] | None = None
        self._repo_map_before: dict[str, Any] | None = None
        self._repo_map_after: dict[str, Any] | None = None
        self._repo_map_delta: dict[str, list[str]] = {
            "new_files_created": [],
            "files_modified": [],
            "removed_files": [],
        }
        self.saved_user_goal = str(self.project_settings.get("user_goal") or "").strip()
        self.user_goal = ""
        if self.user_goal_override:
            self._set_user_goal(self.user_goal_override)
        self._log_startup_diagnostics()

    def run_full_cycle(self) -> bool:
        self.logger.info("Запуск полного цикла agents-pipeline")
        try:
            if not self._ensure_user_goal("research"):
                return False
            for phase_key in self._get_phase_order():
                if not self._preflight_runtime(phase_key):
                    return False
                if not self.run_phase(phase_key):
                    return False
            return True
        finally:
            summary = self.logger.save_summary()
            self._persist_project_codex_context()
            self._persist_project_resume_context()
            self.logger.info(f"Сводка сохранена: {summary}")

    def run_research_phase(self) -> bool:
        if not self._ensure_user_goal("research"):
            return False
        if not self._preflight_runtime("research"):
            return False
        ok = self.run_phase("research")
        self._persist_project_codex_context()
        self._persist_project_resume_context()
        return ok

    def run_implementation_phase(self) -> bool:
        if not self._ensure_user_goal("implementation"):
            return False
        if not self._preflight_runtime("implementation"):
            return False
        ok = self.run_phase("implementation")
        self._update_human_report(final_status="success" if ok else (self._phase_failure_status or "failed"))
        self._persist_project_codex_context()
        self._persist_project_resume_context()
        return ok

    def run_deployment_phase(self) -> bool:
        if not self._preflight_runtime("deployment"):
            return False
        ok = self.run_phase("deployment")
        self._persist_project_codex_context()
        self._persist_project_resume_context()
        return ok

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
        self._selected_task_source = ""
        self._skipped_completed_task_ids = []
        self._dependency_forced_task_id = ""
        self._completed_task_recorded = False
        self._completed_task_record_error = ""
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
        self._task_designer_feedback_for_prompt = False
        self._developer_feedback_file = ""
        self._developer_feedback_source = ""
        self._developer_feedback_chars = 0
        self._implementation_retry_from_agent = ""
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
            self._phase_failure_status = None
            cached_selected_task_id = str((self._selected_implementation_item or {}).get("id") or "").strip()
            if (
                attempt > 1
                and cached_selected_task_id
                and cached_selected_task_id in set(self._completed_implementation_task_ids())
                and cached_selected_task_id != self._dependency_forced_task_id
                and not self._selected_task_from_explicit_cli
                and not self.rerun_completed
            ):
                self._selected_implementation_item = None
                self._selected_task_source = ""
                self._skipped_completed_task_ids = []
            self.logger.info(f"Попытка реализации {attempt}/{max_retries}")
            ok = self._run_phase_agents(phase, "implementation")
            if not ok and self._phase_failure_status in {"scope_violation", "no_changes", "planner_invalid", "task_designer_invalid", "strict_retrieval_blocked", "task_dependencies_incomplete", "dependency_missing_from_backlog"}:
                if self.config["git"]["enabled"] and self.config["git"]["auto_rollback"]:
                    if not self._rollback_git(self._phase_failure_status.replace("_", " ")):
                        self._phase_failure_status = "rollback_failed"
                        self.logger.error("Rollback failed; stopping to avoid retrying on dirty worktree.")
                        self.logger.save_phase_summary("implementation", phase["name"])
                        self.logger.phase_end(phase["name"], self._phase_failure_status)
                        return False
                self.logger.save_phase_summary("implementation", phase["name"])
                self.logger.phase_end(phase["name"], self._phase_failure_status)
                return False
            if ok:
                if self._implementation_resume_stops_before_delivery():
                    self.logger.save_phase_summary("implementation", phase["name"])
                    self.logger.phase_end(phase["name"], "planner_ready")
                    return True
                completion_changed_files = self._implementation_completion_changed_files()
                if self.config["git"]["enabled"] and not self._merge_git():
                    self.logger.save_phase_summary("implementation", phase["name"])
                    self.logger.phase_end(phase["name"], "failed")
                    return False
                if not self._mark_implementation_task_completed(changed_files=completion_changed_files):
                    self.logger.error("Completed task registry was not updated", self._completed_task_record_error)
                    self.logger.save_phase_summary("implementation", phase["name"])
                    self.logger.phase_end(phase["name"], "completed_task_record_failed")
                    return False
                self.logger.info(f"Diagnostic completed_task_recorded={self._completed_task_recorded}")
                self.logger.info(f"Diagnostic completed_task_record_error={self._completed_task_record_error}")
                self.logger.save_phase_summary("implementation", phase["name"])
                self.logger.phase_end(phase["name"], "success")
                next_action = self._prompt_post_implementation_action()
                if next_action == "next":
                    self.next_task_requested = True
                    self.selected_task_ref = ""
                    self._selected_task_from_explicit_cli = False
                    self._selected_task_from_resume = False
                    self._selected_implementation_item = None
                    self._selected_task_source = ""
                    return self._run_implementation_phase()
                if next_action == "deployment":
                    return self.run_deployment_phase()
                return True

            self._save_feedback(task_id, "qa", f"Попытка {attempt} завершилась ошибкой. Проверь логи и исправь регрессии.")
            if self._phase_failure_status in {"qa_failed", "developer_checks_failed", "invalid_output"}:
                self._capture_developer_retry_feedback(task_id)
                self._implementation_retry_from_agent = "developer"
            if self.config["git"]["enabled"] and self.config["git"]["auto_rollback"]:
                if not self._rollback_git(f"attempt {attempt} failed"):
                    self._phase_failure_status = "rollback_failed"
                    self.logger.error("Rollback failed; stopping to avoid retrying on dirty worktree.")
                    self.logger.save_phase_summary("implementation", phase["name"])
                    self.logger.phase_end(phase["name"], self._phase_failure_status)
                    return False
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
                dependency_result = self._handle_selected_task_dependencies()
                if not dependency_result["ok"]:
                    self._phase_failure_status = dependency_result["status"]
                    had_failures = True
                    return False
            if phase_key == "implementation" and agent["name"] == "developer":
                selection = self._prepare_implementation_backlog_selection(require_backlog=True)
                if selection["error"]:
                    self.logger.error(selection["error"])
                    had_failures = True
                    return False
                dependency_result = self._handle_selected_task_dependencies()
                if not dependency_result["ok"]:
                    self._phase_failure_status = dependency_result["status"]
                    had_failures = True
                    return False
                task_designer_required = any(str(candidate.get("name") or "") == "task-designer" for candidate in phase.get("agents", []))
                if task_designer_required and str((self._selected_implementation_item or {}).get("contract_source") or "") != "task-designer":
                    if self.from_agent_name == "developer":
                        if not self._run_task_designer_before_developer(phase, total=total):
                            self.logger.error("Task designer output is missing. Run task-designer before developer.")
                            self._phase_failure_status = "task_designer_invalid"
                            had_failures = True
                            return False
                    else:
                        self.logger.error("Task designer output is missing. Run task-designer before developer.")
                        self._phase_failure_status = "task_designer_invalid"
                        had_failures = True
                        return False
                if not self._enforce_implementation_scope_plan():
                    had_failures = True
                    return False
            if phase_key == "implementation" and agent["name"] == "developer" and self._get_implementation_execution_mode() == "multi_developer_json":
                if not self._run_multi_developer_json_flow(phase, index=index, total=total):
                    had_failures = True
                    if fail_fast:
                        return False
            else:
                if not self._run_agent(agent, phase_key, index=index, total=total):
                    had_failures = True
                    if fail_fast:
                        return False
            if phase_key == "implementation" and agent["name"] == "implementation-planner":
                if not self._validate_or_repair_implementation_planner(agent, phase_key, index=index, total=total):
                    had_failures = True
                    return False
            if phase_key == "implementation" and agent["name"] == "task-designer":
                if not self._apply_task_designer_contract_with_retry(agent, index=index, total=total):
                    had_failures = True
                    return False
            if phase_key == "implementation" and agent["name"] == "developer":
                self._capture_repo_map_after_developer()
                if self._get_implementation_execution_mode() == "multi_developer_json" and self._phase_failure_status in {"failed", "invalid_output"}:
                    had_failures = True
                    return False
                if not self._enforce_implementation_scope_diff():
                    had_failures = True
                    return False
                if not self._run_developer_deterministic_checks():
                    had_failures = True
                    return False
            if self._phase_cost_limit_exceeded(phase_key):
                had_failures = True
                break
        return not had_failures

    def _get_implementation_execution_mode(self) -> str:
        implementation = self.config.get("phases", {}).get("implementation", {})
        return str(implementation.get("execution_mode") or "single_developer").strip().lower()

    def _get_multi_developer_agent_configs(self) -> dict[str, dict[str, Any]]:
        implementation = self.config.get("phases", {}).get("implementation", {})
        configs: dict[str, dict[str, Any]] = {}
        for item in implementation.get("multi_developer_agents", []) or []:
            name = str(item.get("name") or "").strip()
            if name:
                configs[name] = dict(item)
        return configs

    def _build_multi_developer_editable_paths(self) -> list[str]:
        item = self._selected_implementation_item or {}
        paths: list[str] = []
        for key in ("new_files", "required_test_paths"):
            for path in item.get(key, []) or []:
                normalized = self._normalize_repo_relative_path(path)
                if normalized:
                    paths.append(normalized)
        target_file = item.get("target_file") or {}
        test_file = item.get("test_file") or {}
        for candidate in (target_file, test_file):
            if isinstance(candidate, dict):
                normalized = self._normalize_repo_relative_path(candidate.get("path"))
                if normalized:
                    paths.append(normalized)
        return sorted(dict.fromkeys(paths))

    def _scaffold_package_markers(self, paths: list[str]) -> list[str]:
        """Create any empty `__init__.py` package markers declared in the task scope.

        These are trivial empty files; an LLM developer that may write only one file per
        turn otherwise leaves the package marker uncreated, failing QA's contract check.
        Creating them deterministically keeps the package importable and within scope.
        """
        created: list[str] = []
        workspace_root = self.target_workspace.resolve()
        for raw in paths:
            normalized = self._normalize_repo_relative_path(raw)
            if not normalized or Path(normalized).name != "__init__.py":
                continue
            candidate = (self.target_workspace / normalized).resolve()
            try:
                candidate.relative_to(workspace_root)
            except ValueError:
                continue
            if candidate.exists():
                continue
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text("", encoding="utf-8")
            created.append(normalized)
        return created

    def _build_multi_developer_task_override(self, agent_name: str, editable_paths: list[str]) -> dict[str, Any]:
        item = dict(self._selected_implementation_item or {})
        allowed_paths = filter_paths_for_agent(editable_paths, agent_name)
        allowed_path_set = set(allowed_paths)
        item["allowed_paths"] = list(allowed_paths)
        for key in ("new_files", "existing_paths", "required_test_paths"):
            item[key] = [
                path
                for path in (item.get(key) or [])
                if self._normalize_repo_relative_path(path) in allowed_path_set
            ]
        raw_reasons = item.get("reason_each_path_is_needed") or {}
        if isinstance(raw_reasons, dict):
            item["reason_each_path_is_needed"] = {
                path: reason
                for path, reason in raw_reasons.items()
                if self._normalize_repo_relative_path(path) in allowed_path_set
            }
        target_file_in_scope = False
        test_file_in_scope = False
        for key in ("target_file", "test_file"):
            candidate = item.get(key)
            if isinstance(candidate, dict):
                normalized = self._normalize_repo_relative_path(candidate.get("path"))
                keep_as_read_only_test_subject = key == "target_file" and agent_name == "test-developer" and bool(normalized)
                if normalized in allowed_path_set or keep_as_read_only_test_subject:
                    item[key] = dict(candidate)
                    if key == "target_file" and normalized in allowed_path_set:
                        target_file_in_scope = True
                    if key == "test_file" and normalized in allowed_path_set:
                        test_file_in_scope = True
                else:
                    item[key] = {}
        if not target_file_in_scope and agent_name != "test-developer":
            item["must_contain"] = []
            item["must_import"] = []
        if not test_file_in_scope and agent_name != "test-developer":
            item["must_test"] = []
        return item

    def _save_multi_developer_synthetic_report(self, applied_agents: list[str], changed_paths: list[str], warnings: list[str]) -> None:
        parsed_lines = [
            "status=implemented" if changed_paths else "status=no_changes: no-op, already valid",
            "execution_mode=multi_developer_json",
            "applied_agents=" + (", ".join(applied_agents) if applied_agents else "none"),
            "changed_paths=" + (", ".join(changed_paths) if changed_paths else "none"),
        ]
        if warnings:
            parsed_lines.append("warnings=" + " | ".join(warnings))
        payload = {
            "phase": "implementation",
            "agent": "developer",
            "agent_name": "developer",
            "status": "success" if changed_paths else "no_changes",
            "result": "completed" if changed_paths else "no-op, already valid",
            "elapsed_s": 0.0,
            "returncode": 0,
            "runtime": {"provider": "internal", "model": "multi_developer_json", "thinking": "n/a"},
            "command": "multi_developer_json",
            "message": "Synthetic developer report generated from multi_developer_json flow.",
            "parsed_output": "\n".join(parsed_lines),
            "developer_changed_files": changed_paths,
            "developer_diff_lines": 0,
            "write_tools_used": ["multi_developer_json"],
            "no_changes_detected": not bool(changed_paths),
        }
        self.logger.save_agent_report("implementation", "developer", payload)
        payload_status = str(payload.get("status") or "")
        self._update_human_report(final_status=payload_status if payload_status != "success" else "in_progress", agent_name="developer")

    def _multi_developer_current_changed_files(self, allowed_paths: list[str]) -> list[str]:
        diagnostics = self._collect_scope_watchdog_diff_diagnostics()
        allowed = set(allowed_paths)
        return sorted(
            path
            for path in (diagnostics.get("changed_files") or [])
            if str(path) in allowed
        )

    def _build_multi_developer_write_detection(
        self,
        allowed_paths: list[str],
        detected_git_diff_files: list[str],
        written_paths: list[str],
        write_tools_used: list[str],
    ) -> dict[str, Any]:
        normalized_allowed = sorted(
            {
                self._normalize_target_relative_path(path)
                for path in allowed_paths
                if self._normalize_target_relative_path(path)
            }
        )
        normalized_diff = sorted(
            {
                self._normalize_target_relative_path(path)
                for path in detected_git_diff_files
                if self._normalize_target_relative_path(path)
            }
        )
        normalized_written = sorted(
            {
                self._normalize_target_relative_path(path)
                for path in written_paths
                if self._normalize_target_relative_path(path)
            }
        )
        allowed = set(normalized_allowed)
        scoped_diff = sorted(path for path in normalized_diff if path in allowed)
        scoped_written = sorted(path for path in normalized_written if path in allowed)
        actual_changed = sorted(dict.fromkeys([*scoped_diff, *scoped_written]))
        write_operation_detected = bool(write_tools_used or scoped_written)
        if scoped_written:
            stage = "write_tool"
        elif scoped_diff:
            stage = "git_diff"
        else:
            stage = "none"
        return {
            "actual_changed_files": actual_changed,
            "detected_git_diff_files": normalized_diff,
            "normalized_allowed_paths": normalized_allowed,
            "normalized_written_paths": normalized_written,
            "scoped_path_match_result": bool(actual_changed),
            "diff_detection_stage": stage,
            "write_operation_detected": write_operation_detected,
            "write_tools_used": list(write_tools_used),
        }

    def _recover_multi_developer_write_metadata(self, agent_report: dict[str, Any]) -> tuple[list[str], list[str]]:
        sources = [
            str(agent_report.get("parsed_output") or ""),
            str(agent_report.get("stdout") or ""),
            str(agent_report.get("result") or ""),
        ]
        recovered_tools: list[str] = []
        recovered_paths: list[str] = []
        for source in sources:
            if not source.strip():
                continue
            for payload in self._extract_write_operations(source):
                tool = str(payload.get("tool") or "").strip()
                raw_path = str(payload.get("path") or "")
                sanitized_path = (
                    raw_path.replace("\t", "/t")
                    .replace("\r", "/r")
                    .replace("\n", "/n")
                    .replace("\f", "/f")
                    .replace("\v", "/v")
                )
                path = self._normalize_repo_relative_path(
                    self._normalize_target_relative_path(sanitized_path)
                )
                if tool:
                    recovered_tools.append(tool)
                if path:
                    recovered_paths.append(path)
        return (
            sorted(dict.fromkeys(recovered_tools)),
            sorted(dict.fromkeys(recovered_paths)),
        )

    def _validate_multi_developer_no_changes(
        self,
        agent_name: str,
        scoped_item: dict[str, Any],
        allowed_paths: list[str],
        parsed_output: str,
    ) -> tuple[bool, str]:
        if "status=no_changes" not in str(parsed_output or "").lower():
            return False, "agent did not write and did not return status=no_changes"

        findings: list[str] = []
        existing_paths: list[str] = []
        for path in allowed_paths:
            normalized = self._normalize_repo_relative_path(path)
            candidate = self.target_workspace / normalized
            if not normalized or not candidate.exists() or not candidate.is_file():
                findings.append(f"{normalized or path}: scoped file is missing")
            else:
                existing_paths.append(normalized)

        py_paths = [path for path in existing_paths if path.endswith(".py")]
        if py_paths:
            returncode, stdout, stderr = self._run_local_command(
                [sys.executable, "-m", "py_compile", *py_paths],
                timeout=30,
                cwd=self.target_workspace,
            )
            if returncode != 0:
                findings.append("py_compile failed for scoped files")
                findings.append(stderr or stdout or "unknown py_compile failure")

        if agent_name == "infra-developer":
            findings.extend(self._validate_changed_migration_files(py_paths))

        target_file = scoped_item.get("target_file") or {}
        target_path = self._normalize_repo_relative_path(target_file.get("path")) if isinstance(target_file, dict) else ""
        if agent_name == "code-developer" and allowed_paths and not target_path:
            findings.append(
                "code-developer has editable application paths but no scoped target_file contract; "
                "status=no_changes would mask missing code work"
            )
        if target_path:
            target_candidate = self.target_workspace / target_path
            try:
                target_text = target_candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                target_text = ""
            for value in [str(item).strip() for item in (scoped_item.get("must_contain") or []) if str(item).strip()]:
                if not self._contract_requirement_present(target_text, value):
                    findings.append(f"{target_path}: missing must_contain: {value}")

        forbidden = [str(item).strip() for item in (scoped_item.get("forbidden") or []) if str(item).strip()]
        if forbidden:
            for path in existing_paths:
                try:
                    text = (self.target_workspace / path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                for value in forbidden:
                    if value in text:
                        findings.append(f"{path}: forbidden content present: {value}")

        if findings:
            return False, "; ".join(findings)
        return True, "status=no_changes accepted after scoped deterministic validation"

    def _validate_multi_developer_changed_scope(
        self,
        agent_name: str,
        scoped_item: dict[str, Any],
        changed_paths: list[str],
    ) -> tuple[bool, str]:
        findings: list[str] = []
        normalized_paths = sorted(
            {
                self._normalize_repo_relative_path(path)
                for path in changed_paths
                if self._normalize_repo_relative_path(path)
            }
        )
        existing_paths: list[str] = []
        for path in normalized_paths:
            candidate = self.target_workspace / path
            if not candidate.exists() or not candidate.is_file():
                findings.append(f"{path}: scoped changed file is missing")
            else:
                existing_paths.append(path)

        py_paths = [path for path in existing_paths if path.endswith(".py")]
        if py_paths:
            returncode, stdout, stderr = self._run_local_command(
                [sys.executable, "-m", "py_compile", *py_paths],
                timeout=30,
                cwd=self.target_workspace,
            )
            if returncode != 0:
                findings.append("py_compile failed for scoped changed files")
                findings.append(stderr or stdout or "unknown py_compile failure")

        if agent_name == "infra-developer":
            findings.extend(self._validate_changed_migration_files(existing_paths))
        if agent_name == "test-developer":
            findings.extend(self._validate_test_developer_static_constraints(py_paths))

        forbidden = [str(item).strip() for item in (scoped_item.get("forbidden") or []) if str(item).strip()]
        if forbidden:
            for path in existing_paths:
                try:
                    text = (self.target_workspace / path).read_text(encoding="utf-8", errors="replace")
                except OSError:
                    text = ""
                for value in forbidden:
                    if value in text:
                        findings.append(f"{path}: forbidden content present: {value}")

        if findings:
            return False, "; ".join(findings)
        return True, "scoped changed files passed deterministic validation"

    @staticmethod
    def _build_multi_developer_validation_feedback(
        agent_name: str,
        agent_allowed_paths: list[str],
        detection: dict[str, Any],
        detail: str,
    ) -> str:
        return "\n".join(
            [
                "# Multi Developer Validation Feedback",
                "",
                f"- agent: {agent_name}",
                "- result: scoped deterministic validation failed",
                "- editable_paths: " + ", ".join(agent_allowed_paths),
                "",
                "Diagnostics:",
                "- actual_changed_files: " + ", ".join(detection.get("actual_changed_files") or []),
                "- detected_git_diff_files: " + ", ".join(detection.get("detected_git_diff_files") or []),
                "- normalized_allowed_paths: " + ", ".join(detection.get("normalized_allowed_paths") or []),
                "- normalized_written_paths: " + ", ".join(detection.get("normalized_written_paths") or []),
                f"- scoped_path_match_result: {detection.get('scoped_path_match_result')}",
                f"- diff_detection_stage: {detection.get('diff_detection_stage')}",
                f"- write_operation_detected: {detection.get('write_operation_detected')}",
                "",
                "Errors:",
                "- " + detail,
                "",
                "Required next action:",
                "- fix only this agent's scoped files",
                "- do not continue to sibling agents until this scoped validation passes",
            ]
        )

    def _run_multi_developer_json_flow(self, phase: dict[str, Any], *, index: int, total: int) -> bool:
        agent_configs = self._get_multi_developer_agent_configs()
        editable_paths = self._build_multi_developer_editable_paths()
        routed_agents = route_paths(editable_paths)
        if not routed_agents:
            self.logger.error("multi_developer_json could not route any developer agents", "No allowed_paths were classified.")
            self._phase_failure_status = "failed"
            return False
        self._log_operator_summary(
            "План multi-developer",
            [
                f"task={str((self._selected_implementation_item or {}).get('id') or '')}",
                "agents=" + ", ".join(routed_agents),
                "editable_paths=" + ", ".join(editable_paths[:6]) + (f" ... (+{len(editable_paths) - 6})" if len(editable_paths) > 6 else ""),
            ],
        )
        applied_agents: list[str] = []
        scaffolded_markers = self._scaffold_package_markers(editable_paths)
        if scaffolded_markers:
            self.logger.info("Scaffolded package markers: " + ", ".join(scaffolded_markers))
        changed_paths: list[str] = list(scaffolded_markers)
        warnings: list[str] = []
        for routed_name in routed_agents:
            agent_config = agent_configs.get(routed_name)
            if not agent_config:
                self.logger.error(f"multi_developer_json agent config is missing: {routed_name}")
                self._phase_failure_status = "failed"
                return False
            agent_allowed_paths = filter_paths_for_agent(editable_paths, routed_name)
            if not agent_allowed_paths:
                continue
            self.logger.info(f"Multi developer allowed paths ({routed_name}): " + ", ".join(agent_allowed_paths))
            before_changed = set(self._multi_developer_current_changed_files(agent_allowed_paths))
            if not self._wait_for_user(f"Запустить агента {routed_name} ({index}/{total})?"):
                self.logger.warning(f"Агент пропущен: {routed_name}")
                continue
            original_selected_item = self._selected_implementation_item
            scoped_item = self._build_multi_developer_task_override(routed_name, editable_paths)
            self._selected_implementation_item = scoped_item
            try:
                if not self._run_agent(agent_config, "implementation", index=index, total=total):
                    return False
            finally:
                self._selected_implementation_item = original_selected_item
            after_changed = set(self._multi_developer_current_changed_files(agent_allowed_paths))
            changed = sorted(after_changed - before_changed)
            agent_report = self._load_saved_agent_report("implementation", routed_name) or {}
            agent_extras = self._get_agent_report_extras("implementation", routed_name)
            write_tools_used = list(agent_extras.get("write_tools_used") or [])
            write_paths = list(agent_extras.get("write_paths") or [])
            if not write_tools_used or not write_paths:
                recovered_tools, recovered_paths = self._recover_multi_developer_write_metadata(agent_report)
                if not write_tools_used:
                    write_tools_used = recovered_tools
                if not write_paths:
                    write_paths = recovered_paths
                if recovered_tools or recovered_paths:
                    self._set_agent_report_extras(
                        "implementation",
                        routed_name,
                        {
                            "write_tools_used": write_tools_used,
                            "write_paths": write_paths,
                        },
                    )
            detection = self._build_multi_developer_write_detection(
                agent_allowed_paths,
                changed,
                write_paths,
                write_tools_used,
            )
            self._set_agent_report_extras("implementation", routed_name, detection)
            if agent_report:
                self._overwrite_agent_report("implementation", routed_name, {**agent_report, **detection})
            effective_changed = list(detection["actual_changed_files"])
            if not effective_changed and not write_tools_used:
                parsed_output = str(agent_report.get("parsed_output") or agent_report.get("result") or "").strip()
                no_changes_ok, no_changes_detail = self._validate_multi_developer_no_changes(
                    routed_name,
                    scoped_item,
                    agent_allowed_paths,
                    parsed_output,
                )
                if no_changes_ok:
                    warnings.append(f"{routed_name}: {no_changes_detail}")
                    applied_agents.append(routed_name)
                    continue
                feedback = "\n".join(
                    [
                        "# Multi Developer Validation Feedback",
                        "",
                        f"- agent: {routed_name}",
                        "- result: completed without modifying scoped editable paths",
                        "- editable_paths: " + ", ".join(agent_allowed_paths),
                        "",
                        "Diagnostics:",
                        "- actual_changed_files: " + ", ".join(detection["actual_changed_files"]),
                        "- detected_git_diff_files: " + ", ".join(detection["detected_git_diff_files"]),
                        "- normalized_allowed_paths: " + ", ".join(detection["normalized_allowed_paths"]),
                        "- normalized_written_paths: " + ", ".join(detection["normalized_written_paths"]),
                        f"- scoped_path_match_result: {detection['scoped_path_match_result']}",
                        f"- diff_detection_stage: {detection['diff_detection_stage']}",
                        f"- write_operation_detected: {detection['write_operation_detected']}",
                        "",
                        "Errors:",
                        "- agent completed but did not change any file in its allowed scope",
                        "- " + no_changes_detail,
                        "",
                        "Required next action:",
                        "- inspect the scoped file and fix the listed validation errors with write_file or apply_patch",
                        "- do not return status=no_changes until the scoped deterministic validation passes",
                    ]
                )
                self._save_feedback(self.task_counter, routed_name, feedback)
                self.logger.error(f"Constraint validation failed for {routed_name}", "agent completed without modifying scoped editable paths")
                self._phase_failure_status = "invalid_output"
                return False
            if effective_changed:
                changed_ok, changed_detail = self._validate_multi_developer_changed_scope(
                    routed_name,
                    scoped_item,
                    effective_changed,
                )
                if not changed_ok:
                    feedback = self._build_multi_developer_validation_feedback(
                        routed_name,
                        agent_allowed_paths,
                        detection,
                        changed_detail,
                    )
                    self._save_feedback(self.task_counter, routed_name, feedback)
                    self._save_feedback(self.task_counter, "developer", feedback)
                    self.logger.error(f"Constraint validation failed for {routed_name}", changed_detail)
                    self._phase_failure_status = "invalid_output"
                    return False
            applied_agents.append(routed_name)
            changed_paths.extend(effective_changed or agent_allowed_paths)
        self._save_multi_developer_synthetic_report(applied_agents, sorted(dict.fromkeys(changed_paths)), warnings)
        self._phase_failure_status = None
        return True

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
        if phase == "implementation" and agent_name in {"developer", "test-developer"}:
            if self._maybe_skip_already_valid_scope(agent_name, agent_runtime):
                return True
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
        self._apply_execution_policy(agent_name, phase, message_bundle)
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
        self.logger.agent_progress(agent_name, f"Diagnostic canonical_backlog_loaded={message_bundle['canonical_backlog_loaded']}")
        self.logger.agent_progress(agent_name, f"Diagnostic canonical_backlog_path={message_bundle['canonical_backlog_path']}")
        self.logger.agent_progress(agent_name, f"Diagnostic completed_task_registry_path={message_bundle['completed_task_registry_path']}")
        self.logger.agent_progress(agent_name, f"Diagnostic completed_task_count={message_bundle['completed_task_count']}")
        self.logger.agent_progress(agent_name, f"Diagnostic completed_task_ids={message_bundle['completed_task_ids']}")
        self.logger.agent_progress(agent_name, f"Diagnostic developer_feedback_source={message_bundle['developer_feedback_source']}")
        self.logger.agent_progress(agent_name, f"Diagnostic developer_feedback_chars={message_bundle['developer_feedback_chars']}")
        self.logger.agent_progress(agent_name, f"Diagnostic backlog_source={message_bundle['backlog_source']}")
        self.logger.agent_progress(agent_name, f"Diagnostic selected_task_id={message_bundle['selected_task_id']}")
        self.logger.agent_progress(agent_name, f"Diagnostic selected_task_source={message_bundle['selected_task_source']}")
        self.logger.agent_progress(agent_name, f"Diagnostic selected_task_from_explicit_cli={message_bundle['selected_task_from_explicit_cli']}")
        self.logger.agent_progress(agent_name, f"Diagnostic backlog_selected_task_id={message_bundle['backlog_selected_task_id']}")
        self.logger.agent_progress(agent_name, f"Diagnostic skipped_completed_task_ids={message_bundle['skipped_completed_task_ids']}")
        self.logger.agent_progress(agent_name, f"Diagnostic completed_task_recorded={message_bundle['completed_task_recorded']}")
        self.logger.agent_progress(agent_name, f"Diagnostic completed_task_record_error={message_bundle['completed_task_record_error']}")
        self.logger.agent_progress(agent_name, f"Diagnostic research_handoff_sources={', '.join(message_bundle['research_handoff_sources'])}")
        self.logger.agent_progress(agent_name, f"Diagnostic selected_task_scope={message_bundle['selected_task_scope']}")
        self.logger.agent_progress(agent_name, f"Diagnostic selected_task_allowed_paths={message_bundle['selected_task_allowed_paths']}")
        self.logger.agent_progress(agent_name, f"Diagnostic implementation_retrieval_enabled={message_bundle['implementation_retrieval_enabled']}")
        self.logger.agent_progress(agent_name, f"Diagnostic contract_completeness={message_bundle['contract_completeness']}")
        self.logger.agent_progress(agent_name, f"Diagnostic contract_compliance={message_bundle['contract_compliance']}")
        self.logger.agent_progress(agent_name, f"Diagnostic missing_must_contain={message_bundle['missing_must_contain']}")
        self.logger.agent_progress(agent_name, f"Diagnostic missing_test_file={message_bundle['missing_test_file']}")
        self.logger.agent_progress(agent_name, f"Diagnostic strict_execution_mode={message_bundle['strict_execution_mode']}")
        self.logger.agent_progress(agent_name, f"Diagnostic retrieval_budget={message_bundle['retrieval_budget']}")
        self.logger.agent_progress(agent_name, f"Diagnostic retrieval_budget_remaining={message_bundle['retrieval_budget_remaining']}")
        self.logger.agent_progress(agent_name, f"Diagnostic retrieval_limit_reason={message_bundle['retrieval_limit_reason']}")
        self.logger.agent_progress(agent_name, f"Diagnostic retrieval_hard_stop_triggered={message_bundle['retrieval_hard_stop_triggered']}")
        self._log_operator_summary("Prompt brief (RU)", self._build_prompt_brief_lines(agent_name, phase, message_bundle))

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
                    "system_message": message_bundle["system_message"],
                    "user_message": message_bundle["user_message"],
                    "developer_feedback_text": message_bundle.get("developer_feedback_text", ""),
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
                    "canonical_backlog_loaded": message_bundle["canonical_backlog_loaded"],
                    "canonical_backlog_path": message_bundle["canonical_backlog_path"],
                    "completed_task_registry_path": message_bundle["completed_task_registry_path"],
                    "completed_task_count": message_bundle["completed_task_count"],
                    "completed_task_ids": message_bundle["completed_task_ids"],
                    "selected_task_source": message_bundle["selected_task_source"],
                    "selected_task_from_explicit_cli": message_bundle["selected_task_from_explicit_cli"],
                    "backlog_selected_task_id": message_bundle["backlog_selected_task_id"],
                    "skipped_completed_task_ids": message_bundle["skipped_completed_task_ids"],
                    "completed_task_recorded": message_bundle["completed_task_recorded"],
                    "completed_task_record_error": message_bundle["completed_task_record_error"],
                    "developer_feedback_source": message_bundle["developer_feedback_source"],
                    "developer_feedback_chars": message_bundle["developer_feedback_chars"],
                    "research_handoff_sources": message_bundle["research_handoff_sources"],
                    "selected_task_scope": message_bundle["selected_task_scope"],
                    "selected_task_allowed_paths": message_bundle["selected_task_allowed_paths"],
                    "implementation_retrieval_enabled": message_bundle["implementation_retrieval_enabled"],
                    "execution_mode": message_bundle["execution_mode"],
                    "strict_execution_mode": message_bundle["strict_execution_mode"],
                    "retrieval_budget": message_bundle["retrieval_budget"],
                    "retrieval_budget_remaining": message_bundle["retrieval_budget_remaining"],
                    "retrieval_limit_reason": message_bundle["retrieval_limit_reason"],
                    "retrieval_hard_stop_triggered": message_bundle["retrieval_hard_stop_triggered"],
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
            if phase == "implementation":
                self._update_human_report(final_status=status if status != "success" else "in_progress", agent_name=agent_name)
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

        failure_reason = self._detect_agent_failure(stdout, stderr, parsed_output, require_translation=False)
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
        contract_failure = self._detect_agent_output_contract_failure(phase, agent_name, parsed_output)
        if contract_failure:
            self.logger.error(
                f"РђРіРµРЅС‚ РІРµСЂРЅСѓР» РЅРµРєРѕСЂСЂРµРєС‚РЅС‹Р№ СЂРµР·СѓР»СЊС‚Р°С‚: {agent_name}",
                f"elapsed_s={elapsed:.2f} | detected_failure={contract_failure} | stdout_tail={self._tail_text(stdout)}",
            )
            save_agent_report("invalid_output", contract_failure, elapsed, stdout, stderr, parsed_output, " ".join(cmd), process.returncode)
            self.logger.agent_end(agent_name, "invalid_output", contract_failure)
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
        prompt = (
            str(prompt)
            .replace("Р—Р°РїСѓСЃС‚РёС‚СЊ", "Запустить")
            .replace("Р°РіРµРЅС‚Р°", "агента")
            .replace("РђРіРµРЅС‚", "Агент")
        )
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
            default_branch = self._default_branch()
            dirty_worktree = self.repo.is_dirty(untracked_files=True)
            dirty_files = self._collect_dirty_worktree_files()
            self.logger.info(f"rollback_dirty_worktree={dirty_worktree}")
            self.logger.info(f"rollback_dirty_files={', '.join(dirty_files)}")
            strategy = self._get_git_rollback_dirty_strategy()
            if dirty_worktree:
                if strategy != "stash":
                    error_message = (
                        f"Dirty worktree prevents checkout to {default_branch}; "
                        f"rollback_dirty_strategy={strategy}"
                    )
                    self.logger.info("rollback_action=failed")
                    self.logger.info(f"rollback_error={error_message}")
                    return False
                stash_message = (
                    f"agents-pipeline rollback safety stash {self.logger.run_id} "
                    f"attempt {self._implementation_attempt}"
                )
                stash_result = self.repo.git.stash("push", "--include-untracked", "-m", stash_message)
                self.logger.info("rollback_action=stash")
                self.logger.info(f"rollback_stash_result={stash_result}")
                if self.repo.is_dirty(untracked_files=True):
                    error_message = "Worktree remained dirty after stash."
                    self.logger.info(f"rollback_error={error_message}")
                    return False
            self.repo.git.checkout(default_branch)
            self.logger.info("rollback_action=checkout")
            if self.current_branch and self.current_branch != default_branch:
                self.repo.delete_head(self.current_branch, force=True)
            self.current_branch = None
            return True
        except Exception as exc:  # pragma: no cover
            self.logger.info("rollback_action=failed")
            self.logger.info(f"rollback_error={exc}")
            self.logger.error("Не удалось откатить ветку", str(exc))
            return False

    def _get_git_rollback_dirty_strategy(self) -> str:
        strategy = str(self.config.get("git", {}).get("rollback_dirty_strategy", "fail")).strip().lower()
        if strategy not in {"fail", "stash"}:
            return "fail"
        return strategy

    def _collect_dirty_worktree_files(self) -> list[str]:
        if self.repo is None:
            return []

        dirty_paths: set[str] = set()
        try:
            dirty_paths.update(
                path.replace("\\", "/")
                for path in self.repo.untracked_files
                if str(path).strip()
            )
        except Exception:
            pass
        try:
            dirty_paths.update(
                (diff.a_path or diff.b_path or "").replace("\\", "/")
                for diff in self.repo.index.diff(None)
                if (diff.a_path or diff.b_path)
            )
        except Exception:
            pass
        try:
            dirty_paths.update(
                (diff.a_path or diff.b_path or "").replace("\\", "/")
                for diff in self.repo.index.diff("HEAD")
                if (diff.a_path or diff.b_path)
            )
        except Exception:
            pass
        return sorted(path for path in dirty_paths if path)

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
        translation_instruction = "Формат ответа обязателен. Пиши ответ полностью на русском языке."
        if phase == "implementation" and agent_name == "developer":
            translation_instruction = (
                "Формат ответа обязателен. Не пиши повествовательный текст, объяснения, планы или переводы. "
                "Во время edit loop отвечай только одним JSON tool request без окружающего текста. "
                "Если выполнено хотя бы одно реальное изменение файла, финальный не-JSON ответ должен быть ровно "
                "`status=implemented`. Если безопасно изменить ничего нельзя, финальный не-JSON ответ должен быть "
                "`status=no_changes: <reason>`."
            )
        elif phase == "implementation" and agent_name == "implementation-planner":
            translation_instruction = (
                "Формат ответа обязателен. Верни только структурированный YAML или JSON для backlog outline. "
                "Не добавляй переводы, объяснения, markdown fences или комментарии."
            )
        elif phase == "implementation" and agent_name == "task-designer":
            translation_instruction = (
                "Формат ответа обязателен. Верни только структурированный YAML или JSON для контракта выбранной задачи. "
                "Не добавляй переводы, объяснения, markdown fences или комментарии."
            )
        if phase == "implementation" and agent_name == "qa":
            translation_instruction = (
                "Формат ответа обязателен. Пиши ответ полностью на русском языке. "
                "Финальный ответ должен быть завершённым QA-отчётом, а не описанием процесса проверки. "
                "Используй точные заголовки: `Вердикт QA:`, `Проверенные файлы:`, `Соответствие контракту:`, `Замечания:`, `Итог:`. "
                "Не заканчивай ответ фразами вроде `проверяю`, `читаю`, `нужно убедиться`."
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
        developer_feedback_text = ""
        developer_feedback_source = ""
        developer_feedback_chars = 0
        canonical_backlog_loaded = False
        canonical_backlog_path = ""
        completed_task_registry_path = ""
        completed_task_count = 0
        completed_task_ids: list[str] = []
        selected_task_source = ""
        selected_task_from_explicit_cli = False
        backlog_selected_task_id = ""
        skipped_completed_task_ids: list[str] = []
        completed_task_recorded = False
        completed_task_record_error = ""
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
            canonical_backlog_loaded = implementation_context["canonical_backlog_loaded"]
            canonical_backlog_path = implementation_context["canonical_backlog_path"]
            completed_task_registry_path = implementation_context["completed_task_registry_path"]
            completed_task_count = implementation_context["completed_task_count"]
            completed_task_ids = implementation_context["completed_task_ids"]
            selected_task_source = implementation_context["selected_task_source"]
            selected_task_from_explicit_cli = implementation_context["selected_task_from_explicit_cli"]
            backlog_selected_task_id = implementation_context["backlog_selected_task_id"]
            skipped_completed_task_ids = implementation_context["skipped_completed_task_ids"]
            completed_task_recorded = implementation_context["completed_task_recorded"]
            completed_task_record_error = implementation_context["completed_task_record_error"]
            if self._implementation_retry_from_agent == "developer" and (agent_name == "developer" or self._is_multi_developer_edit_agent(agent_name)):
                developer_feedback_text, developer_feedback_source = self._load_developer_feedback_for_retry()
                developer_feedback_chars = len(developer_feedback_text)
            elif agent_name == "task-designer" and self._task_designer_feedback_for_prompt and self._task_designer_rejection_reason:
                developer_feedback_text = self._task_designer_rejection_reason
                developer_feedback_source = self._task_designer_feedback_source or "task-designer"
                developer_feedback_chars = len(developer_feedback_text)
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

        user_goal_summary = self._build_user_goal_summary()
        combined_parts: list[str] = []
        if task:
            combined_parts.append(f"Task: {task}")
        combined_parts.append(prompt_text)
        if phase == "research" and user_goal_summary:
            combined_parts.append(f"User goal:\n{user_goal_summary}")
        if repository_context:
            combined_parts.append(f"Repository context collected locally:\n{repository_context}")
        if previous_context:
            combined_parts.append(f"Previous agent context:\n{previous_context}")
        if developer_feedback_text:
            combined_parts.append(f"Previous validation feedback to repair:\n{developer_feedback_text}")
        if phase == "implementation":
            combined_parts.append(self._build_implementation_scope_instruction(selected_task_scope, agent_name=agent_name))
        combined_parts.append(translation_instruction)
        combined_message = "\n\n".join(combined_parts)

        system_parts = [prompt_text]
        if phase == "research" and user_goal_summary:
            system_parts.append(f"User goal:\n{user_goal_summary}")
        if repository_context:
            system_parts.append(f"Repository context collected locally:\n{repository_context}")
        if retrieval_enabled:
            system_parts.append(self._build_direct_api_retrieval_instruction(phase=phase, agent_name=agent_name))
        if previous_context:
            system_parts.append(f"Previous agent context:\n{previous_context}")
        if developer_feedback_text:
            system_parts.append(f"Previous validation feedback to repair:\n{developer_feedback_text}")
        if phase == "implementation":
            system_parts.append(self._build_implementation_scope_instruction(selected_task_scope, agent_name=agent_name))
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
            "canonical_backlog_loaded": canonical_backlog_loaded if phase == "implementation" else False,
            "canonical_backlog_path": canonical_backlog_path if phase == "implementation" else "",
            "completed_task_registry_path": completed_task_registry_path if phase == "implementation" else "",
            "completed_task_count": completed_task_count if phase == "implementation" else 0,
            "completed_task_ids": completed_task_ids if phase == "implementation" else [],
            "selected_task_source": selected_task_source if phase == "implementation" else "",
            "selected_task_from_explicit_cli": selected_task_from_explicit_cli if phase == "implementation" else False,
            "backlog_selected_task_id": backlog_selected_task_id if phase == "implementation" else "",
            "skipped_completed_task_ids": skipped_completed_task_ids if phase == "implementation" else [],
            "completed_task_recorded": completed_task_recorded if phase == "implementation" else False,
            "completed_task_record_error": completed_task_record_error if phase == "implementation" else "",
            "developer_feedback_source": developer_feedback_source if phase == "implementation" else "",
            "developer_feedback_text": developer_feedback_text if phase == "implementation" else "",
            "developer_feedback_chars": developer_feedback_chars if phase == "implementation" else 0,
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
            "execution_mode": "balanced",
            "strict_execution_mode": False,
            "retrieval_budget": None,
            "retrieval_budget_remaining": None,
            "retrieval_limit_reason": "",
            "retrieval_hard_stop_triggered": False,
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
        return agent_name in {
            "architect",
            "developer",
            "code-developer",
            "infra-developer",
            "test-developer",
            "qa",
            "template-validator",
        }

    @staticmethod
    def _is_multi_developer_edit_agent(agent_name: str) -> bool:
        return agent_name in {"code-developer", "infra-developer", "test-developer"}

    def _apply_execution_policy(self, agent_name: str, phase: str, message_bundle: dict[str, Any]) -> None:
        execution_mode = self._resolve_execution_mode(agent_name, phase, message_bundle)
        budget = self._retrieval_budget_for_execution_mode(execution_mode)
        message_bundle["execution_mode"] = execution_mode
        message_bundle["strict_execution_mode"] = execution_mode == "strict"
        message_bundle["retrieval_budget"] = budget
        message_bundle["retrieval_budget_remaining"] = budget
        message_bundle["retrieval_limit_reason"] = self._strict_execution_limit_reason(agent_name, phase, message_bundle) if execution_mode == "strict" else ""
        message_bundle["retrieval_hard_stop_triggered"] = False
        if execution_mode == "strict":
            strict_instruction = (
                "Strict execution mode is active. Do not perform repository exploration. "
                "Allowed retrieval is at most one read_file/read_files operation or two retrieval operations total. "
                "list_files, search_text, broad scans, and repeated retrieval loops are forbidden. "
                "If full file content or the exact contract is already injected, write the allowed scoped file immediately."
            )
            message_bundle["system_message"] = str(message_bundle["system_message"]).rstrip() + "\n\n" + strict_instruction
            message_bundle["combined_message"] = str(message_bundle["combined_message"]).rstrip() + "\n\n" + strict_instruction
            stats = message_bundle.get("prompt_stats") or {}
            stats["message_chars"] = len(str(message_bundle["combined_message"]))
            stats["message_lines"] = len(str(message_bundle["combined_message"]).splitlines())
            message_bundle["prompt_stats"] = stats

    def _resolve_execution_mode(self, agent_name: str, phase: str, message_bundle: dict[str, Any]) -> str:
        configured = str(message_bundle.get("execution_mode") or "").strip().lower()
        if configured in {"exploratory", "strict"}:
            return configured
        if self.should_enable_strict_mode(agent_name, phase, message_bundle):
            return "strict"
        if phase == "research":
            return "exploratory"
        return "balanced"

    def should_enable_strict_mode(self, agent_name: str, phase: str, message_bundle: dict[str, Any]) -> bool:
        if phase != "implementation":
            return False
        if agent_name not in {"infra-developer", "test-developer", "code-developer"}:
            return False
        # During a repair retry the developer must read the failing file(s) and the injected
        # feedback to fix them. The strict single-read budget makes that impossible (the agent
        # is hard-stopped with strict_retrieval_blocked and can never land a fix, so leftover
        # broken code keeps failing py_compile/pytest). Use balanced retrieval after attempt 1.
        if (
            getattr(self, "_implementation_retry_from_agent", "") == "developer"
            or getattr(self, "_implementation_attempt", 1) > 1
        ):
            return False
        if str(message_bundle.get("selected_task_scope") or "").strip() != "backend-only":
            return False
        allowed_paths = [
            self._normalize_target_relative_path(path)
            for path in (message_bundle.get("selected_task_allowed_paths") or [])
        ]
        if not allowed_paths or len(allowed_paths) > 3:
            return False
        if not bool(message_bundle.get("contract_completeness")):
            return False
        if not self._existing_context_is_injected(message_bundle):
            return False
        category = self._infer_strict_task_category(agent_name, allowed_paths)
        return category in {"migration", "config", "tests", "isolated_file_patch"}

    @staticmethod
    def _retrieval_budget_for_execution_mode(execution_mode: str) -> int | None:
        if execution_mode == "strict":
            return 2
        if execution_mode == "balanced":
            return 6
        return None

    def _strict_execution_limit_reason(self, agent_name: str, phase: str, message_bundle: dict[str, Any]) -> str:
        allowed_paths = ", ".join(str(path) for path in (message_bundle.get("selected_task_allowed_paths") or []))
        return (
            f"strict mode: agent={agent_name}, phase={phase}, "
            f"scope={message_bundle.get('selected_task_scope')}, allowed_paths={allowed_paths}"
        )

    @staticmethod
    def _existing_context_is_injected(message_bundle: dict[str, Any]) -> bool:
        if bool(message_bundle.get("existing_file_contents_injected")):
            return True
        return int(message_bundle.get("implementation_context_chars") or 0) > 0 and int(message_bundle.get("repository_context_chars") or 0) > 0

    def _infer_strict_task_category(self, agent_name: str, allowed_paths: list[str]) -> str:
        if agent_name == "infra-developer":
            if any("alembic/versions/" in path or "/migrations/" in path for path in allowed_paths):
                return "migration"
            return "config"
        if agent_name == "test-developer":
            return "tests"
        if agent_name == "code-developer" and len(allowed_paths) == 1:
            return "isolated_file_patch"
        return "other"

    def _evaluate_retrieval_budget(
        self,
        message_bundle: dict[str, Any],
        tool_name: str,
        retrieval_operations_used: int,
        read_file_operations_used: int,
    ) -> dict[str, Any]:
        execution_mode = str(message_bundle.get("execution_mode") or "balanced")
        budget = message_bundle.get("retrieval_budget")
        retrieval_tools = {"read_file", "read_files", "search_text", "list_files"}
        normalized_tool = str(tool_name or "").strip()
        if normalized_tool not in retrieval_tools:
            remaining = None if budget is None else max(0, int(budget) - retrieval_operations_used)
            return {
                "allowed": True,
                "reason": "",
                "remaining": remaining,
                "hard_stop": False,
            }
        if execution_mode != "strict":
            remaining = None if budget is None else max(0, int(budget) - retrieval_operations_used)
            return {
                "allowed": True,
                "reason": "",
                "remaining": remaining,
                "hard_stop": False,
            }
        forbidden_tools = {"list_files", "search_text"}
        if normalized_tool in forbidden_tools:
            return {
                "allowed": False,
                "reason": f"strict mode forbids {normalized_tool}; use injected context or exact read_file only",
                "remaining": max(0, int(budget or 0) - retrieval_operations_used),
                "hard_stop": True,
            }
        if retrieval_operations_used >= int(budget or 0):
            return {
                "allowed": False,
                "reason": f"strict retrieval budget exhausted before {normalized_tool}",
                "remaining": 0,
                "hard_stop": True,
            }
        if normalized_tool in {"read_file", "read_files"} and read_file_operations_used >= 1:
            return {
                "allowed": False,
                "reason": "strict mode allows only one read_file/read_files operation",
                "remaining": max(0, int(budget or 0) - retrieval_operations_used),
                "hard_stop": True,
            }
        return {
            "allowed": True,
            "reason": "",
            "remaining": max(0, int(budget or 0) - retrieval_operations_used - 1),
            "hard_stop": False,
        }

    def _build_strict_retrieval_feedback(
        self,
        agent_name: str,
        tool_name: str,
        reason: str,
        message_bundle: dict[str, Any],
    ) -> str:
        allowed_paths = ", ".join(str(path) for path in (message_bundle.get("selected_task_allowed_paths") or [])) or "none"
        return (
            "Strict execution mode stopped unnecessary retrieval.\n\n"
            f"Agent: {agent_name}\n"
            f"Blocked tool: {tool_name}\n"
            f"Reason: {reason}\n"
            f"Allowed paths: {allowed_paths}\n\n"
            "Required next action: do not retry automatically. Re-run only after the agent prompt or task contract is adjusted "
            "to write/apply_patch directly from injected context."
        )

    def _should_force_final_after_retrieval_limit(self, phase: str, agent_name: str) -> bool:
        if phase != "implementation":
            return False
        if agent_name == "qa":
            return False
        if agent_name == "developer" or self._is_multi_developer_edit_agent(agent_name):
            return False
        return True

    @staticmethod
    def _build_retrieval_limit_final_instruction(phase: str, agent_name: str) -> str:
        return (
            "Retrieval budget is now exhausted. Do not request any more tools. "
            "Use the repository context and local retrieval results already provided to produce your final answer now. "
            "If information is incomplete, state the assumptions and concrete gaps in the final answer instead of asking for more retrieval. "
            f"Phase: {phase}. Agent: {agent_name}."
        )

    @staticmethod
    def _default_implementation_scope_policy() -> dict[str, Any]:
        # Project-agnostic defaults. No repository-specific paths live in the engine:
        # concrete sensitive files (billing/auth/payment/...) are derived per-target from
        # forbidden_keywords against the repo_map, and a project may declare its own policy
        # in its per-project settings.yaml. Only universal infra files are forbidden here.
        # fnmatch '*' spans '/', so each "X" + "*/X" pair covers root and any nested depth.
        return {
            "allowed_paths": [],
            "forbidden_paths": [
                ".github/*",
                "*/.github/*",
                "Dockerfile",
                "*/Dockerfile",
                "Dockerfile.*",
                "*/Dockerfile.*",
                "docker-compose*.yml",
                "*/docker-compose*.yml",
                "docker-compose*.yaml",
                "*/docker-compose*.yaml",
            ],
            "forbidden_keywords": ["stripe", "billing", "subscription", "payment", "checkout", "marketplace"],
            "max_changed_files": 8,
            "max_diff_lines": 500,
        }

    def _get_implementation_scope_policy(self) -> dict[str, Any]:
        base = self._default_implementation_scope_policy()
        # Global operator config first, then the per-project declaration wins. The engine
        # stays project-agnostic; each project carries its own truth in its settings.yaml.
        override_sources = [
            self.config.get("workflow", {}).get("implementation_scope_policy", {}) or {},
            (getattr(self, "project_settings", {}) or {}).get("implementation_scope_policy", {}) or {},
        ]
        for overrides in override_sources:
            if not isinstance(overrides, dict):
                continue
            # allowed_paths: the project defines its own editable surface -> replace.
            allowed = overrides.get("allowed_paths")
            if isinstance(allowed, list) and allowed:
                base["allowed_paths"] = [str(item).replace("\\", "/").strip() for item in allowed if str(item).strip()]
            # forbidden_paths / forbidden_keywords are a safety fence: a project may ADD to it
            # but must never drop the universal infra/keyword protections -> union.
            for key in ("forbidden_paths", "forbidden_keywords"):
                value = overrides.get(key)
                if isinstance(value, list) and value:
                    additions = [str(item).replace("\\", "/").strip() for item in value if str(item).strip()]
                    base[key] = list(dict.fromkeys(list(base[key]) + additions))
            for key in ("max_changed_files", "max_diff_lines"):
                value = overrides.get(key)
                if value is not None:
                    try:
                        base[key] = int(value)
                    except (TypeError, ValueError):
                        pass
        return base

    def _derive_forbidden_paths_from_repo_map(self) -> list[str]:
        """Concrete forbidden file paths inferred from forbidden_keywords against the repo_map.

        Universal protection: on any target, files whose path contains a sensitive keyword
        (billing, payment, marketplace, ...) are auto-forbidden without per-project config,
        so the engine protects the right files on a repo it has never seen.
        """
        policy = getattr(self, "implementation_scope_policy", None) or {}
        keywords = [str(k).lower() for k in (policy.get("forbidden_keywords") or []) if str(k).strip()]
        if not keywords:
            return []
        if getattr(self, "_repo_map_cache", None) is None and not getattr(self, "repo_map_path", None):
            return []
        try:
            repo_map = self._load_repo_map()
        except Exception:
            return []
        derived: list[str] = []
        for entry in repo_map.get("files") or []:
            path = str(entry.get("path") or "").strip()
            if not path:
                continue
            if any(keyword in path.lower() for keyword in keywords):
                normalized = self._normalize_repo_relative_path(path)
                if normalized:
                    derived.append(normalized)
        return sorted(dict.fromkeys(derived))

    def _effective_forbidden_paths(self) -> list[str]:
        """Declared forbidden patterns plus the ones derived from the target's repo_map."""
        declared = list((getattr(self, "implementation_scope_policy", None) or {}).get("forbidden_paths") or [])
        return list(dict.fromkeys(declared + self._derive_forbidden_paths_from_repo_map()))

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
        user_goal_summary = self._build_user_goal_summary()
        if profile == "repo_overview_full":
            sections = [
                ("User goal", user_goal_summary),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1600)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1600)),
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
                ("User goal", user_goal_summary),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1200)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1200)),
                ("Compressed project-analyst summary", self._build_fallback_project_summary()),
                ("README.md", self._read_file_excerpt(self.target_workspace / "README.md", 1400)),
                ("Compact architecture summary", self._build_compact_architecture_summary()),
                ("Default competitors", "LangGraph, CrewAI, AutoGen, OpenHands, Claude Code, Codex CLI, OpenClaw, Dify, n8n"),
                ("External research instruction", "Use Perplexity/Sonar for external comparison. Do not ask clarification."),
            ]
        elif profile == "market_positioning":
            sections = [
                ("User goal", user_goal_summary),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1000)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1000)),
                ("Positioning", self._build_positioning_summary()),
                ("Workflow goals", self._build_workflow_goals_summary()),
                ("Target users and use cases", self._build_target_users_summary()),
            ]
        elif profile == "technical_architecture":
            sections = [
                ("User goal", user_goal_summary),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1200)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1200)),
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
                ("User goal", user_goal_summary),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1000)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1000)),
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
            sections = [
                ("User goal", user_goal_summary),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1000)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1000)),
                ("README.md", self._read_file_excerpt(self.target_workspace / "README.md", 1500)),
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

    def _build_external_project_repository_context(self, agent_name: str = "project-analyst", limit: int = 12000) -> str:
        profile = self._get_research_context_profile(agent_name)
        user_goal_summary = self._build_user_goal_summary()
        if profile == "repo_overview_full":
            sections = [
                ("User goal", user_goal_summary),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1600)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1600)),
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
                ("User goal", user_goal_summary),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1200)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1200)),
                ("Compressed target project summary", self._build_fallback_project_summary()),
                ("Target README excerpt", self._read_target_repo_file("README.md", 1600)),
                ("Target product and architecture summary", self._build_target_product_architecture_summary()),
                ("Default competitors", "LangGraph, CrewAI, AutoGen, OpenHands, Claude Code, Codex CLI, OpenClaw, Dify, n8n"),
                ("External research instruction", "Use Perplexity/Sonar for external comparison. Do not ask clarification."),
            ]
        elif profile == "market_positioning":
            sections = [
                ("User goal", user_goal_summary),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1000)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1000)),
                ("Target project summary", self._build_fallback_project_summary()),
                ("Target product positioning", self._build_target_positioning_summary()),
                ("Target workflow and business goals", self._build_target_goals_summary()),
                ("Target users and use cases", self._build_target_users_inferred_summary()),
            ]
        elif profile == "technical_architecture":
            sections = [
                ("User goal", user_goal_summary),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1200)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1200)),
                ("Target backend/frontend structure", self._build_target_backend_frontend_summary()),
                ("Target dependency and config files", self._build_target_dependency_context(limit=4200)),
                ("Target docker and deployment files", self._build_target_deployment_context(limit=2400)),
                ("Target top-level tree up to depth 4", self._build_top_level_tree(root=self.target_workspace, depth=4)),
                ("Target tests list", self._build_target_tests_file_list(limit=2200)),
            ]
        elif profile == "external_innovation":
            sections = [
                ("User goal", user_goal_summary),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1000)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1000)),
                ("Target project summary", self._build_fallback_project_summary()),
                ("Target constraints and problems", self._build_target_constraints_summary()),
                ("External inspiration focus", "Look for product, UX, workflow, and automation ideas relevant to this target repository."),
                ("External research instruction", "Use Perplexity/Sonar for external inspiration. Do not ask clarification."),
            ]
        elif profile == "research_synthesis":
            sections = [
                ("User goal", user_goal_summary),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1200)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1200)),
                ("Target docs excerpts", self._build_target_docs_excerpts(limit=2200)),
                ("Target dependency and config files", self._build_target_dependency_context(limit=2200)),
            ]
        else:
            sections = [
                ("User goal", user_goal_summary),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1000)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1000)),
                ("Target README excerpt", self._read_target_repo_file("README.md", 1500)),
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

    def _run_local_command(self, command: list[str], timeout: int = 10, cwd: Path | None = None) -> tuple[int, str, str]:
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
        except Exception as exc:
            return 1, "", str(exc)
        return process.returncode, (process.stdout or "").strip(), (process.stderr or "").strip()

    def resolve_python_executable(self) -> tuple[str, str, bool]:
        candidates: list[tuple[Path, str]] = [
            (self.engine_root / "venv" / "Scripts" / "python.exe", "engine_venv"),
            (self.target_workspace / ".venv" / "Scripts" / "python.exe", "target_venv"),
            (Path(sys.executable), "sys_executable"),
        ]

        first_existing: tuple[str, str, bool] | None = None
        for candidate, source in candidates:
            if not candidate.exists():
                continue
            pytest_available = self._python_has_pytest(candidate)
            if first_existing is None:
                first_existing = (str(candidate), source, pytest_available)
            if pytest_available:
                return str(candidate), source, True

        if first_existing is not None:
            return first_existing
        return sys.executable, "sys_executable_missing", False

    def _python_has_pytest(self, python_executable: Path) -> bool:
        try:
            process = subprocess.run(
                [str(python_executable), "-m", "pytest", "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                cwd=self.target_workspace,
            )
        except Exception:
            return False
        return process.returncode == 0

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

    def _build_user_goal_summary(self) -> str:
        goal = str(self.user_goal or "").strip()
        if not goal:
            return ""
        return (
            "Primary user goal for this run. Treat it as the main objective for research, planning, and implementation. "
            "Do not broaden scope unless the user explicitly asks.\n"
            f"{goal}"
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

    PROJECT_MANIFEST_NAMES = (
        "requirements.txt",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "package.json",
        "go.mod",
        "Cargo.toml",
        "pom.xml",
        "build.gradle",
        "Gemfile",
        "composer.json",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
    )
    PROJECT_SOURCE_DIR_NAMES = ("app", "src", "lib", "internal")

    @staticmethod
    def _dir_is_project_root(directory: Path) -> bool:
        try:
            entries = list(directory.iterdir())
        except OSError:
            return False
        for entry in entries:
            name = entry.name
            if entry.is_file() and name in WorkflowOrchestrator.PROJECT_MANIFEST_NAMES:
                return True
            if entry.is_dir() and name in WorkflowOrchestrator.PROJECT_SOURCE_DIR_NAMES:
                return True
        return False

    def _detect_project_roots(self) -> list[str]:
        """Relative subdirectories that look like project/source roots in the target repo.

        Always includes the repo root (""). Adds any top-level or second-level directory that
        carries a build/dependency manifest or a conventional source layout, so context builders
        gather dependency/deployment files from the REAL layout instead of an assumed one
        (a nested service subdir in one repo, a flat root in another). No project name is hardcoded.
        """
        cached = getattr(self, "_project_roots_cache", None)
        if cached is not None:
            return cached
        roots: list[str] = [""]
        workspace = getattr(self, "target_workspace", None)
        workspace = Path(workspace) if workspace else None
        if not workspace or not workspace.exists():
            self._project_roots_cache = roots
            return roots
        excluded = {
            ".git", ".venv", "venv", "env", "node_modules", "__pycache__", "dist", "build",
            ".pytest_cache", ".mypy_cache", "site-packages", ".openclaw", ".agents-pipeline", "db_data",
        }
        def _scan(base: Path, prefix: str, depth: int) -> None:
            if depth > 2:
                return
            try:
                children = sorted(p for p in base.iterdir() if p.is_dir())
            except OSError:
                return
            for child in children:
                if child.name in excluded or child.name.startswith("."):
                    continue
                rel = f"{prefix}{child.name}"
                if self._dir_is_project_root(child):
                    roots.append(rel)
                _scan(child, f"{rel}/", depth + 1)
        _scan(workspace, "", 1)
        result = list(dict.fromkeys(roots))
        self._project_roots_cache = result
        return result

    def _project_root_candidates(self, filenames: list[str]) -> list[str]:
        """Each filename resolved under every detected project root (repo root included)."""
        candidates: list[str] = []
        for root in self._detect_project_roots():
            for name in filenames:
                candidates.append(f"{root}/{name}" if root else name)
        return list(dict.fromkeys(candidates))

    def _build_target_dependency_context(self, limit: int = 3200) -> str:
        candidates = self._project_root_candidates(
            ["package.json", "pyproject.toml", "requirements.txt", "docker-compose.yml", "Dockerfile", ".env.example"]
        )
        return self._build_target_file_excerpts(candidates, per_file_limit=900, total_limit=limit)

    def _build_target_deployment_context(self, limit: int = 2400) -> str:
        candidates = self._project_root_candidates(["docker-compose.yml", "Dockerfile"]) + [
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
            relative_text
            for path in self.target_workspace.rglob("*")
            for relative_text in [str(path.relative_to(self.target_workspace)).replace("\\", "/")]
            if path.is_file()
            and not is_excluded_path(relative_text)
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
        package_markers = [c for c in self._project_root_candidates(["package.json"]) if (self.target_workspace / c).exists()]
        if package_markers:
            lines.append("JavaScript package files: " + ", ".join(package_markers))
        python_markers = [
            c for c in self._project_root_candidates(["pyproject.toml", "requirements.txt"]) if (self.target_workspace / c).exists()
        ]
        if python_markers:
            lines.append("Python dependency files: " + ", ".join(python_markers))
        deployment_markers: list[str] = [
            c for c in self._project_root_candidates(["docker-compose.yml", "Dockerfile"]) if (self.target_workspace / c).exists()
        ]
        deployment_markers.extend(root for root in self._detect_project_roots() if root and (self.target_workspace / root).is_dir())
        if deployment_markers:
            lines.append("Visible runtime/deployment structure: " + ", ".join(dict.fromkeys(deployment_markers)))
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
        user_goal_summary = self._build_user_goal_summary()
        if user_goal_summary:
            return user_goal_summary[:800]
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
        seen: set[str] = set()
        # Real detected source roots first, then conventional directory names as a fallback.
        directories = [root for root in self._detect_project_roots() if root]
        directories.extend(("frontend", "backend", "src", "app"))
        for directory in directories:
            if directory in seen:
                continue
            seen.add(directory)
            path = self.target_workspace / directory
            if path.exists() and path.is_dir():
                tree = self._build_top_level_tree(root=path, depth=2)
                if tree:
                    sections.append(f"## {directory}\n{tree}")
        return "\n\n".join(sections)

    def _build_target_constraints_summary(self) -> str:
        lines = [
            "Constraints should be inferred from target docs, dependency files, and repository structure.",
        ]
        if any((self.target_workspace / c).exists() for c in self._project_root_candidates(["docker-compose.yml", "docker-compose.yaml"])):
            lines.append("Deployment orchestration is present via docker-compose.")
        if any((self.target_workspace / c).exists() for c in self._project_root_candidates(["requirements.txt", "pyproject.toml"])):
            lines.append("Python runtime dependencies are present.")
        if any((self.target_workspace / c).exists() for c in self._project_root_candidates(["package.json"])):
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
            relative_text = str(relative).replace("\\", "/")
            if is_excluded_path(relative_text):
                continue
            lines.append(relative_text)
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
                developer_base = (
                    "Supported requests are: "
                    '{"tool":"read_file","path":"relative/path.py"}, '
                    '{"tool":"read_files","paths":["relative/path.py"]}. '
                    "Use relative workspace paths only. "
                )
                return developer_base + (
                    'You may also request write operations with '
                    '{"tool":"write_file","path":"<relative/path/from/repo_map>","content":"..."} '
                    'or {"tool":"apply_patch","path":"<relative/path/from/repo_map>","search":"old","replace":"new"}. '
                    "Before editing an existing file, inspect it first with read_file. "
                    "For a new file that does not exist yet, do not read it; create it directly with write_file. "
                    "Use only read_file/read_files for contract and reference paths during retrieval. "
                    "Do not use search_text or list_files in developer implementation mode. "
                    "Make the smallest viable backend-only change. "
                    "Do not return narrative-only output when a safe edit is required. "
                    "Keep using JSON tool requests until you have either completed a real file edit or determined that no safe scoped edit is possible. "
                    "If you complete at least one file edit, your final non-JSON response must be exactly status=implemented. "
                    "If you cannot safely edit within scope, your final non-JSON response must be status=no_changes: <reason>."
                )
            if agent_name == "qa":
                qa_base = (
                    "Supported requests are: "
                    '{"tool":"read_file","path":"relative/path.py"}, '
                    '{"tool":"read_files","paths":["relative/path.py"]}. '
                    "Use relative workspace paths only. "
                )
                return qa_base + (
                    "Use retrieval only for changed files, contract files, and exact reference paths already present in context. "
                    "Do not use search_text or list_files in QA mode. "
                    "Inspect the actual git diff before approving. "
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
        max_attempts = 4
        for attempt in range(1, max_attempts + 1):
            try:
                response = urllib_request.urlopen(request, timeout=timeout)
                if hasattr(response, "__enter__") and hasattr(response, "__exit__"):
                    with response:
                        return 200, response.read().decode("utf-8", errors="replace")
                return 200, response.read().decode("utf-8", errors="replace")
            except urllib_error.HTTPError as exc:
                # Client errors (4xx) are not transient — surface immediately. Server
                # errors (5xx) are retried with backoff.
                if exc.code and exc.code < 500:
                    raise
                last_error = exc
                if attempt >= max_attempts:
                    raise
                time.sleep(min(4.0, 0.75 * attempt))
            except (http.client.HTTPException, OSError) as exc:
                # Transient network failures: IncompleteRead, ssl.SSLError (bad record mac),
                # socket timeouts, connection resets, and other URLError/OSError cases.
                last_error = exc
                if attempt >= max_attempts:
                    raise
                time.sleep(min(4.0, 0.75 * attempt))
        if last_error is not None:
            raise last_error
        raise RuntimeError("direct_api request failed without response")

    @staticmethod
    def _normalize_tool_payload(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        tool_value = payload.get("tool")
        if not isinstance(tool_value, str):
            tool_value = payload.get("name")
        if not isinstance(tool_value, str):
            return None
        tool = str(tool_value).strip().lower()
        if tool not in {"read_file", "read_files", "search_text", "list_files", "write_file", "apply_patch", "tool_batch"}:
            return None
        nested = payload.get("arguments")
        if nested is None:
            nested = payload.get("args")
        if nested is None:
            nested = payload.get("input")
        if isinstance(nested, str):
            try:
                nested = json.loads(nested.strip())
            except json.JSONDecodeError:
                nested = None
        merged: dict[str, Any] = {}
        if isinstance(nested, dict):
            merged.update(nested)
        merged.update(payload)
        if tool == "tool_batch":
            requests = merged.get("requests")
            if not isinstance(requests, list):
                return None
            normalized_requests = [
                normalized
                for request in requests
                if (normalized := WorkflowOrchestrator._normalize_tool_payload(request)) is not None
            ]
            return {"tool": "tool_batch", "requests": normalized_requests}
        normalized: dict[str, Any] = {"tool": tool}
        if tool == "read_file":
            path = merged.get("path")
            if not isinstance(path, str) or not path.strip():
                return None
            normalized["path"] = path.strip()
        elif tool == "read_files":
            paths = merged.get("paths")
            if not isinstance(paths, list):
                return None
            cleaned_paths = [str(path).strip() for path in paths if str(path).strip()]
            if not cleaned_paths:
                return None
            normalized["paths"] = cleaned_paths
        elif tool == "search_text":
            pattern = merged.get("pattern")
            if not isinstance(pattern, str) or not pattern.strip():
                return None
            normalized["pattern"] = pattern.strip()
            if merged.get("limit") is not None:
                normalized["limit"] = merged.get("limit")
        elif tool == "list_files":
            directory = merged.get("directory")
            if isinstance(directory, str) and directory.strip():
                normalized["directory"] = directory.strip()
            if merged.get("max_depth") is not None:
                normalized["max_depth"] = merged.get("max_depth")
        elif tool == "write_file":
            path = merged.get("path")
            content = merged.get("content")
            if not isinstance(path, str) or not path.strip() or not isinstance(content, str):
                return None
            normalized["path"] = path.strip()
            normalized["content"] = content
        elif tool == "apply_patch":
            path = merged.get("path")
            search = merged.get("search")
            replace = merged.get("replace")
            if not isinstance(path, str) or not path.strip() or not isinstance(search, str) or not isinstance(replace, str):
                return None
            normalized["path"] = path.strip()
            normalized["search"] = search
            normalized["replace"] = replace
        return normalized

    @staticmethod
    def _tool_extraction_variants(text: str) -> list[str]:
        variants: list[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            cleaned = str(value or "").strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                variants.append(cleaned)

        add(text)
        for match in re.findall(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE):
            add(match)
        for candidate in list(variants):
            if '\\"' in candidate or "\\n" in candidate or "\\t" in candidate:
                try:
                    add(bytes(candidate, "utf-8").decode("unicode_escape"))
                except UnicodeDecodeError:
                    pass
            if (candidate.startswith('"') and candidate.endswith('"')) or (candidate.startswith("'") and candidate.endswith("'")):
                try:
                    decoded = json.loads(candidate)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, str):
                    add(decoded)
        return variants

    @staticmethod
    def _extract_tool_operations(text: str) -> list[dict[str, Any]]:
        operations: list[dict[str, Any]] = []
        seen: set[str] = set()

        def append_payload(payload: Any) -> None:
            normalized = WorkflowOrchestrator._normalize_tool_payload(payload)
            if normalized is None:
                return
            marker = json.dumps(normalized, ensure_ascii=False, sort_keys=True)
            if marker in seen:
                return
            seen.add(marker)
            operations.append(normalized)

        for candidate in WorkflowOrchestrator._tool_extraction_variants(str(text or "")):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                append_payload(parsed)
            elif isinstance(parsed, list):
                for item in parsed:
                    append_payload(item)

            for payload in WorkflowOrchestrator._extract_json_tool_sequence(candidate):
                append_payload(payload)

            block_read_files_match = re.search(
                r"<read_files>\s*<paths>(?P<body>[\s\S]*?)</paths>\s*</read_files>",
                candidate,
                flags=re.IGNORECASE,
            )
            if block_read_files_match:
                body = str(block_read_files_match.group("body") or "")
                paths = [
                    str(path).strip()
                    for path in re.findall(r"<path>\s*([\s\S]*?)\s*</path>", body, flags=re.IGNORECASE)
                    if str(path).strip()
                ]
                if paths:
                    append_payload({"tool": "read_files", "paths": paths})

            for match in re.finditer(
                r"<(?P<tool>[a-z_][a-z0-9_-]*)\s+(?P<attrs>[^<>]*?)/>",
                candidate,
                flags=re.IGNORECASE,
            ):
                tool = str(match.group("tool") or "").strip().lower()
                attrs_raw = str(match.group("attrs") or "")
                attrs = {
                    key.lower(): value
                    for key, _quote, value in re.findall(
                        r"([a-zA-Z_][a-zA-Z0-9_-]*)\s*=\s*(['\"])(.*?)\2",
                        attrs_raw,
                    )
                }
                append_payload({"tool": tool, **attrs})

            for match in re.finditer(
                r"(?P<tool>read_file|read_files|search_text|list_files|write_file|apply_patch)\s*\(\s*(?P<args>\{[\s\S]*?\})\s*\)",
                candidate,
                flags=re.IGNORECASE,
            ):
                tool = str(match.group("tool") or "").strip().lower()
                args_text = str(match.group("args") or "").strip()
                try:
                    args_payload = json.loads(args_text)
                except json.JSONDecodeError:
                    continue
                if isinstance(args_payload, dict):
                    append_payload({"tool": tool, **args_payload})

            for match in re.finditer(
                r"(?:<tool_call>\s*)?(?P<tool>read_file|read_files|search_text|list_files|write_file|apply_patch)\s*\(\s*(?P<args>[^()]*)\s*\)",
                candidate,
                flags=re.IGNORECASE,
            ):
                tool = str(match.group("tool") or "").strip().lower()
                args_text = str(match.group("args") or "").strip()
                attrs = {
                    key.lower(): value
                    for key, _quote, value in re.findall(
                        r"([a-zA-Z_][a-zA-Z0-9_-]*)\s*=\s*(['\"])(.*?)\2",
                        args_text,
                    )
                }
                append_payload({"tool": tool, **attrs})

            for raw_object in re.findall(r"(\{[\s\S]*?\})", candidate):
                try:
                    append_payload(json.loads(raw_object))
                except json.JSONDecodeError:
                    continue
        return operations

    @staticmethod
    def _extract_write_operations(text: str) -> list[dict[str, Any]]:
        write_operations: list[dict[str, Any]] = []
        for payload in WorkflowOrchestrator._extract_tool_operations(text):
            tool = payload.get("tool")
            if tool in {"write_file", "apply_patch"}:
                write_operations.append(payload)
                continue
            if tool == "tool_batch":
                for request in payload.get("requests") or []:
                    if not isinstance(request, dict):
                        continue
                    request_tool = request.get("tool")
                    if request_tool in {"write_file", "apply_patch"}:
                        write_operations.append(request)
        return write_operations

    @staticmethod
    def _parse_direct_api_retrieval_request(text: str) -> dict[str, Any] | None:
        operations = WorkflowOrchestrator._extract_tool_operations(text)
        if not operations:
            return None
        if len(operations) == 1:
            return operations[0]
        if all(payload.get("tool") == "read_file" and payload.get("path") for payload in operations):
            return {
                "tool": "read_files",
                "paths": [str(payload["path"]) for payload in operations],
            }
        if any(payload.get("tool") in {"write_file", "apply_patch"} for payload in operations):
            return {"tool": "tool_batch", "requests": operations}
        leading_read_files: list[str] = []
        for payload in operations:
            if payload.get("tool") == "read_file" and payload.get("path"):
                leading_read_files.append(str(payload["path"]))
                continue
            break
        if len(leading_read_files) >= 2:
            return {
                "tool": "read_files",
                "paths": leading_read_files,
            }
        return {"tool": "tool_batch", "requests": operations}

    @staticmethod
    def _extract_json_tool_sequence(text: str) -> list[dict[str, Any]]:
        decoder = json.JSONDecoder()
        normalized = str(text or "")
        payloads: list[dict[str, Any]] = []
        index = 0
        length = len(normalized)
        while index < length:
            brace_index = normalized.find("{", index)
            if brace_index < 0:
                break
            try:
                payload, end_index = decoder.raw_decode(normalized, brace_index)
            except json.JSONDecodeError:
                index = brace_index + 1
                continue
            if isinstance(payload, dict) and isinstance(payload.get("tool"), str):
                payloads.append(payload)
            index = max(end_index, brace_index + 1)
        return payloads

    def _execute_direct_api_retrieval_request(self, payload: dict[str, Any], phase: str = "research", agent_name: str = "") -> str:
        tool = str(payload.get("tool") or "").strip()
        if tool == "tool_batch":
            requests = payload.get("requests")
            if not isinstance(requests, list):
                return "Invalid tool_batch request."
            outputs: list[str] = []
            for request in requests:
                if not isinstance(request, dict):
                    continue
                result = self._execute_direct_api_retrieval_request(request, phase=phase, agent_name=agent_name)
                outputs.append(result)
            return "\n\n".join(part for part in outputs if part)
        if tool == "read_file":
            path = str(payload.get("path") or "").strip()
            if not path:
                return "Invalid read_file request."
            self._record_retrieval_tool_usage(phase, agent_name, tool, [path])
            return self._direct_api_read_files([path], limit=self._direct_api_read_limit_for_agent(phase, agent_name))
        if tool == "read_files":
            paths = payload.get("paths")
            if not isinstance(paths, list):
                return "Invalid read_files request."
            normalized_paths = [str(path) for path in paths]
            self._record_retrieval_tool_usage(phase, agent_name, tool, normalized_paths)
            return self._direct_api_read_files(normalized_paths, limit=self._direct_api_read_limit_for_agent(phase, agent_name))
        if tool == "search_text":
            pattern = str(payload.get("pattern") or "").strip()
            limit = self._coerce_int(payload.get("limit")) or 20
            if not pattern:
                return "Invalid search_text request."
            self._record_retrieval_tool_usage(phase, agent_name, tool)
            if phase == "implementation" and (agent_name == "developer" or self._is_multi_developer_edit_agent(agent_name)):
                return (
                    "search_text is disabled for developer in implementation mode. "
                    "Use read_file/read_files for the exact contract paths, then either write the scoped change or return status=no_changes."
                )
            return self._direct_api_search_text(pattern, limit=max(1, min(limit, 50)))
        if tool == "list_files":
            directory = str(payload.get("directory") or ".").strip() or "."
            max_depth = self._coerce_int(payload.get("max_depth")) or 3
            self._record_retrieval_tool_usage(phase, agent_name, tool)
            if phase == "implementation" and (agent_name == "developer" or self._is_multi_developer_edit_agent(agent_name)):
                return (
                    "list_files is disabled for developer in implementation mode. "
                    "Use read_file/read_files for the exact contract paths, then either write the scoped change or return status=no_changes."
                )
            return self._direct_api_list_files(directory, max_depth=max(1, min(max_depth, 6)))
        if tool == "write_file":
            if not (phase == "implementation" and (agent_name == "developer" or self._is_multi_developer_edit_agent(agent_name))):
                return "write_file is not allowed for this agent."
            path = str(payload.get("path") or "").strip()
            content = payload.get("content")
            if not path or not isinstance(content, str):
                return "Invalid write_file request."
            self._record_retrieval_tool_usage(phase, agent_name, tool)
            return self._direct_api_write_file(path, content)
        if tool == "apply_patch":
            if not (phase == "implementation" and (agent_name == "developer" or self._is_multi_developer_edit_agent(agent_name))):
                return "apply_patch is not allowed for this agent."
            path = str(payload.get("path") or "").strip()
            search = payload.get("search")
            replace = payload.get("replace")
            if not path or not isinstance(search, str) or not isinstance(replace, str):
                return "Invalid apply_patch request."
            self._record_retrieval_tool_usage(phase, agent_name, tool)
            return self._direct_api_apply_patch(path, search, replace)
        return f"Unsupported retrieval tool: {tool}"

    @staticmethod
    def _direct_api_read_limit_for_agent(phase: str, agent_name: str) -> int:
        if phase == "implementation" and agent_name in {"qa", "template-validator"}:
            return 40000
        return 8000

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

    def _build_developer_protocol_repair_instruction(self) -> str:
        return (
            "You claimed status=implemented, but orchestration recorded no write_file/apply_patch. "
            "Do not explain. On the next response do exactly one of the following: "
            '1) emit one JSON write request using {"tool":"write_file", ...}, {"tool":"apply_patch", ...}, '
            'or a tool_batch that actually includes a write_file/apply_patch; '
            "or 2) return exactly status=no_changes: <concrete reason>."
        )

    def _build_developer_write_repair_instruction(self, error_text: str, remaining_attempts: int) -> str:
        return (
            "Your last write_file/apply_patch request did not pass orchestration validation. "
            f"Validator error: {error_text}. "
            "Do not request more broad retrieval. "
            "On your next response do exactly one of the following: "
            '1) emit one corrected JSON write request using {"tool":"write_file", ...}, {"tool":"apply_patch", ...}, '
            "or a tool_batch that includes a corrected write; "
            "or 2) return exactly status=no_changes: <concrete reason>. "
            f"Remaining repair attempts after this message: {max(0, remaining_attempts)}."
        )

    def _build_developer_exact_path_repair_instruction(self) -> str:
        item = self._selected_implementation_item or {}
        exact_paths: list[str] = []
        for path in (item.get("allowed_paths") or []):
            normalized = self._normalize_repo_relative_path(path)
            if normalized:
                exact_paths.append(normalized)
        target_file = item.get("target_file") or {}
        test_file = item.get("test_file") or {}
        if isinstance(target_file, dict):
            normalized = self._normalize_repo_relative_path(target_file.get("path"))
            if normalized:
                exact_paths.append(normalized)
        if isinstance(test_file, dict):
            normalized = self._normalize_repo_relative_path(test_file.get("path"))
            if normalized:
                exact_paths.append(normalized)
        exact_paths = sorted(dict.fromkeys(exact_paths))
        path_lines = "\n".join(f"- {path}" for path in exact_paths) or "- none"
        return (
            "Your previous retrieval request used a disallowed broad tool for developer implementation mode. "
            "Do not use list_files or search_text again. "
            "On the next response, do exactly one of the following: "
            '1) emit a JSON read_file/read_files request using only these exact contract paths:\n'
            f"{path_lines}\n"
            '2) emit a JSON write_file/apply_patch request using only these exact contract paths; '
            "or 3) return exactly status=no_changes: <concrete reason>."
        )

    def _log_operator_summary(self, title: str, lines: list[str]) -> None:
        filtered = [str(line).strip() for line in lines if str(line).strip()]
        if not filtered:
            return
        self.logger.operator_box(title, [f"- {line}" for line in filtered], color="cyan")

    def _build_prompt_brief_lines(self, agent_name: str, phase: str, message_bundle: dict[str, Any]) -> list[str]:
        lines = [
            f"фаза={phase}",
            f"агент={agent_name}",
        ]
        user_task = str(message_bundle.get("user_message") or "").strip()
        if user_task:
            lines.append(f"задача={user_task}")
        lines = [
            line.replace("С„Р°Р·Р°", "фаза").replace("Р°РіРµРЅС‚", "агент").replace("Р·Р°РґР°С‡Р°", "задача")
            for line in lines
        ]
        selected_task_id = str(message_bundle.get("selected_task_id") or "").strip()
        if selected_task_id:
            lines.append(f"implementation_task={selected_task_id}")
        selected_task_source = str(message_bundle.get("selected_task_source") or "").strip()
        if selected_task_source:
            lines.append(f"selected_task_source={selected_task_source}")
        backlog_selected_task_id = str(message_bundle.get("backlog_selected_task_id") or "").strip()
        if backlog_selected_task_id:
            lines.append(f"backlog_selected_task_id={backlog_selected_task_id}")
        selected_task_scope = str(message_bundle.get("selected_task_scope") or "").strip()
        if selected_task_scope:
            lines.append(f"scope={selected_task_scope}")
        allowed_paths = list(message_bundle.get("selected_task_allowed_paths") or [])
        if allowed_paths:
            preview = ", ".join(str(path) for path in allowed_paths[:4])
            if len(allowed_paths) > 4:
                preview += f" ... (+{len(allowed_paths) - 4})"
            lines.append(f"allowed_paths={preview}")
        if message_bundle.get("retrieval_enabled"):
            lines.append("retrieval=enabled")
        else:
            lines.append("retrieval=disabled")
        lines.append(f"execution_mode={message_bundle.get('execution_mode') or 'balanced'}")
        if message_bundle.get("strict_execution_mode"):
            lines.append(f"retrieval_budget={message_bundle.get('retrieval_budget')}")
        developer_feedback_source = str(message_bundle.get("developer_feedback_source") or "").strip()
        developer_feedback_chars = int(message_bundle.get("developer_feedback_chars") or 0)
        if developer_feedback_source:
            lines.append(f"repair_feedback_injected=yes")
            lines.append(f"repair_feedback_source={developer_feedback_source}")
            lines.append(f"repair_feedback_chars={developer_feedback_chars}")
        else:
            lines.append("repair_feedback_injected=no")
        prompt_sections = ["base_prompt"]
        if message_bundle.get("repository_context_chars"):
            prompt_sections.append("repo_context")
        if message_bundle.get("previous_context"):
            prompt_sections.append("previous_agent_context")
        if developer_feedback_source:
            prompt_sections.append("repair_feedback")
        if phase == "implementation":
            prompt_sections.append("scope_instruction")
        prompt_sections.append("translation_instruction")
        lines.append("prompt_sections=" + ", ".join(prompt_sections))
        return lines

    def _log_retry_outcome_summary(self, title: str, lines: list[str]) -> None:
        self._log_operator_summary(title, lines)

    @staticmethod
    def _is_compact_console_prompt_mode(agent_name: str, phase: str) -> bool:
        return phase == "implementation" and agent_name in {
            "developer",
            "code-developer",
            "infra-developer",
            "test-developer",
            "qa",
            "template-validator",
        }

    @staticmethod
    def _build_prompt_preview_lines(text: str, limit: int = 24) -> list[str]:
        lines = str(text or "").splitlines()
        if len(lines) <= limit:
            return lines
        remaining = len(lines) - limit
        return [*lines[:limit], f"... [truncated {remaining} lines; full prompt is saved in agent report]"]

    @staticmethod
    def _build_feedback_preview_lines(text: str, limit: int = 18) -> list[str]:
        lines = [line.rstrip() for line in str(text or "").splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        if len(lines) <= limit:
            return lines
        remaining = len(lines) - limit
        return [*lines[:limit], f"... [truncated {remaining} lines; full feedback is saved in feedback file]"]

    def _direct_api_read_files(self, paths: list[str], limit: int = 8000) -> str:
        chunks: list[str] = []
        total = 0
        workspace_root = self.target_workspace.resolve()
        selected_paths = paths[:12]
        per_file_limit = 2500
        if limit > 8000:
            per_file_limit = max(2500, min(20000, limit // max(1, min(len(selected_paths), 5))))
        for raw_path in selected_paths:
            candidate = (workspace_root / raw_path).resolve()
            try:
                candidate.relative_to(workspace_root)
            except ValueError:
                continue
            if not candidate.exists() or not candidate.is_file():
                # Surface missing files explicitly instead of returning an empty result.
                # A developer agent that must create a new file (e.g. a test file) otherwise
                # reads "nothing" and wrongly concludes it cannot proceed.
                chunks.append(
                    f"## {raw_path}\n"
                    "[file does not exist yet — there is nothing to read. "
                    "If this path is within your allowed scope, create it directly with write_file.]"
                )
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            excerpt = text
            if len(excerpt) > per_file_limit:
                omitted = len(excerpt) - per_file_limit
                excerpt = (
                    excerpt[:per_file_limit]
                    + f"\n... [file truncated: {omitted} chars omitted from {raw_path}; request this file alone if needed]"
                )
            chunk = f"## {raw_path}\n{excerpt}"
            remaining = limit - total
            if remaining <= 0:
                break
            if len(chunk) > remaining:
                marker = "\n... [retrieval truncated by total limit]"
                if remaining > len(marker):
                    chunk = chunk[: remaining - len(marker)] + marker
                else:
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
            try:
                relative = path.relative_to(workspace_root)
                relative_text = str(relative).replace("\\", "/")
                if is_excluded_path(relative_text):
                    continue
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
            try:
                workspace_relative = path.relative_to(self.target_workspace)
                workspace_relative_text = str(workspace_relative).replace("\\", "/")
            except ValueError:
                continue
            if is_excluded_path(workspace_relative_text):
                continue
            relative = path.relative_to(base)
            if len(relative.parts) > max_depth:
                continue
            lines.append(workspace_relative_text)
        return "\n".join(lines)

    def _resolve_target_relative_file(self, raw_path: str) -> tuple[Path | None, str]:
        relative_path = self._normalize_target_relative_path(raw_path)
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
        if self._get_implementation_execution_mode() == "multi_developer_json":
            scoped_allowed_paths = {
                self._normalize_repo_relative_path(path)
                for path in (selected_item.get("allowed_paths") or [])
                if self._normalize_repo_relative_path(path)
            }
            if scoped_allowed_paths:
                contract_paths = scoped_allowed_paths
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

    def _active_write_agent_name(self) -> str:
        agent_name = str(getattr(self, "_active_direct_api_agent_name", "") or "").strip()
        return agent_name or "developer"

    def _record_retrieval_tool_usage(self, phase: str, agent_name: str, tool_name: str, paths: list[str] | None = None) -> None:
        if not agent_name:
            return
        current = self._get_agent_report_extras(phase, agent_name)
        tools = list(current.get("retrieval_tools_used") or [])
        tools.append(tool_name)
        read_paths = list(current.get("read_paths") or [])
        if tool_name in {"read_file", "read_files"}:
            for path in paths or []:
                normalized = self._normalize_target_relative_path(path)
                if normalized:
                    read_paths.append(normalized)
        self._set_agent_report_extras(
            phase,
            agent_name,
            {
                "retrieval_tools_used": tools,
                "read_paths": sorted(dict.fromkeys(read_paths)),
                "files_read": sorted(dict.fromkeys(read_paths)),
            },
        )

    def _record_write_tool_usage(self, tool_name: str, path: str = "") -> None:
        agent_name = self._active_write_agent_name()
        current = self._get_agent_report_extras("implementation", agent_name)
        used = list(current.get("write_tools_used") or [])
        used.append(tool_name)
        write_paths = list(current.get("write_paths") or [])
        normalized_path = self._normalize_target_relative_path(path)
        if normalized_path:
            write_paths.append(normalized_path)
        self._set_agent_report_extras(
            "implementation",
            agent_name,
            {
                "write_tools_used": used,
                "write_paths": write_paths,
                "files_written": sorted(dict.fromkeys(write_paths)),
                "last_write_error": "",
            },
        )

    def _record_failed_write_attempt(self, tool_name: str, error_text: str) -> None:
        agent_name = self._active_write_agent_name()
        current = self._get_agent_report_extras("implementation", agent_name)
        failed_attempts = int(current.get("failed_write_attempts") or 0) + 1
        self._set_agent_report_extras(
            "implementation",
            agent_name,
            {
                "last_write_tool": tool_name,
                "last_write_error": error_text,
                "failed_write_attempts": failed_attempts,
            },
        )

    def _direct_api_write_file(self, path: str, content: str) -> str:
        allowed, detail, candidate = self._validate_direct_api_write_request(path, [content])
        if not allowed or candidate is None:
            self._record_failed_write_attempt("write_file", detail)
            return detail
        content, repaired = self._repair_escaped_file_content(path, content)
        if repaired:
            self.logger.warning(f"Repaired escaped newlines in write_file content for {path}")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(content, encoding="utf-8")
        self._record_write_tool_usage("write_file", detail)
        return f"Wrote file: {detail}"

    @staticmethod
    def _repair_escaped_file_content(path: str, content: str) -> tuple[str, bool]:
        """Repair file content where a model escaped newlines into the JSON string.

        Some models emit a write_file ``content`` value with literal ``\\n``/``\\t`` sequences
        and no real newlines, collapsing the whole file onto one physical line. That is valid
        JSON but produces a broken source file (``SyntaxError: unterminated triple-quoted
        string``). When a ``.py`` file has no real newline yet contains escaped newline
        sequences, decode the standard backslash escapes. This is done with an explicit
        character walk (not ``unicode_escape``) so UTF-8 text such as Cyrillic and emoji is
        preserved instead of being mangled.
        """
        if not str(path or "").strip().lower().endswith(".py"):
            return content, False
        if "\n" in content or "\r" in content:
            return content, False
        if "\\n" not in content and "\\r" not in content:
            return content, False
        mapping = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "'": "'", "\\": "\\", "/": "/"}
        out: list[str] = []
        index = 0
        length = len(content)
        while index < length:
            char = content[index]
            if char == "\\" and index + 1 < length and content[index + 1] in mapping:
                out.append(mapping[content[index + 1]])
                index += 2
                continue
            out.append(char)
            index += 1
        return "".join(out), True

    def _direct_api_apply_patch(self, path: str, search: str, replace: str) -> str:
        allowed, detail, candidate = self._validate_direct_api_write_request(path, [search, replace])
        if not allowed or candidate is None:
            self._record_failed_write_attempt("apply_patch", detail)
            return detail
        if not candidate.exists() or not candidate.is_file():
            self._record_failed_write_attempt("apply_patch", f"Target file does not exist: {detail}")
            return f"Target file does not exist: {detail}"
        try:
            original = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            self._record_failed_write_attempt("apply_patch", f"Unable to read target file: {detail}")
            return f"Unable to read target file: {detail}"
        if search not in original:
            self._record_failed_write_attempt("apply_patch", f"Search block not found in {detail}")
            return f"Search block not found in {detail}"
        updated = original.replace(search, replace, 1)
        candidate.write_text(updated, encoding="utf-8")
        self._record_write_tool_usage("apply_patch", detail)
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
            if phase == "implementation" and (agent_name == "developer" or self._is_multi_developer_edit_agent(agent_name)):
                max_turns = 6
            elif phase == "implementation" and agent_name == "qa":
                max_turns = 2
            else:
                max_turns = 3
        if message_bundle.get("strict_execution_mode"):
            max_turns = min(max_turns, 3)

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
        developer_feedback_text = str(message_bundle.get("developer_feedback_text") or "").strip()
        compact_console_prompt = self._is_compact_console_prompt_mode(agent_name, phase)
        if developer_feedback_text:
            feedback_lines = self._build_prompt_preview_lines(developer_feedback_text, limit=18) if compact_console_prompt else developer_feedback_text.splitlines()
            if compact_console_prompt:
                self.logger.operator_box(f"Feedback для retry -> {agent_name}", feedback_lines, color="magenta")
            else:
                self.logger.agent_progress(agent_name, "Developer repair feedback:")
                for line in feedback_lines:
                    self.logger.agent_progress(agent_name, line)
                self.logger.agent_progress(agent_name, "")
        if compact_console_prompt:
            report_path = self.logger.run_dir / "agents" / phase / f"{self.logger._safe_name(agent_name)}.md"
            self.logger.operator_box(
                f"Prompt сохранён -> {agent_name}",
                [
                    "Полный system prompt сохранён в agent report file.",
                    f"Файл: {report_path}",
                ],
                color="cyan",
            )
        else:
            self.logger.agent_progress(agent_name, "System prompt (raw):")
            for line in str(message_bundle["system_message"]).splitlines():
                self.logger.agent_progress(agent_name, line)
            self.logger.agent_progress(agent_name, "")
        user_task_lines = str(message_bundle["user_message"]).splitlines() or [""]
        if compact_console_prompt:
            self.logger.operator_box(f"User task -> {agent_name}", user_task_lines, color="cyan")
            self.logger.operator_box(
                f"Ожидание ответа -> {agent_name}",
                ["direct_api запрос отправлен, ждём ответ модели."],
                color="yellow",
            )
        else:
            self.logger.agent_progress(agent_name, "User task (raw):")
            for line in user_task_lines:
                self.logger.agent_progress(agent_name, line)
            self.logger.agent_progress(agent_name, "")
            self.logger.agent_progress(agent_name, "Waiting for direct_api response...")

        started_at = time.monotonic()
        last_raw_body = ""
        response_payload: dict[str, Any] | None = None
        retrieval_rounds = 0
        retrieval_operations_used = 0
        read_file_operations_used = 0
        blocked_retrieval_count = 0
        developer_performed_write = False
        developer_base_retrieval_limit = 4
        self._active_direct_api_agent_name = agent_name
        if phase == "implementation" and (agent_name == "developer" or self._is_multi_developer_edit_agent(agent_name)):
            self._set_agent_report_extras(
                "implementation",
                agent_name,
                {
                    "write_tools_used": [],
                    "write_paths": [],
                    "developer_changed_files": [],
                    "developer_diff_lines": 0,
                    "no_changes_detected": False,
                    "failed_write_attempts": 0,
                    "last_write_error": "",
                    "last_write_tool": "",
                },
            )
        try:
            for turn in range(1, max_turns + 1):
                max_tokens = self._direct_api_max_tokens(phase, agent_name)
                request_payload = {
                    "model": normalized_model,
                    "messages": messages,
                    "temperature": 0.2,
                }
                if max_tokens is not None:
                    request_payload["max_tokens"] = max_tokens
                _status_code, raw_body = self._perform_direct_api_request(request_payload, api_key, timeout)
                last_raw_body = raw_body
                response_payload = json.loads(raw_body)
                output_text = self._extract_direct_api_text(response_payload)
                retrieval_request = self._parse_direct_api_retrieval_request(output_text)
                if not retrieval_request:
                    break
                tool_name = str(retrieval_request.get("tool") or "")
                retrieval_budget_check = self._evaluate_retrieval_budget(
                    message_bundle,
                    tool_name,
                    retrieval_operations_used,
                    read_file_operations_used,
                )
                message_bundle["retrieval_budget_remaining"] = retrieval_budget_check["remaining"]
                if not retrieval_budget_check["allowed"]:
                    reason = str(retrieval_budget_check["reason"])
                    message_bundle["retrieval_limit_reason"] = reason
                    message_bundle["retrieval_hard_stop_triggered"] = True
                    self._set_agent_report_extras(
                        phase,
                        agent_name,
                        {
                            "retrieval_budget_remaining": retrieval_budget_check["remaining"],
                            "retrieval_limit_reason": reason,
                            "retrieval_hard_stop_triggered": True,
                            "blocked_retrieval_tool": tool_name,
                        },
                    )
                    feedback = self._build_strict_retrieval_feedback(agent_name, tool_name, reason, message_bundle)
                    if phase == "implementation":
                        self._save_feedback(self.task_counter, agent_name, feedback)
                        self._save_feedback(self.task_counter, "developer", feedback)
                        self._phase_failure_status = "strict_retrieval_blocked"
                    elapsed = time.monotonic() - started_at
                    stdout_payload = {
                        "output_text": feedback,
                        "model": str(response_payload.get("model") or normalized_model),
                        "provider": "openrouter",
                    }
                    stdout = json.dumps(stdout_payload, ensure_ascii=False)
                    self.logger.error(
                        f"Strict execution blocked retrieval: {agent_name}",
                        f"tool={tool_name} | reason={reason}",
                    )
                    save_agent_report("strict_retrieval_blocked", reason, elapsed, stdout, "", feedback, command, 0)
                    self.logger.agent_end(agent_name, "strict_retrieval_blocked", reason)
                    return False
                retrieval_output = self._execute_direct_api_retrieval_request(retrieval_request, phase=phase, agent_name=agent_name)
                if tool_name in {"read_file", "read_files", "search_text", "list_files"}:
                    retrieval_operations_used += 1
                    if tool_name in {"read_file", "read_files"}:
                        read_file_operations_used += 1
                if message_bundle.get("retrieval_budget") is not None:
                    message_bundle["retrieval_budget_remaining"] = max(
                        0,
                        int(message_bundle.get("retrieval_budget") or 0) - retrieval_operations_used,
                    )
                if (
                    phase == "implementation"
                    and agent_name == "developer"
                    and tool_name in {"write_file", "apply_patch", "tool_batch"}
                ):
                    current_write_tools = self._get_agent_report_extras("implementation", "developer").get("write_tools_used") or []
                    if current_write_tools or str(retrieval_output or "").startswith(("Wrote file:", "Patched file:")):
                        developer_performed_write = True
                retrieval_rounds = turn
                message_bundle["retrieval_rounds"] = retrieval_rounds
                self.logger.agent_progress(
                    agent_name,
                    f"Direct API retrieval turn {turn}: {retrieval_request.get('tool')}",
                )
                if (
                    phase == "implementation"
                    and self._is_multi_developer_edit_agent(agent_name)
                    and str(retrieval_request.get("tool") or "") in {"write_file", "apply_patch", "tool_batch"}
                    and ("Wrote file:" in str(retrieval_output or "") or "Patched file:" in str(retrieval_output or ""))
                ):
                    response_payload = {
                        "choices": [{"message": {"content": "status=implemented"}}],
                        "model": normalized_model,
                    }
                    break
                if (
                    phase == "implementation"
                    and agent_name == "developer"
                    and str(retrieval_request.get("tool") or "") in {"search_text", "list_files"}
                ):
                    blocked_retrieval_count += 1
                    messages.append(
                        {
                            "role": "user",
                            "content": self._build_developer_exact_path_repair_instruction(),
                        }
                    )
                if (
                    phase == "implementation"
                    and agent_name == "qa"
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
                if turn >= max_turns and self._should_force_final_after_retrieval_limit(phase, agent_name):
                    messages.append(
                        {
                            "role": "user",
                            "content": self._build_retrieval_limit_final_instruction(phase, agent_name),
                        }
                    )
                    final_payload = {
                        "model": normalized_model,
                        "messages": messages,
                        "temperature": 0.2,
                    }
                    final_max_tokens = self._direct_api_max_tokens(phase, agent_name)
                    if final_max_tokens is not None:
                        final_payload["max_tokens"] = final_max_tokens
                    _status_code, final_raw_body = self._perform_direct_api_request(final_payload, api_key, timeout)
                    last_raw_body = final_raw_body
                    response_payload = json.loads(final_raw_body)
                    final_output_text = self._extract_direct_api_text(response_payload)
                    if self._parse_direct_api_retrieval_request(final_output_text):
                        response_payload = {
                            "choices": [{"message": {"content": "Exceeded retrieval rounds.\n\nRussian translation\nПревышен лимит раундов retrieval."}}],
                            "model": normalized_model,
                        }
                        message_bundle["retrieval_rounds"] = max_turns
                    break
                developer_extras = self._get_agent_report_extras("implementation", "developer")
                write_tools_used = developer_extras.get("write_tools_used") or []
                failed_write_attempts = int(developer_extras.get("failed_write_attempts") or 0)
                last_write_error = str(developer_extras.get("last_write_error") or "").strip()
                if (
                    phase == "implementation"
                    and agent_name == "developer"
                    and turn >= 3
                    and not write_tools_used
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
                    and str(retrieval_request.get("tool") or "") in {"write_file", "apply_patch", "tool_batch"}
                    and not write_tools_used
                    and last_write_error
                ):
                    messages.append(
                        {
                            "role": "user",
                            "content": self._build_developer_write_repair_instruction(last_write_error, max_turns - turn),
                        }
                    )
                if (
                    phase == "implementation"
                    and agent_name == "developer"
                    and not write_tools_used
                    and (blocked_retrieval_count >= 2 or turn >= developer_base_retrieval_limit)
                ):
                    if blocked_retrieval_count == 1 and turn < max_turns:
                        continue
                    if failed_write_attempts > 0 and turn < max_turns:
                        continue
                    if failed_write_attempts > 0:
                        developer_failure_text = (
                            "status=no_changes\n"
                            "Developer exhausted the write repair budget after validation errors. "
                            f"Last validator error: {last_write_error}\n\n"
                            "Russian translation\n"
                            "status=no_changes\n"
                            "Разработчик исчерпал бюджет repair-попыток после ошибок валидации записи. "
                            f"Последняя ошибка валидатора: {last_write_error}"
                        )
                    else:
                        developer_failure_text = (
                            "status=no_changes\n"
                            "Developer exceeded the allowed retrieval policy without making a scoped file edit. "
                            "Use only read_file/read_files for exact contract paths, then write the target/test file or report a concrete scope gap.\n\n"
                            "Russian translation\n"
                            "status=no_changes\n"
                            "Разработчик превысил допустимую политику retrieval, не выполнив scoped-изменение файла. "
                            "Используй только read_file/read_files для точных путей из контракта, затем запиши target/test файл или сообщи конкретную нехватку scope."
                        )
                    response_payload = {
                        "choices": [
                            {
                                "message": {
                                    "content": developer_failure_text
                                }
                            }
                        ],
                        "model": normalized_model,
                    }
                    break
                if (
                    phase == "implementation"
                    and agent_name == "qa"
                    and (blocked_retrieval_count >= 1 or turn >= 2)
                ):
                    response_payload = {
                        "choices": [
                            {
                                "message": {
                                    "content": "Превышен лимит раундов retrieval."
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
            if compact_console_prompt:
                self.logger.operator_box(
                    f"Ответ агента -> {agent_name}",
                    self._build_prompt_preview_lines(parsed_output, limit=24),
                    color="white",
                )
            else:
                self.logger.agent_progress(agent_name, "")
                self.logger.agent_progress(agent_name, "Agent response:")
                for line in parsed_output.splitlines():
                    self.logger.agent_progress(agent_name, line)

        failure_reason = self._detect_agent_failure(stdout, "", parsed_output, require_translation=False)
        if failure_reason:
            failure_status = self._classify_failure_status(failure_reason)
            self.logger.error(
                f"Agent returned invalid direct_api output: {agent_name}",
                f"elapsed_s={elapsed:.2f} | detected_failure={failure_reason} | stdout_tail={self._tail_text(stdout)}",
            )
            save_agent_report(failure_status, failure_reason, elapsed, stdout, "", parsed_output, command, 0)
            self.logger.agent_end(agent_name, failure_status, failure_reason)
            return False
        contract_failure = self._detect_agent_output_contract_failure(phase, agent_name, parsed_output)
        if contract_failure:
            self.logger.error(
                f"Agent returned invalid direct_api output: {agent_name}",
                f"elapsed_s={elapsed:.2f} | detected_failure={contract_failure} | stdout_tail={self._tail_text(stdout)}",
            )
            save_agent_report("invalid_output", contract_failure, elapsed, stdout, "", parsed_output, command, 0)
            self.logger.agent_end(agent_name, "invalid_output", contract_failure)
            return False

        if phase == "implementation" and agent_name == "qa":
            qa_verdict = self._extract_qa_verdict(parsed_output)
            if qa_verdict == "failed":
                result = "qa reported regressions"
                save_agent_report("qa_failed", result, elapsed, stdout, "", parsed_output, command, 0)
                self.logger.agent_end(agent_name, "qa_failed", result)
                self._phase_failure_status = "qa_failed"
                self._log_retry_outcome_summary(
                    "Human summary (RU)",
                    [
                        "QA нашёл регрессии или несоответствия контракту.",
                        "Следующий шаг: сформировать repair-feedback и вернуть задачу в developer.",
                        "Подробности смотри в сохранённом qa.md и в следующем developer feedback.",
                    ],
                )
                return False

        if phase == "implementation" and agent_name == "developer":
            developer_extras = self._get_agent_report_extras("implementation", "developer")
            write_tools_used = developer_extras.get("write_tools_used", [])
            normalized_output = str(parsed_output or "").strip()
            normalized_output_lower = normalized_output.lower()
            if not write_tools_used and not developer_performed_write and normalized_output_lower.startswith("status=implemented"):
                messages.append({"role": "assistant", "content": output_text})
                messages.append(
                    {
                        "role": "user",
                        "content": self._build_developer_protocol_repair_instruction(),
                    }
                )
                repair_payload = {
                    "model": normalized_model,
                    "messages": messages,
                    "temperature": 0.2,
                }
                repair_max_tokens = self._direct_api_max_tokens(phase, agent_name)
                if repair_max_tokens is not None:
                    repair_payload["max_tokens"] = repair_max_tokens
                _status_code, repair_raw_body = self._perform_direct_api_request(repair_payload, api_key, timeout)
                repair_response_payload = json.loads(repair_raw_body)
                repair_output_text = self._extract_direct_api_text(repair_response_payload)
                repair_request = self._parse_direct_api_retrieval_request(repair_output_text)
                if repair_request:
                    repair_result = self._execute_direct_api_retrieval_request(repair_request, phase=phase, agent_name=agent_name)
                    self.logger.agent_progress(agent_name, "Direct API developer repair turn: " + str(repair_request.get("tool")))
                    messages.append({"role": "assistant", "content": repair_output_text})
                    messages.append({"role": "user", "content": "Local retrieval result:\n" + (repair_result or "No matching local results.")})
                    developer_extras = self._get_agent_report_extras("implementation", "developer")
                    write_tools_used = developer_extras.get("write_tools_used", [])
                    if write_tools_used:
                        parsed_output = "status=implemented"
                        stdout_payload["output_text"] = parsed_output
                        stdout = json.dumps(stdout_payload, ensure_ascii=False)
                        normalized_output = parsed_output
                        normalized_output_lower = parsed_output.lower()
                    else:
                        normalized_output = str(repair_output_text or "").strip()
                        normalized_output_lower = normalized_output.lower()
                        parsed_output = normalized_output or "status=no_changes: protocol_violation"
                        stdout_payload["output_text"] = parsed_output
                        stdout = json.dumps(stdout_payload, ensure_ascii=False)
                else:
                    parsed_output = str(repair_output_text or "").strip()
                    stdout_payload["output_text"] = parsed_output
                    stdout = json.dumps(stdout_payload, ensure_ascii=False)
                    normalized_output = parsed_output
                    normalized_output_lower = normalized_output.lower()
            if write_tools_used or developer_performed_write:
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

    def _build_implementation_scope_instruction(self, selected_scope: str, agent_name: str = "") -> str:
        lines = [
            "User goal:",
            str(self.user_goal or "not specified"),
            "",
            "Selected implementation scope:",
            selected_scope,
            "",
        ]
        multi_developer_agent = self._is_multi_developer_edit_agent(agent_name)
        if self._selected_implementation_item and self._selected_implementation_item.get("allowed_paths"):
            if multi_developer_agent:
                lines.append("Agent-scoped allowed files for this invocation:")
            else:
                lines.append("Allowed files for the selected task:")
            lines.extend(f"- {path}" for path in self._selected_implementation_item["allowed_paths"])
            lines.append("")
            if multi_developer_agent:
                lines.extend(
                    [
                        "Agent-scoped write boundary:",
                        "- Write only the files listed directly above.",
                        "- Treat every other path mentioned in repository context, resume handoff, planner output, or sibling agent reports as read-only context.",
                        "- If another file is needed, leave it for the matching multi-developer agent and return status=no_changes with the missing-path reason.",
                        "",
                    ]
                )
        lines.extend(
            [
            "Implementation guardrails:",
            "- Use repo_map as the source of truth for real repository paths.",
            "- Do not implement marketplace.",
            "- Do not change Stripe or billing flows.",
            "- Do not make broad frontend changes.",
            "- Prefer small backend-first changes.",
            "- Frontend changes are limited to API client stubs only if required.",
            "- Inspect existing target files with read_file before editing them.",
            "- For a new file that does not exist yet, do not try to read it; create it directly with write_file.",
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
        if phase == "implementation":
            report_status = str(normalized.get("status") or "")
            self._update_human_report(final_status=report_status if report_status != "success" else "in_progress", agent_name=agent_name)

    @staticmethod
    def _normalize_repo_relative_path(value: str) -> str:
        cleaned = str(value or "").strip().strip("`'\"()[]{}:;,")
        cleaned = cleaned.replace("\\", "/")
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        return cleaned.strip("/")

    def _normalize_target_relative_path(self, value: str) -> str:
        normalized = self._normalize_repo_relative_path(value)
        if not normalized:
            return ""
        workspace = self._normalize_repo_relative_path(str(self.target_workspace))
        normalized_lower = normalized.lower()
        workspace_lower = workspace.lower()
        if normalized_lower == workspace_lower:
            return ""
        if workspace_lower and normalized_lower.startswith(workspace_lower + "/"):
            return normalized[len(workspace) + 1 :].strip("/")
        return normalized

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
        effective_forbidden = self._effective_forbidden_paths()
        # An empty allow-list means "no explicit allow constraint" (only the forbidden fence
        # applies), not "forbid everything". Real runs always carry the selected task's
        # allowed_paths; this keeps a project that declares no global allow-list workable.
        enforce_allowlist = bool(allowed_patterns)
        allowed_paths_matched: list[str] = []
        forbidden_hits: list[str] = []
        violations: list[str] = []
        for path in normalized_paths:
            if self._path_matches_any(path, effective_forbidden):
                forbidden_hits.append(f"path:{path}")
                violations.append(path)
                continue
            if not enforce_allowlist or self._path_matches_any(path, allowed_patterns):
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
        # When the backlog comes from the canonical state file, implementation-planner is
        # intentionally skipped, so its per-run report is absent. The selected task itself
        # carries the allowed/forbidden paths needed to validate scope, so a missing planner
        # report is only fatal when there is no canonical backlog to fall back on.
        canonical_backlog = self._load_canonical_implementation_backlog()
        if not self._selected_implementation_item or (not planner_report and not canonical_backlog):
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
        status_output = self._run_local_capture(["git", "status", "--porcelain", "-uall"], timeout=10, cwd=self.target_workspace)
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

    @staticmethod
    def _developer_no_changes_claims_completion(reason: str) -> bool:
        lowered = str(reason or "").strip().lower()
        if not lowered:
            return False
        completion_markers = [
            "already complete",
            "already implemented",
            "already exists",
            "no modifications are needed",
            "implementation is already complete",
            "implementation is complete",
            "satisfy all contract requirements",
            "satisfies all contract requirements",
            "already satisfies",
            "no changes needed",
            "nothing to change",
            "already valid",
            "no-op, already valid",
        ]
        return any(marker in lowered for marker in completion_markers)

    def _run_developer_no_change_validation(self, reason: str) -> bool:
        item = self._selected_implementation_item or {}
        relevant_paths: list[str] = []
        target_file = item.get("target_file") or {}
        test_file = item.get("test_file") or {}
        if isinstance(target_file, dict):
            normalized = self._normalize_repo_relative_path(target_file.get("path"))
            if normalized:
                relevant_paths.append(normalized)
        if isinstance(test_file, dict):
            normalized = self._normalize_repo_relative_path(test_file.get("path"))
            if normalized:
                relevant_paths.append(normalized)
        for path in (item.get("required_test_paths") or []):
            normalized = self._normalize_repo_relative_path(path)
            if normalized:
                relevant_paths.append(normalized)
        python_paths = [path for path in sorted(set(relevant_paths)) if path.endswith(".py")]

        findings: list[str] = []
        contract_diag = self._evaluate_selected_task_contract_compliance()
        if not contract_diag.get("contract_compliance", False):
            findings.append("contract_compliance check failed for developer no_changes claim")
            missing_must_contain = list(contract_diag.get("missing_must_contain") or [])
            if missing_must_contain:
                findings.append("missing_must_contain: " + ", ".join(missing_must_contain))
            if contract_diag.get("missing_test_file", False):
                findings.append("missing_test_file: selected test_file is missing")
            forbidden_hits = list(contract_diag.get("forbidden_contract_hits") or [])
            if forbidden_hits:
                findings.append("forbidden_contract_hits: " + ", ".join(forbidden_hits))

        if python_paths:
            returncode, stdout, stderr = self._run_local_command(
                [sys.executable, "-m", "py_compile", *python_paths],
                timeout=30,
                cwd=self.target_workspace,
            )
            if returncode != 0:
                findings.append("py_compile failed for developer no_changes validation")
                findings.append(stderr or stdout or "unknown py_compile failure")

        selected_test_path = ""
        if isinstance(test_file, dict):
            selected_test_path = self._normalize_repo_relative_path(test_file.get("path"))
        if selected_test_path:
            candidate = self.target_workspace / selected_test_path
            if not candidate.exists() or not candidate.is_file():
                findings.append(f"selected test_file is missing: {selected_test_path}")
            elif selected_test_path.endswith(".py"):
                selected_python, selected_python_source, pytest_available = self.resolve_python_executable()
                self.logger.info(f"selected_python={selected_python}")
                self.logger.info(f"selected_python_source={selected_python_source}")
                self.logger.info(f"pytest_available={pytest_available}")
                if not pytest_available:
                    findings.append("pytest is not available in the resolved Python environment")
                    findings.append(f"selected_python={selected_python}")
                    findings.append(f"selected_python_source={selected_python_source}")
                    selected_test_path = ""
                else:
                    returncode, stdout, stderr = self._run_local_command(
                        [selected_python, "-m", "pytest", selected_test_path],
                        timeout=60,
                        cwd=self.target_workspace,
                    )
                    if returncode != 0:
                        findings.append(f"pytest failed for selected test file: {selected_test_path}")
                        findings.append(stderr or stdout or "unknown pytest failure")

        findings.extend(self._validate_changed_migration_files(python_paths))

        if not findings:
            self._save_developer_checks_report(
                "success",
                "developer no_changes claim passed deterministic validation",
                "developer no_changes claim passed deterministic validation",
            )
            return True

        parsed_output = "\n".join(findings)
        self._save_developer_checks_report(
            "failed",
            "developer no_changes claim failed deterministic validation",
            parsed_output,
        )
        feedback = "\n\n".join(
            [
                "Developer no_changes claim is not valid",
                f"Original reason: {reason}",
                "",
                "Deterministic validation findings",
                parsed_output,
            ]
        )
        feedback_file = self._save_feedback(self.task_counter or 0, "developer", feedback)
        self._developer_feedback_file = str(feedback_file)
        self._developer_feedback_source = str(feedback_file)
        self._developer_feedback_chars = len(feedback)
        self._implementation_retry_from_agent = "developer"
        self._phase_failure_status = "developer_checks_failed"
        self.logger.error("Developer no_changes claim failed deterministic validation", parsed_output)
        self._log_retry_outcome_summary(
            "Human summary (RU)",
            [
                "developer заявил, что правки не нужны, но детерминированная проверка это опровергла.",
                "Что сломалось: " + (findings[0] if findings else "см. feedback файл"),
                f"Следующий шаг: retry developer с feedback из {feedback_file}",
            ],
        )
        return False

    def _enforce_implementation_scope_diff(self) -> bool:
        diagnostics = self._collect_scope_watchdog_diff_diagnostics()
        existing_developer = self._load_saved_agent_report("implementation", "developer") or {}
        if self._get_implementation_execution_mode() == "multi_developer_json":
            synthetic_changed_files = [
                self._normalize_repo_relative_path(path)
                for path in (existing_developer.get("developer_changed_files") or [])
                if self._normalize_repo_relative_path(path)
            ]
            synthetic_write_tools = list(existing_developer.get("write_tools_used") or [])
            if synthetic_changed_files or synthetic_write_tools:
                diagnostics["changed_files"] = sorted(dict.fromkeys([*diagnostics.get("changed_files", []), *synthetic_changed_files]))
                diagnostics["changed_files_count"] = len(diagnostics["changed_files"])
                diagnostics["write_tools_used"] = synthetic_write_tools
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
        if existing_developer:
            self._overwrite_agent_report("implementation", "developer", {**existing_developer, **diagnostics})
        if diagnostics["changed_files_count"] == 0:
            developer_report = self._load_saved_agent_report("implementation", "developer") or {}
            developer_reason = str(developer_report.get("parsed_output") or developer_report.get("result") or "").strip()
            if self._developer_no_changes_claims_completion(developer_reason):
                return self._run_developer_no_change_validation(developer_reason)
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
        research_context_limit = 2500 if agent_name == "implementation-planner" else 5000
        research_context = self._build_implementation_research_context(research_reports, agent_name, limit=research_context_limit)
        previous_parts = [part for part in [research_context, same_phase_context] if part.strip()]
        previous_context = "\n\n".join(previous_parts)
        repo_map = self._load_repo_map()

        if agent_name == "implementation-planner":
            sections = [
                ("User goal", self._build_user_goal_summary()),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1200)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1200)),
                ("Selected implementation scope", selected_scope),
                ("Repo map summary", self._build_repo_map_summary(repo_map=repo_map, agent_name=agent_name, limit=1600)),
                ("Target README and docs", self._build_target_docs_excerpts(limit=900)),
                ("Target dependency and config files", self._build_target_dependency_context(limit=1200)),
                ("Target top-level tree up to depth 3", self._build_top_level_tree(root=self.target_workspace, depth=3)),
            ]
        else:
            sections = [
                ("User goal", self._build_user_goal_summary()),
                ("Shared Codex project context", self._build_project_codex_context_summary(limit=1600)),
                ("Shared resume handoff", self._build_project_resume_context_summary(limit=1600)),
                ("Selected implementation scope", selected_scope),
                ("Repo map summary", self._build_repo_map_summary(repo_map=repo_map, agent_name=agent_name, limit=2600)),
                ("Target README and docs", self._build_target_docs_excerpts(limit=2000)),
                ("Target dependency and config files", self._build_target_dependency_context(limit=2200)),
                ("Target top-level tree up to depth 4", self._build_top_level_tree(root=self.target_workspace, depth=4)),
            ]
        edit_agent = agent_name == "developer" or self._is_multi_developer_edit_agent(agent_name)
        if agent_name == "task-designer" or edit_agent:
            ground_truth = self._build_migration_ground_truth_note()
            if ground_truth:
                insert_at = next(
                    (idx + 1 for idx, (title, _) in enumerate(sections) if title == "Selected implementation scope"),
                    len(sections),
                )
                sections.insert(insert_at, ("Migration ground truth", ground_truth))
        # Ground the planning and edit agents in the codebase's real DB concurrency model and
        # session-injection pattern so contracts are achievable (e.g. no async DB demands on a
        # synchronous SQLAlchemy stack). Placed right after the scope so it is never starved by
        # lower-value sections when the joined context is capped.
        if (
            agent_name in {"architect", "implementation-planner", "task-designer", "qa", "template-validator"}
            or edit_agent
        ):
            architecture_note = self._build_architecture_ground_truth_note()
            if architecture_note:
                insert_at = next(
                    (idx + 1 for idx, (title, _) in enumerate(sections) if title == "Selected implementation scope"),
                    len(sections),
                )
                sections.insert(insert_at, ("Backend architecture ground truth", architecture_note))
        if (
            agent_name in {"task-designer", "developer", "qa", "template-validator"}
            or self._is_multi_developer_edit_agent(agent_name)
        ):
            scoped_excerpts = self._build_selected_task_file_excerpts(limit=4200)
            if scoped_excerpts:
                if edit_agent:
                    # Place the file(s) under edit right after the scope so they are never
                    # starved by lower-value sections (README, dependency files, directory
                    # tree) when the joined context is capped.
                    insert_at = next(
                        (idx + 1 for idx, (title, _) in enumerate(sections) if title == "Selected implementation scope"),
                        len(sections),
                    )
                    sections.insert(insert_at, ("Selected task file excerpts", scoped_excerpts))
                else:
                    sections.append(("Selected task file excerpts", scoped_excerpts))
        if agent_name == "implementation-planner":
            sections.extend(
                [
                    ("Existing file list", self._build_target_existing_file_list(limit=1800)),
                    ("Relevant implementation files", self._build_relevant_implementation_file_list(limit=1500)),
                    ("Relevant implementation file excerpts", self._build_relevant_implementation_file_excerpts(limit=1200)),
                ]
            )
        if agent_name in {"architect", "qa", "template-validator"}:
            sections.append(("Target tests list", self._build_target_tests_file_list(limit=1500)))
        if agent_name == "qa":
            diff_excerpt = self._build_target_git_diff_excerpt(limit=4000)
            if diff_excerpt:
                sections.append(("Target git diff", diff_excerpt))
            sections.append(("Repo map before/after summary", self._build_repo_map_delta_summary(limit=2200)))

        context_limit = max(limit, 20000) if edit_agent else limit
        repository_context = self._join_context_sections(sections, limit=context_limit)
        contract_diag = self._evaluate_selected_task_contract_compliance() if agent_name in {"qa", "template-validator"} else {}
        sources = [
            f"{run_dir.name}:{report.get('agent_name') or report.get('agent')}"
            for report in research_reports
            if report.get("status") == "success"
        ]
        completed_task_ids = self._completed_implementation_task_ids()
        return {
            "repository_context": repository_context,
            "previous_context": previous_context,
            "research_handoff_sources": sources,
            "selected_task_scope": selected_scope,
            "user_goal": str(self.user_goal or ""),
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
            "canonical_backlog_loaded": self._canonical_backlog_loaded,
            "canonical_backlog_path": str(self.canonical_backlog_path),
            "completed_task_registry_path": str(self.completed_tasks_path),
            "completed_task_count": len(completed_task_ids),
            "completed_task_ids": completed_task_ids,
            "selected_task_source": self._selected_task_source,
            "selected_task_from_explicit_cli": self._selected_task_from_explicit_cli,
            "backlog_selected_task_id": self._backlog_selected_task_id,
            "skipped_completed_task_ids": list(self._skipped_completed_task_ids),
            "completed_task_recorded": self._completed_task_recorded,
            "completed_task_record_error": self._completed_task_record_error,
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

        reference_set = {
            self._normalize_repo_relative_path(path)
            for path in item.get("reference_files") or []
        }
        new_file_set = {
            self._normalize_repo_relative_path(path)
            for path in item.get("new_files") or []
        }

        # Files the current agent must EDIT and that already exist are injected in full.
        # If such a file is truncated, a developer using write_file rewrites it from a
        # partial view and silently destroys the classes that were cut from the excerpt.
        editable_existing: list[str] = []

        def _add_editable(raw_path: str, *, allow_reference: bool) -> None:
            normalized = self._normalize_repo_relative_path(raw_path)
            if not normalized or normalized in new_file_set or normalized in editable_existing:
                return
            if not allow_reference and normalized in reference_set:
                return
            candidate = (self.target_workspace / normalized).resolve()
            if candidate.exists() and candidate.is_file():
                editable_existing.append(normalized)

        # Declared write targets are always shown in full, even when the contract also
        # (redundantly) lists them among reference_files — otherwise the file the agent
        # edits would be truncated and rewritten from a partial view.
        for key in ("target_file", "test_file"):
            spec = item.get(key)
            if isinstance(spec, dict):
                _add_editable(spec.get("path") or "", allow_reference=True)
        # Other allowed, non-reference, existing files are editable too.
        for path in item.get("allowed_paths") or []:
            _add_editable(path, allow_reference=False)

        primary_excerpt = ""
        if editable_existing:
            # Generous budget so editable target files are never truncated by the shared limit.
            primary_excerpt = self._direct_api_read_files(editable_existing, limit=20000)
            if primary_excerpt:
                sections.append(primary_excerpt)

        candidate_paths: list[str] = []
        for key in ("existing_paths", "required_test_paths", "reference_files"):
            for path in item.get(key) or []:
                normalized = self._normalize_repo_relative_path(path)
                if normalized and normalized not in editable_existing:
                    candidate_paths.append(normalized)
        ordered_paths = list(dict.fromkeys(candidate_paths))
        if ordered_paths:
            remaining = max(1200, limit - len(primary_excerpt))
            file_excerpt = self._direct_api_read_files(ordered_paths, limit=remaining)
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
        # Preserve the full editable target file (placed first); only bound the remainder.
        return text[: len(primary_excerpt) + max(limit, 1200)]

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
        if agent_name in {"developer", "qa", "template-validator"} or self._is_multi_developer_edit_agent(agent_name):
            return self._build_selected_task_contract_context(limit=limit)
        return self._build_previous_agent_context("implementation", agent_name, limit=limit)

    @staticmethod
    def _contract_requirement_present(text: str, requirement: str) -> bool:
        requirement = str(requirement or "").strip()
        if not requirement:
            return True
        if requirement in text:
            return True
        if requirement == 'revision = "<non-empty string>"':
            return bool(re.search(r'^revision\s*=\s*[\'"][^\'"]+[\'"]', text, re.MULTILINE))
        assignment_match = re.fullmatch(r"(revision|down_revision)\s*=\s*['\"]([^'\"]+)['\"]", requirement)
        if assignment_match:
            name, expected = assignment_match.groups()
            return bool(
                re.search(
                    rf"^{re.escape(name)}\s*=\s*['\"]{re.escape(expected)}['\"]",
                    text,
                    re.MULTILINE,
                )
            )
        if requirement == "op.create_index(":
            return WorkflowOrchestrator._contract_has_ast_call(text, "op.create_index")

        op_match = re.match(r"op\.(create_table|drop_table)\(\s*['\"]([^'\"]+)['\"]", requirement)
        if op_match:
            function_name, first_arg = op_match.groups()
            return WorkflowOrchestrator._contract_has_ast_call(
                text,
                f"op.{function_name}",
                first_arg=first_arg,
            )

        column_match = re.match(
            r"sa\.Column\(\s*['\"]([^'\"]+)['\"]\s*,\s*sa\.([A-Za-z_][A-Za-z0-9_]*)\s*\(",
            requirement,
        )
        if column_match:
            column_name, type_name = column_match.groups()
            nullable: bool | None = None
            if "nullable=False" in requirement.replace(" ", ""):
                nullable = False
            elif "nullable=True" in requirement.replace(" ", ""):
                nullable = True
            return WorkflowOrchestrator._contract_has_sqlalchemy_column(
                text,
                column_name=column_name,
                type_name=type_name,
                nullable=nullable,
            )
        return False

    @staticmethod
    def _contract_ast_call_name(node: ast.AST) -> str | None:
        if isinstance(node, ast.Call):
            return WorkflowOrchestrator._contract_ast_call_name(node.func)
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = WorkflowOrchestrator._contract_ast_call_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return None

    @staticmethod
    def _contract_ast_literal(node: ast.AST | None) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        return None

    @staticmethod
    def _contract_has_ast_call(text: str, call_name: str, first_arg: str | None = None) -> bool:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if WorkflowOrchestrator._contract_ast_call_name(node.func) != call_name:
                continue
            if first_arg is None:
                return True
            if node.args and WorkflowOrchestrator._contract_ast_literal(node.args[0]) == first_arg:
                return True
        return False

    @staticmethod
    def _contract_has_sqlalchemy_column(text: str, column_name: str, type_name: str, nullable: bool | None = None) -> bool:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if WorkflowOrchestrator._contract_ast_call_name(node.func) != "sa.Column":
                continue
            if len(node.args) < 2:
                continue
            if WorkflowOrchestrator._contract_ast_literal(node.args[0]) != column_name:
                continue
            if WorkflowOrchestrator._contract_ast_call_name(node.args[1]) != f"sa.{type_name}":
                continue
            if nullable is None:
                return True
            for keyword in node.keywords:
                if keyword.arg == "nullable" and WorkflowOrchestrator._contract_ast_literal(keyword.value) is nullable:
                    return True
        return False

    def _extract_migration_schema(self, item: dict[str, Any]) -> tuple[bool, dict[str, dict[str, Any]], dict[str, Any]]:
        """Per-table schema + metadata parsed from in-scope migration files (ground truth).

        Returns (found_existing_migration, schema, meta), where schema[table] = {
            "columns": [{"name", "type", "primary_key", "nullable"}],
            "primary_key": [column names],
            "indexes": [[column names], ...],
        } and meta = {"revision", "down_revision", "downgrade_drop_tables",
        "downgrade_drop_index_count"}. Migrations declared as new_files are excluded
        (they do not exist yet), so the result is authoritative reality the agents must
        match rather than invent.
        """
        new_files = {self._normalize_repo_relative_path(path) for path in item.get("new_files") or []}
        scope_paths: list[str] = []
        for key in ("allowed_paths", "existing_paths", "reference_files"):
            for path in item.get(key) or []:
                normalized = self._normalize_repo_relative_path(path)
                if normalized:
                    scope_paths.append(normalized)
        schema: dict[str, dict[str, Any]] = {}
        meta: dict[str, Any] = {
            "revision": None,
            "down_revision": None,
            "downgrade_drop_tables": [],
            "downgrade_drop_index_count": 0,
        }
        found = False
        for rel in dict.fromkeys(scope_paths):
            if "/alembic/versions/" not in rel or not rel.endswith(".py") or rel in new_files:
                continue
            candidate = (self.target_workspace / rel).resolve()
            if not candidate.exists() or not candidate.is_file():
                continue
            try:
                tree = ast.parse(candidate.read_text(encoding="utf-8", errors="replace"))
            except (OSError, SyntaxError):
                continue
            found = True
            for stmt in tree.body:
                if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Constant):
                    for target in stmt.targets:
                        if isinstance(target, ast.Name) and target.id in {"revision", "down_revision"}:
                            meta[target.id] = stmt.value.value
            downgrade_fn = next(
                (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "downgrade"), None
            )
            if downgrade_fn is not None:
                for node in ast.walk(downgrade_fn):
                    if not isinstance(node, ast.Call):
                        continue
                    drop_name = self._contract_ast_call_name(node.func)
                    if drop_name in {"op.drop_table", "drop_table"} and node.args:
                        dropped = self._contract_ast_literal(node.args[0])
                        if isinstance(dropped, str):
                            meta["downgrade_drop_tables"].append(dropped)
                    elif drop_name in {"op.drop_index", "drop_index"}:
                        meta["downgrade_drop_index_count"] += 1
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                call_name = self._contract_ast_call_name(node.func)
                if call_name in {"op.create_table", "create_table"} and node.args:
                    table = self._contract_ast_literal(node.args[0])
                    if not isinstance(table, str):
                        continue
                    entry = schema.setdefault(table, {"columns": [], "primary_key": [], "indexes": []})
                    for arg in node.args[1:]:
                        if not isinstance(arg, ast.Call):
                            continue
                        arg_name = self._contract_ast_call_name(arg.func)
                        if arg_name in {"sa.Column", "Column"} and arg.args:
                            col_name = self._contract_ast_literal(arg.args[0])
                            if not isinstance(col_name, str):
                                continue
                            col_type = ""
                            if len(arg.args) > 1:
                                col_type = (self._contract_ast_call_name(arg.args[1]) or "").split(".")[-1]
                            is_pk = False
                            nullable: bool | None = None
                            for kw in arg.keywords:
                                if kw.arg == "primary_key":
                                    is_pk = self._contract_ast_literal(kw.value) is True
                                elif kw.arg == "nullable":
                                    nullable = self._contract_ast_literal(kw.value)
                            entry["columns"].append(
                                {"name": col_name, "type": col_type, "primary_key": is_pk, "nullable": nullable}
                            )
                            if is_pk:
                                entry["primary_key"].append(col_name)
                        elif arg_name in {"sa.PrimaryKeyConstraint", "PrimaryKeyConstraint"}:
                            for pk_arg in arg.args:
                                pk_col = self._contract_ast_literal(pk_arg)
                                if isinstance(pk_col, str):
                                    entry["primary_key"].append(pk_col)
                elif call_name in {"op.create_index", "create_index"} and len(node.args) >= 3:
                    table = self._contract_ast_literal(node.args[1])
                    cols_node = node.args[2]
                    cols: list[str] = []
                    if isinstance(cols_node, (ast.List, ast.Tuple)):
                        for el in cols_node.elts:
                            val = self._contract_ast_literal(el)
                            if isinstance(val, str):
                                cols.append(val)
                    if isinstance(table, str) and cols:
                        schema.setdefault(table, {"columns": [], "primary_key": [], "indexes": []})["indexes"].append(cols)
        for entry in schema.values():
            entry["primary_key"] = list(dict.fromkeys(entry["primary_key"]))
        meta["downgrade_drop_tables"] = list(dict.fromkeys(meta["downgrade_drop_tables"]))
        return found, schema, meta

    def _in_scope_migration_tables(self, item: dict[str, Any]) -> tuple[bool, set[str]]:
        """Tables actually created by existing migration files in the task scope."""
        found, schema, _meta = self._extract_migration_schema(item)
        return found, set(schema.keys())

    def _build_migration_ground_truth_note(self) -> str:
        """Authoritative full-schema note (tables, columns, types, PK, indexes).

        Grounds planning/edit agents so they match the real migration instead of
        inventing tables, columns, or a composite primary key. Empty when the task has
        no existing migration in scope.
        """
        found, schema, meta = self._extract_migration_schema(self._selected_implementation_item or {})
        if not found:
            return ""
        if not schema:
            return (
                "The in-scope Alembic migration file(s) create no tables. "
                "Do NOT add models, columns, or migration tests for any table."
            )
        lines = [
            "Authoritative database schema parsed from the in-scope Alembic migration source "
            "(ground truth — trust this over any plan, summary, or assumption):",
        ]
        for table in sorted(schema):
            entry = schema[table]
            col_descs: list[str] = []
            for col in entry["columns"]:
                detail: list[str] = []
                if col["type"]:
                    detail.append(col["type"])
                if col["primary_key"]:
                    detail.append("pk")
                if col["nullable"] is False:
                    detail.append("not null")
                col_descs.append(f"{col['name']} ({', '.join(detail)})" if detail else col["name"])
            pk = ", ".join(entry["primary_key"]) if entry["primary_key"] else "(none declared)"
            idx = "; ".join("[" + ", ".join(cols) + "]" for cols in entry["indexes"]) or "(none)"
            lines.append(f"- Table {table}:")
            lines.append(f"    columns: {', '.join(col_descs) if col_descs else '(none parsed)'}")
            lines.append(f"    primary key: {pk}")
            lines.append(f"    indexes: {idx}")
        if meta.get("revision") is not None or meta.get("down_revision") is not None:
            lines.append(
                f"- Migration metadata: revision = {meta['revision']!r}, down_revision = {meta['down_revision']!r} "
                "(use these exact values; the revision is NOT the file name)."
            )
        drop_tables = meta.get("downgrade_drop_tables") or []
        lines.append(
            f"- downgrade(): drops table(s) {', '.join(drop_tables) if drop_tables else '(none)'} and makes "
            f"{meta.get('downgrade_drop_index_count', 0)} explicit op.drop_index call(s) "
            "(dropping a table removes its indexes implicitly — do not assert index drops that are not there)."
        )
        lines.append(
            "Models, migration tests, and contracts MUST match this schema and metadata exactly. Do NOT add, "
            "rename, or remove columns; do NOT invent a primary key (e.g. a composite provider+model key) "
            "different from the one above; do NOT assert tables, columns, indexes, revisions, or downgrade "
            "behavior that are not listed."
        )
        return "\n".join(lines)

    def _extract_db_architecture(self) -> dict[str, Any]:
        """Detect the target codebase's DB concurrency + session-injection model (ground truth).

        Parses real source via AST so the planning agents (architect, planner, task-designer)
        produce contracts that match the stack instead of demanding, e.g., asynchronous DB
        operations on a synchronous SQLAlchemy codebase, or a service that builds its own
        ``SessionLocal()`` instead of using the injected session. Universal: it reads the
        actual engine/session wiring rather than assuming a particular framework.

        Returns a dict with keys: ``found``, ``is_async`` (True/False/None when unknown),
        ``engine_call``, ``session_factory``, ``session_type``, ``session_dependency``,
        ``dependency_is_async``, ``db_module``, ``query_style``, ``base``.
        """
        cached = getattr(self, "_db_architecture_cache", None)
        if cached is not None:
            return cached

        result: dict[str, Any] = {
            "found": False,
            "is_async": None,
            "engine_call": "",
            "session_factory": "",
            "session_type": "",
            "session_dependency": "",
            "dependency_is_async": None,
            "db_module": "",
            "query_style": "",
            "base": "",
        }
        async_signal = False
        sync_signal = False

        workspace = getattr(self, "target_workspace", None)
        if not workspace:
            self._db_architecture_cache = result
            return result
        workspace = Path(workspace)
        if not workspace.exists():
            self._db_architecture_cache = result
            return result

        db_basenames = (
            "database.py",
            "db.py",
            "session.py",
            "sessions.py",
            "base.py",
            "deps.py",
            "dependencies.py",
            "models.py",
        )
        excluded_dirs = {
            ".git",
            ".venv",
            "venv",
            "env",
            "node_modules",
            "__pycache__",
            "dist",
            "build",
            ".pytest_cache",
            ".mypy_cache",
            "migrations",
            "alembic",
            "site-packages",
            ".openclaw",
            ".agents-pipeline",
        }
        prioritized: list[Path] = []
        others: list[Path] = []
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [name for name in dirs if name not in excluded_dirs and not name.startswith(".")]
            try:
                depth = len(Path(root).relative_to(workspace).parts)
            except ValueError:
                depth = 0
            if depth > 6:
                dirs[:] = []
                continue
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = Path(root) / name
                (prioritized if name in db_basenames else others).append(path)
        candidates = prioritized + others

        engine_tokens = (
            "create_engine",
            "create_async_engine",
            "sessionmaker",
            "async_sessionmaker",
            "declarative_base",
            "DeclarativeBase",
            "AsyncSession",
        )
        parsed_files = 0
        for path in candidates:
            if parsed_files >= 120:
                break
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not any(token in source for token in engine_tokens):
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            parsed_files += 1
            try:
                rel = path.relative_to(workspace).as_posix()
            except ValueError:
                rel = path.name

            module_async, module_sync = self._scan_db_module_ast(tree, source, rel, result)
            async_signal = async_signal or module_async
            sync_signal = sync_signal or module_sync
            if result["found"] and result["session_factory"] and result["session_dependency"]:
                break

        if async_signal:
            result["is_async"] = True
        elif sync_signal:
            result["is_async"] = False
        self._db_architecture_cache = result
        return result

    def _scan_db_module_ast(
        self, tree: ast.AST, source: str, rel: str, result: dict[str, Any]
    ) -> tuple[bool, bool]:
        """Merge DB-stack signals from one parsed module into ``result``.

        Returns ``(async_signal, sync_signal)`` observed in this module.
        """
        async_signal = False
        sync_signal = False

        def _note_module() -> None:
            if not result["db_module"]:
                result["db_module"] = rel
            result["found"] = True

        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name == "AsyncSession" or alias.name.endswith(".AsyncSession"):
                        async_signal = True
                        if not result["session_type"]:
                            result["session_type"] = "AsyncSession"
                continue
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
                call_name = self._contract_ast_call_name(node.value.func) or ""
                leaf = call_name.split(".")[-1]
                target_name = ""
                if node.targets and isinstance(node.targets[0], ast.Name):
                    target_name = node.targets[0].id
                if leaf == "create_async_engine":
                    async_signal = True
                    result["engine_call"] = result["engine_call"] or "create_async_engine"
                    _note_module()
                elif leaf == "create_engine":
                    sync_signal = True
                    result["engine_call"] = result["engine_call"] or "create_engine"
                    _note_module()
                elif leaf == "async_sessionmaker":
                    async_signal = True
                    result["session_factory"] = result["session_factory"] or target_name or "async_session"
                    _note_module()
                elif leaf == "sessionmaker":
                    # sessionmaker(class_=AsyncSession, ...) is the async pattern.
                    uses_async_class = any(
                        kw.arg == "class_" and (self._contract_ast_call_name(kw.value) or "").split(".")[-1] == "AsyncSession"
                        for kw in node.value.keywords
                    )
                    if uses_async_class:
                        async_signal = True
                    else:
                        sync_signal = True
                    result["session_factory"] = result["session_factory"] or target_name or "SessionLocal"
                    _note_module()
                elif leaf == "declarative_base":
                    result["base"] = result["base"] or target_name or "Base"
                    _note_module()
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    base_name = (self._contract_ast_call_name(base) or "").split(".")[-1]
                    if base_name == "DeclarativeBase":
                        result["base"] = result["base"] or node.name
                        _note_module()

        # Session dependency: a (possibly async) generator that yields a session. Prefer one
        # that references the detected session factory; fall back to a get_*/session name.
        if not result["session_dependency"]:
            factory = result["session_factory"]
            name_hints = ("get_db", "get_session", "get_async_session", "get_db_session", "db_session")
            best: tuple[str, bool] | None = None
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                has_yield = any(isinstance(inner, (ast.Yield, ast.YieldFrom)) for inner in ast.walk(node))
                if not has_yield:
                    continue
                body_src = ast.get_source_segment(source, node) or ""
                references_factory = bool(factory) and factory in body_src
                name_match = any(hint in node.name for hint in name_hints)
                if references_factory or name_match:
                    is_async = isinstance(node, ast.AsyncFunctionDef)
                    if references_factory:
                        best = (node.name, is_async)
                        break
                    if best is None:
                        best = (node.name, is_async)
            if best is not None:
                result["session_dependency"], result["dependency_is_async"] = best
                if result["dependency_is_async"]:
                    async_signal = True
                _note_module()

        if not result["query_style"]:
            if re.search(r"\.query\s*\(", source):
                result["query_style"] = "orm_query"
            elif re.search(r"\bselect\s*\(", source) and re.search(r"\.execute\s*\(", source):
                result["query_style"] = "select_execute"

        return async_signal, sync_signal

    def _build_architecture_ground_truth_note(self) -> str:
        """Authoritative note on the DB concurrency model and session-injection pattern.

        Grounds the planning and edit agents so contracts match the real stack (e.g. do not
        demand asynchronous DB on a synchronous SQLAlchemy codebase, and require services to
        use the injected session instead of constructing their own). Empty when the target's
        persistence stack cannot be determined.
        """
        arch = self._extract_db_architecture()
        if not arch.get("found") or arch.get("is_async") is None:
            return ""
        engine = arch.get("engine_call") or ""
        factory = arch.get("session_factory") or ""
        session_type = arch.get("session_type") or ""
        dependency = arch.get("session_dependency") or ""
        module = arch.get("db_module") or ""
        signature = ", ".join(part for part in (engine, factory, session_type) if part)
        lines = [
            "Authoritative backend persistence architecture parsed from the target source "
            "(ground truth — trust this over any plan, summary, or assumption):",
        ]
        if module:
            lines.append(f"- Database layer is defined in {module}.")
        if arch["is_async"] is False:
            lines.append(
                "- The persistence stack is SYNCHRONOUS SQLAlchemy"
                + (f" ({signature})." if signature else ".")
            )
            if dependency:
                lines.append(
                    f"- Services, routers, and repositories receive a session by dependency injection "
                    f"(e.g. FastAPI `Depends({dependency})`) or as a passed-in `{session_type or 'Session'}` "
                    "parameter. They MUST use that injected session and MUST NOT construct their own"
                    + (f" `{factory}()`." if factory else " session.")
                )
            elif factory:
                lines.append(
                    f"- Services receive a passed-in session and MUST NOT construct their own `{factory}()`."
                )
            lines.append(
                "- Use the synchronous ORM API directly (for example "
                "`db.query(Model).filter(...).first()/.all()`, `db.add(obj)`, `db.commit()`)."
            )
            lines.append(
                "- Do NOT introduce asynchronous database access: no `async def` methods that touch the "
                "database, no `await` on database calls, and no `AsyncSession`, `create_async_engine`, "
                "`async_sessionmaker`, or `asyncio.to_thread(...)` wrappers around synchronous DB calls."
            )
            lines.append(
                "- Contracts, method signatures, and tests MUST match this synchronous model. Do NOT "
                'require "all database operations to be asynchronous" or forbid "synchronous database '
                'calls" on this stack.'
            )
        else:
            lines.append(
                "- The persistence stack is ASYNCHRONOUS SQLAlchemy"
                + (f" ({signature})." if signature else ".")
            )
            if dependency:
                lines.append(
                    f"- Services and routers receive an `{session_type or 'AsyncSession'}` by dependency "
                    f"injection (e.g. FastAPI `Depends({dependency})`) or as a passed-in parameter. They "
                    "MUST use that injected session and MUST NOT construct their own."
                )
            lines.append(
                "- Use the asynchronous API (`await session.execute(select(...))`, `await session.commit()`); "
                "database-touching service methods are `async def` and must be awaited."
            )
            lines.append(
                "- Contracts, method signatures, and tests MUST match this asynchronous model. Do NOT "
                "require synchronous blocking `db.query(...)` calls on this asynchronous stack."
            )
        return "\n".join(lines)

    @staticmethod
    def _fact(value: Any, confidence: str, source: str = "") -> dict[str, Any]:
        """One profile fact with its trust level. confidence in {verified, inferred, unknown}.

        verified = a deterministic detector confirmed it; inferred = an agent guessed it (no
        detector); unknown = could not be determined. Downstream guardrails hard-block only on
        verified facts and soft-warn on inferred ones.
        """
        return {"value": value, "confidence": confidence, "source": source}

    def _repo_map_digest(self) -> str:
        paths = sorted(str(entry.get("path") or "") for entry in (self._load_repo_map().get("files") or []))
        return hashlib.sha1("\n".join(paths).encode("utf-8", "replace")).hexdigest()[:16]

    def _safe_repo_map_digest(self) -> str:
        try:
            return self._repo_map_digest()
        except Exception:
            return ""

    def _detect_repo_languages(self) -> list[str]:
        workspace = Path(getattr(self, "target_workspace", ".") or ".")
        checks = (
            ("python", ("requirements.txt", "pyproject.toml", "setup.py", "setup.cfg")),
            ("javascript", ("package.json",)),
            ("go", ("go.mod",)),
            ("rust", ("Cargo.toml",)),
            ("java", ("pom.xml", "build.gradle")),
            ("ruby", ("Gemfile",)),
            ("php", ("composer.json",)),
        )
        languages: list[str] = []
        for language, manifests in checks:
            for root in self._detect_project_roots():
                if any((workspace / (f"{root}/{m}" if root else m)).exists() for m in manifests):
                    languages.append(language)
                    break
        return list(dict.fromkeys(languages))

    def _detect_repo_migration_tool(self) -> tuple[str, str]:
        for entry in (self._load_repo_map().get("files") or []):
            path = str(entry.get("path") or "").replace("\\", "/")
            if "alembic/versions/" in path:
                prefix = path.split("alembic/versions/")[0].rstrip("/")
                return "alembic", (f"{prefix}/alembic" if prefix else "alembic")
        return "none", ""

    def _detect_tests_root(self) -> str:
        candidates: list[str] = []
        for entry in (self._load_repo_map().get("files") or []):
            parts = str(entry.get("path") or "").replace("\\", "/").split("/")
            for index, part in enumerate(parts[:-1]):
                if part in ("tests", "test", "__tests__"):
                    candidates.append("/".join(parts[: index + 1]))
                    break
        return sorted(candidates, key=len)[0] if candidates else ""

    def _build_architecture_profile(self) -> dict[str, Any]:
        """Structured, confidence-tagged profile of the target's CURRENT architecture.

        Deterministic-first: every field is filled from existing detectors (marked verified)
        where a detector exists; gaps are left explicit (unknown) for a profiler agent to fill
        in later (as inferred). No project name is hardcoded — facts come from whatever repo is
        targeted. `target_architecture` is reserved for the architect's intended end-state.
        """
        digest = self._safe_repo_map_digest()
        cached = getattr(self, "_architecture_profile_cache", None)
        if cached is not None and cached.get("repo_map_digest") == digest:
            return cached

        arch = self._extract_db_architecture()
        roots = self._detect_project_roots()
        languages = self._detect_repo_languages()
        migration_tool, migration_location = self._detect_repo_migration_tool()
        tests_root = self._detect_tests_root()
        policy = getattr(self, "implementation_scope_policy", None) or {}

        if arch.get("found") and arch.get("is_async") is not None:
            db_module = arch.get("db_module") or ""
            concurrency = self._fact("async" if arch["is_async"] else "sync", "verified", db_module)
            access = self._fact(
                "sqlalchemy_core" if arch.get("query_style") == "select_execute" else "sqlalchemy_orm",
                "verified",
                db_module,
            )
            dependency = arch.get("session_dependency")
            session = self._fact(dependency or None, "verified" if dependency else "unknown", db_module)
        else:
            concurrency = self._fact(None, "unknown", "no SQLAlchemy stack detected")
            access = self._fact(None, "unknown", "")
            session = self._fact(None, "unknown", "")

        open_questions: list[str] = []
        if concurrency["confidence"] == "unknown":
            open_questions.append("DB concurrency/access not auto-detected; a profiler agent must read the data layer.")
        if not tests_root:
            open_questions.append("No tests directory detected; a backend task may need to scaffold the test harness.")

        profile = {
            "schema_version": 1,
            "repo_map_digest": digest,
            "language": self._fact(languages, "verified" if languages else "unknown", "build manifests"),
            "persistence": {
                "concurrency": concurrency,
                "access": access,
                "session_dependency": session,
                "migrations": self._fact(
                    {"tool": migration_tool, "location": migration_location},
                    "verified" if migration_tool != "none" else "inferred",
                    migration_location,
                ),
            },
            "layout": {
                "project_roots": self._fact(roots, "verified", "repo scan"),
                "tests_root": self._fact(tests_root or None, "verified" if tests_root else "unknown", "repo_map"),
            },
            "sensitive_areas": {
                "forbidden_paths": list(policy.get("forbidden_paths") or []),
                "forbidden_keywords": list(policy.get("forbidden_keywords") or []),
            },
            "target_architecture": None,  # reserved: the architect's intended end-state (Target Profile)
            "open_questions": open_questions,
        }
        self._architecture_profile_cache = profile
        return profile

    def _render_architecture_profile_note(self) -> str:
        """Human-readable Project Architecture Profile for injection into agent context."""
        profile = self._build_architecture_profile()
        lines = ["Project Architecture Profile — CURRENT codebase (trust verified facts as ground truth):"]
        languages = profile["language"]["value"]
        if languages:
            lines.append(f"- Language(s): {', '.join(languages)} [{profile['language']['confidence']}].")
        persistence = profile["persistence"]
        concurrency = persistence["concurrency"]["value"]
        if concurrency:
            lines.append(
                f"- Persistence: {concurrency} [{persistence['concurrency']['confidence']}]"
                f", access={persistence['access']['value']}."
            )
            dependency = persistence["session_dependency"]["value"]
            if dependency:
                lines.append(f"- Session is injected via `{dependency}`; use it, do not construct a new session.")
        else:
            lines.append("- Persistence: not auto-detected (a profiler agent should read the data layer).")
        migrations = persistence["migrations"]["value"]
        lines.append(f"- Migrations: {migrations['tool']}" + (f" @ {migrations['location']}" if migrations["location"] else ""))
        roots = profile["layout"]["project_roots"]["value"]
        lines.append("- Project roots: " + ", ".join(root or "(repo root)" for root in roots) + ".")
        tests_root = profile["layout"]["tests_root"]["value"]
        lines.append(f"- Tests root: {tests_root or '(none detected)'}.")
        if profile["open_questions"]:
            lines.append("- Confirm before relying: " + "; ".join(profile["open_questions"]))
        return "\n".join(lines)

    def _architecture_profile_has_gaps(self, profile: dict[str, Any] | None = None) -> bool:
        """True when the deterministic profile left fields unknown — i.e. the profiler agent
        is worth running. When everything is already verified (e.g. a SQLAlchemy repo), the
        agent is skipped entirely to save tokens."""
        profile = profile or self._build_architecture_profile()
        persistence = profile.get("persistence") or {}
        for key in ("concurrency", "access", "session_dependency"):
            if (persistence.get(key) or {}).get("confidence") == "unknown":
                return True
        return False

    def _merge_profiler_agent_output(self, profile: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        """Merge a profiler agent's JSON into the deterministic profile.

        Only fills fields that are unknown/missing; NEVER overrides a verified fact. Anything
        the agent supplies is marked `inferred` (it read the code, but no detector confirmed it).
        """
        if not isinstance(payload, dict):
            return profile

        def _value_source(raw: Any) -> tuple[Any, str]:
            if isinstance(raw, dict):
                return raw.get("value"), str(raw.get("source") or "profiler-agent")
            return raw, "profiler-agent"

        persistence = profile.setdefault("persistence", {})
        agent_persistence = payload.get("persistence") or {}
        if isinstance(agent_persistence, dict):
            for key in ("concurrency", "access", "session_dependency", "engine", "session_pattern", "models_location"):
                current = persistence.get(key) or {}
                if current.get("confidence") == "verified":
                    continue  # ground truth wins
                value, source = _value_source(agent_persistence.get(key))
                if value not in (None, "", []):
                    persistence[key] = self._fact(value, "inferred", source)

        conventions = payload.get("conventions")
        if conventions:
            value, source = _value_source(conventions)
            if value:
                profile["conventions"] = self._fact(value, "inferred", source)

        # Drop the now-answered persistence open-question.
        if (persistence.get("concurrency") or {}).get("value"):
            profile["open_questions"] = [
                question for question in (profile.get("open_questions") or [])
                if "concurrency/access" not in question
            ]
        self._architecture_profile_cache = profile
        return profile

    def _contract_demands_async_db(self, item: dict[str, Any]) -> str:
        """Reason string when a contract demands async DB on a synchronous stack (else '').

        Gives the contract-repair loop teeth so a task-designer contract that requires
        asynchronous database operations on a synchronous SQLAlchemy codebase is rejected and
        regenerated, instead of producing an implementation QA can never accept.
        """
        arch = self._extract_db_architecture()
        if not arch.get("found") or arch.get("is_async") is not False:
            return ""
        must_contain = [str(value) for value in (item.get("must_contain") or [])]
        prose: list[str] = []
        for key in ("integration", "forbidden", "must_test", "task_designer_notes", "notes"):
            prose.extend(str(value) for value in (item.get(key) or []))
        haystack = "\n".join(must_contain + prose).lower()

        explicit_patterns = (
            r"all\s+(?:database|db)\s+operations?\s+(?:must|should|are|be)\b[^\n]*async",
            r"(?:database|db)\s+operations?\s+(?:must|should)\s+be\s+async",
            r"async(?:hronous|/await|\s*/\s*await)?\s+(?:pattern\s+)?(?:for\s+)?(?:all\s+)?(?:database|db|persistence)\b",
            r"asynchronous\s+(?:database|db|persistence|sqlalchemy|metric)",
            r"synchronous\s+blocking\s+database",
            r"asyncio\.to_thread",
        )
        for pattern in explicit_patterns:
            if re.search(pattern, haystack):
                return (
                    "contract requires asynchronous database access, but the target persistence stack is "
                    "synchronous SQLAlchemy"
                    + (f" ({arch.get('engine_call')}/{arch.get('session_factory')})" if arch.get("session_factory") else "")
                    + "; make database-touching methods synchronous (def, not async def) and use the "
                    "injected synchronous session with the ORM query API"
                )

        imports = [str(value).lower() for value in (item.get("must_import") or [])]
        factory = (arch.get("session_factory") or "").lower()
        references_sync_session = any(
            "sqlalchemy.orm import session" in imp or "sessionlocal" in imp or (factory and factory in imp)
            for imp in imports
        )
        has_async_def = any(re.search(r"\basync\s+def\b", value) for value in must_contain)
        if has_async_def and references_sync_session:
            return (
                "contract declares async def methods while importing the synchronous SQLAlchemy Session; "
                "the target stack is synchronous — database-touching methods must be synchronous (def, not "
                "async def) and use the injected session"
            )
        return ""

    def _configured_alembic_down_revision(self) -> str:
        implementation = self.config.get("phases", {}).get("implementation", {})
        value = str(implementation.get("default_alembic_down_revision") or DEFAULT_ALEMBIC_DOWN_REVISION).strip()
        return value or DEFAULT_ALEMBIC_DOWN_REVISION

    def _validate_test_developer_static_constraints(self, relative_paths: list[str]) -> list[str]:
        findings: list[str] = []
        for relative_path in relative_paths:
            if not relative_path.endswith(".py"):
                continue
            candidate = self.target_workspace / relative_path
            if not candidate.exists() or not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                findings.append(f"{relative_path}: unable to read file: {exc}")
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError as exc:
                findings.append(f"{relative_path}: ast parse failed: {exc}")
                continue
            imported_modules: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported_modules.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported_modules.add(node.module)
            for module in sorted(imported_modules):
                root = module.split(".", 1)[0]
                if module in TEST_DEVELOPER_FORBIDDEN_IMPORTS or root in TEST_DEVELOPER_FORBIDDEN_IMPORTS:
                    findings.append(f"{relative_path}: forbidden import: {module}")
                elif module not in TEST_DEVELOPER_ALLOWED_IMPORTS and root not in TEST_DEVELOPER_ALLOWED_IMPORTS:
                    findings.append(f"{relative_path}: import outside must_use_only: {module}")
            for marker in ("pytest.main(", "alembic.command.", "command.upgrade(", "command.downgrade(", "create_engine("):
                if marker in text:
                    findings.append(f"{relative_path}: validation_style requires static_text_and_ast only")
                    break
            format_sensitive_patterns = [
                r"""["']op\.(?:create_table|drop_table|create_index|drop_index)\(\s*["']""",
                r"""["']sa\.Column\(\s*["']""",
            ]
            for pattern in format_sensitive_patterns:
                if re.search(pattern, text):
                    findings.append(
                        f"{relative_path}: avoid format-sensitive raw string assertions for Alembic/SQLAlchemy calls; inspect AST calls and literal args instead"
                    )
                    break
            if "revision\\s*=" in text or "down_revision\\s*=" in text:
                findings.append(
                    f"{relative_path}: avoid format-sensitive raw regex assertions for migration revision assignments; inspect AST assignment values instead"
                )
        return findings

    def _prevalidate_current_scope(self, agent_name: str) -> tuple[bool, str]:
        item = self._selected_implementation_item or {}
        if not item or not bool(item.get("contract_completeness")):
            return False, ""
        findings: list[str] = []
        contract_diag = self._evaluate_selected_task_contract_compliance(item)
        if not contract_diag.get("contract_compliance", False):
            findings.extend(
                f"missing_must_contain: {value}"
                for value in (contract_diag.get("missing_must_contain") or [])
            )
            if contract_diag.get("missing_test_file", False):
                findings.append("missing_test_file: selected test_file is missing")
            findings.extend(
                f"forbidden_contract_hits: {value}"
                for value in (contract_diag.get("forbidden_contract_hits") or [])
            )
        relevant_paths: list[str] = []
        target_file = item.get("target_file") or {}
        test_file = item.get("test_file") or {}
        if isinstance(target_file, dict):
            normalized = self._normalize_repo_relative_path(target_file.get("path"))
            if normalized:
                relevant_paths.append(normalized)
        if isinstance(test_file, dict):
            normalized = self._normalize_repo_relative_path(test_file.get("path"))
            if normalized:
                relevant_paths.append(normalized)
        python_paths = [path for path in sorted(set(relevant_paths)) if path.endswith(".py")]
        if python_paths:
            returncode, stdout, stderr = self._run_local_command(
                [sys.executable, "-m", "py_compile", *python_paths],
                timeout=30,
                cwd=self.target_workspace,
            )
            if returncode != 0:
                findings.append(stderr or stdout or "py_compile failed")
        if isinstance(target_file, dict):
            target_path = self._normalize_repo_relative_path(target_file.get("path"))
            if target_path and self._is_alembic_migration_path(target_path):
                findings.extend(self._validate_changed_migration_files([target_path]))
        if agent_name == "test-developer":
            findings.extend(self._validate_test_developer_static_constraints(python_paths))
        if findings:
            return False, "\n".join(findings)
        return True, "no-op, already valid"

    def _maybe_skip_already_valid_scope(self, agent_name: str, agent_runtime: dict[str, str]) -> bool:
        already_valid, detail = self._prevalidate_current_scope(agent_name)
        if not already_valid:
            return False
        self.logger.save_agent_report(
            "implementation",
            agent_name,
            {
                "phase": "implementation",
                "agent": agent_name,
                "agent_name": agent_name,
                "status": "no_changes",
                "result": detail,
                "elapsed_s": 0.0,
                "returncode": 0,
                "runtime": agent_runtime,
                "command": "",
                "message": "",
                "prompt_stats": {},
                "stdout": "",
                "stderr": "",
                "parsed_output": f"status=no_changes: {detail}",
                "usage": {},
                "developer_changed_files": [],
                "developer_diff_lines": 0,
                "write_tools_used": [],
                "no_changes_detected": True,
            },
        )
        self.logger.agent_end(agent_name, "no_changes", detail)
        return True

    def _evaluate_selected_task_contract_compliance(self, item: dict[str, Any] | None = None) -> dict[str, Any]:
        item = item or self._selected_implementation_item or {}
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
        missing_must_contain = [value for value in must_contain if not self._contract_requirement_present(target_text, value)]
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
        if self.user_goal:
            return self.user_goal
        if self._is_agents_pipeline_self_analysis():
            return (
                "Implement one small backend-only improvement to agents-pipeline orchestration reliability. "
                "Prefer status/resume/doctor/repo-map/validation improvements. "
                "Do not implement provider marketplace, billing, frontend, or unrelated AI Gateway features."
            )
        project_scope = str((getattr(self, "project_settings", {}) or {}).get("default_implementation_scope") or "").strip()
        if project_scope:
            return project_scope
        return str(
            self.config.get("workflow", {}).get(
                "default_implementation_scope",
                "Implement one small, safe, backend-first improvement that fits the target "
                "repository's existing architecture and conventions.",
            )
        ).strip()

    def _should_regenerate_implementation_backlog(self) -> bool:
        implementation_config = self.config.get("phases", {}).get("implementation", {})
        workflow_config = self.config.get("workflow", {})
        return bool(
            self.fresh_run
            or implementation_config.get("regenerate_backlog")
            or workflow_config.get("regenerate_backlog")
        )

    def _load_canonical_implementation_backlog_payload(self) -> dict[str, Any]:
        if self._should_regenerate_implementation_backlog():
            self._canonical_backlog_loaded = False
            return {}
        if self._canonical_backlog_payload:
            return dict(self._canonical_backlog_payload)
        if not self.canonical_backlog_path.exists():
            self._canonical_backlog_loaded = False
            return {}
        try:
            payload = json.loads(self.canonical_backlog_path.read_text(encoding="utf-8"))
        except Exception:
            self._canonical_backlog_loaded = False
            return {}
        tasks = payload.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            self._canonical_backlog_loaded = False
            return {}
        self._canonical_backlog_payload = dict(payload)
        self._canonical_backlog_loaded = True
        self._backlog_selected_task_id = str(payload.get("selected_task_id") or "").strip()
        return dict(payload)

    def _load_canonical_implementation_backlog(self) -> list[dict[str, Any]]:
        payload = self._load_canonical_implementation_backlog_payload()
        tasks = payload.get("tasks") if payload else []
        if not isinstance(tasks, list):
            return []
        return [dict(item) for item in tasks if isinstance(item, dict)]

    def _save_canonical_implementation_backlog(self, tasks: list[dict[str, Any]], *, selected_task_id: str = "") -> None:
        if not tasks:
            return
        if self.canonical_backlog_path.exists() and not self._should_regenerate_implementation_backlog():
            return
        now = datetime.now().isoformat()
        payload = {
            "backlog_id": f"{self.project_id}:{self.logger.run_dir.name}:{now}",
            "created_at": now,
            "source_run_id": self.logger.run_dir.name,
            "tasks": [dict(item) for item in tasks],
            "selected_task_id": str(selected_task_id or "").strip(),
        }
        self.canonical_backlog_path.parent.mkdir(parents=True, exist_ok=True)
        self.canonical_backlog_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._canonical_backlog_payload = dict(payload)
        self._canonical_backlog_loaded = True
        self._backlog_selected_task_id = str(selected_task_id or "").strip()

    def _update_canonical_backlog_selected_task(self, task_id: str) -> None:
        task_id = str(task_id or "").strip()
        if not task_id or not self.canonical_backlog_path.exists():
            return
        try:
            payload = json.loads(self.canonical_backlog_path.read_text(encoding="utf-8"))
        except Exception:
            return
        payload["selected_task_id"] = task_id
        self.canonical_backlog_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._canonical_backlog_payload = dict(payload)
        self._canonical_backlog_loaded = True

    def print_implementation_backlog(self) -> int:
        reports, _run_dir = self._load_latest_project_research_reports()
        backlog, backlog_source = self._build_implementation_backlog(reports, allow_research_fallback=True)
        if not backlog:
            print("No implementation backlog found. Run research first.")
            return 1
        print(self._format_implementation_backlog(backlog, backlog_source))
        return 0

    def explain_latest_run(self) -> int:
        run_dir = find_latest_implementation_run(self.logger.log_dir)
        if run_dir is None:
            print("No implementation run found.")
            return 1
        path = generate_human_report(
            run_dir,
            project_id=self.project_id,
            feedback_root=self._engine_path(self.config.get("paths", {}).get("feedback_dir", ".openclaw/feedback")),
        )
        print(path)
        return 0

    def _update_human_report(self, *, final_status: str = "", agent_name: str = "") -> bool:
        self._human_report_updated = False
        self._human_report_error = ""
        try:
            self._human_report_path = generate_human_report(
                self.logger.run_dir,
                project_id=self.project_id,
                target_workspace=self.target_workspace,
                feedback_root=self._engine_path(self.config.get("paths", {}).get("feedback_dir", ".openclaw/feedback")),
                final_status=final_status,
            )
            self._human_report_updated = True
        except Exception as exc:
            self._human_report_updated = False
            self._human_report_error = str(exc)
        if agent_name:
            self.logger.agent_progress(agent_name, f"Diagnostic human_report_path={self._human_report_path}")
            self.logger.agent_progress(agent_name, f"Diagnostic human_report_updated={self._human_report_updated}")
            if self._human_report_error:
                self.logger.agent_progress(agent_name, f"Diagnostic human_report_error={self._human_report_error}")
        else:
            self.logger.info(f"Diagnostic human_report_path={self._human_report_path}")
            self.logger.info(f"Diagnostic human_report_updated={self._human_report_updated}")
            if self._human_report_error:
                self.logger.info(f"Diagnostic human_report_error={self._human_report_error}")
        return self._human_report_updated

    def _prepare_implementation_backlog_selection(
        self,
        reports: list[dict[str, Any]] | None = None,
        *,
        require_backlog: bool,
        allow_research_fallback: bool = False,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        if (
            self._selected_implementation_item is not None
            and not force_refresh
            and not self._selected_task_from_explicit_cli
            and not self.rerun_completed
            and str(self._selected_implementation_item.get("contract_source") or "") != "task-designer"
            and str(self._selected_implementation_item.get("id") or "").strip() != self._dependency_forced_task_id
            and str(self._selected_implementation_item.get("id") or "").strip() in set(self._completed_implementation_task_ids())
        ):
            self._selected_implementation_item = None
            self._selected_task_source = ""
            force_refresh = True
        if self._selected_implementation_item is not None and not force_refresh:
            return {
                "selected_item": self._selected_implementation_item,
                "backlog": self._implementation_backlog_cache or [],
                "backlog_source": self._implementation_backlog_source,
                "error": "",
            }
        if force_refresh:
            self._selected_implementation_item = None
            self._implementation_backlog_cache = []
            self._implementation_backlog_source = ""
            self._selected_task_source = ""
            self._skipped_completed_task_ids = []
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
            completed_ids = set(self._completed_implementation_task_ids())
            backlog_ids = [str(item.get("id") or "").strip() for item in backlog if str(item.get("id") or "").strip()]
            if backlog_ids and all(task_id in completed_ids for task_id in backlog_ids):
                return {
                    "selected_item": None,
                    "backlog": backlog,
                    "backlog_source": backlog_source,
                    "error": (
                        "All implementation backlog tasks are completed. "
                        "Use --fresh-run to regenerate the backlog, or pass --task-id TASK-XXX --rerun-completed to rerun one task."
                    ),
                }
            return {
                "selected_item": None,
                "backlog": backlog,
                "backlog_source": backlog_source,
                "error": "Selected implementation task was not found in the planner backlog.",
            }
        completed_ids = set(self._completed_implementation_task_ids())
        selected_task_id = str((selected_item or {}).get("id") or "").strip()
        if self._selected_task_from_explicit_cli and selected_task_id in completed_ids and not self.rerun_completed:
            return {
                "selected_item": None,
                "backlog": backlog,
                "backlog_source": backlog_source,
                "error": (
                    f"Selected implementation task {selected_task_id} is already completed. "
                    "Pass --rerun-completed with --task-id to run it again."
                ),
            }
        self._selected_implementation_item = selected_item
        self._selected_task_source = self._selected_task_source_for(backlog_source)
        if selected_item:
            self._update_canonical_backlog_selected_task(str(selected_item.get("id") or ""))
        return {
            "selected_item": selected_item,
            "backlog": backlog,
            "backlog_source": backlog_source,
            "error": "",
        }

    def _reset_selected_implementation_item(self) -> None:
        self._selected_implementation_item = None
        self._implementation_backlog_cache = []
        self._implementation_backlog_source = ""
        self._selected_task_source = ""
        self._skipped_completed_task_ids = []

    def _selected_task_source_for(self, backlog_source: str) -> str:
        if self._selected_task_from_explicit_cli:
            return "explicit_task_id"
        if backlog_source == "canonical_backlog":
            return "canonical_backlog"
        return "new_planner_output"

    def _build_implementation_backlog(
        self,
        reports: list[dict[str, Any]],
        *,
        allow_research_fallback: bool = False,
    ) -> tuple[list[dict[str, Any]], str]:
        canonical_backlog = self._load_canonical_implementation_backlog()
        if canonical_backlog:
            self._implementation_planner_output_chars = 0
            return canonical_backlog, "canonical_backlog"

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
                "- If a later task uses a file created by an earlier task, declare depends_on and place that reused file in existing_paths for the later task.",
                "- This also applies to package markers like a test package's __init__.py: if reused in a later task, put it in existing_paths for that later task and declare depends_on.",
                "- If a required test file lives under a new directory (e.g. the repo's tests directory), declare that directory in new_directories.",
                "- If adding a test package __init__.py that does not already exist, declare it in new_files and include it in allowed_paths.",
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
            + ". Repair rules: every path in allowed_paths must also appear in existing_paths or new_files; existing_paths must contain exact existing files only, never directories; never guess an existing filename from a directory name or naming pattern; if an exact migration or test file is not present in repo_map, do not place it in existing_paths and treat it as new_files or omit it; if a later task reuses a file created by an earlier task, declare depends_on and list that reused file in existing_paths for the later task; this includes package markers such as a test package's __init__.py (and any nested test package __init__.py); if a new file lives under a directory missing from repo_map, declare that parent in new_directories; if required_test_paths uses a new test package directory, include that directory in new_directories, add the package __init__.py to new_files, and include that __init__.py in allowed_paths."
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
        valid = not (
            self._planner_invalid_paths
            or self._planner_missing_directories
            or self._planner_missing_tests
            or self._planner_conflicting_forbidden_paths
            or self._generic_root_dirs_rejected
            or self._planner_dependency_validation_errors
            or self._planner_parse_error
            or self._planner_schema_errors
        )
        if valid:
            self._save_canonical_implementation_backlog(items)
        return {
            "valid": valid,
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
                    partial_items = self._parse_partial_planner_yaml_items(candidate)
                    if partial_items:
                        extracted_payload_excerpt = str(candidate).strip()[:400]
                        items: list[dict[str, Any]] = []
                        schema_errors: list[str] = []
                        for index, raw_item in enumerate(partial_items, start=1):
                            normalized, item_errors = self._normalize_planner_task(raw_item, item_index=index)
                            if item_errors:
                                schema_errors.extend(item_errors)
                            elif normalized:
                                items.append(normalized)
                        if items:
                            return {
                                "items": items,
                                "parse_error": "",
                                "schema_errors": schema_errors,
                                "raw_output_excerpt": normalized_text[:400],
                                "extracted_payload_excerpt": extracted_payload_excerpt,
                            }
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

    @staticmethod
    def _parse_partial_planner_yaml_items(text: str) -> list[dict[str, Any]]:
        normalized = str(text or "").replace("\r\n", "\n").strip()
        if not normalized:
            return []
        normalized = re.sub(r"^\s*```(?:yaml|yml|json)?\s*\n?", "", normalized, flags=re.IGNORECASE)
        normalized = re.sub(r"\n```+\s*$", "", normalized).strip()
        lines = normalized.splitlines()
        blocks: list[str] = []
        current: list[str] = []
        for line in lines:
            if re.match(r"^\s*-\s+id\s*:", line):
                if current:
                    blocks.append("\n".join(current))
                current = [line]
                continue
            if current:
                current.append(line)
        if current:
            blocks.append("\n".join(current))

        parsed_items: list[dict[str, Any]] = []
        for block in blocks:
            try:
                payload = yaml.safe_load(textwrap.dedent(block).strip())
            except yaml.YAMLError:
                continue
            if isinstance(payload, list) and payload and isinstance(payload[0], dict):
                parsed_items.append(payload[0])
            elif isinstance(payload, dict):
                parsed_items.append(payload)
        return parsed_items

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
        if target_file_path and self._is_alembic_migration_path(target_file_path):
            must_contain = list(
                dict.fromkeys(
                    [
                        *must_contain,
                        'revision = "<non-empty string>"',
                        f'down_revision = "{self._configured_alembic_down_revision()}"',
                    ]
                )
            )
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
        # Import statements are concrete, verifiable substrings (e.g. a developer contract may
        # require "from app.models import ProviderMetrics" to appear in the target file).
        if normalized.startswith("import ") or (normalized.startswith("from ") and " import " in normalized):
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
        for value in must_contain:
            if self._is_vague_contract_item(value):
                errors.append(f"{task_id}:vague_must_contain:{value[:80]}")

        if len(must_test) < 1:
            errors.append(f"{task_id}:must_test_empty")
        for value in must_test:
            if self._is_vague_contract_item(value):
                errors.append(f"{task_id}:vague_must_test:{value[:80]}")

        # Ground migration-table claims in reality: the contract must not require a test
        # asserting that an in-scope migration creates a table that migration never creates.
        found_migration, migration_tables = self._in_scope_migration_tables(item)
        if found_migration:
            declared_model_tables = set(
                re.findall(r'__tablename__\s*=\s*["\']([A-Za-z0-9_]+)["\']', "\n".join(must_contain))
            )
            for table in sorted(declared_model_tables):
                if table in migration_tables:
                    continue
                if any(table in entry and "migration" in entry.lower() for entry in must_test):
                    errors.append(
                        f"{task_id}:must_test asserts table '{table}' is created by the migration, "
                        f"but the in-scope migration(s) only create {sorted(migration_tables)}; "
                        f"remove the migration test for '{table}' (and drop the model if it has no migration)"
                    )

        # Ground the concurrency model in reality: a contract must not require asynchronous DB
        # operations on a synchronous SQLAlchemy stack (QA can never accept such an implementation).
        async_db_reason = self._contract_demands_async_db(item)
        if async_db_reason:
            errors.append(f"{task_id}:{async_db_reason}")

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
                    partial_items = self._parse_partial_planner_yaml_items(candidate)
                    if partial_items:
                        extracted_payload_excerpt = str(candidate).strip()[:400]
                        items: list[dict[str, Any]] = []
                        schema_errors: list[str] = []
                        for index, raw_item in enumerate(partial_items, start=1):
                            normalized, item_errors = self._normalize_planner_task(raw_item, item_index=index)
                            if item_errors:
                                schema_errors.extend(item_errors)
                            elif normalized:
                                items.append(normalized)
                        if items:
                            return {
                                "items": items,
                                "parse_error": "",
                                "schema_errors": schema_errors,
                                "raw_output_excerpt": normalized_text[:400],
                                "extracted_payload_excerpt": extracted_payload_excerpt,
                            }
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
        if self._implementation_retry_from_agent == "developer":
            return agent_name in {"architect", "implementation-planner", "task-designer"}
        if (
            agent_name == "implementation-planner"
            and not self._should_regenerate_implementation_backlog()
            and not self.retry_agent_name
            and self.from_agent_name != "implementation-planner"
            and self._load_canonical_implementation_backlog()
        ):
            self.logger.info(f"Skipping implementation-planner because canonical backlog exists: {self.canonical_backlog_path}")
            return True
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
            self._selected_task_from_resume = True
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
            self._selected_task_from_resume = True
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
        # A detected top-level project root that exists on disk must appear in repo_map; a
        # repo_map that omits a real source root is stale/broken and would mislead planning.
        # Generalizes a former gateway-v4/MYAI-specific guard to any project layout.
        top_directories = {
            str(entry).rstrip("/")
            for entry in (repo_map.get("top_level_tree") or [])
            if str(entry).endswith("/")
        }
        for root in self._detect_project_roots():
            if not root or "/" in root:
                continue  # only top-level roots are comparable to the top-level tree
            if (self.target_workspace / root).is_dir() and root not in top_directories:
                self._planner_parse_error = "repo_map_missing_project_root"
                self._planner_schema_errors = [f"{root}_exists_in_target_workspace_but_missing_from_repo_map"]
                return False
        return True

    def _resolve_selected_implementation_item(self, backlog: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not backlog:
            return None
        completed = set(self._completed_implementation_task_ids())
        first_incomplete = next((item for item in backlog if str(item.get("id")) not in completed), None)
        self._skipped_completed_task_ids = [
            str(item.get("id") or "").strip()
            for item in backlog
            if str(item.get("id") or "").strip() in completed
        ]
        if (self._selected_task_from_explicit_cli or self._selected_task_from_resume) and self.selected_task_ref:
            ref = self.selected_task_ref.strip()
            selected_by_ref: dict[str, Any] | None = None
            if ref.isdigit():
                index = int(ref) - 1
                if 0 <= index < len(backlog):
                    selected_by_ref = backlog[index]
            else:
                for item in backlog:
                    if str(item.get("id")) == ref:
                        selected_by_ref = item
                        break
            if selected_by_ref is None:
                return None
            selected_by_ref_id = str(selected_by_ref.get("id") or "").strip()
            if self._selected_task_from_explicit_cli:
                return selected_by_ref
            if selected_by_ref_id not in completed or self.rerun_completed:
                return selected_by_ref
            if selected_by_ref_id and selected_by_ref_id not in self._skipped_completed_task_ids:
                self._skipped_completed_task_ids.append(selected_by_ref_id)
            return first_incomplete
        if first_incomplete is None:
            return None
        if self.next_task_requested:
            return first_incomplete
        if self._implementation_backlog_source == "canonical_backlog":
            print(self._format_implementation_backlog(backlog, self._implementation_backlog_source))
            return first_incomplete
        if self.config["workflow"]["mode"] == "auto":
            print(self._format_implementation_backlog(backlog, self._implementation_backlog_source or "research"))
            return first_incomplete
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
        preview_lines = self._build_feedback_preview_lines(feedback)
        if preview_lines:
            self.logger.operator_box(
                f"Обратная связь сохранена -> {agent}",
                [f"Файл: {feedback_file}", "", *preview_lines],
                color="magenta",
            )
        return feedback_file

    def _developer_feedback_path_for_current_attempt(self) -> Path:
        feedback_root = self._engine_path(self.config.get("paths", {}).get("feedback_dir", ".openclaw/feedback"))
        run_id = self.logger.run_dir.name
        attempt = self._implementation_attempt if self._implementation_attempt > 0 else 1
        return feedback_root / self.project_id / run_id / f"attempt_{attempt}" / "developer.md"

    def _load_developer_feedback_for_retry(self) -> tuple[str, str]:
        candidates: list[Path] = []
        current_attempt_path = self._developer_feedback_path_for_current_attempt()
        candidates.append(current_attempt_path)
        if self._developer_feedback_file:
            candidates.append(Path(self._developer_feedback_file))
        latest_path = self._engine_path(self.config.get("paths", {}).get("feedback_dir", ".openclaw/feedback")) / self.project_id / "latest" / "developer.md"
        candidates.append(latest_path)
        for candidate in candidates:
            try:
                if candidate.exists():
                    text = candidate.read_text(encoding="utf-8").strip()
                    if text:
                        self._developer_feedback_source = str(candidate)
                        self._developer_feedback_chars = len(text)
                        return text, str(candidate)
            except Exception:
                continue
        self._developer_feedback_source = ""
        self._developer_feedback_chars = 0
        return "", ""

    @staticmethod
    def _extract_qa_verdict(parsed_output: str) -> str:
        text = str(parsed_output or "")
        match = re.search(r"Вердикт QA:\s*(.+)", text, re.IGNORECASE)
        if not match:
            return ""
        verdict = match.group(1).strip().lower()
        if "не пройдено" in verdict:
            return "failed"
        if "пройдено" in verdict:
            return "passed"
        return ""

    def _capture_developer_retry_feedback(self, task_id: int) -> None:
        sections: list[str] = []
        developer_report = self._load_saved_agent_report("implementation", "developer") or {}
        qa_report = self._load_saved_agent_report("implementation", "qa") or {}
        validator_report = self._load_saved_agent_report("implementation", "template-validator") or {}
        deterministic_report = self._load_saved_agent_report("implementation", "developer-checks") or {}

        developer_findings: list[str] = []
        last_write_error = str(developer_report.get("last_write_error") or "").strip()
        if last_write_error:
            developer_findings.append("Developer write validator error:")
            developer_findings.append(last_write_error)
        developer_result = str(developer_report.get("result") or "").strip()
        if developer_result and developer_result not in developer_findings:
            developer_findings.append("Developer result:")
            developer_findings.append(developer_result)
        if developer_findings:
            sections.append("\n".join(["Developer feedback", *developer_findings]))

        qa_output = str(qa_report.get("parsed_output") or "").strip()
        if qa_output:
            sections.append("\n".join(["QA findings to repair", qa_output]))

        validator_output = str(validator_report.get("parsed_output") or "").strip()
        if validator_output:
            sections.append("\n".join(["Template validator findings to respect", validator_output]))

        deterministic_output = str(deterministic_report.get("parsed_output") or deterministic_report.get("result") or "").strip()
        if deterministic_output:
            sections.append("\n".join(["Deterministic checks to repair", deterministic_output]))

        feedback_root = self._engine_path(self.config.get("paths", {}).get("feedback_dir", ".openclaw/feedback"))
        attempt = self._implementation_attempt if self._implementation_attempt > 0 else max(int(task_id or 0), 1)
        run_feedback_dir = feedback_root / self.project_id / self.logger.run_dir.name / f"attempt_{attempt}"
        for sub_agent in ("code-developer", "infra-developer", "test-developer"):
            sub_feedback_file = run_feedback_dir / f"{sub_agent}.md"
            try:
                sub_feedback = sub_feedback_file.read_text(encoding="utf-8").strip()
            except OSError:
                sub_feedback = ""
            if sub_feedback:
                sections.append("\n".join([f"{sub_agent} feedback to repair", sub_feedback]))

        if not sections:
            fallback_status = str(self._phase_failure_status or "").strip() or "unknown_failure"
            sections.append(
                "\n".join(
                    [
                        "Pipeline failure summary",
                        f"Failure status: {fallback_status}",
                        "No detailed QA or deterministic findings were captured for this attempt.",
                        "Inspect the latest agent reports for the failing sub-agent and the implementation developer report.",
                    ]
                )
            )
        feedback = "\n\n".join(
            [
                "Implementation repair feedback",
                "Fix all findings below in the next developer attempt.",
                *sections,
            ]
        )
        feedback_file = self._save_feedback(task_id, "developer", feedback)
        self._developer_feedback_file = str(feedback_file)
        self._developer_feedback_source = str(feedback_file)
        self._developer_feedback_chars = len(feedback)

    def _save_developer_checks_report(self, status: str, result: str, parsed_output: str = "") -> None:
        self.logger.save_agent_report(
            "implementation",
            "developer-checks",
            {
                "phase": "implementation",
                "agent": "developer-checks",
                "agent_name": "developer-checks",
                "status": status,
                "result": result,
                "parsed_output": parsed_output or result,
                "selected_task_id": self.selected_task_ref,
            },
        )
        self._update_human_report(final_status=status if status != "success" else "in_progress", agent_name="developer-checks")

    @staticmethod
    def _is_alembic_migration_path(path: str) -> bool:
        normalized = path.replace("\\", "/").lower()
        return "/alembic/versions/" in f"/{normalized}" or "/migrations/" in f"/{normalized}"

    def _validate_changed_migration_files(self, paths: list[str]) -> list[str]:
        issues: list[str] = []
        normalized_paths = [self._normalize_repo_relative_path(path) for path in paths if self._normalize_repo_relative_path(path)]
        migration_paths = [path for path in normalized_paths if self._is_alembic_migration_path(path)]
        for relative_path in migration_paths:
            candidate = self.target_workspace / relative_path
            if not candidate.exists() or not candidate.is_file():
                issues.append(f"{relative_path}: migration file is missing")
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                issues.append(f"{relative_path}: unable to read migration file: {exc}")
                continue
            revision_match = re.search(r"^revision\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE)
            down_revision_match = re.search(r"^down_revision\s*=\s*(.+)$", text, re.MULTILINE)
            revision = revision_match.group(1).strip() if revision_match else ""
            raw_down_revision = down_revision_match.group(1).strip() if down_revision_match else ""
            if not revision:
                issues.append(
                    f'{relative_path}: missing module-level revision assignment; add revision = "{candidate.stem}" after imports '
                    "(docstring Revision ID is not enough)"
                )
            if not raw_down_revision:
                issues.append(
                    f'{relative_path}: missing module-level down_revision assignment; add down_revision = "{self._configured_alembic_down_revision()}" '
                    "(docstring Revises is not enough)"
                )
                continue
            normalized_down_revision = raw_down_revision.split("#", 1)[0].strip().rstrip(",")
            normalized_down_revision = normalized_down_revision.strip()
            if normalized_down_revision.lower() == "none":
                issues.append(
                    f'{relative_path}: down_revision must not be None; use down_revision = "{self._configured_alembic_down_revision()}" '
                    "unless the task contract specifies another parent revision"
                )
                continue
            if normalized_down_revision.startswith(("'", '"')) and normalized_down_revision.endswith(("'", '"')):
                normalized_down_revision = normalized_down_revision[1:-1].strip()
            if not normalized_down_revision:
                issues.append(f"{relative_path}: down_revision is empty")
                continue
            versions_dir = candidate.parent
            revision_index: dict[str, str] = {}
            for sibling in versions_dir.glob("*.py"):
                try:
                    sibling_text = sibling.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                sibling_match = re.search(r"^revision\s*=\s*['\"]([^'\"]+)['\"]", sibling_text, re.MULTILINE)
                if sibling_match:
                    revision_index[sibling_match.group(1).strip()] = str(sibling.relative_to(self.target_workspace)).replace("\\", "/")
            if normalized_down_revision not in revision_index:
                issues.append(
                    f"{relative_path}: down_revision '{normalized_down_revision}' does not match any existing migration revision in {versions_dir}"
                )
        return issues

    def _run_developer_deterministic_checks(self) -> bool:
        developer_report = self._load_saved_agent_report("implementation", "developer") or {}
        changed_files = [
            self._normalize_repo_relative_path(path)
            for path in (developer_report.get("developer_changed_files") or [])
            if self._normalize_repo_relative_path(path)
        ]
        changed_python_files = [path for path in changed_files if path.endswith(".py")]
        item = self._selected_implementation_item or {}
        test_file_path = ""
        if isinstance(item.get("test_file"), dict):
            test_file_path = self._normalize_repo_relative_path((item.get("test_file") or {}).get("path"))

        findings: list[str] = []
        if changed_python_files:
            compile_command = [sys.executable, "-m", "py_compile", *changed_python_files]
            returncode, stdout, stderr = self._run_local_command(compile_command, timeout=30, cwd=self.target_workspace)
            if returncode != 0:
                findings.append("py_compile failed for changed Python files:")
                findings.append(stderr or stdout or "unknown py_compile failure")

        if test_file_path:
            candidate = self.target_workspace / test_file_path
            if not candidate.exists() or not candidate.is_file():
                findings.append(f"selected test_file is missing: {test_file_path}")
            elif test_file_path.endswith(".py") and Path(test_file_path).name != "__init__.py":
                selected_python, selected_python_source, pytest_available = self.resolve_python_executable()
                self.logger.info(f"selected_python={selected_python}")
                self.logger.info(f"selected_python_source={selected_python_source}")
                self.logger.info(f"pytest_available={pytest_available}")
                if not pytest_available:
                    findings.append("pytest is not available in the resolved Python environment")
                    findings.append(f"selected_python={selected_python}")
                    findings.append(f"selected_python_source={selected_python_source}")
                else:
                    returncode, stdout, stderr = self._run_local_command(
                        [selected_python, "-m", "pytest", test_file_path],
                        timeout=60,
                        cwd=self.target_workspace,
                    )
                    if returncode != 0:
                        findings.append(f"pytest failed for selected test file: {test_file_path}")
                        findings.append(stderr or stdout or "unknown pytest failure")

        findings.extend(self._validate_changed_migration_files(changed_python_files))

        if findings:
            parsed_output = "\n".join(findings)
            self._save_developer_checks_report("failed", "developer deterministic checks failed", parsed_output)
            feedback = "\n\n".join(
                [
                    "Deterministic validation findings",
                    parsed_output,
                ]
            )
            feedback_file = self._save_feedback(self.task_counter or 0, "developer", feedback)
            self._developer_feedback_file = str(feedback_file)
            self._developer_feedback_source = str(feedback_file)
            self._developer_feedback_chars = len(feedback)
            self._implementation_retry_from_agent = "developer"
            self._phase_failure_status = "developer_checks_failed"
            self.logger.error("Developer deterministic checks failed", parsed_output)
            self._log_retry_outcome_summary(
                "Human summary (RU)",
                [
                    "developer внёс правки, но автоматические проверки не пропустили результат.",
                    "Что сломалось: " + (findings[0] if findings else "см. feedback файл"),
                    f"Следующий шаг: retry developer с feedback из {feedback_file}",
                ],
            )
            return False

        self._save_developer_checks_report("success", "developer deterministic checks passed", "developer deterministic checks passed")
        return True

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
        if (
            self._selected_implementation_item
            and (
                agent_name in {"task-designer", "developer", "qa", "template-validator"}
                or self._is_multi_developer_edit_agent(agent_name)
            )
        ):
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
        ids: list[str] = []
        for record in self._load_completed_implementation_task_records():
            task_id = str(record.get("task_id") or "").strip()
            if task_id:
                ids.append(task_id)
        completed = self.project_settings.get("completed_implementation_tasks", [])
        if isinstance(completed, list):
            ids.extend(str(item).strip() for item in completed if str(item).strip())
        return list(dict.fromkeys(ids))

    def _load_completed_implementation_task_records(self) -> list[dict[str, Any]]:
        if not self.completed_tasks_path.exists():
            return []
        try:
            payload = json.loads(self.completed_tasks_path.read_text(encoding="utf-8"))
        except Exception:
            return []
        records = payload.get("completed_tasks")
        if not isinstance(records, list):
            return []
        return [dict(item) for item in records if isinstance(item, dict)]

    def _save_completed_implementation_task_records(self, records: list[dict[str, Any]]) -> None:
        self.completed_tasks_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"completed_tasks": records}
        self.completed_tasks_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _selected_task_dependency_status(self) -> dict[str, Any]:
        """Inspect the selected task's declared dependencies against the backlog and repo state.

        Returns a structured snapshot used for both diagnostics and dependency-aware
        task selection. A dependency is considered "blocking" when it is not recorded as
        completed, or when it is completed but the artifacts it was supposed to create are
        missing from the workspace. A declared dependency that is absent from the backlog and
        not completed is reported separately as ``missing_from_backlog``.
        """
        item = self._selected_implementation_item or {}
        task_id = str(item.get("id") or "").strip()
        declared_dependencies = [str(value).strip() for value in (item.get("depends_on") or []) if str(value).strip()]
        status: dict[str, Any] = {
            "selected_task_id": task_id,
            "declared_dependencies": declared_dependencies,
            "blocked": False,
            "blocking_dependency_ids": [],
            "dependency_missing_artifacts": {},
            "dependency_not_completed": [],
            "missing_from_backlog": [],
        }
        if not declared_dependencies:
            return status
        backlog = self._implementation_backlog_cache or []
        backlog_by_id = {
            str(candidate.get("id") or "").strip(): candidate
            for candidate in backlog
            if str(candidate.get("id") or "").strip()
        }
        completed = set(self._completed_implementation_task_ids())
        repo_map = self._load_repo_map()
        repo_files = {
            str(repo_item.get("path") or "").strip()
            for repo_item in (repo_map.get("files") or [])
            if str(repo_item.get("path") or "").strip()
        }
        repo_directories = {str(path).strip() for path in (repo_map.get("directories") or []) if str(path).strip()}

        def _mark_blocking(dependency_id: str) -> None:
            if dependency_id not in status["blocking_dependency_ids"]:
                status["blocking_dependency_ids"].append(dependency_id)

        for dependency_id in declared_dependencies:
            dependency_item = backlog_by_id.get(dependency_id)
            if dependency_item is None:
                # A completed dependency that simply dropped out of the current backlog is
                # treated as resolved (its artifacts cannot be re-verified here). A missing,
                # not-completed dependency is a hard error that cannot be auto-resolved.
                if dependency_id not in completed:
                    status["missing_from_backlog"].append(dependency_id)
                continue
            if dependency_id not in completed:
                status["dependency_not_completed"].append(dependency_id)
                _mark_blocking(dependency_id)
                continue
            missing_artifacts: list[str] = []
            for path in dependency_item.get("new_files") or []:
                normalized = self._normalize_repo_relative_path(path)
                if normalized and normalized not in repo_files:
                    missing_artifacts.append(normalized)
            for path in dependency_item.get("new_directories") or []:
                normalized = self._normalize_repo_relative_path(path)
                if normalized and normalized not in repo_directories:
                    missing_artifacts.append(normalized)
            if missing_artifacts:
                status["dependency_missing_artifacts"][dependency_id] = missing_artifacts
                _mark_blocking(dependency_id)
        status["blocked"] = bool(status["blocking_dependency_ids"] or status["missing_from_backlog"])
        return status

    def _format_unresolved_dependencies(self, status: dict[str, Any]) -> str:
        parts: list[str] = []
        for dependency_id in status.get("dependency_not_completed") or []:
            parts.append(f"{dependency_id}:not_completed")
        for dependency_id, artifacts in (status.get("dependency_missing_artifacts") or {}).items():
            parts.append(f"{dependency_id}:missing_artifacts:{', '.join(artifacts[:4])}")
        for dependency_id in status.get("missing_from_backlog") or []:
            parts.append(f"{dependency_id}:missing_from_backlog")
        return "; ".join(parts)

    def _format_dependency_missing_artifacts(self, status: dict[str, Any]) -> str:
        mapping = status.get("dependency_missing_artifacts") or {}
        if not mapping:
            return "none"
        return "; ".join(
            f"{dependency_id}:{', '.join(artifacts)}" for dependency_id, artifacts in mapping.items()
        )

    def _selected_task_dependency_error(self) -> str:
        status = self._selected_task_dependency_status()
        if not status["blocked"]:
            return ""
        task_id = status["selected_task_id"] or "selected task"
        return (
            f"Selected task {task_id} has incomplete dependencies. "
            f"Resolve depends_on first: {self._format_unresolved_dependencies(status)}"
        )

    def _handle_selected_task_dependencies(self) -> dict[str, Any]:
        """Validate the selected task's dependencies and apply dependency-aware selection.

        Behaviour:
        - If the selected task has no blocking dependencies, proceed unchanged.
        - If a declared dependency is missing from the backlog, fail clearly (no retry).
        - If the user explicitly selected this task via ``--task-id``, fail clearly once and
          tell them to run the blocking dependency first (no auto-switch, no retry loop).
        - Otherwise, automatically switch the selection to the first blocking dependency so
          the pipeline produces the missing artifacts before retrying the dependent task.

        Diagnostics are emitted for every code path. The return value is a dict with ``ok``
        (whether the phase may proceed) and ``status`` (the failure status code when ``ok`` is
        False).
        """
        status = self._selected_task_dependency_status()
        initial_id = status["selected_task_id"] or "selected task"
        explicit = bool(self._selected_task_from_explicit_cli)
        blocking_ids = status["blocking_dependency_ids"]
        missing_from_backlog = status["missing_from_backlog"]
        self.logger.info(f"Diagnostic selected_task_id_initial={initial_id}")
        self.logger.info(f"Diagnostic explicit_task_id={explicit}")
        self.logger.info(f"Diagnostic dependency_blocked={status['blocked']}")
        self.logger.info(f"Diagnostic blocking_dependency_ids={','.join(blocking_ids) if blocking_ids else 'none'}")
        self.logger.info(f"Diagnostic dependency_missing_artifacts={self._format_dependency_missing_artifacts(status)}")

        if not status["blocked"]:
            # Keep the pin while we are still on the auto-selected dependency itself (it is now
            # unblocked because task-designer produced its contract). Clearing it here would let
            # the retry-loop / selection guards treat the still-stale "completed" dependency as
            # done again on the next attempt and skip ahead to the dependent task. The pin is
            # reset when the phase restarts for the next task.
            if initial_id != self._dependency_forced_task_id:
                self._dependency_forced_task_id = ""
            self.logger.info(f"Diagnostic selected_task_id_final={initial_id}")
            self.logger.info("Diagnostic dependency_auto_selected=False")
            return {"ok": True, "status": "", "auto_selected": False}

        if missing_from_backlog:
            self._dependency_forced_task_id = ""
            self.logger.info(f"Diagnostic dependency_missing_from_backlog={','.join(missing_from_backlog)}")
            self.logger.error(
                f"Selected task {initial_id} depends on tasks missing from the backlog: "
                f"{', '.join(missing_from_backlog)}. Regenerate the backlog with --fresh-run before retrying."
            )
            self.logger.info(f"Diagnostic selected_task_id_final={initial_id}")
            self.logger.info("Diagnostic dependency_auto_selected=False")
            return {"ok": False, "status": "dependency_missing_from_backlog", "auto_selected": False}

        if explicit:
            self._dependency_forced_task_id = ""
            self.logger.error(
                f"Selected task {initial_id} has incomplete dependencies. "
                f"Resolve depends_on first: {self._format_unresolved_dependencies(status)}. "
                f"Run the blocking dependency first (for example --task-id {blocking_ids[0]}), "
                f"or pass --rerun-completed if its artifacts must be rebuilt."
            )
            self.logger.info(f"Diagnostic selected_task_id_final={initial_id}")
            self.logger.info("Diagnostic dependency_auto_selected=False")
            return {"ok": False, "status": "task_dependencies_incomplete", "auto_selected": False}

        blocking_id = blocking_ids[0]
        backlog_by_id = {
            str(candidate.get("id") or "").strip(): candidate
            for candidate in (self._implementation_backlog_cache or [])
            if str(candidate.get("id") or "").strip()
        }
        dependency_item = backlog_by_id.get(blocking_id)
        if dependency_item is None:
            self._dependency_forced_task_id = ""
            self.logger.info(f"Diagnostic dependency_missing_from_backlog={blocking_id}")
            self.logger.error(
                f"Selected task {initial_id} is blocked by dependency {blocking_id}, "
                f"which is missing from the backlog. Regenerate the backlog with --fresh-run."
            )
            self.logger.info(f"Diagnostic selected_task_id_final={initial_id}")
            self.logger.info("Diagnostic dependency_auto_selected=False")
            return {"ok": False, "status": "dependency_missing_from_backlog", "auto_selected": False}

        self.logger.info(f"{initial_id} blocked by {blocking_id}, selecting dependency {blocking_id}")
        switched_item = dict(dependency_item)
        # The dependency must be (re)built from scratch, so drop any stale task-designer
        # contract marker carried on the backlog entry.
        switched_item.pop("contract_source", None)
        self._selected_implementation_item = switched_item
        self._selected_task_source = self._selected_task_source_for(self._implementation_backlog_source)
        # Pin the auto-selected dependency so the completed-task guards in
        # _prepare_implementation_backlog_selection / the retry loop do not revert the
        # selection back to the dependent task before task-designer/developer run for it.
        self._dependency_forced_task_id = blocking_id
        self._update_canonical_backlog_selected_task(blocking_id)
        self.logger.info(f"Diagnostic selected_task_id_final={blocking_id}")
        self.logger.info("Diagnostic dependency_auto_selected=True")
        return {"ok": True, "status": "", "auto_selected": True}

    def _apply_task_designer_contract_with_retry(
        self, agent: dict[str, Any], *, index: int, total: int, max_retries: int = 2
    ) -> bool:
        """Apply the latest task-designer contract; on validation failure, regenerate.

        The task-designer is non-deterministic and can emit an invalid contract (e.g. a
        migration test for a table no in-scope migration creates). Rather than failing the
        whole run on a single bad draft, re-run the task-designer with the validation
        feedback injected so it can self-correct.
        """
        report = self._load_saved_agent_report("implementation", "task-designer") or {}
        if self._apply_task_designer_contract_from_report(report):
            return True
        for attempt in range(1, max_retries + 1):
            self.logger.info(
                f"Task-designer contract invalid; regenerating with feedback (retry {attempt}/{max_retries})"
            )
            self._task_designer_feedback_for_prompt = True
            try:
                ran = self._run_agent(agent, "implementation", index=index, total=total)
            finally:
                self._task_designer_feedback_for_prompt = False
            if not ran:
                return False
            report = self._load_saved_agent_report("implementation", "task-designer") or {}
            if self._apply_task_designer_contract_from_report(report):
                return True
        return False

    def _run_task_designer_before_developer(self, phase: dict[str, Any], *, total: int) -> bool:
        task_designer_config: dict[str, Any] | None = None
        task_designer_index = 0
        for idx, candidate in enumerate(phase.get("agents", []), start=1):
            if str(candidate.get("name") or "") == "task-designer":
                task_designer_config = candidate
                task_designer_index = idx
                break
        if not task_designer_config:
            return False
        self.logger.info("Selected task requires a fresh task-designer contract. Running task-designer before developer.")
        if not self._run_agent(task_designer_config, "implementation", index=task_designer_index, total=total):
            return False
        return self._apply_task_designer_contract_with_retry(
            task_designer_config, index=task_designer_index, total=total
        )

    def _implementation_completion_changed_files(self) -> list[str]:
        diagnostics = self._collect_scope_watchdog_diff_diagnostics()
        changed_files = [str(path).strip() for path in diagnostics.get("changed_files") or [] if str(path).strip()]
        if changed_files:
            return sorted(dict.fromkeys(changed_files))
        developer_report = self._load_saved_agent_report("implementation", "developer") or {}
        return sorted(
            dict.fromkeys(
                str(path).strip()
                for path in (developer_report.get("developer_changed_files") or [])
                if str(path).strip()
            )
        )

    def _current_head_commit(self) -> str:
        if self.repo is None:
            return ""
        try:
            return str(self.repo.head.commit.hexsha)
        except Exception:
            return ""

    def _completion_selected_implementation_item(self) -> dict[str, Any]:
        if self._selected_implementation_item:
            return dict(self._selected_implementation_item)
        payload = self._load_canonical_implementation_backlog_payload()
        selected_task_id = str(payload.get("selected_task_id") or "").strip()
        backlog = self._implementation_backlog_cache or self._load_canonical_implementation_backlog()
        if selected_task_id:
            for item in backlog:
                if str(item.get("id") or "").strip() == selected_task_id:
                    self._selected_implementation_item = dict(item)
                    return dict(item)
        if self.selected_task_ref and not self.selected_task_ref.isdigit():
            for item in backlog:
                if str(item.get("id") or "").strip() == self.selected_task_ref:
                    self._selected_implementation_item = dict(item)
                    return dict(item)
        return {}

    def _mark_implementation_task_completed(self, *, changed_files: list[str] | None = None) -> bool:
        self._completed_task_recorded = False
        self._completed_task_record_error = ""
        selected_item = self._completion_selected_implementation_item()
        if not selected_item:
            self._completed_task_record_error = (
                "selected implementation task is unavailable; cannot update completed task registry. "
                f"Manual recovery: create {self.completed_tasks_path} with "
                '{"completed_tasks":[{"task_id":"TASK-XXX","title":"...","completed_at":"...","run_id":"...","commit":null,"changed_files":[]}]}'
            )
            return False
        task_id = str(selected_item.get("id") or "").strip()
        if not task_id:
            self._completed_task_record_error = "selected implementation task has no id"
            return False
        records = self._load_completed_implementation_task_records()
        existing_index = next(
            (index for index, record in enumerate(records) if str(record.get("task_id") or "").strip() == task_id),
            None,
        )
        record = {
            "task_id": task_id,
            "title": str(selected_item.get("title") or ""),
            "completed_at": datetime.now().isoformat(),
            "commit": self._current_head_commit() or None,
            "run_id": self.logger.run_dir.name,
            "changed_files": sorted(dict.fromkeys(changed_files or [])),
        }
        if existing_index is None:
            records.append(record)
        else:
            records[existing_index] = {**records[existing_index], **record}
        try:
            self._save_completed_implementation_task_records(records)
        except Exception as exc:
            self._completed_task_record_error = f"failed to write {self.completed_tasks_path}: {exc}"
            return False

        completed = self._completed_implementation_task_ids()
        if task_id not in completed:
            completed.append(task_id)
        self.project_settings["completed_implementation_tasks"] = completed
        self._save_project_settings()
        self._completed_task_recorded = True
        return True

    def mark_selected_implementation_task_complete(self) -> int:
        if not self.selected_task_ref:
            payload = self._load_canonical_implementation_backlog_payload()
            selected_task_id = str(payload.get("selected_task_id") or "").strip()
            if selected_task_id:
                self.selected_task_ref = selected_task_id
                self._selected_task_from_explicit_cli = True
        original_rerun_completed = self.rerun_completed
        self.rerun_completed = True
        try:
            selection = self._prepare_implementation_backlog_selection(require_backlog=True)
            if selection["error"]:
                print(selection["error"])
                return 1
            if not self._mark_implementation_task_completed(changed_files=[]):
                print(self._completed_task_record_error)
                return 1
        finally:
            self.rerun_completed = original_rerun_completed
            self._selected_task_from_explicit_cli = bool(self.selected_task_ref)
        task_id = str((self._selected_implementation_item or {}).get("id") or "").strip()
        print(f"Marked implementation task complete: {task_id}")
        print(f"completed_task_registry_path={self.completed_tasks_path}")
        return 0

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
        for name in ("context", "memory", "logs", "summaries", "state"):
            (base / name).mkdir(parents=True, exist_ok=True)
        settings_path = base / "settings.yaml"
        local_settings_path = base / "local.yaml"
        codex_context_path = base / "codex.md"
        resume_context_path = base / "resume.md"
        settings_payload: dict[str, Any] = {}
        local_settings_payload: dict[str, Any] = {}
        if settings_path.exists():
            settings_payload = yaml.safe_load(settings_path.read_text(encoding="utf-8")) or {}
        if local_settings_path.exists():
            local_settings_payload = yaml.safe_load(local_settings_path.read_text(encoding="utf-8")) or {}
        settings_payload.pop("target_workspace", None)
        settings_payload.pop("engine_root", None)
        shared_payload = dict(settings_payload)
        shared_payload.update(
            {
                "project_id": self.project_id,
                "git_remote": self.git_remote,
                "completed_implementation_tasks": shared_payload.get("completed_implementation_tasks", []),
            }
        )
        local_payload = dict(local_settings_payload)
        local_payload.update(
            {
                "project_id": self.project_id,
                "target_workspace": str(self.target_workspace),
                "engine_root": str(self.engine_root),
            }
        )
        settings_path.write_text(yaml.safe_dump(shared_payload, sort_keys=False), encoding="utf-8")
        local_settings_path.write_text(yaml.safe_dump(local_payload, sort_keys=False), encoding="utf-8")
        if not codex_context_path.exists():
            codex_context_path.write_text(PROJECT_CODEX_TEMPLATE, encoding="utf-8")
        if not resume_context_path.exists():
            resume_context_path.write_text(PROJECT_RESUME_TEMPLATE, encoding="utf-8")
        return base

    def _load_project_settings(self) -> dict[str, Any]:
        if not self.project_settings_path.exists():
            return {}
        return yaml.safe_load(self.project_settings_path.read_text(encoding="utf-8")) or {}

    def _load_project_local_settings(self) -> dict[str, Any]:
        if not self.project_local_settings_path.exists():
            return {}
        return yaml.safe_load(self.project_local_settings_path.read_text(encoding="utf-8")) or {}

    def _load_project_codex_context(self) -> str:
        if not self.project_codex_context_path.exists():
            return ""
        content = self.project_codex_context_path.read_text(encoding="utf-8").strip()
        if not content or content == PROJECT_CODEX_TEMPLATE.strip():
            return ""
        return content

    def _load_project_resume_context(self) -> str:
        if not self.project_resume_context_path.exists():
            return ""
        content = self.project_resume_context_path.read_text(encoding="utf-8").strip()
        if not content or content == PROJECT_RESUME_TEMPLATE.strip():
            return ""
        return content

    def _save_project_settings(self) -> None:
        self.project_settings_path.write_text(yaml.safe_dump(self.project_settings, sort_keys=False), encoding="utf-8")

    def _build_project_codex_context_summary(self, limit: int = 2400) -> str:
        context = str(self.project_codex_context or "").strip()
        if not context:
            return ""
        summary = (
            "Shared Codex project context synced via git. "
            "Treat it as durable project memory that applies across machines.\n"
            f"{context}"
        )
        if len(summary) <= limit:
            return summary
        return summary[:limit].rstrip()

    def _build_project_resume_context_summary(self, limit: int = 2600) -> str:
        context = str(self.project_resume_context or "").strip()
        if not context:
            return ""
        summary = (
            "Shared resume handoff synced via git. "
            "Use it to continue from the latest known project checkpoint across machines.\n"
            f"{context}"
        )
        if len(summary) <= limit:
            return summary
        return summary[:limit].rstrip()

    def _persist_project_codex_context(self) -> None:
        try:
            auto_block = self._build_project_codex_auto_block()
            existing = (
                self.project_codex_context_path.read_text(encoding="utf-8")
                if self.project_codex_context_path.exists()
                else PROJECT_CODEX_TEMPLATE
            )
            start = existing.find(PROJECT_CODEX_AUTO_START)
            end = existing.find(PROJECT_CODEX_AUTO_END)
            if start != -1 and end != -1 and end > start:
                updated = (
                    existing[:start].rstrip()
                    + "\n\n"
                    + auto_block
                    + "\n"
                    + existing[end + len(PROJECT_CODEX_AUTO_END):].lstrip()
                ).rstrip() + "\n"
            else:
                updated = existing.rstrip() + "\n\n" + auto_block + "\n"
            self.project_codex_context_path.write_text(updated, encoding="utf-8")
            self.project_codex_context = self._load_project_codex_context()
        except Exception as exc:
            self.logger.warning(f"Unable to persist project Codex context automatically: {exc}")

    def _persist_project_resume_context(self) -> None:
        try:
            auto_block = self._build_project_resume_auto_block()
            existing = (
                self.project_resume_context_path.read_text(encoding="utf-8")
                if self.project_resume_context_path.exists()
                else PROJECT_RESUME_TEMPLATE
            )
            start = existing.find(PROJECT_RESUME_AUTO_START)
            end = existing.find(PROJECT_RESUME_AUTO_END)
            if start != -1 and end != -1 and end > start:
                updated = (
                    existing[:start].rstrip()
                    + "\n\n"
                    + auto_block
                    + "\n"
                    + existing[end + len(PROJECT_RESUME_AUTO_END):].lstrip()
                ).rstrip() + "\n"
            else:
                updated = existing.rstrip() + "\n\n" + auto_block + "\n"
            self.project_resume_context_path.write_text(updated, encoding="utf-8")
            self.project_resume_context = self._load_project_resume_context()
        except Exception as exc:
            self.logger.warning(f"Unable to persist project resume context automatically: {exc}")

    def _build_project_codex_auto_block(self) -> str:
        reports = self.logger._load_agent_reports()
        run_summary_path = self.logger.save_run_summary()
        run_summary = json.loads(run_summary_path.read_text(encoding="utf-8")) if run_summary_path.exists() else {}
        phase_names = sorted({str(report.get("phase") or "").strip() for report in reports if str(report.get("phase") or "").strip()})
        lines = [
            PROJECT_CODEX_AUTO_START,
            "## Auto-updated Run Context",
            "",
            f"- updated_at: {datetime.now().isoformat(timespec='seconds')}",
            f"- last_run_id: {self.logger.run_dir.name}",
            f"- phases_touched: {', '.join(phase_names) if phase_names else 'none'}",
            f"- completed_agents: {run_summary.get('completed_agents', 0)}",
            f"- failed_agents: {run_summary.get('failed_agents', 0)}",
            f"- total_tokens: {run_summary.get('total_tokens', 0)}",
            f"- estimated_cost_usd: {run_summary.get('estimated_cost_usd', 0.0)}",
        ]
        if self.saved_user_goal:
            lines.extend(["", "### Saved User Goal", "", self.saved_user_goal])
        completed_tasks = self._completed_implementation_task_ids()
        if completed_tasks:
            lines.extend(["", "### Completed Implementation Tasks", ""])
            lines.extend(f"- {task_id}" for task_id in completed_tasks[:20])
        research_summaries = self._load_research_handoff_summaries()
        if research_summaries:
            lines.extend(["", "### Latest Research Handoffs", ""])
            for summary in research_summaries[:4]:
                handoff_text = str(summary.get("handoff_summary") or "").strip()
                sections = self._extract_handoff_sections(handoff_text)
                finding = next(
                    (
                        item
                        for item in (sections.get("findings") or [])
                        if not str(item).strip().lower().startswith("agent:")
                    ),
                    "",
                )
                recommendation = next(iter(sections.get("recommended_next_tasks") or []), "")
                compact = finding or recommendation or (handoff_text.splitlines()[0] if handoff_text else "")
                compact = " ".join(str(compact).split())
                if compact:
                    lines.append(f"- {summary.get('agent_name')}: {compact[:240]}")
        if self._selected_implementation_item:
            selected_id = str(self._selected_implementation_item.get("id") or "").strip()
            selected_scope = str(self._selected_implementation_item.get("scope") or "").strip()
            if selected_id or selected_scope:
                lines.extend(["", "### Current Selected Implementation Task", ""])
                if selected_id:
                    lines.append(f"- id: {selected_id}")
                if selected_scope:
                    lines.append(f"- scope: {' '.join(selected_scope.split())[:300]}")
        lines.append(PROJECT_CODEX_AUTO_END)
        return "\n".join(lines)

    def _build_project_resume_auto_block(self) -> str:
        reports = self.logger._load_agent_reports()
        run_summary_path = self.logger.save_run_summary()
        run_summary = json.loads(run_summary_path.read_text(encoding="utf-8")) if run_summary_path.exists() else {}
        last_report = reports[-1] if reports else {}
        last_agent = str(last_report.get("agent_name") or "").strip() or "none"
        last_phase = str(last_report.get("phase") or "").strip() or "none"
        last_status = str(last_report.get("status") or "").strip() or "none"
        next_step = self._build_resume_next_step_hint(run_summary=run_summary, reports=reports)
        lines = [
            PROJECT_RESUME_AUTO_START,
            "## Resume Checkpoint",
            "",
            f"- updated_at: {datetime.now().isoformat(timespec='seconds')}",
            f"- last_run_id: {self.logger.run_dir.name}",
            f"- last_phase: {last_phase}",
            f"- last_agent: {last_agent}",
            f"- last_status: {last_status}",
            f"- next_step: {next_step}",
        ]
        if self.saved_user_goal:
            lines.extend(["", "### User Goal", "", self.saved_user_goal])
        task_lines = self._build_resume_selected_task_lines()
        if task_lines:
            lines.extend(["", "### Current Task", ""])
            lines.extend(task_lines)
        failure_lines = self._build_resume_failure_lines(reports)
        if failure_lines:
            lines.extend(["", "### Attention", ""])
            lines.extend(failure_lines)
        handoff_lines = self._build_resume_handoff_lines()
        if handoff_lines:
            lines.extend(["", "### Latest Handoffs", ""])
            lines.extend(handoff_lines)
        lines.append(PROJECT_RESUME_AUTO_END)
        return "\n".join(lines)

    def _build_resume_next_step_hint(self, *, run_summary: dict[str, Any], reports: list[dict[str, Any]]) -> str:
        failed_agents = int(run_summary.get("failed_agents") or 0)
        if failed_agents > 0 and reports:
            failed = next((report for report in reversed(reports) if str(report.get("status") or "") != "success"), {})
            failed_agent = str(failed.get("agent_name") or failed.get("agent") or "").strip()
            failed_result = " ".join(str(failed.get("result") or "").split())
            if failed_agent and failed_result:
                return f"Inspect failed agent {failed_agent}: {failed_result[:220]}"
            if failed_agent:
                return f"Inspect failed agent {failed_agent} before continuing."
        if self._selected_implementation_item:
            task_id = str(self._selected_implementation_item.get("id") or "").strip()
            scope = " ".join(str(self._selected_implementation_item.get("scope") or "").split())
            if task_id and scope:
                return f"Continue implementation task {task_id}: {scope[:220]}"
            if task_id:
                return f"Continue implementation task {task_id}."
        completed_tasks = self._completed_implementation_task_ids()
        if completed_tasks:
            return "Resume from the next uncompleted implementation task or start deployment if implementation is done."
        research_summaries = self._load_research_handoff_summaries()
        if research_summaries:
            return "Use the latest research handoffs to start or continue implementation."
        return "Review the latest repo state and choose the next phase."

    def _build_resume_selected_task_lines(self) -> list[str]:
        item = dict(self._selected_implementation_item or {})
        if not item:
            report, _source = self._load_latest_implementation_report_with_selected_task()
            if report:
                item = dict(report.get("selected_task_contract") or {})
                if not item:
                    item = {
                        "id": report.get("selected_task_id"),
                        "scope": report.get("selected_task_scope"),
                        "allowed_paths": report.get("selected_task_allowed_paths") or [],
                    }
        task_id = str(item.get("id") or "").strip()
        scope = " ".join(str(item.get("scope") or "").split())
        allowed_paths = [str(path).strip() for path in (item.get("allowed_paths") or []) if str(path).strip()]
        lines: list[str] = []
        if task_id:
            lines.append(f"- id: {task_id}")
        if scope:
            lines.append(f"- scope: {scope[:300]}")
        if allowed_paths:
            lines.append(f"- allowed_paths: {', '.join(allowed_paths[:6])}")
        return lines

    @staticmethod
    def _build_resume_failure_lines(reports: list[dict[str, Any]]) -> list[str]:
        failed = [report for report in reports if str(report.get("status") or "") != "success"]
        lines: list[str] = []
        for report in failed[-3:]:
            agent_name = str(report.get("agent_name") or report.get("agent") or "").strip() or "agent"
            result = " ".join(str(report.get("result") or "").split())
            status = str(report.get("status") or "").strip() or "failed"
            if result:
                lines.append(f"- {agent_name} [{status}]: {result[:240]}")
            else:
                lines.append(f"- {agent_name} [{status}]")
        return lines

    def _build_resume_handoff_lines(self) -> list[str]:
        lines: list[str] = []
        for summary in self._load_research_handoff_summaries()[:4]:
            handoff_text = str(summary.get("handoff_summary") or "").strip()
            sections = self._extract_handoff_sections(handoff_text)
            finding = next(
                (
                    item
                    for item in (sections.get("findings") or [])
                    if not str(item).strip().lower().startswith("agent:")
                ),
                "",
            )
            recommendation = next(iter(sections.get("recommended_next_tasks") or []), "")
            compact = finding or recommendation or (handoff_text.splitlines()[0] if handoff_text else "")
            compact = " ".join(str(compact).split())
            if compact:
                lines.append(f"- {summary.get('agent_name')}: {compact[:240]}")
        return lines

    def _set_user_goal(self, goal: str) -> None:
        normalized = str(goal or "").strip()
        self.user_goal = normalized
        if normalized:
            self.saved_user_goal = normalized
            self.project_settings["user_goal"] = normalized
        else:
            self.saved_user_goal = ""
            self.project_settings.pop("user_goal", None)
        self._save_project_settings()

    def _should_prompt_for_user_goal(self, phase_key: str) -> bool:
        if self.config["workflow"]["mode"] == "auto":
            return False
        if self.user_goal_override:
            return False
        if phase_key == "research":
            return True
        if phase_key != "implementation":
            return False
        if self.from_agent_name in {"developer", "qa", "template-validator"}:
            return False
        return True

    def _ensure_user_goal(self, phase_key: str) -> bool:
        if self.user_goal_override:
            return True
        if not self._should_prompt_for_user_goal(phase_key):
            return True
        current_goal = str(self.saved_user_goal or "").strip()
        if current_goal:
            self.logger.info(f"Saved user goal: {current_goal}")
            prompt = "Укажите цель этого прогона (Enter = оставить текущую): "
        else:
            prompt = "Укажите цель этого прогона (Enter = использовать стандартный scope): "
        answer = input(prompt).strip()
        if answer:
            self._set_user_goal(answer)
        elif current_goal:
            self.user_goal = current_goal
        return True

    def _log_startup_diagnostics(self) -> None:
        self.logger.info(f"Startup diagnostic: engine_root={self.engine_root}")
        self.logger.info(f"Startup diagnostic: launch_cwd={self.launch_cwd}")
        self.logger.info(f"Startup diagnostic: target_workspace={self.target_workspace}")
        self.logger.info(f"Startup diagnostic: project_id={self.project_id}")
        self.logger.info(f"Startup diagnostic: git_remote={self.git_remote or 'unavailable'}")
        self.logger.info(f"Startup diagnostic: project_settings_path={self.project_settings_path}")
        self.logger.info(f"Startup diagnostic: project_local_settings_path={self.project_local_settings_path}")
        self.logger.info(f"Startup diagnostic: project_codex_context_path={self.project_codex_context_path}")
        self.logger.info(f"Startup diagnostic: project_resume_context_path={self.project_resume_context_path}")
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
        if not pricing:
            pricing = self.pricing.get(self._normalize_model_for_pricing(model))
        if not pricing or input_tokens is None or output_tokens is None:
            return None
        input_rate = float(pricing.get("input_per_1m_usd") or 0.0)
        output_rate = float(pricing.get("output_per_1m_usd") or 0.0)
        cost = (input_tokens / 1_000_000) * input_rate + (output_tokens / 1_000_000) * output_rate
        return round(cost, 6)

    @staticmethod
    def _normalize_model_for_pricing(model: str) -> str:
        normalized = str(model or "").strip()
        if not normalized:
            return normalized
        claude_sonnet_match = re.match(r"^(?P<prefix>(?:openrouter/)?anthropic/)?claude-(?P<version>\d+(?:\.\d+)?)-sonnet(?:-(?P<date>\d{8}))?$", normalized)
        if claude_sonnet_match:
            prefix = claude_sonnet_match.group("prefix") or ""
            version = claude_sonnet_match.group("version")
            reordered = f"{prefix}claude-sonnet-{version}"
            return reordered
        candidates = [normalized]
        if normalized.startswith("openrouter/"):
            candidates.append(normalized[len("openrouter/"):])
        if normalized.startswith("anthropic/"):
            suffix = normalized[len("anthropic/"):]
            candidates.append(suffix)
        if normalized.startswith("deepseek/"):
            suffix = normalized[len("deepseek/"):]
            candidates.append(suffix)
        stripped_date = re.sub(r"([:/-])\d{8}$", "", normalized)
        if stripped_date != normalized:
            candidates.append(stripped_date)
        stripped_version = re.sub(r"[-:]20\d{6,}$", "", normalized)
        if stripped_version != normalized:
            candidates.append(stripped_version)
        for candidate in list(candidates):
            if candidate.startswith("openrouter/"):
                candidates.append(candidate[len("openrouter/"):])
        for candidate in candidates:
            if candidate.startswith("anthropic/"):
                candidates.append(candidate[len("anthropic/"):])
            if candidate.startswith("deepseek/"):
                candidates.append(candidate[len("deepseek/"):])
        for candidate in candidates:
            cleaned = re.sub(r"-20\d{6,}$", "", candidate)
            cleaned = re.sub(r":20\d{6,}$", "", cleaned)
            if cleaned:
                return cleaned
        return normalized

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

    @staticmethod
    def _direct_api_max_tokens(phase: str, agent_name: str) -> int | None:
        if phase == "research":
            if agent_name == "product-manager":
                return 2200
            if agent_name in {"project-analyst", "market-analyst", "tech-analyst"}:
                return 1800
            return 1400
        if phase == "implementation":
            if agent_name == "architect":
                return 2200
            if agent_name == "implementation-planner":
                return None
            if agent_name == "task-designer":
                return 1600
            # Developer-class agents emit whole files via write_file, so they need a large
            # output budget. A tight cap truncates the write_file JSON mid-content, which then
            # fails to parse as a tool call and is silently dropped (the file is never written).
            if agent_name == "developer" or agent_name in {"code-developer", "infra-developer", "test-developer"}:
                return 8000
            if agent_name == "qa":
                return 1600
            if agent_name == "template-validator":
                return 1000
        if phase == "deployment":
            return 1600
        return 1600

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
        if "exceeded retrieval rounds" in combined or "превышен лимит раундов retrieval" in combined:
            return "retrieval rounds exceeded"
        if "llm request timed out" in combined:
            return "llm request timeout"
        if "the model did not produce a response before the llm idle timeout" in combined:
            return "llm idle timeout"
        if "idle timeout" in combined and "model did not produce a response" in combined:
            return "llm idle timeout"
        if "did not produce a response" in combined:
            return "llm produced no response"
        return ""

    @staticmethod
    def _detect_agent_output_contract_failure(phase: str, agent_name: str, parsed_output: str) -> str:
        normalized = str(parsed_output or "").strip()
        lowered = normalized.lower()
        if phase == "implementation" and agent_name == "qa":
            required_markers = [
                "вердикт qa:",
                "проверенные файлы:",
                "соответствие контракту:",
                "замечания:",
                "итог:",
            ]
            if any(marker not in lowered for marker in required_markers):
                return "qa incomplete final report"
            unfinished_prefixes = (
                "проверяю ",
                "читаю ",
                "нужно убедиться",
                "необходимо убедиться",
            )
            last_line = normalized.splitlines()[-1].strip().lower() if normalized else ""
            if any(last_line.startswith(prefix) for prefix in unfinished_prefixes):
                return "qa incomplete final report"
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
