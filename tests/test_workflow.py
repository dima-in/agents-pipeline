import json
from pathlib import Path
from types import SimpleNamespace

import git
import manage_agents as manage_agents_module
import yaml
from manage_agents import AgentManager
from monitor_logs import LogMonitor
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
