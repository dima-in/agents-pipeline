from pathlib import Path
from types import SimpleNamespace

import manage_agents as manage_agents_module
from manage_agents import AgentManager
from monitor_logs import LogMonitor
from workflow.orchestrator import WorkflowOrchestrator


def test_config_loads() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    assert "phases" in orchestrator.config
    assert "research" in orchestrator.config["phases"]
    assert orchestrator.config["project"]["name"] == "agents-pipeline"
    assert orchestrator.config["workflow"]["executor"] == "direct_api"
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
        "openrouter/deepseek/deepseek-chat-v3",
    ]
    assert calls[1][1]["OPENCLAW_PROVIDER"] == "openrouter"
    assert calls[1][1]["OPENCLAW_MODEL"] == "openrouter/deepseek/deepseek-chat-v3"


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
    monkeypatch.setattr(
        manager,
        "_run_command_capture",
        lambda cmd, env=None: SimpleNamespace(
            returncode=0,
            stdout='[{"id":"project-analyst","model":"openrouter/deepseek/deepseek-chat-v3"}]',
            stderr="",
        ),
    )

    calls: list[list[str]] = []
    monkeypatch.setattr(manager, "_run_command", lambda cmd, env=None: calls.append(cmd) or 0)

    exit_code = manager.register_agent("project-analyst", "research")

    assert exit_code == 0
    assert calls == []
