from pathlib import Path

from manage_agents import AgentManager
from monitor_logs import LogMonitor
from workflow.orchestrator import WorkflowOrchestrator


def test_config_loads() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    assert "phases" in orchestrator.config
    assert "research" in orchestrator.config["phases"]
    assert orchestrator.config["project"]["name"] == "agents-pipeline"


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
