import json
import sys
from pathlib import Path
from types import SimpleNamespace

import git
import manage_agents as manage_agents_module
import yaml
from manage_agents import AgentManager
from monitor_logs import LogMonitor
from start import build_parser
from workflow.orchestrator import WorkflowOrchestrator


def _write_minimal_workflow_config(config_path: Path, workspace: str = ".") -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "workflow": {
                    "executor": "direct_api",
                    "mode": "auto",
                    "console_language": "ru",
                    "max_phase_cost_usd": None,
                    "default_implementation_scope": "Implement backend-only MVP provider performance monitoring and smart routing foundation. No marketplace, no Stripe changes, no frontend changes except API client stubs if required.",
                    "implementation_scope_policy": {
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
                    },
                    "require_registry_preflight": False,
                    "require_model_list_preflight": False,
                },
                "project": {
                    "name": "agents-pipeline",
                    "workspace": workspace,
                    "default_branch": "main",
                },
                "paths": {
                    "agents_dir": ".openclaw/agents",
                    "logs_dir": ".openclaw/logs",
                    "feedback_dir": ".openclaw/feedback",
                },
                "phases": {},
                "runtime": {
                    "provider": "openrouter",
                    "model": "perplexity/sonar",
                    "thinking": "low",
                },
                "git": {
                    "enabled": False,
                    "branch_prefix": "feature/",
                    "auto_rollback": True,
                },
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _seed_research_run(log_root: Path, run_id: str, reports: list[dict[str, object]]) -> Path:
    run_dir = log_root / f"run_{run_id}"
    research_dir = run_dir / "agents" / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    for report in reports:
        agent_name = str(report["agent_name"])
        payload = {
            "agent_name": agent_name,
            "status": "success",
            "result": "completed",
            **report,
        }
        (research_dir / f"{agent_name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return run_dir


def test_config_loads() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    assert "phases" in orchestrator.config
    assert "research" in orchestrator.config["phases"]
    assert orchestrator.config["project"]["name"] == "agents-pipeline"
    assert orchestrator.config["workflow"]["executor"] == "direct_api"
    assert orchestrator.config["workflow"]["max_phase_cost_usd"] is None
    assert "default_implementation_scope" in orchestrator.config["workflow"]
    assert orchestrator.config["workflow"]["require_registry_preflight"] is False
    assert orchestrator.config["workflow"]["require_model_list_preflight"] is False
    assert orchestrator.runtime.executor == "direct_api"
    assert orchestrator.runtime.require_registry_preflight is False
    assert orchestrator.runtime.require_model_list_preflight is False


def test_start_parser_accepts_retry_resume_flags() -> None:
    args = build_parser().parse_args(
        [
            "--phase",
            "implementation",
            "--retry-agent",
            "implementation-planner",
            "--from-agent",
            "implementation-planner",
            "--reuse-architect",
        ]
    )

    assert args.retry_agent == "implementation-planner"
    assert args.from_agent == "implementation-planner"
    assert args.reuse_architect is True


def test_start_parser_accepts_from_agent_developer() -> None:
    args = build_parser().parse_args(
        [
            "--phase",
            "implementation",
            "--from-agent",
            "developer",
        ]
    )

    assert args.from_agent == "developer"


def test_start_parser_accepts_goal() -> None:
    args = build_parser().parse_args(
        [
            "--phase",
            "research",
            "--goal",
            "Implement only repo-map validation improvements",
        ]
    )

    assert args.goal == "Implement only repo-map validation improvements"


def test_create_git_branch_reuses_existing_branch_on_rerun(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repo = git.Repo.init(workspace)
    repo.git.checkout("-b", "main")
    tracked = workspace / "README.md"
    tracked.write_text("seed\n", encoding="utf-8")
    repo.git.add("README.md")
    repo.index.commit("initial")

    config_path = tmp_path / "workflow.yaml"
    _write_minimal_workflow_config(config_path, workspace=str(workspace))
    payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    payload["git"]["enabled"] = True
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")

    orchestrator = WorkflowOrchestrator(str(config_path))

    assert orchestrator._create_git_branch(1) is True
    assert repo.active_branch.name == "feature/task_1"
    repo.git.checkout("main")

    assert orchestrator._create_git_branch(1) is True
    assert repo.active_branch.name == "feature/task_1"
    assert orchestrator.current_branch == "feature/task_1"


def test_agent_directories_exist() -> None:
    manager = AgentManager()
    agents = manager.list_agents()
    assert "research" in agents
    assert "implementation" in agents
    assert "competitor-analyst" in agents["research"]
    assert "architect" in agents["implementation"]
    assert "implementation-planner" in agents["implementation"]
    assert "task-designer" in agents["implementation"]


def test_user_goal_becomes_default_implementation_scope(tmp_path: Path) -> None:
    config_path = tmp_path / "workflow.yaml"
    _write_minimal_workflow_config(config_path)

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        user_goal="Implement only a status command for the pipeline",
    )

    assert orchestrator._select_implementation_scope([]) == "Implement only a status command for the pipeline"


def test_user_goal_is_included_in_implementation_phase_context(tmp_path: Path) -> None:
    config_path = tmp_path / "workflow.yaml"
    _write_minimal_workflow_config(config_path)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("demo\n", encoding="utf-8")

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        workspace=str(workspace),
        user_goal="Fix planner validation only",
    )
    orchestrator._repo_map_cache = {
        "target_workspace": str(workspace),
        "project_id": "workspace",
        "top_level_tree": ["README.md"],
        "directories": [],
        "files": [{"path": "README.md"}],
        "entrypoints": [],
        "dependency_files": [],
        "config_files": [],
        "test_files": [],
        "docker_files": [],
        "agent_relevant_files": [],
    }

    context = orchestrator._build_implementation_phase_context("architect")

    assert "User goal" in context["repository_context"]
    assert "Fix planner validation only" in context["repository_context"]
    assert context["selected_task_scope"] == "Fix planner validation only"
    assert context["user_goal"] == "Fix planner validation only"


def test_research_phase_prompts_for_user_goal_in_interactive_mode(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "workflow.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path))
    orchestrator.config["workflow"]["mode"] = "interactive"

    prompts: list[str] = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return "Improve repo-map diagnostics"

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(orchestrator, "_preflight_runtime", lambda _phase: True)
    monkeypatch.setattr(orchestrator, "run_phase", lambda _phase: True)

    assert orchestrator.run_research_phase() is True
    assert prompts
    assert orchestrator.user_goal == "Improve repo-map diagnostics"


def test_agent_catalog_loads() -> None:
    manager = AgentManager()
    catalog = manager.agent_catalog
    assert "agents" in catalog
    assert any(agent["name"] == "qa" for agent in catalog["agents"])


def test_log_monitor_handles_missing_logs() -> None:
    monitor = LogMonitor(".openclaw/logs")
    Path(".openclaw/logs").mkdir(parents=True, exist_ok=True)
    assert monitor.get_latest_log() is None or monitor.get_latest_log().exists()


def test_manager_prefers_openclaw_bin_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("OPENCLAW_BIN", "custom-openclaw.cmd")
    manager = AgentManager()
    assert manager._get_openclaw_bin() == "custom-openclaw.cmd"


def test_prepare_command_for_windows_wraps_cmd_and_bat(monkeypatch) -> None:
    monkeypatch.setattr(manage_agents_module.os, "name", "nt")
    monkeypatch.setenv("COMSPEC", "C:\\Windows\\System32\\cmd.exe")

    wrapped_cmd = AgentManager._prepare_command_for_windows(["openclaw.cmd", "agents", "list"])
    wrapped_bat = AgentManager._prepare_command_for_windows(["openclaw.bat", "agents", "list"])
    plain_exe = AgentManager._prepare_command_for_windows(["openclaw.exe", "agents", "list"])

    assert wrapped_cmd == ["C:\\Windows\\System32\\cmd.exe", "/d", "/c", "openclaw.cmd", "agents", "list"]
    assert wrapped_bat == ["C:\\Windows\\System32\\cmd.exe", "/d", "/c", "openclaw.bat", "agents", "list"]
    assert plain_exe == ["openclaw.exe", "agents", "list"]


def test_register_all_reregisters_agent_when_registry_model_is_stale(monkeypatch) -> None:
    manager = AgentManager()
    monkeypatch.setattr(manager, "list_agents", lambda: {"research": ["project-analyst"]})
    expected_overrides = manager._get_agent_registration_overrides("project-analyst", "research")
    def fake_run_command_capture(cmd, env=None):
        if cmd[:3] == [manager._get_openclaw_bin(), "agents", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout='[{"id":"project-analyst","model":"deepseek/deepseek-v4-pro"}]',
                stderr="",
            )
        if cmd[:4] == [manager._get_openclaw_bin(), "agents", "delete", "--help"]:
            return SimpleNamespace(
                returncode=0,
                stdout="Usage: openclaw agents delete [options] <id>\nOptions:\n  --force     Skip confirmation\n",
                stderr="",
            )
        raise AssertionError(f"unexpected capture command: {cmd}")

    monkeypatch.setattr(manager, "_run_command_capture", fake_run_command_capture)

    calls: list[tuple[list[str], dict[str, str] | None]] = []

    def fake_run_command(cmd: list[str], env=None) -> int:
        calls.append((cmd, env))
        return 0

    monkeypatch.setattr(manager, "_run_command", fake_run_command)

    exit_code = manager.register_all()

    assert exit_code == 0
    assert calls[0] == ([manager._get_openclaw_bin(), "agents", "delete", "project-analyst", "--force"], None)
    assert calls[1][0] == [
        manager._get_openclaw_bin(),
        "agents",
        "add",
        "project-analyst",
        "--agent-dir",
        str(manager.base_dir / "research" / "project-analyst"),
        "--workspace",
        manager.workspace,
        "--model",
        expected_overrides["model"],
    ]
    assert calls[1][1]["OPENCLAW_PROVIDER"] == "openrouter"
    assert calls[1][1]["OPENCLAW_MODEL"] == expected_overrides["model"]


def test_reregister_delete_uses_noninteractive_flag_when_supported(monkeypatch) -> None:
    manager = AgentManager()

    def fake_run_command_capture(cmd, env=None):
        if cmd[:3] == [manager._get_openclaw_bin(), "agents", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout='[{"id":"project-analyst","model":"deepseek/deepseek-v4-pro"}]',
                stderr="",
            )
        if cmd[:4] == [manager._get_openclaw_bin(), "agents", "delete", "--help"]:
            return SimpleNamespace(
                returncode=0,
                stdout="Usage: openclaw agents delete [options] <id>\nOptions:\n  --force     Skip confirmation\n",
                stderr="",
            )
        raise AssertionError(f"unexpected capture command: {cmd}")

    calls: list[tuple[list[str], dict[str, str] | None]] = []

    monkeypatch.setattr(manager, "_run_command_capture", fake_run_command_capture)
    monkeypatch.setattr(manager, "_run_command", lambda cmd, env=None: calls.append((cmd, env)) or 0)

    exit_code = manager.register_agent("project-analyst", "research")

    assert exit_code == 0
    assert calls[0][0] == [manager._get_openclaw_bin(), "agents", "delete", "project-analyst", "--force"]


def test_register_agent_skips_reregistration_when_model_matches(monkeypatch) -> None:
    manager = AgentManager()
    expected_overrides = manager._get_agent_registration_overrides("project-analyst", "research")
    monkeypatch.setattr(
        manager,
        "_run_command_capture",
        lambda cmd, env=None: SimpleNamespace(
            returncode=0,
            stdout=f'[{{"id":"project-analyst","model":"{expected_overrides["model"]}"}}]',
            stderr="",
        ),
    )

    calls: list[list[str]] = []
    monkeypatch.setattr(manager, "_run_command", lambda cmd, env=None: calls.append(cmd) or 0)

    exit_code = manager.register_agent("project-analyst", "research")

    assert exit_code == 0
    assert calls == []


def test_workspace_defaults_to_launch_cwd_when_omitted(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    launch_cwd = tmp_path / "target"
    launch_cwd.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path, workspace="configured-project")

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(launch_cwd),
    )

    assert orchestrator.target_workspace == launch_cwd.resolve()
    assert Path(orchestrator.runtime.workspace) == launch_cwd.resolve()
    assert orchestrator.project_id == "target"


def test_workspace_argument_overrides_launch_cwd(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    launch_cwd = tmp_path / "launch"
    explicit_workspace = tmp_path / "target"
    launch_cwd.mkdir(parents=True)
    explicit_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path, workspace="configured-project")

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(launch_cwd),
        workspace=str(explicit_workspace),
    )

    assert orchestrator.target_workspace == explicit_workspace.resolve()
    assert Path(orchestrator.runtime.workspace) == explicit_workspace.resolve()


def test_workspace_falls_back_to_config_when_running_from_engine_root(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    configured_workspace = engine_root / "configured-project"
    configured_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path, workspace="configured-project")

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(engine_root),
    )

    assert orchestrator.target_workspace == configured_workspace.resolve()
    assert Path(orchestrator.runtime.workspace) == configured_workspace.resolve()


def test_logs_stay_under_engine_root_when_workspace_changes(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        workspace=str(target_workspace),
    )

    assert orchestrator.logger.log_dir == (engine_root / ".openclaw" / "logs" / orchestrator.project_id).resolve()
    assert orchestrator.logger.run_dir.parent == orchestrator.logger.log_dir


def test_project_id_is_derived_from_git_remote(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    repo = git.Repo.init(target_workspace)
    repo.create_remote("origin", "https://github.com/example/acme-app.git")

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )

    assert orchestrator.git_remote == "https://github.com/example/acme-app.git"
    assert orchestrator.project_id == "github.com-example-acme-app"


def test_create_git_branch_reuses_existing_branch(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    repo = git.Repo.init(target_workspace)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test")
        writer.set_value("user", "email", "test@example.com")
    tracked_file = target_workspace / "README.md"
    tracked_file.write_text("init\n", encoding="utf-8")
    repo.index.add(["README.md"])
    repo.index.commit("init")
    repo.git.checkout("-b", "feature/task_1")
    repo.git.checkout("master")

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.config["git"]["enabled"] = True

    ok = orchestrator._create_git_branch(1)

    assert ok is True
    assert orchestrator.current_branch == "feature/task_1"
    assert repo.active_branch.name == "feature/task_1"


def test_project_id_falls_back_to_folder_name_without_git_remote(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "Sample Repo"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )

    assert orchestrator.git_remote == ""
    assert orchestrator.project_id == "sample-repo"


def test_project_state_dirs_are_created_under_engine_root(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )

    base = engine_root / ".agents-pipeline" / "projects" / orchestrator.project_id
    assert orchestrator.project_state_dir == base.resolve()
    assert (base / "context").exists()
    assert (base / "memory").exists()
    assert (base / "logs").exists()
    assert (base / "summaries").exists()
    assert (base / "settings.yaml").exists()
    assert (base / "local.yaml").exists()
    assert (base / "codex.md").exists()
    assert (base / "resume.md").exists()

    shared_settings = yaml.safe_load((base / "settings.yaml").read_text(encoding="utf-8")) or {}
    local_settings = yaml.safe_load((base / "local.yaml").read_text(encoding="utf-8")) or {}
    assert shared_settings["project_id"] == orchestrator.project_id
    assert shared_settings["git_remote"] == ""
    assert "target_workspace" not in shared_settings
    assert "engine_root" not in shared_settings
    assert local_settings["target_workspace"] == str(target_workspace.resolve())
    assert local_settings["engine_root"] == str(engine_root.resolve())


def test_legacy_project_settings_migrate_machine_paths_to_local_yaml(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    project_dir = engine_root / ".agents-pipeline" / "projects" / "target"
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "project_id": "target",
                "git_remote": "",
                "target_workspace": "D:\\OldTarget",
                "engine_root": "D:\\OldEngine",
                "user_goal": "Keep the saved goal",
                "completed_implementation_tasks": ["TASK-1"],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )

    shared_settings = yaml.safe_load(orchestrator.project_settings_path.read_text(encoding="utf-8")) or {}
    local_settings = yaml.safe_load(orchestrator.project_local_settings_path.read_text(encoding="utf-8")) or {}
    assert shared_settings["user_goal"] == "Keep the saved goal"
    assert shared_settings["completed_implementation_tasks"] == ["TASK-1"]
    assert "target_workspace" not in shared_settings
    assert "engine_root" not in shared_settings
    assert local_settings["target_workspace"] == str(target_workspace.resolve())
    assert local_settings["engine_root"] == str(engine_root.resolve())


def test_project_codex_context_is_loaded_into_agent_context(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.project_codex_context_path.write_text(
        "# Codex Project Context\n\nUse feature flags before touching billing.\nCurrent rollout owner: Dima.\n",
        encoding="utf-8",
    )
    orchestrator.project_codex_context = orchestrator._load_project_codex_context()

    context = orchestrator._build_external_project_repository_context("project-analyst", limit=4000)

    assert "Shared Codex project context" in context
    assert "Use feature flags before touching billing." in context
    assert "Current rollout owner: Dima." in context


def test_project_resume_context_is_loaded_into_agent_context(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.project_resume_context_path.write_text(
        "# Project Resume\n\nContinue with TASK-7 in gateway-v4/app/services/router.py.\n",
        encoding="utf-8",
    )
    orchestrator.project_resume_context = orchestrator._load_project_resume_context()

    context = orchestrator._build_external_project_repository_context("project-analyst", limit=4000)

    assert "Shared resume handoff" in context
    assert "Continue with TASK-7 in gateway-v4/app/services/router.py." in context


def test_persist_project_codex_context_preserves_manual_notes(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        user_goal="Stabilize routing",
    )
    orchestrator.project_codex_context_path.write_text(
        "# Codex Project Context\n\nManual note stays.\n",
        encoding="utf-8",
    )
    orchestrator.project_codex_context = orchestrator._load_project_codex_context()
    orchestrator.logger.save_agent_report(
        "research",
        "project-analyst",
        {
            "status": "success",
            "result": "completed",
            "elapsed_s": 1.0,
            "parsed_output": "summary",
            "handoff_summary": "agent: project-analyst\nfindings:\n- repo stable\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- inspect routing",
            "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15, "estimated_cost_usd": 0.001},
        },
    )

    orchestrator._persist_project_codex_context()
    content = orchestrator.project_codex_context_path.read_text(encoding="utf-8")

    assert "Manual note stays." in content
    assert "<!-- AUTO-GENERATED:RUN-CONTEXT START -->" in content
    assert "### Saved User Goal" in content
    assert "Stabilize routing" in content
    assert "project-analyst: repo stable" in content


def test_persist_project_resume_context_preserves_manual_notes(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        user_goal="Ship implementation resume",
    )
    orchestrator.project_resume_context_path.write_text(
        "# Project Resume\n\nManual resume note stays.\n",
        encoding="utf-8",
    )
    orchestrator.project_resume_context = orchestrator._load_project_resume_context()
    orchestrator._selected_implementation_item = {
        "id": "TASK-9",
        "scope": "Finish backend routing patch.",
        "allowed_paths": ["gateway-v4/app/services/router.py", "gateway-v4/tests/test_router.py"],
    }
    orchestrator.logger.save_agent_report(
        "implementation",
        "developer",
        {
            "status": "success",
            "result": "completed",
            "elapsed_s": 2.0,
            "parsed_output": "status=implemented",
            "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30, "estimated_cost_usd": 0.002},
        },
    )

    orchestrator._persist_project_resume_context()
    content = orchestrator.project_resume_context_path.read_text(encoding="utf-8")

    assert "Manual resume note stays." in content
    assert "<!-- AUTO-GENERATED:RESUME-CONTEXT START -->" in content
    assert "Resume Checkpoint" in content
    assert "next_step: Continue implementation task TASK-9" in content
    assert "- id: TASK-9" in content
    assert "gateway-v4/app/services/router.py" in content


def test_run_research_phase_auto_updates_project_codex_context(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        user_goal="Document current state",
    )
    monkeypatch.setattr(orchestrator, "_run_standard_phase", lambda _phase: True)

    ok = orchestrator.run_research_phase()
    content = orchestrator.project_codex_context_path.read_text(encoding="utf-8")

    assert ok is True
    assert "## Auto-updated Run Context" in content
    assert "Document current state" in content
    assert orchestrator.logger.run_dir.name in content


def test_run_research_phase_auto_updates_project_resume_context(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        user_goal="Document resume state",
    )
    monkeypatch.setattr(orchestrator, "_run_standard_phase", lambda _phase: True)

    ok = orchestrator.run_research_phase()
    content = orchestrator.project_resume_context_path.read_text(encoding="utf-8")

    assert ok is True
    assert "## Resume Checkpoint" in content
    assert "Document resume state" in content
    assert orchestrator.logger.run_dir.name in content


def test_engine_config_still_loads_from_engine_root(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    (target_workspace / "workflow").mkdir(parents=True)
    (target_workspace / "workflow" / "config.yaml").write_text("project:\n  name: wrong\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path, workspace="configured-project")

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )

    assert orchestrator.config["project"]["name"] == "agents-pipeline"
    assert orchestrator.config_path == config_path.resolve()


def test_phase_cost_limit_stops_remaining_agents(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    launch_cwd = tmp_path / "target"
    launch_cwd.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(launch_cwd),
    )
    orchestrator.max_phase_cost_usd = 0.01
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)

    calls: list[str] = []

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        orchestrator.logger.save_agent_report(
            phase_key,
            agent_config["name"],
            {
                "status": "success",
                "result": "completed",
                "elapsed_s": 1,
                "returncode": 0,
                "message": "prompt",
                "stdout": "",
                "stderr": "",
                "parsed_output": "done",
                "usage": {"estimated_cost_usd": 0.02},
            },
        )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)

    ok = orchestrator._run_phase_agents(
        {
            "agents": [
                {"name": "first"},
                {"name": "second"},
            ]
        },
        "research",
    )

    assert ok is False
    assert calls == ["first"]


def test_implementation_fails_before_architect_if_research_handoff_missing(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.config["phases"]["implementation"] = {"name": "Implementation", "agents": []}

    ok = orchestrator._run_implementation_phase()

    assert ok is False


def test_implementation_planner_runs_after_architect(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    monitoring_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    monitoring_file.parent.mkdir(parents=True, exist_ok=True)
    monitoring_file.write_text("pass\n", encoding="utf-8")
    monitoring_test = target_workspace / "tests" / "test_monitoring.py"
    monitoring_test.parent.mkdir(parents=True, exist_ok=True)
    monitoring_test.write_text("def test_monitoring():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    calls: list[str] = []

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        if agent_config["name"] == "implementation-planner":
            orchestrator.logger.save_agent_report(
                "implementation",
                "implementation-planner",
                {
                    "status": "success",
                    "result": "completed",
                    "elapsed_s": 1,
                    "returncode": 0,
                    "message": "prompt",
                    "stdout": "",
                    "stderr": "",
                    "parsed_output": json.dumps(
                        [
                            {
                                "id": "backend-task",
                                "title": "Backend task",
                                "priority": "P0",
                                "scope": "Touch backend only.",
                                "existing_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
                                "allowed_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
                                "forbidden_paths": [],
                                "required_test_paths": ["tests/test_monitoring.py"],
                                "depends_on": [],
                                "acceptance_criteria": ["Backend updated."],
                                "reason_each_path_is_needed": {
                                    "gateway-v4/app/services/monitoring.py": "Needed.",
                                    "tests/test_monitoring.py": "Needed.",
                                },
                                "target_file": {"path": "gateway-v4/app/services/monitoring.py", "action": "update", "purpose": "Backend update."},
                                "test_file": {"path": "tests/test_monitoring.py", "action": "update"},
                                "must_contain": ["def record_request(", "response_time_ms"],
                                "must_import": ["from typing import Any"],
                                "integration": ["Provider flow uses monitoring."],
                                "reference_files": ["gateway-v4/app/services/monitoring.py"],
                                "reference_excerpts": {"gateway-v4/app/services/monitoring.py": "pass"},
                                "must_test": ["test_record_request_updates_metrics: assert metrics update succeeds"],
                                "forbidden": ["Do not edit frontend files."],
                                "risk_level": "low",
                                "estimated_effort": "S",
                            }
                        ]
                    ),
                },
            )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)
    monkeypatch.setattr(orchestrator, "_enforce_implementation_scope_diff", lambda: True)
    phase = {"agents": [{"name": "architect"}, {"name": "implementation-planner"}, {"name": "developer"}]}

    ok = orchestrator._run_phase_agents(phase, "implementation")

    assert ok is True
    assert calls[:2] == ["architect", "implementation-planner"]


def test_developer_is_blocked_when_planner_output_is_invalid(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    marketplace_file = target_workspace / "gateway-v4" / "app" / "services" / "marketplace.py"
    marketplace_file.parent.mkdir(parents=True, exist_ok=True)
    marketplace_file.write_text("pass\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    calls: list[str] = []

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        if agent_config["name"] == "implementation-planner":
            orchestrator.logger.save_agent_report(
                "implementation",
                "implementation-planner",
                {
                    "status": "success",
                    "result": "completed",
                    "elapsed_s": 1,
                    "returncode": 0,
                    "message": "prompt",
                    "stdout": "",
                    "stderr": "",
                    "parsed_output": "not valid yaml or json backlog",
                },
            )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)
    phase = {"agents": [{"name": "architect"}, {"name": "implementation-planner"}, {"name": "developer"}]}

    ok = orchestrator._run_phase_agents(phase, "implementation")

    assert ok is False
    assert calls == ["architect", "implementation-planner", "implementation-planner"]


def test_developer_blocked_when_selected_task_plans_marketplace_file(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    marketplace_file = target_workspace / "gateway-v4" / "app" / "services" / "marketplace.py"
    marketplace_file.parent.mkdir(parents=True, exist_ok=True)
    marketplace_file.write_text("pass\n", encoding="utf-8")
    marketplace_test = target_workspace / "tests" / "test_marketplace.py"
    marketplace_test.parent.mkdir(parents=True, exist_ok=True)
    marketplace_test.write_text("def test_marketplace():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    calls: list[str] = []

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        if agent_config["name"] == "implementation-planner":
            orchestrator.logger.save_agent_report(
                "implementation",
                "implementation-planner",
                {
                    "status": "success",
                    "result": "completed",
                    "elapsed_s": 1,
                    "returncode": 0,
                    "message": "prompt",
                    "stdout": "",
                    "stderr": "",
                    "parsed_output": json.dumps(
                        [
                            {
                                "id": "planner-marketplace-task",
                                "title": "Marketplace billing task",
                                "priority": "P0",
                                "scope": "Touch marketplace code.",
                                "existing_paths": ["gateway-v4/app/services/marketplace.py", "tests/test_marketplace.py"],
                                "allowed_paths": ["gateway-v4/app/services/marketplace.py", "tests/test_marketplace.py"],
                                "forbidden_paths": [],
                                "required_test_paths": ["tests/test_marketplace.py"],
                                "depends_on": [],
                                "acceptance_criteria": ["Marketplace code updated."],
                                "reason_each_path_is_needed": {
                                    "gateway-v4/app/services/marketplace.py": "Marketplace implementation target.",
                                    "tests/test_marketplace.py": "Marketplace regression test.",
                                },
                                "target_file": {"path": "gateway-v4/app/services/marketplace.py", "action": "update", "purpose": "Marketplace update."},
                                "test_file": {"path": "tests/test_marketplace.py", "action": "update"},
                                "must_contain": ["def marketplace_handler(", "return"],
                                "must_import": ["from typing import Any"],
                                "integration": ["Marketplace flow must remain isolated from implementation scope."],
                                "reference_files": ["gateway-v4/app/services/marketplace.py"],
                                "reference_excerpts": {"gateway-v4/app/services/marketplace.py": "pass"},
                                "must_test": ["test_marketplace_handler_blocks_scope: assert marketplace path remains forbidden"],
                                "forbidden": ["Do not edit frontend files."],
                                "risk_level": "high",
                                "estimated_effort": "M",
                            }
                        ]
                    ),
                },
            )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)
    phase = {"agents": [{"name": "architect"}, {"name": "implementation-planner"}, {"name": "developer"}, {"name": "qa"}]}

    ok = orchestrator._run_phase_agents(phase, "implementation")

    assert ok is False
    assert calls == ["architect", "implementation-planner"]
    payload = json.loads(
        (orchestrator.logger.run_dir / "agents" / "implementation" / "developer.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "scope_violation"
    assert "marketplace" in payload["result"]


def test_task_with_forbidden_paths_passes_when_allowed_paths_do_not_touch_them(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    monitoring_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    monitoring_file.parent.mkdir(parents=True, exist_ok=True)
    monitoring_file.write_text("pass\n", encoding="utf-8")
    monitoring_test = target_workspace / "tests" / "test_monitoring.py"
    monitoring_test.parent.mkdir(parents=True, exist_ok=True)
    monitoring_test.write_text("def test_monitoring():\n    assert True\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    calls: list[str] = []

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        if agent_config["name"] == "implementation-planner":
            orchestrator.logger.save_agent_report(
                "implementation",
                "implementation-planner",
                {
                    "status": "success",
                    "result": "completed",
                    "elapsed_s": 1,
                    "returncode": 0,
                    "message": "prompt",
                    "stdout": "",
                    "stderr": "",
                    "parsed_output": json.dumps(
                        [
                            {
                                "id": "planner-safe-task",
                                "title": "Safe task",
                                "priority": "P0",
                                "scope": "Touch monitoring only.",
                                "existing_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
                                "allowed_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
                                "forbidden_paths": ["gateway-v4/app/routers/billing.py", "frontend/src/lib/api.js"],
                                "required_test_paths": ["tests/test_monitoring.py"],
                                "depends_on": [],
                                "acceptance_criteria": ["Monitoring updated."],
                                "reason_each_path_is_needed": {
                                    "gateway-v4/app/services/monitoring.py": "Monitoring implementation target.",
                                    "tests/test_monitoring.py": "Required regression test.",
                                },
                                "target_file": {"path": "gateway-v4/app/services/monitoring.py", "action": "update", "purpose": "Monitoring update."},
                                "test_file": {"path": "tests/test_monitoring.py", "action": "update"},
                                "must_contain": ["def record_request(", "response_time_ms"],
                                "must_import": ["from typing import Any"],
                                "integration": ["Provider flow must call record_request."],
                                "reference_files": ["gateway-v4/app/services/monitoring.py"],
                                "reference_excerpts": {"gateway-v4/app/services/monitoring.py": "pass"},
                                "must_test": ["test_record_request_updates_metrics: assert metrics update succeeds"],
                                "forbidden": ["Do not edit frontend files."],
                                "risk_level": "low",
                                "estimated_effort": "S",
                            }
                        ]
                    ),
                },
            )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)
    monkeypatch.setattr(orchestrator, "_collect_scope_watchdog_diff_diagnostics", lambda: {
        "allowed": True,
        "changed_files": ["gateway-v4/app/services/monitoring.py"],
        "changed_files_count": 1,
        "diff_lines_count": 8,
        "forbidden_hits": [],
        "allowed_paths_matched": ["gateway-v4/app/services/monitoring.py"],
        "scope_policy_result": "allowed",
        "selected_task_forbidden_paths": ["gateway-v4/app/routers/billing.py", "frontend/src/lib/api.js"],
        "planned_edit_paths": ["gateway-v4/app/services/monitoring.py"],
        "forbidden_hits_source": "",
    })
    phase = {"agents": [{"name": "architect"}, {"name": "implementation-planner"}, {"name": "developer"}, {"name": "qa"}]}

    ok = orchestrator._run_phase_agents(phase, "implementation")

    assert ok is True
    planner_payload = json.loads(
        (orchestrator.logger.run_dir / "agents" / "implementation" / "implementation-planner.json").read_text(encoding="utf-8")
    )
    assert planner_payload["scope_policy_result"] == "allowed"
    assert planner_payload["selected_task_forbidden_paths"] == ["gateway-v4/app/routers/billing.py", "frontend/src/lib/api.js"]
    assert planner_payload["planned_edit_paths"] == ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"]
    assert planner_payload["forbidden_hits_source"] == "precheck"


def test_scope_watchdog_blocks_billing_diff(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    repo = git.Repo.init(target_workspace)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test")
        writer.set_value("user", "email", "test@example.com")
    billing_file = target_workspace / "gateway-v4" / "app" / "routers" / "billing.py"
    billing_file.parent.mkdir(parents=True, exist_ok=True)
    billing_file.write_text("def handler():\n    return 'old'\n", encoding="utf-8")
    repo.index.add([str(billing_file.relative_to(target_workspace)).replace("\\", "/")])
    repo.index.commit("init")
    billing_file.write_text("import stripe\n", encoding="utf-8")

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    diagnostics = orchestrator._collect_scope_watchdog_diff_diagnostics()

    assert diagnostics["allowed"] is False
    assert any("billing" in hit or "stripe" in hit for hit in diagnostics["forbidden_hits"])
    assert diagnostics["forbidden_hits_source"] == "diff"


def test_scope_watchdog_blocks_when_too_many_files_changed(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    repo = git.Repo.init(target_workspace)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test")
        writer.set_value("user", "email", "test@example.com")
    paths = [
        "gateway-v4/app/services/monitoring.py",
        "gateway-v4/app/services/routing.py",
        "gateway-v4/app/services/proxy.py",
    ]
    for relative in paths:
        path = target_workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n", encoding="utf-8")
    repo.index.add(paths)
    repo.index.commit("init")
    for relative in paths:
        (target_workspace / relative).write_text("changed\n", encoding="utf-8")

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.implementation_scope_policy["max_changed_files"] = 2
    diagnostics = orchestrator._collect_scope_watchdog_diff_diagnostics()

    assert diagnostics["allowed"] is False
    assert any("max_changed_files" in hit for hit in diagnostics["forbidden_hits"])


def test_scope_watchdog_allows_monitoring_and_routing_backend_files(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    repo = git.Repo.init(target_workspace)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test")
        writer.set_value("user", "email", "test@example.com")
    paths = [
        "gateway-v4/app/services/monitoring.py",
        "gateway-v4/app/services/routing.py",
    ]
    for relative in paths:
        path = target_workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("pass\n", encoding="utf-8")
    repo.index.add(paths)
    repo.index.commit("init")
    (target_workspace / paths[0]).write_text("print('monitoring')\n", encoding="utf-8")
    (target_workspace / paths[1]).write_text("print('routing')\n", encoding="utf-8")

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    diagnostics = orchestrator._collect_scope_watchdog_diff_diagnostics()

    assert diagnostics["allowed"] is True
    assert sorted(diagnostics["allowed_paths_matched"]) == sorted(paths)


def test_scope_watchdog_uses_untracked_files_not_parent_directory(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    repo = git.Repo.init(target_workspace)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test")
        writer.set_value("user", "email", "test@example.com")
    tracked = "gateway-v4/app/models.py"
    tracked_path = target_workspace / tracked
    tracked_path.parent.mkdir(parents=True, exist_ok=True)
    tracked_path.write_text("pass\n", encoding="utf-8")
    repo.index.add([tracked])
    repo.index.commit("init")
    untracked_test = "gateway-v4/tests/test_provider_metrics_migration.py"
    untracked_path = target_workspace / untracked_test
    untracked_path.parent.mkdir(parents=True, exist_ok=True)
    untracked_path.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator._selected_implementation_item = {
        "allowed_paths": [tracked, untracked_test],
        "forbidden_paths": [],
    }

    diagnostics = orchestrator._collect_scope_watchdog_diff_diagnostics()

    assert diagnostics["allowed"] is True
    assert "gateway-v4/tests" not in diagnostics["changed_files"]
    assert untracked_test in diagnostics["changed_files"]
    assert untracked_test in diagnostics["allowed_paths_matched"]


def test_allow_scope_expansion_bypasses_watchdog_with_warning(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        allow_scope_expansion=True,
    )
    frontend_file = target_workspace / "frontend" / "src" / "App.jsx"
    frontend_file.parent.mkdir(parents=True, exist_ok=True)
    frontend_file.write_text("export default null;\n", encoding="utf-8")
    frontend_file = target_workspace / "frontend" / "src" / "App.jsx"
    frontend_file.parent.mkdir(parents=True, exist_ok=True)
    frontend_file.write_text("export default null;\n", encoding="utf-8")
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    warnings: list[str] = []
    monkeypatch.setattr(orchestrator.logger, "warning", warnings.append)
    calls: list[str] = []

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        if agent_config["name"] == "implementation-planner":
            orchestrator.logger.save_agent_report(
                "implementation",
                "implementation-planner",
                {
                    "status": "success",
                    "result": "completed",
                    "elapsed_s": 1,
                    "returncode": 0,
                    "message": "prompt",
                    "stdout": "",
                    "stderr": "",
                    "parsed_output": json.dumps(
                        [
                            {
                                "id": "planner-frontend-task",
                                "title": "Frontend task",
                                "priority": "P1",
                                "scope": "Touch frontend code.",
                                "existing_paths": ["frontend/src/App.jsx"],
                                "allowed_paths": ["frontend/src/App.jsx"],
                                "forbidden_paths": [],
                                "acceptance_criteria": ["Frontend code updated."],
                                "risk_level": "high",
                                "estimated_effort": "M",
                            }
                        ]
                    ),
                },
            )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)
    monkeypatch.setattr(
        orchestrator,
        "_collect_scope_watchdog_diff_diagnostics",
        lambda: {
            "allowed": True,
            "changed_files": ["gateway-v4/app/services/monitoring.py"],
            "changed_files_count": 1,
            "diff_lines_count": 12,
            "forbidden_hits": [],
            "allowed_paths_matched": ["gateway-v4/app/services/monitoring.py"],
            "scope_policy_result": "allowed",
        },
    )
    phase = {"agents": [{"name": "architect"}, {"name": "implementation-planner"}, {"name": "developer"}, {"name": "qa"}]}

    ok = orchestrator._run_phase_agents(phase, "implementation")

    assert ok is True
    assert calls == ["architect", "implementation-planner", "developer", "qa"]
    assert any("Scope watchdog bypassed" in warning for warning in warnings)


def test_scope_watchdog_uses_selected_task_allowed_paths(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    repo = git.Repo.init(target_workspace)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test")
        writer.set_value("user", "email", "test@example.com")
    allowed_path = "docs/implementation-plan.md"
    disallowed_global_path = target_workspace / allowed_path
    disallowed_global_path.parent.mkdir(parents=True, exist_ok=True)
    disallowed_global_path.write_text("old\n", encoding="utf-8")
    repo.index.add([allowed_path])
    repo.index.commit("init")
    disallowed_global_path.write_text("new\n", encoding="utf-8")

    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator._selected_implementation_item = {
        "id": "docs-task",
        "title": "Docs task",
        "priority": "P1",
        "scope": "Update docs only.",
        "allowed_paths": [allowed_path],
        "forbidden_paths": [],
        "acceptance_criteria": ["Docs updated."],
        "risk_level": "low",
        "estimated_effort": "S",
    }

    diagnostics = orchestrator._collect_scope_watchdog_diff_diagnostics()

    assert diagnostics["allowed"] is True
    assert diagnostics["allowed_paths_matched"] == [allowed_path]


def test_planner_nonexistent_services_path_is_rejected(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "bad-services-path",
                        "title": "Bad services path",
                        "priority": "P0",
                        "scope": "Invalid path",
                        "allowed_paths": ["services/foo.py"],
                        "new_files": [],
                        "forbidden_paths": [],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {"services/foo.py": "Needed."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert any("services/foo.py" in item for item in diagnostics["invalid_paths"])
    assert any("services/foo.py" in item for item in diagnostics["generic_root_dirs_rejected"])


def test_planner_nonexistent_src_path_is_rejected(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "bad-src-path",
                        "title": "Bad src path",
                        "priority": "P0",
                        "scope": "Invalid path",
                        "allowed_paths": ["src/main.py", "tests/test_main.py"],
                        "existing_paths": ["src/main.py"],
                        "new_files": [],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_workflow.py"],
                        "depends_on": [],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {
                            "src/main.py": "Needed.",
                            "tests/test_main.py": "Needed.",
                        },
                        "target_file": {"path": "src/main.py", "action": "update", "purpose": "Invalid path"},
                        "test_file": {"path": "tests/test_workflow.py", "action": "update"},
                        "must_contain": ["def bad_path(", "return False"],
                        "must_import": ["from pathlib import Path"],
                        "integration": ["Invalid path used to test repair."],
                        "reference_files": ["workflow/orchestrator.py"],
                        "reference_excerpts": {"workflow/orchestrator.py": "pass"},
                        "must_test": ["test_bad_path_rejected: assert invalid path is rejected"],
                        "forbidden": ["Do not edit frontend files."],
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert any("src/main.py" in item for item in diagnostics["invalid_paths"])


def test_planner_existing_target_file_passes_validation(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    file_path = target_workspace / "workflow" / "orchestrator.py"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("pass\n", encoding="utf-8")
    test_path = target_workspace / "tests" / "test_workflow.py"
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "good-existing-file",
                        "title": "Good existing file",
                        "priority": "P0",
                        "scope": "Valid path",
                        "allowed_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                        "existing_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                        "new_files": [],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_workflow.py"],
                        "depends_on": [],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed.", "tests/test_workflow.py": "Needed."},
                        "target_file": {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Workflow update."},
                        "test_file": {"path": "tests/test_workflow.py", "action": "update"},
                        "must_contain": ["def _validate_implementation_planner_output(", "return {"],
                        "must_import": ["from pathlib import Path"],
                        "integration": ["Validation must integrate with planner flow."],
                        "reference_files": ["workflow/orchestrator.py"],
                        "reference_excerpts": {"workflow/orchestrator.py": "pass"},
                        "must_test": ["test_validate_output_accepts_valid_paths: assert diagnostics valid"],
                        "forbidden": ["Do not edit frontend files."],
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is True
    assert diagnostics["task_count"] == 1


def test_planner_allowed_path_must_be_declared(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    file_path = target_workspace / "workflow" / "orchestrator.py"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("pass\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "undeclared-allowed-path",
                        "title": "Undeclared allowed path",
                        "priority": "P0",
                        "scope": "Touch one file but forget to declare it properly.",
                        "allowed_paths": ["workflow/orchestrator.py"],
                        "existing_paths": [],
                        "new_directories": [],
                        "new_files": [],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_workflow.py"],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert any("allowed_path_not_declared" in item for item in diagnostics["invalid_paths"])


def test_planner_new_file_under_existing_directory_passes_when_marked_new_file(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    existing_dir = target_workspace / "docs"
    existing_dir.mkdir(parents=True, exist_ok=True)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "new-doc-file",
                        "title": "New doc file",
                        "priority": "P1",
                        "scope": "Create docs file",
                        "allowed_paths": ["docs/new-plan.md"],
                        "existing_paths": [],
                        "new_files": ["docs/new-plan.md"],
                        "forbidden_paths": [],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {"docs/new-plan.md": "Needed."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is True


def test_planner_validates_new_test_package_scaffolding(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    (target_workspace / "gateway-v4" / "app" / "services").mkdir(parents=True, exist_ok=True)
    (target_workspace / "gateway-v4" / "app" / "services" / "router.py").write_text(
        "def route():\n    return None\n",
        encoding="utf-8",
    )

    positive = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    positive.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "new-test-package-positive",
                        "title": "Add smart routing test package",
                        "priority": "P1",
                        "scope": "Backend-only test scaffolding",
                        "existing_paths": ["gateway-v4/app/services/router.py"],
                        "new_directories": ["gateway-v4/tests"],
                        "new_files": [
                            "gateway-v4/tests/__init__.py",
                            "gateway-v4/tests/test_router_smart.py",
                        ],
                        "allowed_paths": [
                            "gateway-v4/app/services/router.py",
                            "gateway-v4/tests/__init__.py",
                            "gateway-v4/tests/test_router_smart.py",
                        ],
                        "forbidden_paths": [],
                        "required_test_paths": ["gateway-v4/tests/test_router_smart.py"],
                        "depends_on": [],
                        "acceptance_criteria": ["n/a"],
                        "target_file": {"path": "gateway-v4/app/services/router.py", "action": "update", "purpose": "Router update."},
                        "test_file": {"path": "gateway-v4/tests/test_router_smart.py", "action": "create"},
                        "must_contain": ["def route(", "return None"],
                        "must_import": ["from typing import Any"],
                        "integration": ["Routing behavior must use the router service."],
                        "reference_files": ["gateway-v4/app/services/router.py"],
                        "reference_excerpts": {"gateway-v4/app/services/router.py": "def route():\n    return None"},
                        "must_test": ["test_route_uses_stats: assert routing picks the expected provider"],
                        "forbidden": ["Do not edit frontend files."],
                        "reason_each_path_is_needed": {
                            "gateway-v4/app/services/router.py": "Existing router service under test.",
                            "gateway-v4/tests/__init__.py": "Package marker for new tests.",
                            "gateway-v4/tests/test_router_smart.py": "Smart routing test file.",
                        },
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    positive_diagnostics = positive._validate_implementation_planner_output()

    assert positive_diagnostics["valid"] is True
    assert not any(
        "new-test-package-positive" in item and "allowed_path_not_declared" in item
        for item in positive_diagnostics["invalid_paths"]
    )

    negative = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    negative.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "new-test-package-negative",
                        "title": "Broken smart routing test package",
                        "priority": "P1",
                        "scope": "Backend-only test scaffolding",
                        "existing_paths": ["gateway-v4/app/services/router.py"],
                        "new_directories": ["gateway-v4/tests"],
                        "new_files": ["gateway-v4/tests/test_router_smart.py"],
                        "allowed_paths": [
                            "gateway-v4/app/services/router.py",
                            "gateway-v4/tests/__init__.py",
                            "gateway-v4/tests/test_router_smart.py",
                        ],
                        "forbidden_paths": [],
                        "required_test_paths": ["gateway-v4/tests/test_router_smart.py"],
                        "depends_on": [],
                        "acceptance_criteria": ["n/a"],
                        "target_file": {"path": "gateway-v4/app/services/router.py", "action": "update", "purpose": "Router update."},
                        "test_file": {"path": "gateway-v4/tests/test_router_smart.py", "action": "create"},
                        "must_contain": ["def route(", "return None"],
                        "must_import": ["from typing import Any"],
                        "integration": ["Routing behavior must use the router service."],
                        "reference_files": ["gateway-v4/app/services/router.py"],
                        "reference_excerpts": {"gateway-v4/app/services/router.py": "def route():\n    return None"},
                        "must_test": ["test_route_uses_stats: assert routing picks the expected provider"],
                        "forbidden": ["Do not edit frontend files."],
                        "reason_each_path_is_needed": {
                            "gateway-v4/app/services/router.py": "Existing router service under test.",
                            "gateway-v4/tests/__init__.py": "Package marker for new tests.",
                            "gateway-v4/tests/test_router_smart.py": "Smart routing test file.",
                        },
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    negative_diagnostics = negative._validate_implementation_planner_output()

    assert negative_diagnostics["valid"] is False
    assert any(
        "gateway-v4/tests/__init__.py" in item and "allowed_path_not_declared" in item
        for item in negative_diagnostics["invalid_paths"]
    )


def test_planner_new_directory_and_file_passes(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "docs").mkdir(parents=True, exist_ok=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "nested-docs-file",
                        "title": "Nested doc file",
                        "priority": "P1",
                        "scope": "Documentation only in new nested directory.",
                        "allowed_paths": ["docs/guides/setup.md"],
                        "existing_paths": [],
                        "new_directories": ["docs/guides"],
                        "new_files": ["docs/guides/setup.md"],
                        "forbidden_paths": [],
                        "required_test_paths": [],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {"docs/guides/setup.md": "Needed."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is True


def test_planner_new_file_in_undeclared_directory_fails(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "docs").mkdir(parents=True, exist_ok=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "undeclared-dir-file",
                        "title": "Undeclared dir file",
                        "priority": "P1",
                        "scope": "Documentation only in nested directory.",
                        "allowed_paths": ["docs/guides/setup.md"],
                        "existing_paths": [],
                        "new_directories": [],
                        "new_files": ["docs/guides/setup.md"],
                        "forbidden_paths": [],
                        "required_test_paths": [],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {"docs/guides/setup.md": "Needed."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert any("new_file_parent_missing" in item for item in diagnostics["invalid_paths"])


def test_planner_generic_nonexistent_api_path_is_rejected(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "bad-api-path",
                        "title": "Bad api path",
                        "priority": "P0",
                        "scope": "Invalid path",
                        "allowed_paths": ["api/routing.py"],
                        "existing_paths": ["api/routing.py"],
                        "new_files": [],
                        "forbidden_paths": [],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {"api/routing.py": "Needed."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert any("api/routing.py" in item for item in diagnostics["invalid_paths"])


def test_generic_root_allowed_if_directory_exists(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "services").mkdir(parents=True, exist_ok=True)
    (target_workspace / "services" / "handler.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "existing-services-dir",
                        "title": "Existing services dir",
                        "priority": "P0",
                        "scope": "Documentation only for compatibility.",
                        "allowed_paths": ["services/handler.py"],
                        "existing_paths": ["services/handler.py"],
                        "new_files": [],
                        "forbidden_paths": [],
                        "required_test_paths": [],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {"services/handler.py": "Needed."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is True


def test_generic_root_allowed_with_allow_scope_expansion(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        allow_scope_expansion=True,
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "expanded-structure",
                        "title": "Expanded structure",
                        "priority": "P0",
                        "scope": "allow project restructuring for compatibility.",
                        "allowed_paths": ["src/main.py", "tests/test_main.py"],
                        "existing_paths": [],
                        "new_directories": ["src", "tests"],
                        "new_files": ["src/main.py", "tests/test_main.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_main.py"],
                        "depends_on": [],
                        "target_file": {"path": "src/main.py", "action": "create", "purpose": "Create new entrypoint in expanded structure."},
                        "test_file": {"path": "tests/test_main.py", "action": "create"},
                        "must_contain": ["def main(", "return"],
                        "must_import": ["from pathlib import Path"],
                        "integration": ["New src layout is allowed only because scope expansion is enabled."],
                        "reference_files": [],
                        "reference_excerpts": {},
                        "must_test": ["test_main_entrypoint_exists: assert main entrypoint is callable"],
                        "forbidden": ["Do not edit frontend files."],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {
                            "src/main.py": "Needed.",
                            "tests/test_main.py": "Needed.",
                        },
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is True


def test_planner_rejection_prints_reasons_and_feedback_contains_block(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(orchestrator.logger, "error", lambda message, details="": errors.append((message, details)))
    monkeypatch.setattr(orchestrator, "_run_agent", lambda *args, **kwargs: True)
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "bad-services-path",
                        "title": "Bad services path",
                        "priority": "P0",
                        "scope": "Invalid backend path",
                        "allowed_paths": ["services/foo.py"],
                        "existing_paths": ["services/foo.py"],
                        "new_files": [],
                        "forbidden_paths": ["tests/*"],
                        "required_test_paths": [],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {"services/foo.py": "Needed."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    ok = orchestrator._validate_or_repair_implementation_planner(
        {"name": "implementation-planner", "description": "Convert plan to backlog"},
        "implementation",
        index=2,
        total=5,
    )

    assert ok is False
    assert any("Implementation planner output is invalid after repair round" in message for message, _details in errors)
    assert any("invalid_paths" in details for _message, details in errors)
    assert orchestrator._planner_feedback_file
    feedback_text = Path(orchestrator._planner_feedback_file).read_text(encoding="utf-8")
    assert "Implementation planner rejection reasons:" in feedback_text
    assert "generic_root_dirs_rejected" in feedback_text


def test_feedback_path_is_project_and_run_scoped_with_metadata(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True, exist_ok=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator._implementation_attempt = 2

    feedback_file = orchestrator._save_feedback(1, "implementation-planner", "Planner feedback body")

    assert feedback_file == (
        engine_root
        / ".openclaw"
        / "feedback"
        / orchestrator.project_id
        / orchestrator.logger.run_dir.name
        / "attempt_2"
        / "implementation-planner.md"
    )
    feedback_text = feedback_file.read_text(encoding="utf-8")
    assert "project_id:" in feedback_text
    assert f"project_id: {orchestrator.project_id}" in feedback_text
    assert f"run_id: {orchestrator.logger.run_dir.name}" in feedback_text
    assert "attempt: 2" in feedback_text
    assert "phase: implementation" in feedback_text
    assert "agent: implementation-planner" in feedback_text
    assert f"target_workspace: {orchestrator.target_workspace}" in feedback_text
    assert f"repo_map_target_workspace: {orchestrator.target_workspace}" in feedback_text
    latest_file = engine_root / ".openclaw" / "feedback" / orchestrator.project_id / "latest" / "implementation-planner.md"
    assert latest_file.exists()
    assert latest_file.read_text(encoding="utf-8") == feedback_text


def test_feedback_does_not_overwrite_between_projects_or_attempts(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    first_workspace = tmp_path / "first-target"
    second_workspace = tmp_path / "second-target"
    first_workspace.mkdir(parents=True, exist_ok=True)
    second_workspace.mkdir(parents=True, exist_ok=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    first = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(first_workspace), project_id="project-one")
    second = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(second_workspace), project_id="project-two")
    first._implementation_attempt = 1
    second._implementation_attempt = 2

    first_feedback = first._save_feedback(1, "implementation-planner", "First project")
    second_feedback = second._save_feedback(1, "implementation-planner", "Second project")

    assert "project-one" in first_feedback.as_posix()
    assert "project-two" in second_feedback.as_posix()
    assert "attempt_1" in first_feedback.as_posix()
    assert "attempt_2" in second_feedback.as_posix()
    assert first_feedback.read_text(encoding="utf-8") != second_feedback.read_text(encoding="utf-8")


def test_feedback_does_not_overwrite_between_attempts_for_same_project(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True, exist_ok=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace), project_id="same-project")
    orchestrator._implementation_attempt = 1
    first_feedback = orchestrator._save_feedback(1, "implementation-planner", "Attempt one")
    orchestrator._implementation_attempt = 2
    second_feedback = orchestrator._save_feedback(1, "implementation-planner", "Attempt two")

    assert first_feedback != second_feedback
    assert first_feedback.exists()
    assert second_feedback.exists()
    assert "attempt_1" in first_feedback.as_posix()
    assert "attempt_2" in second_feedback.as_posix()
    latest_file = engine_root / ".openclaw" / "feedback" / "same-project" / "latest" / "implementation-planner.md"
    assert "Attempt two" in latest_file.read_text(encoding="utf-8")


def test_fenced_yaml_with_russian_translation_parses_correctly(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    payload = """```yaml
- id: planner-task
  title: Planner task
  priority: P0
  scope: Improve backend validation.
  existing_paths:
    - workflow/orchestrator.py
  allowed_paths:
    - workflow/orchestrator.py
  forbidden_paths: []
  required_test_paths:
    - tests/test_workflow.py
  acceptance_criteria:
    - done
  reason_each_path_is_needed:
    workflow/orchestrator.py: Needed.
  risk_level: low
  estimated_effort: S
```

Russian translation
Перевод
"""
    result = orchestrator._parse_implementation_planner_output(payload)

    assert result["parse_error"] == ""
    assert result["schema_errors"] == []
    assert result["items"][0]["id"] == "planner-task"


def test_top_level_tasks_object_parses_correctly(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    result = orchestrator._parse_implementation_planner_output(
        json.dumps(
            {
                "tasks": [
                        {
                            "id": "planner-task",
                            "title": "Planner task",
                            "priority": "P0",
                            "scope": "Improve backend validation.",
                            "existing_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                            "allowed_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                            "forbidden_paths": [],
                            "required_test_paths": ["tests/test_workflow.py"],
                            "depends_on": [],
                            "acceptance_criteria": ["done"],
                            "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed.", "tests/test_workflow.py": "Needed."},
                            "target_file": {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Improve backend validation."},
                            "test_file": {"path": "tests/test_workflow.py", "action": "update"},
                            "must_contain": ["def _validate_implementation_planner_output(", "return {"],
                            "must_import": ["from pathlib import Path"],
                            "integration": ["Validation must integrate with planner flow."],
                            "reference_files": ["workflow/orchestrator.py"],
                            "reference_excerpts": {"workflow/orchestrator.py": "pass"},
                            "must_test": ["test_validate_output_accepts_valid_paths: assert diagnostics valid"],
                            "forbidden": ["Do not edit frontend files."],
                            "risk_level": "low",
                            "estimated_effort": "S",
                        }
                ]
            }
        )
    )

    assert result["items"][0]["id"] == "planner-task"


def test_top_level_list_parses_correctly(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    result = orchestrator._parse_implementation_planner_output(
        json.dumps(
            [
                        {
                            "id": "planner-task",
                            "title": "Planner task",
                            "priority": "P0",
                            "scope": "Improve backend validation.",
                            "existing_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                            "allowed_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                            "forbidden_paths": [],
                            "required_test_paths": ["tests/test_workflow.py"],
                            "depends_on": [],
                            "acceptance_criteria": ["done"],
                            "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed.", "tests/test_workflow.py": "Needed."},
                            "target_file": {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Improve backend validation."},
                            "test_file": {"path": "tests/test_workflow.py", "action": "update"},
                            "must_contain": ["def _validate_implementation_planner_output(", "return {"],
                            "must_import": ["from pathlib import Path"],
                            "integration": ["Validation must integrate with planner flow."],
                            "reference_files": ["workflow/orchestrator.py"],
                            "reference_excerpts": {"workflow/orchestrator.py": "pass"},
                            "must_test": ["test_validate_output_accepts_valid_paths: assert diagnostics valid"],
                            "forbidden": ["Do not edit frontend files."],
                            "risk_level": "low",
                            "estimated_effort": "S",
                        }
            ]
        )
    )

    assert result["items"][0]["id"] == "planner-task"


def test_malformed_yaml_prints_parse_error(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {"status": "success", "result": "completed", "parsed_output": "```yaml\n- id: x\n  title: bad\n  : nope\n```"},
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert diagnostics["parse_error"]
    assert orchestrator._planner_validation_stage == "parse"


def test_missing_required_schema_field_prints_schema_errors(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {"status": "success", "result": "completed", "parsed_output": json.dumps([{"title": "Missing id"}])},
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert "missing_id" in ",".join(diagnostics["schema_errors"])
    assert orchestrator._planner_validation_stage == "schema"


def test_empty_path_errors_but_schema_error_still_fails_with_schema_reason(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {"status": "success", "result": "completed", "parsed_output": json.dumps({"tasks": [{"title": "Missing id"}]})},
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert diagnostics["invalid_paths"] == []
    assert diagnostics["schema_errors"]
    assert orchestrator._planner_validation_stage == "schema"


def test_wrong_repo_map_target_workspace_fails_clearly(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    wrong_repo_map = orchestrator.repo_map_path
    wrong_repo_map.parent.mkdir(parents=True, exist_ok=True)
    wrong_repo_map.write_text(json.dumps({"target_workspace": str(tmp_path / "other"), "top_level_tree": [], "directories": [], "files": []}), encoding="utf-8")
    orchestrator._repo_map_cache = None
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "planner-task",
                        "title": "Planner task",
                        "priority": "P0",
                        "scope": "Improve backend validation.",
                        "existing_paths": ["workflow/orchestrator.py"],
                        "allowed_paths": ["workflow/orchestrator.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_workflow.py"],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert diagnostics["parse_error"] == "repo_map_target_workspace_mismatch"
    assert any("repo_map_path=" in item for item in diagnostics["schema_errors"])


def test_repo_map_target_mismatch_blocks_implementation_planner_before_run(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True, exist_ok=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.repo_map_path.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.repo_map_path.write_text(
        json.dumps({"target_workspace": str(tmp_path / "other"), "top_level_tree": [], "directories": [], "files": []}),
        encoding="utf-8",
    )
    orchestrator._repo_map_cache = None
    orchestrator._wait_for_user = lambda _prompt: True
    calls: list[str] = []
    orchestrator._run_agent = lambda agent, phase, index=None, total=None: calls.append(agent["name"]) or True

    ok = orchestrator._run_phase_agents({"agents": [{"name": "implementation-planner"}]}, "implementation")

    assert ok is False
    assert calls == []
    assert orchestrator._phase_failure_status == "repo_map_target_workspace_mismatch"


def test_repo_map_target_mismatch_blocks_developer_before_run(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True, exist_ok=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.repo_map_path.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.repo_map_path.write_text(
        json.dumps({"target_workspace": str(tmp_path / "other"), "top_level_tree": [], "directories": [], "files": []}),
        encoding="utf-8",
    )
    orchestrator._repo_map_cache = None
    orchestrator._wait_for_user = lambda _prompt: True
    orchestrator._prepare_implementation_backlog_selection = lambda require_backlog=True: {"error": "", "item": None}
    calls: list[str] = []
    orchestrator._run_agent = lambda agent, phase, index=None, total=None: calls.append(agent["name"]) or True

    ok = orchestrator._run_phase_agents({"agents": [{"name": "developer"}]}, "implementation")

    assert ok is False
    assert calls == []
    assert orchestrator._phase_failure_status == "repo_map_target_workspace_mismatch"


def test_task_designer_blocked_when_selected_task_dependencies_incomplete(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "gateway-v4" / "app").mkdir(parents=True, exist_ok=True)
    (target_workspace / "gateway-v4" / "app" / "models.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "gateway-v4" / "app" / "database.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator._wait_for_user = lambda _prompt: True
    orchestrator._implementation_backlog_cache = [
        {
            "id": "TASK-001",
            "title": "Create tests package",
            "priority": "P0",
            "scope": "backend-only",
            "existing_paths": [],
            "new_directories": ["gateway-v4/tests"],
            "new_files": ["gateway-v4/tests/__init__.py"],
            "allowed_paths": ["gateway-v4/tests/__init__.py"],
            "forbidden_paths": [],
            "required_test_paths": ["gateway-v4/tests/__init__.py"],
            "depends_on": [],
            "target_file": {"path": "gateway-v4/tests/__init__.py", "action": "create", "purpose": "Create test package marker."},
            "reference_files": [],
            "acceptance_criteria": ["done"],
            "reason_each_path_is_needed": {"gateway-v4/tests/__init__.py": "Package marker."},
            "risk_level": "low",
            "estimated_effort": "S",
        },
        {
            "id": "TASK-002",
            "title": "Add provider metrics model",
            "priority": "P0",
            "scope": "backend-only",
            "existing_paths": ["gateway-v4/app/models.py", "gateway-v4/app/database.py", "gateway-v4/tests/__init__.py"],
            "new_directories": [],
            "new_files": ["gateway-v4/tests/test_provider_metrics_model.py"],
            "allowed_paths": [
                "gateway-v4/app/models.py",
                "gateway-v4/app/database.py",
                "gateway-v4/tests/__init__.py",
                "gateway-v4/tests/test_provider_metrics_model.py",
            ],
            "forbidden_paths": [],
            "required_test_paths": ["gateway-v4/tests/test_provider_metrics_model.py"],
            "depends_on": ["TASK-001"],
            "target_file": {"path": "gateway-v4/app/models.py", "action": "update", "purpose": "Add model."},
            "reference_files": ["gateway-v4/app/models.py", "gateway-v4/app/database.py"],
            "acceptance_criteria": ["done"],
            "reason_each_path_is_needed": {
                "gateway-v4/app/models.py": "Update model file.",
                "gateway-v4/app/database.py": "Reference base.",
                "gateway-v4/tests/__init__.py": "Dependency artifact.",
                "gateway-v4/tests/test_provider_metrics_model.py": "Test file.",
            },
            "risk_level": "low",
            "estimated_effort": "S",
        },
    ]
    orchestrator._implementation_backlog_source = "implementation-planner"
    orchestrator._selected_implementation_item = orchestrator._implementation_backlog_cache[1]
    repo_map = {
        "target_workspace": str(target_workspace),
        "top_level_tree": ["gateway-v4/"],
        "directories": ["", "gateway-v4", "gateway-v4/app"],
        "files": [
            {"path": "gateway-v4/app/models.py"},
            {"path": "gateway-v4/app/database.py"},
        ],
    }
    orchestrator.repo_map_path.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.repo_map_path.write_text(json.dumps(repo_map), encoding="utf-8")
    orchestrator._repo_map_cache = repo_map
    calls: list[str] = []
    orchestrator._run_agent = lambda agent, phase, index=None, total=None: calls.append(agent["name"]) or True

    ok = orchestrator._run_phase_agents({"agents": [{"name": "task-designer"}]}, "implementation")

    assert ok is False
    assert calls == []
    assert orchestrator._phase_failure_status == "task_dependencies_incomplete"


def test_developer_blocked_when_selected_task_dependencies_incomplete(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "gateway-v4" / "app").mkdir(parents=True, exist_ok=True)
    (target_workspace / "gateway-v4" / "app" / "models.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "gateway-v4" / "app" / "database.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator._wait_for_user = lambda _prompt: True
    orchestrator._implementation_backlog_cache = [
        {
            "id": "TASK-001",
            "title": "Create tests package",
            "priority": "P0",
            "scope": "backend-only",
            "existing_paths": [],
            "new_directories": ["gateway-v4/tests"],
            "new_files": ["gateway-v4/tests/__init__.py"],
            "allowed_paths": ["gateway-v4/tests/__init__.py"],
            "forbidden_paths": [],
            "required_test_paths": ["gateway-v4/tests/__init__.py"],
            "depends_on": [],
            "target_file": {"path": "gateway-v4/tests/__init__.py", "action": "create", "purpose": "Create test package marker."},
            "reference_files": [],
            "acceptance_criteria": ["done"],
            "reason_each_path_is_needed": {"gateway-v4/tests/__init__.py": "Package marker."},
            "risk_level": "low",
            "estimated_effort": "S",
        },
        {
            "id": "TASK-002",
            "title": "Add provider metrics model",
            "priority": "P0",
            "scope": "backend-only",
            "existing_paths": ["gateway-v4/app/models.py", "gateway-v4/app/database.py", "gateway-v4/tests/__init__.py"],
            "new_directories": [],
            "new_files": ["gateway-v4/tests/test_provider_metrics_model.py"],
            "allowed_paths": [
                "gateway-v4/app/models.py",
                "gateway-v4/app/database.py",
                "gateway-v4/tests/__init__.py",
                "gateway-v4/tests/test_provider_metrics_model.py",
            ],
            "forbidden_paths": [],
            "required_test_paths": ["gateway-v4/tests/test_provider_metrics_model.py"],
            "depends_on": ["TASK-001"],
            "target_file": {"path": "gateway-v4/app/models.py", "action": "update", "purpose": "Add model."},
            "reference_files": ["gateway-v4/app/models.py", "gateway-v4/app/database.py"],
            "acceptance_criteria": ["done"],
            "reason_each_path_is_needed": {
                "gateway-v4/app/models.py": "Update model file.",
                "gateway-v4/app/database.py": "Reference base.",
                "gateway-v4/tests/__init__.py": "Dependency artifact.",
                "gateway-v4/tests/test_provider_metrics_model.py": "Test file.",
            },
            "risk_level": "low",
            "estimated_effort": "S",
            "contract_source": "task-designer",
        },
    ]
    orchestrator._implementation_backlog_source = "implementation-planner"
    orchestrator._selected_implementation_item = orchestrator._implementation_backlog_cache[1]
    repo_map = {
        "target_workspace": str(target_workspace),
        "top_level_tree": ["gateway-v4/"],
        "directories": ["", "gateway-v4", "gateway-v4/app"],
        "files": [
            {"path": "gateway-v4/app/models.py"},
            {"path": "gateway-v4/app/database.py"},
        ],
    }
    orchestrator.repo_map_path.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.repo_map_path.write_text(json.dumps(repo_map), encoding="utf-8")
    orchestrator._repo_map_cache = repo_map
    calls: list[str] = []
    orchestrator._run_agent = lambda agent, phase, index=None, total=None: calls.append(agent["name"]) or True

    ok = orchestrator._run_phase_agents({"agents": [{"name": "developer"}]}, "implementation")

    assert ok is False
    assert calls == []
    assert orchestrator._phase_failure_status == "task_dependencies_incomplete"


def test_later_task_may_use_file_created_earlier(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "tests").mkdir(parents=True, exist_ok=True)
    (target_workspace / "tests" / "test_workflow.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                        {
                            "id": "TASK-001",
                            "title": "Create run state",
                            "priority": "P0",
                            "scope": "Add run state persistence.",
                            "existing_paths": [],
                            "new_files": ["workflow/run_state.py", "tests/test_run_state.py"],
                            "allowed_paths": ["workflow/run_state.py", "tests/test_run_state.py"],
                            "forbidden_paths": [],
                            "required_test_paths": ["tests/test_run_state.py"],
                            "depends_on": [],
                            "target_file": {"path": "workflow/run_state.py", "action": "create", "purpose": "Create runtime state module."},
                            "test_file": {"path": "tests/test_run_state.py", "action": "create"},
                            "must_contain": ["class RunState", "def load("],
                            "must_import": ["from pathlib import Path"],
                            "integration": ["Run state module will be reused by later orchestration tasks."],
                            "reference_files": ["workflow/orchestrator.py"],
                            "reference_excerpts": {"workflow/orchestrator.py": "pass"},
                            "must_test": ["test_run_state_roundtrip: assert persisted state can be loaded"],
                            "forbidden": ["Do not edit frontend files."],
                            "acceptance_criteria": ["done"],
                            "reason_each_path_is_needed": {
                                "workflow/run_state.py": "Create runtime state module.",
                                "tests/test_run_state.py": "Regression test for the new runtime state module.",
                            },
                            "risk_level": "low",
                            "estimated_effort": "S",
                        },
                    {
                        "id": "TASK-002",
                        "title": "Use run state",
                        "priority": "P1",
                        "scope": "Use the new run state module from the orchestrator.",
                        "depends_on": ["TASK-001"],
                        "existing_paths": ["workflow/run_state.py", "workflow/orchestrator.py", "tests/test_workflow.py"],
                        "new_files": [],
                        "allowed_paths": ["workflow/run_state.py", "workflow/orchestrator.py", "tests/test_workflow.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_workflow.py"],
                        "target_file": {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Wire run state into orchestration."},
                        "test_file": {"path": "tests/test_workflow.py", "action": "update"},
                        "must_contain": ["RunState", "def _load_saved_agent_report("],
                        "must_import": ["from pathlib import Path"],
                        "integration": ["Orchestrator must use the run state module created by TASK-001."],
                        "reference_files": ["workflow/run_state.py", "workflow/orchestrator.py"],
                        "reference_excerpts": {"workflow/run_state.py": "pass", "workflow/orchestrator.py": "pass"},
                        "must_test": ["test_orchestrator_uses_run_state: assert orchestrator imports or references RunState"],
                        "forbidden": ["Do not edit frontend files."],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {
                            "workflow/run_state.py": "Reuse the created module.",
                            "workflow/orchestrator.py": "Wire persistence into orchestration.",
                            "tests/test_workflow.py": "Regression test for orchestrator integration.",
                        },
                        "risk_level": "low",
                        "estimated_effort": "S",
                    },
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is True
    assert diagnostics["dependency_validation_errors"] == []
    assert orchestrator._planner_dependency_graph["TASK-002"]["effective"] == ["TASK-001"]


def test_missing_dependency_fails(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "TASK-002",
                        "title": "Use run state",
                        "priority": "P1",
                        "scope": "Use an unavailable dependency.",
                        "depends_on": ["TASK-001"],
                        "existing_paths": ["workflow/orchestrator.py"],
                        "new_files": [],
                        "allowed_paths": ["workflow/orchestrator.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_workflow.py"],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {"workflow/orchestrator.py": "Update orchestration."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert any("missing_dependency" in item for item in diagnostics["dependency_validation_errors"])


def test_cyclic_dependencies_fail(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "TASK-001",
                        "title": "First task",
                        "priority": "P0",
                        "scope": "Backfill state.",
                        "depends_on": ["TASK-002"],
                        "existing_paths": ["workflow/orchestrator.py"],
                        "new_files": [],
                        "allowed_paths": ["workflow/orchestrator.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_workflow.py"],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {"workflow/orchestrator.py": "Update orchestration."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    },
                    {
                        "id": "TASK-002",
                        "title": "Second task",
                        "priority": "P1",
                        "scope": "Finish wiring.",
                        "depends_on": ["TASK-001"],
                        "existing_paths": ["workflow/orchestrator.py"],
                        "new_files": [],
                        "allowed_paths": ["workflow/orchestrator.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_workflow.py"],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {"workflow/orchestrator.py": "Update orchestration."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    },
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert any("cyclic_dependency" in item for item in diagnostics["dependency_validation_errors"])


def test_dependency_order_violation_fails(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "TASK-001",
                        "title": "Task one",
                        "priority": "P0",
                        "scope": "Uses a later dependency.",
                        "depends_on": ["TASK-002"],
                        "existing_paths": ["workflow/orchestrator.py"],
                        "new_files": [],
                        "allowed_paths": ["workflow/orchestrator.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_workflow.py"],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {"workflow/orchestrator.py": "Update orchestration."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    },
                    {
                        "id": "TASK-002",
                        "title": "Task two",
                        "priority": "P1",
                        "scope": "Later task.",
                        "existing_paths": ["workflow/orchestrator.py"],
                        "new_files": [],
                        "allowed_paths": ["workflow/orchestrator.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_workflow.py"],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {"workflow/orchestrator.py": "Update orchestration."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    },
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert any("dependency_order_violation" in item for item in diagnostics["dependency_validation_errors"])


def test_nested_new_directories_and_new_files_pass(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "TASK-001",
                        "title": "Create nested state directory",
                        "priority": "P0",
                        "scope": "Add nested run state storage.",
                        "existing_paths": ["workflow/orchestrator.py"],
                        "new_directories": ["workflow/state", "workflow/state/runtime", "tests"],
                        "new_files": ["workflow/state/runtime/store.py", "tests/test_store.py"],
                        "allowed_paths": ["workflow/orchestrator.py", "workflow/state/runtime/store.py", "tests/test_store.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_store.py"],
                        "depends_on": [],
                        "target_file": {"path": "workflow/state/runtime/store.py", "action": "create", "purpose": "Implement nested runtime storage."},
                        "test_file": {"path": "tests/test_store.py", "action": "create"},
                        "must_contain": ["class RuntimeStore", "def save("],
                        "must_import": ["from pathlib import Path"],
                        "integration": ["Workflow state storage is wired from orchestrator."],
                        "reference_files": ["workflow/orchestrator.py"],
                        "reference_excerpts": {"workflow/orchestrator.py": "pass"},
                        "must_test": ["test_runtime_store_roundtrip: assert nested store persists data"],
                        "forbidden": ["Do not edit frontend files."],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {
                            "workflow/orchestrator.py": "Wire the feature.",
                            "workflow/state/runtime/store.py": "Implement storage.",
                            "tests/test_store.py": "Regression test for nested storage.",
                        },
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is True
    assert diagnostics["invalid_paths"] == []


def test_known_paths_update_incrementally(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "tools").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "tests").mkdir(parents=True, exist_ok=True)
    (target_workspace / "tests" / "test_workflow.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                        {
                            "id": "TASK-001",
                            "title": "Create run state",
                            "priority": "P0",
                            "scope": "Add run state persistence.",
                            "existing_paths": [],
                            "new_files": ["workflow/run_state.py", "tests/test_run_state.py"],
                            "allowed_paths": ["workflow/run_state.py", "tests/test_run_state.py"],
                            "forbidden_paths": [],
                            "required_test_paths": ["tests/test_run_state.py"],
                            "depends_on": [],
                            "target_file": {"path": "workflow/run_state.py", "action": "create", "purpose": "Create runtime state module."},
                            "test_file": {"path": "tests/test_run_state.py", "action": "create"},
                            "must_contain": ["class RunState", "def load("],
                            "must_import": ["from pathlib import Path"],
                            "integration": ["Run state module will be reused by later tasks."],
                            "reference_files": ["workflow/orchestrator.py"],
                            "reference_excerpts": {"workflow/orchestrator.py": "pass"},
                            "must_test": ["test_run_state_roundtrip: assert persisted state can be loaded"],
                            "forbidden": ["Do not edit frontend files."],
                            "acceptance_criteria": ["done"],
                            "reason_each_path_is_needed": {
                                "workflow/run_state.py": "Create runtime state module.",
                                "tests/test_run_state.py": "Regression test for run state creation.",
                            },
                            "risk_level": "low",
                            "estimated_effort": "S",
                        },
                    {
                        "id": "TASK-002",
                        "title": "Create status command",
                        "priority": "P1",
                        "scope": "Reuse run state in status command.",
                        "depends_on": ["TASK-001"],
                        "existing_paths": ["workflow/run_state.py", "workflow/orchestrator.py"],
                        "new_files": ["tools/status_cmd.py", "tests/test_status_cmd.py"],
                        "allowed_paths": ["workflow/run_state.py", "workflow/orchestrator.py", "tools/status_cmd.py", "tests/test_status_cmd.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_status_cmd.py"],
                        "target_file": {"path": "tools/status_cmd.py", "action": "create", "purpose": "Implement status command."},
                        "test_file": {"path": "tests/test_status_cmd.py", "action": "create"},
                        "must_contain": ["def main(", "RunState"],
                        "must_import": ["from pathlib import Path"],
                        "integration": ["Status command must reuse the run state module from TASK-001."],
                        "reference_files": ["workflow/run_state.py", "workflow/orchestrator.py"],
                        "reference_excerpts": {"workflow/run_state.py": "pass", "workflow/orchestrator.py": "pass"},
                        "must_test": ["test_status_command_reads_run_state: assert status command uses persisted state"],
                        "forbidden": ["Do not edit frontend files."],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {
                            "workflow/run_state.py": "Reuse created state module.",
                            "workflow/orchestrator.py": "Expose status plumbing.",
                            "tools/status_cmd.py": "Implement status command.",
                            "tests/test_status_cmd.py": "Regression test for the new status command.",
                        },
                        "risk_level": "low",
                        "estimated_effort": "S",
                    },
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is True
    assert "workflow/run_state.py" in orchestrator._planner_future_known_paths["TASK-001"]["known_files_after_task"]
    assert "tools/status_cmd.py" in orchestrator._planner_future_known_paths["TASK-002"]["known_files_after_task"]


def test_planner_failure_does_not_rerun_architect_on_interactive_retry(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "tests").mkdir(parents=True, exist_ok=True)
    (target_workspace / "tests" / "test_workflow.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.config["workflow"]["mode"] = "interactive"
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    answers = iter(["r"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    calls: list[str] = []
    planner_calls = {"count": 0}

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        if agent_config["name"] == "architect":
            orchestrator.logger.save_agent_report("implementation", "architect", {"status": "success", "result": "completed", "parsed_output": "Architect output"})
            return True
        if agent_config["name"] == "implementation-planner":
            planner_calls["count"] += 1
            payload = {
                "status": "success",
                "result": "completed",
                "parsed_output": json.dumps(
                    [
                        {
                            "id": "planner-task",
                            "title": "Planner task",
                            "priority": "P0",
                            "scope": "Improve backend validation.",
                            "existing_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"] if planner_calls["count"] >= 3 else [],
                            "new_directories": [],
                            "allowed_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"] if planner_calls["count"] >= 3 else ["services/foo.py"],
                            "new_files": [],
                            "forbidden_paths": [],
                            "required_test_paths": ["tests/test_workflow.py"] if planner_calls["count"] >= 3 else [],
                            "depends_on": [],
                            "acceptance_criteria": ["done"],
                            "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed.", "tests/test_workflow.py": "Needed."} if planner_calls["count"] >= 3 else {"services/foo.py": "Needed."},
                            "target_file": {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Improve backend validation."} if planner_calls["count"] >= 3 else {"path": "services/foo.py", "action": "update", "purpose": "Invalid path"},
                            "test_file": {"path": "tests/test_workflow.py", "action": "update"} if planner_calls["count"] >= 3 else {"path": "", "action": ""},
                            "must_contain": ["def _validate_implementation_planner_output(", "return {"] if planner_calls["count"] >= 3 else [],
                            "must_import": ["from pathlib import Path"] if planner_calls["count"] >= 3 else [],
                            "integration": ["Validation must integrate with planner flow."] if planner_calls["count"] >= 3 else [],
                            "reference_files": ["workflow/orchestrator.py"] if planner_calls["count"] >= 3 else [],
                            "reference_excerpts": {"workflow/orchestrator.py": "pass"} if planner_calls["count"] >= 3 else {},
                            "must_test": ["test_validate_output_accepts_valid_paths: assert diagnostics valid"] if planner_calls["count"] >= 3 else [],
                            "forbidden": ["Do not edit frontend files."] if planner_calls["count"] >= 3 else [],
                            "risk_level": "low",
                            "estimated_effort": "S",
                        }
                    ]
                ),
            }
            orchestrator.logger.save_agent_report("implementation", "implementation-planner", payload)
            return True
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)
    phase = {"agents": [{"name": "architect"}, {"name": "implementation-planner"}]}

    ok = orchestrator._run_phase_agents(phase, "implementation")

    assert ok is True
    assert calls == ["architect", "implementation-planner", "implementation-planner", "implementation-planner"]


def test_retry_agent_implementation_planner_reuses_architect_output(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "tests").mkdir(parents=True, exist_ok=True)
    (target_workspace / "tests" / "test_workflow.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        retry_agent="implementation-planner",
    )
    orchestrator.config["phases"]["implementation"] = {
        "name": "Implementation",
        "max_retries": 1,
        "agents": [
            {"name": "architect"},
            {"name": "implementation-planner"},
            {"name": "developer"},
            {"name": "qa"},
            {"name": "template-validator"},
        ],
    }
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120100",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )
    previous_run = orchestrator.logger.log_dir / "run_20260101_120101" / "agents" / "implementation"
    previous_run.mkdir(parents=True, exist_ok=True)
    (previous_run / "architect.json").write_text(json.dumps({"status": "success", "parsed_output": "Architect output", "result": "completed"}), encoding="utf-8")
    calls: list[str] = []
    merges: list[str] = []
    prompts: list[str] = []

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        if agent_config["name"] == "implementation-planner":
            orchestrator.logger.save_agent_report(
                "implementation",
                "implementation-planner",
                {
                    "status": "success",
                    "result": "completed",
                    "parsed_output": json.dumps(
                        [
                            {
                                "id": "planner-task",
                                "title": "Planner task",
                                "priority": "P0",
                                "scope": "Improve backend validation.",
                                "existing_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                                "allowed_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                                "forbidden_paths": [],
                                "required_test_paths": ["tests/test_workflow.py"],
                                "depends_on": [],
                                "acceptance_criteria": ["done"],
                                "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed.", "tests/test_workflow.py": "Needed."},
                                "target_file": {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Improve backend validation."},
                                "test_file": {"path": "tests/test_workflow.py", "action": "update"},
                                "must_contain": ["def _validate_implementation_planner_output(", "return {"],
                                "must_import": ["from pathlib import Path"],
                                "integration": ["Validation must integrate with planner flow."],
                                "reference_files": ["workflow/orchestrator.py"],
                                "reference_excerpts": {"workflow/orchestrator.py": "pass"},
                                "must_test": ["test_validate_output_accepts_valid_paths: assert diagnostics valid"],
                                "forbidden": ["Do not edit frontend files."],
                                "risk_level": "low",
                                "estimated_effort": "S",
                            }
                        ]
                    ),
                },
            )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    monkeypatch.setattr(orchestrator, "_merge_git", lambda: merges.append("merge") or True)
    monkeypatch.setattr(orchestrator, "_prompt_post_implementation_action", lambda: prompts.append("prompt") or "stop")

    ok = orchestrator._run_implementation_phase()

    assert ok is True
    assert calls == ["implementation-planner"]
    assert orchestrator._reused_architect_output is True
    assert orchestrator._architect_output_source.endswith("architect.json")
    assert merges == []
    assert prompts == []


def test_from_agent_developer_reuses_planner_and_selected_task(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "tests").mkdir(parents=True, exist_ok=True)
    (target_workspace / "tests" / "test_workflow.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        from_agent="developer",
    )
    orchestrator.config["phases"]["implementation"] = {
        "name": "Implementation",
        "max_retries": 1,
        "agents": [
            {"name": "architect"},
            {"name": "implementation-planner"},
            {"name": "developer"},
            {"name": "qa"},
            {"name": "template-validator"},
        ],
    }
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120200",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )
    previous_run = orchestrator.logger.log_dir / "run_20260101_120201" / "agents" / "implementation"
    previous_run.mkdir(parents=True, exist_ok=True)
    (previous_run / "architect.json").write_text(json.dumps({"status": "success", "parsed_output": "Architect output", "result": "completed"}), encoding="utf-8")
    (previous_run / "implementation-planner.json").write_text(
        json.dumps(
            {
                "status": "success",
                "result": "completed",
                "parsed_output": json.dumps(
                    [
                        {
                            "id": "planner-task",
                            "title": "Planner task",
                            "priority": "P0",
                            "scope": "Improve backend validation.",
                            "existing_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                            "allowed_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                            "forbidden_paths": [],
                            "required_test_paths": ["tests/test_workflow.py"],
                            "depends_on": [],
                            "acceptance_criteria": ["done"],
                            "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed.", "tests/test_workflow.py": "Needed."},
                            "target_file": {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Improve backend validation."},
                            "test_file": {"path": "tests/test_workflow.py", "action": "update"},
                            "must_contain": ["def _validate_implementation_planner_output(", "return {"],
                            "must_import": ["from pathlib import Path"],
                            "integration": ["Validation must integrate with planner flow."],
                            "reference_files": ["workflow/orchestrator.py"],
                            "reference_excerpts": {"workflow/orchestrator.py": "pass"},
                            "must_test": ["test_validate_output_accepts_valid_paths: assert diagnostics valid"],
                            "forbidden": ["Do not edit frontend files."],
                            "risk_level": "low",
                            "estimated_effort": "S",
                        }
                    ]
                ),
            }
        ),
        encoding="utf-8",
    )
    (previous_run / "developer.json").write_text(
        json.dumps(
            {
                "status": "no_changes",
                "result": "Developer completed without modifying target files.",
                "selected_task_id": "planner-task",
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        orchestrator.logger.save_agent_report(
            "implementation",
            agent_config["name"],
            {
                "status": "success",
                "result": "completed",
                "parsed_output": "done",
                "selected_task_id": orchestrator.selected_task_ref,
            },
        )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    monkeypatch.setattr(orchestrator, "_enforce_implementation_scope_diff", lambda: True)
    monkeypatch.setattr(orchestrator, "_prompt_post_implementation_action", lambda: "stop")

    ok = orchestrator._run_implementation_phase()

    assert ok is True
    assert calls == ["developer", "qa", "template-validator"]
    assert orchestrator.selected_task_ref == "planner-task"


def test_extract_qa_verdict_detects_pass_and_fail(tmp_path: Path) -> None:
    config_path = tmp_path / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(tmp_path), launch_cwd=str(tmp_path))

    assert orchestrator._extract_qa_verdict("Вердикт QA: НЕ ПРОЙДЕНО\n\nИтог: есть замечания") == "failed"
    assert orchestrator._extract_qa_verdict("Вердикт QA: ПРОЙДЕНО\n\nИтог: ок") == "passed"
    assert orchestrator._extract_qa_verdict("Итог: без вердикта") == ""


def test_developer_bundle_includes_retry_feedback(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    engine_root.mkdir(parents=True, exist_ok=True)
    target_workspace.mkdir(parents=True, exist_ok=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    prompt_file = engine_root / ".openclaw" / "agents" / "implementation" / "developer" / "prompt.md"
    prompt_file.parent.mkdir(parents=True, exist_ok=True)
    prompt_file.write_text("Developer prompt", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator._implementation_retry_from_agent = "developer"
    orchestrator._implementation_attempt = 2
    feedback_file = orchestrator._save_feedback(1, "developer", "Implementation repair feedback\n\nQA findings to repair\n- fix migration")
    orchestrator._developer_feedback_file = str(feedback_file)

    monkeypatch.setattr(
        orchestrator,
        "_build_implementation_phase_context",
        lambda agent_name, limit=12000: {
            "repository_context": "repo",
            "previous_context": "prev",
            "research_handoff_sources": [],
            "selected_task_scope": "backend-only",
            "selected_task_id": "TASK-001",
            "selected_task_allowed_paths": ["workflow/orchestrator.py"],
            "backlog_task_count": 1,
            "implementation_planner_output_chars": 0,
            "planner_model": "",
            "planner_invalid_paths": [],
            "planner_repair_attempted": False,
            "validated_backlog_task_count": 1,
            "planner_missing_directories": [],
            "planner_missing_tests": [],
            "planner_conflicting_forbidden_paths": [],
            "generic_root_dirs_rejected": [],
            "planner_dependency_graph": {},
            "planner_future_known_paths": {},
            "planner_dependency_validation_errors": [],
            "planner_rejection_reason": "",
            "planner_feedback_file": "",
            "planner_feedback_source": "",
            "planner_feedback_chars": 0,
            "planner_parse_error": "",
            "planner_schema_errors": [],
            "planner_raw_output_excerpt": "",
            "planner_extracted_payload_excerpt": "",
            "planner_validation_stage": "",
            "reused_architect_output": False,
            "architect_output_source": "",
            "planner_retry_count": 0,
            "planner_retry_reason": "",
            "repo_map_path": "",
            "repo_map_file_count": 0,
            "repo_map_directory_count": 0,
            "backlog_source": "test",
            "contract_completeness": True,
            "contract_compliance": True,
            "missing_must_contain": [],
            "missing_test_file": False,
            "context_chars": 8,
        },
    )

    bundle = orchestrator._build_agent_message_bundle(
        "developer",
        {"name": "developer", "description": "Implement the task"},
        prompt_file,
        "implementation",
    )

    assert "Previous validation feedback to repair" in bundle["combined_message"]
    assert "fix migration" in bundle["combined_message"]
    assert bundle["developer_feedback_chars"] > 0
    assert bundle["developer_feedback_source"].endswith("developer.md")


def test_implementation_phase_retries_from_developer_after_qa_failed(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.config["phases"]["implementation"] = {
        "name": "Implementation",
        "max_retries": 2,
        "agents": [
            {"name": "architect"},
            {"name": "implementation-planner"},
            {"name": "task-designer"},
            {"name": "developer"},
            {"name": "qa"},
            {"name": "template-validator"},
        ],
    }
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120260",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )

    calls: list[tuple[int, str]] = []

    monkeypatch.setattr(orchestrator, "_refresh_repo_map", lambda snapshot_path=None: True)
    monkeypatch.setattr(orchestrator, "_load_repo_map", lambda: {"target_workspace": str(target_workspace), "files": [], "directories": []})
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    monkeypatch.setattr(orchestrator, "_prepare_implementation_backlog_selection", lambda require_backlog=True: {"error": "", "backlog": [{"id": "TASK-001"}], "backlog_source": "test"})
    monkeypatch.setattr(orchestrator, "_selected_task_dependency_error", lambda: "")
    monkeypatch.setattr(orchestrator, "_enforce_implementation_scope_plan", lambda: True)
    monkeypatch.setattr(orchestrator, "_enforce_implementation_scope_diff", lambda: True)
    monkeypatch.setattr(orchestrator, "_capture_repo_map_after_developer", lambda: None)
    monkeypatch.setattr(orchestrator, "_merge_git", lambda: True)
    monkeypatch.setattr(orchestrator, "_prompt_post_implementation_action", lambda: "stop")
    monkeypatch.setattr(orchestrator, "_validate_or_repair_implementation_planner", lambda *args, **kwargs: True)
    monkeypatch.setattr(orchestrator, "_apply_task_designer_contract_from_report", lambda report: setattr(orchestrator, "_selected_implementation_item", {"id": "TASK-001", "contract_source": "task-designer"}) or True)

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append((orchestrator._implementation_attempt, agent_config["name"]))
        name = agent_config["name"]
        if orchestrator._implementation_attempt == 1 and name == "qa":
            orchestrator._phase_failure_status = "qa_failed"
            orchestrator.logger.save_agent_report(
                "implementation",
                "qa",
                {
                    "status": "qa_failed",
                    "result": "qa reported regressions",
                    "parsed_output": "Вердикт QA: НЕ ПРОЙДЕНО\n\nПроверенные файлы:\n- a\n\nСоответствие контракту:\n- нет\n\nЗамечания:\n- fix migration\n\nИтог:\n- переделать",
                    "selected_task_id": "TASK-001",
                },
            )
            return False
        orchestrator.logger.save_agent_report(
            "implementation",
            name,
            {
                "status": "success",
                "result": "completed",
                "parsed_output": "done",
                "selected_task_id": "TASK-001",
            },
        )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)

    ok = orchestrator._run_implementation_phase()

    assert ok is True
    assert calls == [
        (1, "architect"),
        (1, "implementation-planner"),
        (1, "task-designer"),
        (1, "developer"),
        (1, "qa"),
        (1, "template-validator"),
        (2, "developer"),
        (2, "qa"),
        (2, "template-validator"),
    ]
    assert orchestrator._developer_feedback_file.endswith("developer.md")


def test_developer_deterministic_checks_fail_on_python_compile_error(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    bad_file = target_workspace / "gateway-v4" / "tests" / "test_bad.py"
    bad_file.parent.mkdir(parents=True, exist_ok=True)
    bad_file.write_text("def broken(:\n    pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.task_counter = 1
    orchestrator._selected_implementation_item = {"id": "TASK-001", "test_file": {"path": "gateway-v4/tests/test_bad.py"}}
    orchestrator.logger.save_agent_report(
        "implementation",
        "developer",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": "status=implemented",
            "developer_changed_files": ["gateway-v4/tests/test_bad.py"],
            "selected_task_id": "TASK-001",
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_local_command",
        lambda command, timeout=10, cwd=None: (1, "", "SyntaxError: invalid syntax")
        if command[:3] == [sys.executable, "-m", "py_compile"]
        else (0, "ok", ""),
    )

    ok = orchestrator._run_developer_deterministic_checks()

    assert ok is False
    assert orchestrator._phase_failure_status == "developer_checks_failed"
    report = orchestrator._load_saved_agent_report("implementation", "developer-checks") or {}
    assert report["status"] == "failed"
    assert "py_compile failed" in report["parsed_output"]


def test_validate_changed_migration_files_rejects_none_down_revision(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    versions_dir = target_workspace / "gateway-v4" / "alembic" / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    (versions_dir / "0005_personalization.py").write_text("revision = '0005'\ndown_revision = '0004'\n", encoding="utf-8")
    (versions_dir / "20240801_add_provider_metrics.py").write_text(
        "revision = '20240801_add_provider_metrics'\ndown_revision = None\n",
        encoding="utf-8",
    )
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))

    issues = orchestrator._validate_changed_migration_files(["gateway-v4/alembic/versions/20240801_add_provider_metrics.py"])

    assert issues == ["gateway-v4/alembic/versions/20240801_add_provider_metrics.py: down_revision must not be None"]


def test_no_changes_completion_claim_triggers_deterministic_validation(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True, exist_ok=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.task_counter = 1
    orchestrator._selected_implementation_item = {
        "id": "TASK-001",
        "target_file": {"path": "gateway-v4/alembic/versions/20240801_add_provider_metrics.py"},
        "test_file": {"path": "gateway-v4/tests/test_provider_metrics_migration.py"},
        "must_contain": ["def upgrade()"],
    }
    orchestrator.logger.save_agent_report(
        "implementation",
        "developer",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": "status=no_changes: The implementation is already complete and satisfies all contract requirements.",
            "selected_task_id": "TASK-001",
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_collect_scope_watchdog_diff_diagnostics",
        lambda: {
            "allowed": True,
            "changed_files": [],
            "changed_files_count": 0,
            "diff_lines_count": 0,
            "forbidden_hits": [],
            "allowed_paths_matched": [],
            "scope_policy_result": "allowed",
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_run_local_command",
        lambda command, timeout=10, cwd=None: (1, "", "SyntaxError: invalid syntax")
        if command[:3] == [sys.executable, "-m", "py_compile"]
        else (0, "ok", ""),
    )

    ok = orchestrator._enforce_implementation_scope_diff()

    assert ok is False
    assert orchestrator._phase_failure_status == "developer_checks_failed"
    report = orchestrator._load_saved_agent_report("implementation", "developer-checks") or {}
    assert report["status"] == "failed"
    assert "contract_compliance check failed" in report["parsed_output"]
    assert orchestrator._developer_feedback_file.endswith("developer.md")


def test_from_agent_developer_reselects_task_when_reused_selection_has_incomplete_dependencies(
    tmp_path: Path, monkeypatch
) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "gateway-v4" / "app").mkdir(parents=True, exist_ok=True)
    (target_workspace / "gateway-v4" / "app" / "models.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "gateway-v4" / "app" / "database.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "gateway-v4" / "tests").mkdir(parents=True, exist_ok=True)
    (target_workspace / "gateway-v4" / "tests" / "__init__.py").write_text("", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        from_agent="developer",
    )
    orchestrator.config["workflow"]["mode"] = "interactive"
    orchestrator.config["phases"]["implementation"] = {
        "name": "Implementation",
        "max_retries": 1,
        "agents": [
            {"name": "architect"},
            {"name": "implementation-planner"},
            {"name": "task-designer"},
            {"name": "developer"},
        ],
    }
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_130000",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )
    previous_run = orchestrator.logger.log_dir / "run_20260101_130100" / "agents" / "implementation"
    previous_run.mkdir(parents=True, exist_ok=True)
    (previous_run / "architect.json").write_text(json.dumps({"status": "success", "parsed_output": "Architect output", "result": "completed"}), encoding="utf-8")
    planner_backlog = [
        {
            "id": "TASK-001",
            "title": "Create tests package",
            "priority": "P0",
            "scope": "backend-only",
            "existing_paths": [],
            "new_directories": ["gateway-v4/tests"],
            "new_files": ["gateway-v4/tests/__init__.py"],
            "allowed_paths": ["gateway-v4/tests/__init__.py"],
            "forbidden_paths": [],
            "required_test_paths": ["gateway-v4/tests/__init__.py"],
            "depends_on": [],
            "acceptance_criteria": ["done"],
            "reason_each_path_is_needed": {"gateway-v4/tests/__init__.py": "Package marker."},
            "target_file": {"path": "gateway-v4/tests/__init__.py", "action": "create", "purpose": "Create test package marker."},
            "reference_files": [],
            "risk_level": "low",
            "estimated_effort": "S",
        },
        {
            "id": "TASK-002",
            "title": "Add provider metrics model",
            "priority": "P0",
            "scope": "backend-only",
            "existing_paths": ["gateway-v4/app/models.py", "gateway-v4/app/database.py", "gateway-v4/tests/__init__.py"],
            "new_directories": [],
            "new_files": ["gateway-v4/tests/test_provider_metrics_model.py"],
            "allowed_paths": [
                "gateway-v4/app/models.py",
                "gateway-v4/app/database.py",
                "gateway-v4/tests/__init__.py",
                "gateway-v4/tests/test_provider_metrics_model.py",
            ],
            "forbidden_paths": [],
            "required_test_paths": ["gateway-v4/tests/test_provider_metrics_model.py"],
            "depends_on": ["TASK-001"],
            "acceptance_criteria": ["done"],
            "reason_each_path_is_needed": {
                "gateway-v4/app/models.py": "Update model file.",
                "gateway-v4/app/database.py": "Reference base.",
                "gateway-v4/tests/__init__.py": "Dependency artifact.",
                "gateway-v4/tests/test_provider_metrics_model.py": "Test file.",
            },
            "target_file": {"path": "gateway-v4/app/models.py", "action": "update", "purpose": "Add model."},
            "reference_files": ["gateway-v4/app/models.py", "gateway-v4/app/database.py"],
            "risk_level": "low",
            "estimated_effort": "S",
        },
    ]
    (previous_run / "implementation-planner.json").write_text(
        json.dumps({"status": "success", "parsed_output": json.dumps(planner_backlog), "result": "completed"}, ensure_ascii=False),
        encoding="utf-8",
    )
    selected_contract = dict(planner_backlog[1])
    selected_contract["contract_source"] = "task-designer"
    selected_contract["test_file"] = {"path": "gateway-v4/tests/test_provider_metrics_model.py", "action": "create"}
    selected_contract["must_contain"] = ["class ProviderMetrics(Base):", "response_time = Column(Float, nullable=False)"]
    selected_contract["must_import"] = ["from sqlalchemy import Column, Float", "from app.database import Base"]
    selected_contract["integration"] = ["ProviderMetrics uses Base from app.database."]
    selected_contract["reference_excerpts"] = {"gateway-v4/app/models.py": "pass", "gateway-v4/app/database.py": "pass"}
    selected_contract["must_test"] = ["test_provider_metrics_creation: assert model instance is created"]
    selected_contract["forbidden"] = ["Do not edit billing files."]
    (previous_run / "task-designer.json").write_text(
        json.dumps(
            {
                "status": "success",
                "parsed_output": json.dumps(selected_contract),
                "result": "completed",
                "selected_task_id": "TASK-002",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    repo_map = {
        "target_workspace": str(target_workspace),
        "top_level_tree": ["gateway-v4/"],
        "directories": ["", "gateway-v4", "gateway-v4/app", "gateway-v4/tests"],
        "files": [
            {"path": "gateway-v4/app/models.py"},
            {"path": "gateway-v4/app/database.py"},
            {"path": "gateway-v4/tests/__init__.py"},
        ],
    }
    orchestrator.repo_map_path.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.repo_map_path.write_text(json.dumps(repo_map), encoding="utf-8")
    orchestrator._repo_map_cache = repo_map
    answers = iter(["2"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    monkeypatch.setattr(orchestrator, "_capture_repo_map_after_developer", lambda: None)
    monkeypatch.setattr(orchestrator, "_enforce_implementation_scope_diff", lambda: True)
    assert orchestrator._reuse_planner_output_for_current_run() is True
    assert orchestrator._reuse_task_designer_output_for_current_run() is True
    calls: list[str] = []
    helper_calls: list[str] = []

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)

    def fake_run_task_designer_before_developer(phase, *, total):
        helper_calls.append("task-designer")
        selected_item = dict(orchestrator._selected_implementation_item or {})
        selected_item["contract_source"] = "task-designer"
        orchestrator._selected_implementation_item = selected_item
        return True

    monkeypatch.setattr(orchestrator, "_run_task_designer_before_developer", fake_run_task_designer_before_developer)
    ok = orchestrator._run_phase_agents(orchestrator.config["phases"]["implementation"], "implementation")

    assert ok is True
    assert orchestrator._selected_implementation_item["id"] == "TASK-001"
    assert helper_calls == ["task-designer"]
    assert calls == ["developer"]


def test_from_agent_implementation_planner_does_not_merge_delivery_branch(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "tests").mkdir(parents=True, exist_ok=True)
    (target_workspace / "tests" / "test_workflow.py").write_text("def test_ok(): pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        from_agent="implementation-planner",
    )
    orchestrator.config["phases"]["implementation"] = {
        "name": "Implementation",
        "max_retries": 1,
        "agents": [
            {"name": "architect"},
            {"name": "implementation-planner"},
            {"name": "task-designer"},
            {"name": "developer"},
            {"name": "qa"},
            {"name": "template-validator"},
        ],
    }
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120250",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )
    previous_run = orchestrator.logger.log_dir / "run_20260101_120251" / "agents" / "implementation"
    previous_run.mkdir(parents=True, exist_ok=True)
    (previous_run / "architect.json").write_text(json.dumps({"status": "success", "parsed_output": "Architect output", "result": "completed"}), encoding="utf-8")
    calls: list[str] = []
    merges: list[str] = []
    prompts: list[str] = []

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        if agent_config["name"] == "implementation-planner":
            orchestrator.logger.save_agent_report(
                "implementation",
                "implementation-planner",
                {
                    "status": "success",
                    "result": "completed",
                    "parsed_output": json.dumps(
                        [
                            {
                                "id": "planner-task",
                                "title": "Planner task",
                                "priority": "P0",
                                "scope": "Improve backend validation.",
                                "existing_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                                "allowed_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                                "forbidden_paths": [],
                                "required_test_paths": ["tests/test_workflow.py"],
                                "depends_on": [],
                                "acceptance_criteria": ["done"],
                                "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed.", "tests/test_workflow.py": "Needed."},
                                "target_file": {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Improve backend validation."},
                                "reference_files": ["workflow/orchestrator.py"],
                                "risk_level": "low",
                                "estimated_effort": "S",
                            }
                        ]
                    ),
                },
            )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    monkeypatch.setattr(orchestrator, "_merge_git", lambda: merges.append("merge") or True)
    monkeypatch.setattr(orchestrator, "_prompt_post_implementation_action", lambda: prompts.append("prompt") or "stop")

    ok = orchestrator._run_implementation_phase()

    assert ok is True
    assert calls == ["implementation-planner"]
    assert merges == []
    assert prompts == []


def test_retry_prompt_includes_previous_rejection_reason(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "tests").mkdir(parents=True, exist_ok=True)
    (target_workspace / "tests" / "test_workflow.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.config["workflow"]["mode"] = "interactive"
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    answers = iter(["r"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    planner_descriptions: list[str] = []
    planner_calls = {"count": 0}

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        if agent_config["name"] == "architect":
            orchestrator.logger.save_agent_report("implementation", "architect", {"status": "success", "result": "completed", "parsed_output": "Architect output"})
            return True
        if agent_config["name"] == "implementation-planner":
            planner_calls["count"] += 1
            planner_descriptions.append(str(agent_config.get("description") or ""))
            orchestrator.logger.save_agent_report(
                "implementation",
                "implementation-planner",
                {
                    "status": "success",
                    "result": "completed",
                    "parsed_output": json.dumps(
                        [
                            {
                                "id": "planner-task",
                                "title": "Planner task",
                                "priority": "P0",
                                "scope": "Improve backend validation.",
                                "existing_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"] if planner_calls["count"] >= 3 else [],
                                "allowed_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"] if planner_calls["count"] >= 3 else ["services/foo.py"],
                                "forbidden_paths": [],
                                "required_test_paths": ["tests/test_workflow.py"] if planner_calls["count"] >= 3 else [],
                                "depends_on": [],
                                "acceptance_criteria": ["done"],
                                "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed.", "tests/test_workflow.py": "Needed."} if planner_calls["count"] >= 3 else {"services/foo.py": "Needed."},
                                "target_file": {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Improve backend validation."} if planner_calls["count"] >= 3 else {"path": "services/foo.py", "action": "update", "purpose": "Invalid path"},
                                "test_file": {"path": "tests/test_workflow.py", "action": "update"} if planner_calls["count"] >= 3 else {"path": "", "action": ""},
                                "must_contain": ["def _validate_implementation_planner_output(", "return {"] if planner_calls["count"] >= 3 else [],
                                "must_import": ["from pathlib import Path"] if planner_calls["count"] >= 3 else [],
                                "integration": ["Validation must integrate with planner flow."] if planner_calls["count"] >= 3 else [],
                                "reference_files": ["workflow/orchestrator.py"] if planner_calls["count"] >= 3 else [],
                                "reference_excerpts": {"workflow/orchestrator.py": "pass"} if planner_calls["count"] >= 3 else {},
                                "must_test": ["test_validate_output_accepts_valid_paths: assert diagnostics valid"] if planner_calls["count"] >= 3 else [],
                                "forbidden": ["Do not edit frontend files."] if planner_calls["count"] >= 3 else [],
                                "risk_level": "low",
                                "estimated_effort": "S",
                            }
                        ]
                    ),
                },
            )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)

    ok = orchestrator._run_phase_agents({"agents": [{"name": "architect"}, {"name": "implementation-planner"}]}, "implementation")

    assert ok is True
    assert any("Previous rejection reasons:" in description for description in planner_descriptions[2:])


def test_auto_mode_retries_planner_once_then_stops(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.config["workflow"]["mode"] = "auto"
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    calls: list[str] = []

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        if agent_config["name"] == "architect":
            orchestrator.logger.save_agent_report("implementation", "architect", {"status": "success", "result": "completed", "parsed_output": "Architect output"})
            return True
        if agent_config["name"] == "implementation-planner":
            orchestrator.logger.save_agent_report(
                "implementation",
                "implementation-planner",
                {
                    "status": "success",
                    "result": "completed",
                    "parsed_output": json.dumps(
                        [
                            {
                                "id": "planner-task",
                                "title": "Planner task",
                                "priority": "P0",
                                "scope": "Improve backend validation.",
                                "allowed_paths": ["services/foo.py"],
                                "forbidden_paths": [],
                                "required_test_paths": [],
                                "acceptance_criteria": ["done"],
                                "reason_each_path_is_needed": {"services/foo.py": "Needed."},
                                "risk_level": "low",
                                "estimated_effort": "S",
                            }
                        ]
                    ),
                },
            )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)

    ok = orchestrator._run_phase_agents({"agents": [{"name": "architect"}, {"name": "implementation-planner"}]}, "implementation")

    assert ok is False
    assert calls == ["architect", "implementation-planner", "implementation-planner"]


def test_rerun_architect_option_still_works(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "tests").mkdir(parents=True, exist_ok=True)
    (target_workspace / "tests" / "test_workflow.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.config["workflow"]["mode"] = "interactive"
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    answers = iter(["a"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    calls: list[str] = []
    planner_calls = {"count": 0}

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        if agent_config["name"] == "architect":
            orchestrator.logger.save_agent_report("implementation", "architect", {"status": "success", "result": "completed", "parsed_output": "Architect output"})
            return True
        if agent_config["name"] == "implementation-planner":
            planner_calls["count"] += 1
            orchestrator.logger.save_agent_report(
                "implementation",
                "implementation-planner",
                {
                    "status": "success",
                    "result": "completed",
                    "parsed_output": json.dumps(
                        [
                            {
                                "id": "planner-task",
                                "title": "Planner task",
                                "priority": "P0",
                                "scope": "Improve backend validation.",
                                "existing_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"] if planner_calls["count"] >= 3 else [],
                                "allowed_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"] if planner_calls["count"] >= 3 else ["services/foo.py"],
                                "forbidden_paths": [],
                                "required_test_paths": ["tests/test_workflow.py"] if planner_calls["count"] >= 3 else [],
                                "depends_on": [],
                                "acceptance_criteria": ["done"],
                                "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed.", "tests/test_workflow.py": "Needed."} if planner_calls["count"] >= 3 else {"services/foo.py": "Needed."},
                                "target_file": {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Improve backend validation."} if planner_calls["count"] >= 3 else {"path": "services/foo.py", "action": "update", "purpose": "Invalid path"},
                                "test_file": {"path": "tests/test_workflow.py", "action": "update"} if planner_calls["count"] >= 3 else {"path": "", "action": ""},
                                "must_contain": ["def _validate_implementation_planner_output(", "return {"] if planner_calls["count"] >= 3 else [],
                                "must_import": ["from pathlib import Path"] if planner_calls["count"] >= 3 else [],
                                "integration": ["Validation must integrate with planner flow."] if planner_calls["count"] >= 3 else [],
                                "reference_files": ["workflow/orchestrator.py"] if planner_calls["count"] >= 3 else [],
                                "reference_excerpts": {"workflow/orchestrator.py": "pass"} if planner_calls["count"] >= 3 else {},
                                "must_test": ["test_validate_output_accepts_valid_paths: assert diagnostics valid"] if planner_calls["count"] >= 3 else [],
                                "forbidden": ["Do not edit frontend files."] if planner_calls["count"] >= 3 else [],
                                "risk_level": "low",
                                "estimated_effort": "S",
                            }
                        ]
                    ),
                },
            )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)

    ok = orchestrator._run_phase_agents({"agents": [{"name": "architect"}, {"name": "implementation-planner"}]}, "implementation")

    assert ok is True
    assert calls == ["architect", "implementation-planner", "implementation-planner", "architect", "implementation-planner"]


def test_retry_prompt_includes_previous_feedback_block(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator._implementation_attempt = 1
    orchestrator._planner_rejection_reason = "\n".join(
        [
            "Implementation planner rejection reasons:",
            "- invalid_paths: planner-task:workflow/orchestrator.py:allowed_path_not_declared",
            "- missing_tests: planner-task:missing_required_test_paths",
        ]
    )
    orchestrator._planner_invalid_paths = ["planner-task:workflow/orchestrator.py:allowed_path_not_declared"]
    orchestrator._planner_missing_tests = ["planner-task:missing_required_test_paths"]
    orchestrator._capture_planner_rejection_feedback()

    prompt = orchestrator._build_implementation_planner_retry_prompt({"name": "implementation-planner", "description": "Convert plan to backlog"})

    assert "Previous validation feedback to repair" in prompt
    assert "allowed_path_not_declared" in prompt
    assert "missing_required_test_paths" in prompt
    assert "Do not repeat invalid allowed_paths." in prompt
    assert "Every backend task must include required_test_paths." in prompt
    assert "Every allowed_path must be explicitly declared in existing_paths, new_files, or new_directories." in prompt
    assert "If a later task uses a file created by an earlier task, declare depends_on and place that reused file in existing_paths for the later task." in prompt
    assert "If adding gateway-v4/tests/__init__.py and it does not already exist, declare it in new_files and include it in allowed_paths." in prompt
    assert "Return only corrected YAML/JSON." in prompt
    assert orchestrator._planner_feedback_source.endswith("implementation-planner.md")
    assert orchestrator._planner_feedback_chars > 0


def test_retry_prompt_warns_when_feedback_file_missing(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    warnings: list[str] = []
    monkeypatch.setattr(orchestrator.logger, "warning", warnings.append)
    orchestrator._planner_feedback_file = str(engine_root / "missing-feedback.md")

    prompt = orchestrator._build_implementation_planner_retry_prompt({"name": "implementation-planner", "description": "Convert plan to backlog"})

    assert "Previous validation feedback to repair" in prompt
    assert "No previous validation feedback file was available for this retry." in prompt
    assert any("Planner feedback file is missing" in warning for warning in warnings)


def test_retry_injects_feedback_without_rerunning_architect_and_keeps_scope(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "tests").mkdir(parents=True, exist_ok=True)
    (target_workspace / "tests" / "test_workflow.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        task_scope="Scoped backend-only retry task.",
    )
    orchestrator.config["workflow"]["mode"] = "interactive"
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    answers = iter(["r"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    captured_retry_descriptions: list[str] = []
    calls: list[str] = []
    planner_calls = {"count": 0}

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        calls.append(agent_config["name"])
        if agent_config["name"] == "architect":
            orchestrator.logger.save_agent_report("implementation", "architect", {"status": "success", "result": "completed", "parsed_output": "Architect output"})
            return True
        if agent_config["name"] == "implementation-planner":
            planner_calls["count"] += 1
            if planner_calls["count"] >= 2:
                captured_retry_descriptions.append(str(agent_config.get("description") or ""))
            orchestrator.logger.save_agent_report(
                "implementation",
                "implementation-planner",
                {
                    "status": "success",
                    "result": "completed",
                    "parsed_output": json.dumps(
                        [
                            {
                                "id": "planner-task",
                                "title": "Planner task",
                                "priority": "P0",
                                "scope": "Improve backend validation.",
                                "existing_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"] if planner_calls["count"] >= 3 else [],
                                "allowed_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"] if planner_calls["count"] >= 3 else ["workflow/orchestrator.py"],
                                "new_files": [],
                                "forbidden_paths": [],
                                "required_test_paths": ["tests/test_workflow.py"] if planner_calls["count"] >= 3 else [],
                                "depends_on": [],
                                "acceptance_criteria": ["done"],
                                "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed.", "tests/test_workflow.py": "Needed."},
                                "target_file": {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Improve backend validation."} if planner_calls["count"] >= 3 else {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Incomplete contract"},
                                "test_file": {"path": "tests/test_workflow.py", "action": "update"} if planner_calls["count"] >= 3 else {"path": "", "action": ""},
                                "must_contain": ["def _validate_implementation_planner_output(", "return {"] if planner_calls["count"] >= 3 else [],
                                "must_import": ["from pathlib import Path"] if planner_calls["count"] >= 3 else [],
                                "integration": ["Validation must integrate with planner flow."] if planner_calls["count"] >= 3 else [],
                                "reference_files": ["workflow/orchestrator.py"] if planner_calls["count"] >= 3 else [],
                                "reference_excerpts": {"workflow/orchestrator.py": "pass"} if planner_calls["count"] >= 3 else {},
                                "must_test": ["test_validate_output_accepts_valid_paths: assert diagnostics valid"] if planner_calls["count"] >= 3 else [],
                                "forbidden": ["Do not edit frontend files."] if planner_calls["count"] >= 3 else [],
                                "risk_level": "low",
                                "estimated_effort": "S",
                            }
                        ]
                    ),
                },
            )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)

    ok = orchestrator._run_phase_agents({"agents": [{"name": "architect"}, {"name": "implementation-planner"}]}, "implementation")

    assert ok is True
    assert calls == ["architect", "implementation-planner", "implementation-planner", "implementation-planner"]
    assert captured_retry_descriptions
    retry_prompt = captured_retry_descriptions[0]
    assert "Previous validation feedback to repair" in retry_prompt
    assert "missing_required_test_paths" in retry_prompt
    assert "allowed_path_not_declared" in retry_prompt
    assert "Scoped backend-only retry task." not in retry_prompt
    assert orchestrator._select_implementation_scope([]) == "Scoped backend-only retry task."


def test_backend_task_without_tests_fails(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    file_path = target_workspace / "workflow" / "orchestrator.py"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("pass\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "missing-tests",
                        "title": "Backend file change",
                        "priority": "P0",
                        "scope": "Update backend implementation.",
                        "allowed_paths": ["workflow/orchestrator.py"],
                        "existing_paths": ["workflow/orchestrator.py"],
                        "new_files": [],
                        "forbidden_paths": [],
                        "required_test_paths": [],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert any("missing_required_test_paths" in item for item in diagnostics["invalid_paths"])


def test_forbidden_tests_conflict_fails(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    file_path = target_workspace / "workflow" / "orchestrator.py"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("pass\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "test-conflict",
                        "title": "Backend file change",
                        "priority": "P0",
                        "scope": "Update backend implementation.",
                        "allowed_paths": ["workflow/orchestrator.py"],
                        "existing_paths": ["workflow/orchestrator.py"],
                        "new_files": [],
                        "forbidden_paths": ["tests/*"],
                        "required_test_paths": ["tests/test_workflow.py"],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is False
    assert any("forbidden_test_path_conflict" in item for item in diagnostics["invalid_paths"])


def test_planner_repair_round_receives_invalid_paths_and_available_directories(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "tests").mkdir(parents=True, exist_ok=True)
    (target_workspace / "tests" / "test_workflow.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "bad-src-path",
                        "title": "Bad src path",
                        "priority": "P0",
                        "scope": "Invalid path",
                        "allowed_paths": ["src/main.py"],
                        "existing_paths": ["src/main.py"],
                        "new_files": [],
                        "forbidden_paths": [],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {"src/main.py": "Needed."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )
    repair_prompts: list[str] = []

    def fake_run_agent(agent_config, phase_key, index=None, total=None):
        description = str(agent_config.get("description") or "")
        repair_prompts.append(description)
        orchestrator.logger.save_agent_report(
            "implementation",
            "implementation-planner",
            {
                "status": "success",
                "result": "completed",
                "parsed_output": json.dumps(
                    [
                        {
                            "id": "good-existing-file",
                            "title": "Good existing file",
                            "priority": "P0",
                            "scope": "Valid path",
                            "allowed_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                            "existing_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                            "new_files": [],
                            "forbidden_paths": [],
                            "required_test_paths": ["tests/test_workflow.py"],
                            "depends_on": [],
                            "acceptance_criteria": ["n/a"],
                            "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed.", "tests/test_workflow.py": "Needed."},
                            "target_file": {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Workflow update."},
                            "test_file": {"path": "tests/test_workflow.py", "action": "update"},
                            "must_contain": ["def _validate_implementation_planner_output(", "return {"],
                            "must_import": ["from pathlib import Path"],
                            "integration": ["Validation must integrate with planner flow."],
                            "reference_files": ["workflow/orchestrator.py"],
                            "reference_excerpts": {"workflow/orchestrator.py": "pass"},
                            "must_test": ["test_validate_output_accepts_valid_paths: assert diagnostics valid"],
                            "forbidden": ["Do not edit frontend files."],
                            "risk_level": "low",
                            "estimated_effort": "S",
                        }
                    ]
                ),
            },
        )
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)

    ok = orchestrator._validate_or_repair_implementation_planner(
        {"name": "implementation-planner", "description": "Convert plan to backlog"},
        "implementation",
        index=2,
        total=5,
    )

    assert ok is True
    assert repair_prompts
    assert "src/main.py" in repair_prompts[0]
    assert "workflow" in repair_prompts[0]


def test_developer_write_validation_blocks_path_outside_target_workspace(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "docs").mkdir(parents=True, exist_ok=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator._selected_implementation_item = {
        "id": "docs-task",
        "title": "Docs task",
        "priority": "P1",
        "scope": "Docs only.",
        "existing_paths": [],
        "new_files": ["docs/new-plan.md"],
        "allowed_paths": ["docs/new-plan.md"],
        "forbidden_paths": [],
        "acceptance_criteria": ["Docs updated."],
        "reason_each_path_is_needed": {"docs/new-plan.md": "Needed."},
        "risk_level": "low",
        "estimated_effort": "S",
    }
    orchestrator._refresh_repo_map()

    allowed, detail, _candidate = orchestrator._validate_direct_api_write_request("../outside.py", ["x"])

    assert allowed is False
    assert "escapes target_workspace" in detail


def test_developer_write_validation_blocks_paths_outside_contract_target_and_test(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "docs").mkdir(parents=True, exist_ok=True)
    (target_workspace / "tests").mkdir(parents=True, exist_ok=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator._selected_implementation_item = {
        "id": "docs-task",
        "title": "Docs task",
        "priority": "P1",
        "scope": "Docs only.",
        "existing_paths": [],
        "new_files": ["docs/new-plan.md", "tests/test_plan.py"],
        "allowed_paths": ["docs/new-plan.md", "tests/test_plan.py"],
        "forbidden_paths": [],
        "target_file": {"path": "docs/new-plan.md", "action": "create", "purpose": "Add plan"},
        "test_file": {"path": "tests/test_plan.py"},
        "acceptance_criteria": ["Docs updated."],
        "reason_each_path_is_needed": {"docs/new-plan.md": "Needed."},
        "risk_level": "low",
        "estimated_effort": "S",
    }
    orchestrator._refresh_repo_map()

    allowed, detail, _candidate = orchestrator._validate_direct_api_write_request("gateway-v4/app/services/monitoring.py", ["x"])

    assert allowed is False
    assert "violates" in detail


def test_contract_compliance_detects_missing_test_file_and_missing_must_contain(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    target_file = target_workspace / "docs" / "new-plan.md"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("hello\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator._selected_implementation_item = {
        "id": "docs-task",
        "title": "Docs task",
        "priority": "P1",
        "scope": "Docs only.",
        "target_file": {"path": "docs/new-plan.md", "action": "modify", "purpose": "Add plan"},
        "test_file": {"path": "tests/test_plan.py"},
        "must_contain": ["MUST_INCLUDE"],
        "forbidden": ["DO_NOT_ADD"],
        "contract_completeness": True,
    }

    diagnostics = orchestrator._evaluate_selected_task_contract_compliance()

    assert diagnostics["contract_completeness"] is True
    assert diagnostics["contract_compliance"] is False
    assert diagnostics["missing_test_file"] is True
    assert diagnostics["missing_must_contain"] == ["MUST_INCLUDE"]


def test_planner_backend_task_contract_requires_executable_fields(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    target_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("def record_request():\n    pass\n", encoding="utf-8")
    test_file = target_workspace / "gateway-v4" / "tests" / "test_monitoring.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_record_request():\n    assert True\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "TASK-001",
                        "title": "Implement monitoring",
                        "priority": "P0",
                        "scope": "backend-only",
                        "existing_paths": ["gateway-v4/app/services/monitoring.py", "gateway-v4/tests/test_monitoring.py"],
                        "new_directories": [],
                        "new_files": [],
                        "allowed_paths": ["gateway-v4/app/services/monitoring.py", "gateway-v4/tests/test_monitoring.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["gateway-v4/tests/test_monitoring.py"],
                        "depends_on": [],
                        "acceptance_criteria": ["Monitoring works."],
                        "reason_each_path_is_needed": {
                            "gateway-v4/app/services/monitoring.py": "Implementation target.",
                            "gateway-v4/tests/test_monitoring.py": "Required regression test.",
                        },
                        "target_file": {"path": "gateway-v4/app/services/monitoring.py", "action": "update", "purpose": "Add monitoring behavior."},
                        "test_file": {"path": "gateway-v4/tests/test_monitoring.py", "action": "update"},
                        "must_contain": ["def record_request(", "response_time_ms: int"],
                        "must_import": ["from typing import Any"],
                        "integration": ["Provider calls must invoke record_request."],
                        "reference_files": ["gateway-v4/app/services/monitoring.py"],
                        "reference_excerpts": {"gateway-v4/app/services/monitoring.py": "def record_request():\n    pass"},
                        "must_test": ["test_record_request_writes_metric: assert metrics are persisted"],
                        "forbidden": ["Do not edit frontend files."],
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is True


def test_validator_rejects_vague_backend_contract(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    target_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("pass\n", encoding="utf-8")
    test_file = target_workspace / "gateway-v4" / "tests" / "test_monitoring.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_monitoring():\n    assert True\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    planner_item = {
        "id": "TASK-002",
        "title": "Implement monitoring vaguely",
        "priority": "P0",
        "scope": "backend-only",
        "existing_paths": ["gateway-v4/app/services/monitoring.py", "gateway-v4/tests/test_monitoring.py"],
        "new_directories": [],
        "new_files": [],
        "allowed_paths": ["gateway-v4/app/services/monitoring.py", "gateway-v4/tests/test_monitoring.py"],
        "forbidden_paths": [],
        "required_test_paths": ["gateway-v4/tests/test_monitoring.py"],
        "depends_on": [],
        "acceptance_criteria": ["Monitoring works."],
        "reason_each_path_is_needed": {
            "gateway-v4/app/services/monitoring.py": "Implementation target.",
            "gateway-v4/tests/test_monitoring.py": "Required regression test.",
        },
        "target_file": {"path": "gateway-v4/app/services/monitoring.py", "action": "update", "purpose": "Add monitoring behavior."},
        "reference_files": ["gateway-v4/app/services/monitoring.py"],
        "risk_level": "low",
        "estimated_effort": "S",
    }

    parsed = orchestrator._parse_task_designer_output(
        json.dumps(
            {
                "task_id": "TASK-002",
                "title": "Implement monitoring vaguely",
                "target_file": {"path": "gateway-v4/app/services/monitoring.py", "action": "update", "purpose": "Add monitoring behavior."},
                "test_file": {"path": "gateway-v4/tests/test_monitoring.py", "action": "update"},
                "depends_on": [],
                "must_contain": ["Implement monitoring functionality", "Handle provider calls"],
                "must_import": ["from typing import Any"],
                "integration": ["Connect monitoring into provider flow."],
                "reference_files": ["gateway-v4/app/services/monitoring.py"],
                "reference_excerpts": {"gateway-v4/app/services/monitoring.py": "pass"},
                "must_test": ["Test that monitoring works"],
                "forbidden": ["Do not edit frontend files."],
            }
        ),
        planner_item,
    )

    assert parsed["item"] is not None
    assert any("vague_must_contain" in item for item in parsed["errors"])
    assert any("vague_must_test" in item for item in parsed["errors"])


def test_repo_map_after_detects_newly_created_allowed_file(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "docs").mkdir(parents=True, exist_ok=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    assert orchestrator._refresh_repo_map(snapshot_path=orchestrator.repo_map_before_path) is True
    orchestrator._repo_map_before = orchestrator._load_repo_map()
    orchestrator._selected_implementation_item = {
        "id": "docs-task",
        "title": "Docs task",
        "priority": "P1",
        "scope": "Docs only.",
        "existing_paths": [],
        "new_files": ["docs/new-plan.md"],
        "allowed_paths": ["docs/new-plan.md"],
        "forbidden_paths": [],
        "acceptance_criteria": ["Docs updated."],
        "reason_each_path_is_needed": {"docs/new-plan.md": "Needed."},
        "risk_level": "low",
        "estimated_effort": "S",
    }
    (target_workspace / "docs" / "new-plan.md").write_text("plan\n", encoding="utf-8")

    orchestrator._capture_repo_map_after_developer()

    assert orchestrator.repo_map_after_path.exists()
    assert orchestrator._repo_map_delta["new_files_created"] == ["docs/new-plan.md"]


def test_implementation_phase_includes_task_designer_between_planner_and_developer() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    agent_names = [agent["name"] for agent in orchestrator.config["phases"]["implementation"]["agents"]]
    assert agent_names.index("implementation-planner") < agent_names.index("task-designer") < agent_names.index("developer")


def test_planner_validation_accepts_backlog_outline_without_full_contract(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    target_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("pass\n", encoding="utf-8")
    test_file = target_workspace / "gateway-v4" / "tests" / "test_monitoring.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_monitoring():\n    assert True\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator.logger.save_agent_report(
        "implementation",
        "implementation-planner",
        {
            "status": "success",
            "result": "completed",
            "parsed_output": json.dumps(
                [
                    {
                        "id": "TASK-001",
                        "title": "Update monitoring service",
                        "priority": "P0",
                        "scope": "backend-only",
                        "existing_paths": ["gateway-v4/app/services/monitoring.py", "gateway-v4/tests/test_monitoring.py"],
                        "new_directories": [],
                        "new_files": [],
                        "allowed_paths": ["gateway-v4/app/services/monitoring.py", "gateway-v4/tests/test_monitoring.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["gateway-v4/tests/test_monitoring.py"],
                        "acceptance_criteria": ["Monitoring path updated."],
                        "reason_each_path_is_needed": {
                            "gateway-v4/app/services/monitoring.py": "Implementation target.",
                            "gateway-v4/tests/test_monitoring.py": "Required regression test.",
                        },
                        "target_file": {"path": "gateway-v4/app/services/monitoring.py", "action": "update", "purpose": "Update monitoring behavior."},
                        "reference_files": ["gateway-v4/app/services/monitoring.py"],
                        "depends_on": [],
                        "risk_level": "low",
                        "estimated_effort": "S",
                    }
                ]
            ),
        },
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is True


def test_task_designer_contract_is_applied_to_selected_item(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    target_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("def record_request():\n    pass\n", encoding="utf-8")
    test_file = target_workspace / "gateway-v4" / "tests" / "test_monitoring.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_monitoring():\n    assert True\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target_workspace))
    orchestrator._selected_implementation_item = {
        "id": "TASK-001",
        "title": "Update monitoring service",
        "priority": "P0",
        "scope": "backend-only",
        "existing_paths": ["gateway-v4/app/services/monitoring.py", "gateway-v4/tests/test_monitoring.py"],
        "new_directories": [],
        "new_files": [],
        "allowed_paths": ["gateway-v4/app/services/monitoring.py", "gateway-v4/tests/test_monitoring.py"],
        "forbidden_paths": [],
        "required_test_paths": ["gateway-v4/tests/test_monitoring.py"],
        "acceptance_criteria": ["Monitoring path updated."],
        "reason_each_path_is_needed": {
            "gateway-v4/app/services/monitoring.py": "Implementation target.",
            "gateway-v4/tests/test_monitoring.py": "Required regression test.",
        },
        "target_file": {"path": "gateway-v4/app/services/monitoring.py", "action": "update", "purpose": "Update monitoring behavior."},
        "reference_files": ["gateway-v4/app/services/monitoring.py"],
        "depends_on": [],
        "risk_level": "low",
        "estimated_effort": "S",
    }
    report = {
        "status": "success",
        "result": "completed",
        "parsed_output": json.dumps(
            {
                "task_id": "TASK-001",
                "title": "Update monitoring service",
                "target_file": {"path": "gateway-v4/app/services/monitoring.py", "action": "update", "purpose": "Update monitoring behavior."},
                "test_file": {"path": "gateway-v4/tests/test_monitoring.py", "action": "update"},
                "depends_on": [],
                "must_contain": ["def record_request(", "response_time_ms = response_time_ms"],
                "must_import": ["from typing import Any"],
                "integration": ["Connect monitoring updates to the existing provider flow."],
                "reference_files": ["gateway-v4/app/services/monitoring.py"],
                "reference_excerpts": {"gateway-v4/app/services/monitoring.py": "def record_request():\n    pass"},
                "must_test": ["test_record_request_updates_metrics: call record_request and assert the metric state changes"],
                "forbidden": ["Do not edit frontend files."],
            }
        ),
    }

    ok = orchestrator._apply_task_designer_contract_from_report(report)

    assert ok is True
    assert orchestrator._selected_implementation_item is not None
    assert orchestrator._selected_implementation_item["contract_source"] == "task-designer"
    assert orchestrator._selected_implementation_item["target_file"]["path"] == "gateway-v4/app/services/monitoring.py"
    assert orchestrator._selected_implementation_item["test_file"]["path"] == "gateway-v4/tests/test_monitoring.py"


def test_task_designer_contract_preserves_overlong_must_contain(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "gateway-v4" / "app").mkdir(parents=True, exist_ok=True)
    (target_workspace / "gateway-v4" / "app" / "database.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "gateway-v4" / "app" / "models.py").write_text("pass\n", encoding="utf-8")
    (target_workspace / "gateway-v4" / "alembic" / "versions").mkdir(parents=True, exist_ok=True)
    (target_workspace / "gateway-v4" / "tests").mkdir(parents=True, exist_ok=True)
    (target_workspace / "gateway-v4" / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (target_workspace / "gateway-v4" / "tests" / "test_provider_metrics_migration.py").write_text("pass\n", encoding="utf-8")
    config_path = engine_root / "workflow" / "config.yaml"
    _write_minimal_workflow_config(config_path)
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator._selected_implementation_item = {
        "id": "TASK-001",
        "title": "Add database migration for provider performance metrics",
        "scope": "backend-only",
        "existing_paths": ["gateway-v4/app/database.py", "gateway-v4/app/models.py"],
        "new_directories": ["gateway-v4/tests"],
        "new_files": [
            "gateway-v4/alembic/versions/20240801_add_provider_metrics.py",
            "gateway-v4/tests/__init__.py",
            "gateway-v4/tests/test_provider_metrics_migration.py",
        ],
        "allowed_paths": [
            "gateway-v4/app/database.py",
            "gateway-v4/app/models.py",
            "gateway-v4/alembic/versions/20240801_add_provider_metrics.py",
            "gateway-v4/tests/__init__.py",
            "gateway-v4/tests/test_provider_metrics_migration.py",
        ],
        "forbidden_paths": [],
        "required_test_paths": ["gateway-v4/tests/test_provider_metrics_migration.py"],
        "depends_on": [],
        "acceptance_criteria": ["done"],
        "reason_each_path_is_needed": {
            "gateway-v4/app/database.py": "DB config",
            "gateway-v4/app/models.py": "Model reference",
            "gateway-v4/alembic/versions/20240801_add_provider_metrics.py": "Migration file",
            "gateway-v4/tests/__init__.py": "Package marker",
            "gateway-v4/tests/test_provider_metrics_migration.py": "Migration tests",
        },
        "_target_file_declared": True,
        "_test_file_declared": True,
        "_must_contain_declared": True,
        "_must_test_declared": True,
        "_depends_on_declared": True,
        "target_file": {"path": "gateway-v4/alembic/versions/20240801_add_provider_metrics.py", "action": "create", "purpose": "Create migration."},
        "must_contain": [],
        "must_import": [],
        "integration": [],
        "reference_files": ["gateway-v4/app/database.py", "gateway-v4/app/models.py"],
        "reference_excerpts": {},
        "test_file": {"path": "gateway-v4/tests/test_provider_metrics_migration.py", "action": "create"},
        "must_test": [],
        "forbidden": [],
        "contract_completeness": False,
        "risk_level": "low",
        "estimated_effort": "S",
    }
    repo_map = {
        "target_workspace": str(target_workspace),
        "top_level_tree": ["gateway-v4/"],
        "directories": ["", "gateway-v4", "gateway-v4/app", "gateway-v4/alembic", "gateway-v4/alembic/versions", "gateway-v4/tests"],
        "files": [
            {"path": "gateway-v4/app/database.py"},
            {"path": "gateway-v4/app/models.py"},
            {"path": "gateway-v4/tests/__init__.py"},
            {"path": "gateway-v4/tests/test_provider_metrics_migration.py"},
        ],
    }
    orchestrator.repo_map_path.parent.mkdir(parents=True, exist_ok=True)
    orchestrator.repo_map_path.write_text(json.dumps(repo_map), encoding="utf-8")
    orchestrator._repo_map_cache = repo_map
    report = {
        "selected_task_contract": {
            **orchestrator._selected_implementation_item,
            "contract_source": "task-designer",
            "must_contain": [
                "def upgrade():",
                "op.create_table(",
                "sa.Column(",
                "op.create_index(",
                "def downgrade():",
                "op.drop_table(",
            ],
            "must_import": ["from alembic import op", "import sqlalchemy as sa"],
            "integration": ["Migration follows Alembic conventions."],
            "must_test": [
                "test_upgrade_creates_provider_metrics_table: assert table exists",
                "test_downgrade_removes_provider_metrics_table: assert table removed",
                "test_provider_metrics_indexes: assert indexes exist",
            ],
            "forbidden": ["Do not modify billing routes."],
        }
    }

    ok = orchestrator._apply_task_designer_contract_from_report(report)

    assert ok is True
    assert orchestrator._selected_implementation_item["contract_source"] == "task-designer"
    assert len(orchestrator._selected_implementation_item["must_contain"]) == 6
