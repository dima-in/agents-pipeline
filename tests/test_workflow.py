import json
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


def test_agent_directories_exist() -> None:
    manager = AgentManager()
    agents = manager.list_agents()
    assert "research" in agents
    assert "implementation" in agents
    assert "competitor-analyst" in agents["research"]
    assert "architect" in agents["implementation"]
    assert "implementation-planner" in agents["implementation"]


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
                                "allowed_paths": ["gateway-v4/app/services/monitoring.py"],
                                "forbidden_paths": [],
                                "required_test_paths": ["tests/test_monitoring.py"],
                                "acceptance_criteria": ["Backend updated."],
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
                                "allowed_paths": ["gateway-v4/app/services/marketplace.py"],
                                "forbidden_paths": [],
                                "required_test_paths": ["tests/test_marketplace.py"],
                                "acceptance_criteria": ["Marketplace code updated."],
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
                        "allowed_paths": ["workflow/orchestrator.py"],
                        "existing_paths": ["workflow/orchestrator.py"],
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

    assert diagnostics["valid"] is True
    assert diagnostics["task_count"] == 1


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
                        "allowed_paths": ["src/main.py"],
                        "existing_paths": [],
                        "new_directories": ["src"],
                        "new_files": ["src/main.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_main.py"],
                        "acceptance_criteria": ["n/a"],
                        "reason_each_path_is_needed": {"src/main.py": "Needed."},
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


def test_later_task_may_use_file_created_earlier(tmp_path: Path) -> None:
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
                        "title": "Create run state",
                        "priority": "P0",
                        "scope": "Add run state persistence.",
                        "existing_paths": [],
                        "new_files": ["workflow/run_state.py"],
                        "allowed_paths": ["workflow/run_state.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_run_state.py"],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {"workflow/run_state.py": "Create runtime state module."},
                        "risk_level": "low",
                        "estimated_effort": "S",
                    },
                    {
                        "id": "TASK-002",
                        "title": "Use run state",
                        "priority": "P1",
                        "scope": "Use the new run state module from the orchestrator.",
                        "depends_on": ["TASK-001"],
                        "existing_paths": ["workflow/run_state.py", "workflow/orchestrator.py"],
                        "new_files": [],
                        "allowed_paths": ["workflow/run_state.py", "workflow/orchestrator.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_workflow.py"],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {
                            "workflow/run_state.py": "Reuse the created module.",
                            "workflow/orchestrator.py": "Wire persistence into orchestration.",
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
                        "new_directories": ["workflow/state", "workflow/state/runtime"],
                        "new_files": ["workflow/state/runtime/store.py"],
                        "allowed_paths": ["workflow/orchestrator.py", "workflow/state/runtime/store.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_store.py"],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {
                            "workflow/orchestrator.py": "Wire the feature.",
                            "workflow/state/runtime/store.py": "Implement storage.",
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
                        "new_files": ["workflow/run_state.py"],
                        "allowed_paths": ["workflow/run_state.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_run_state.py"],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {"workflow/run_state.py": "Create runtime state module."},
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
                        "new_files": ["tools/status_cmd.py"],
                        "allowed_paths": ["workflow/run_state.py", "workflow/orchestrator.py", "tools/status_cmd.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_status_cmd.py"],
                        "acceptance_criteria": ["done"],
                        "reason_each_path_is_needed": {
                            "workflow/run_state.py": "Reuse created state module.",
                            "workflow/orchestrator.py": "Expose status plumbing.",
                            "tools/status_cmd.py": "Implement status command.",
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
                            "existing_paths": ["workflow/orchestrator.py"] if planner_calls["count"] >= 3 else [],
                            "allowed_paths": ["workflow/orchestrator.py"] if planner_calls["count"] >= 3 else ["services/foo.py"],
                            "new_files": [],
                            "forbidden_paths": [],
                            "required_test_paths": ["tests/test_workflow.py"] if planner_calls["count"] >= 3 else [],
                            "acceptance_criteria": ["done"],
                            "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed."} if planner_calls["count"] >= 3 else {"services/foo.py": "Needed."},
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
        return True

    monkeypatch.setattr(orchestrator, "_run_agent", fake_run_agent)
    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)

    ok = orchestrator._run_implementation_phase()

    assert ok is True
    assert calls == ["implementation-planner"]
    assert orchestrator._reused_architect_output is True
    assert orchestrator._architect_output_source.endswith("architect.json")


def test_retry_prompt_includes_previous_rejection_reason(tmp_path: Path, monkeypatch) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (target_workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (target_workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
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
                                "existing_paths": ["workflow/orchestrator.py"] if planner_calls["count"] >= 3 else [],
                                "allowed_paths": ["workflow/orchestrator.py"] if planner_calls["count"] >= 3 else ["services/foo.py"],
                                "forbidden_paths": [],
                                "required_test_paths": ["tests/test_workflow.py"] if planner_calls["count"] >= 3 else [],
                                "acceptance_criteria": ["done"],
                                "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed."} if planner_calls["count"] >= 3 else {"services/foo.py": "Needed."},
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
                                "existing_paths": ["workflow/orchestrator.py"] if planner_calls["count"] >= 3 else [],
                                "allowed_paths": ["workflow/orchestrator.py"] if planner_calls["count"] >= 3 else ["services/foo.py"],
                                "forbidden_paths": [],
                                "required_test_paths": ["tests/test_workflow.py"] if planner_calls["count"] >= 3 else [],
                                "acceptance_criteria": ["done"],
                                "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed."} if planner_calls["count"] >= 3 else {"services/foo.py": "Needed."},
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
                            "allowed_paths": ["workflow/orchestrator.py"],
                            "existing_paths": ["workflow/orchestrator.py"],
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
