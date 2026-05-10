from pathlib import Path

from manage_agents import AgentManager
from workflow.orchestrator import WorkflowOrchestrator


def test_orchestrator_resolves_agent_runtime_override() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    runtime = orchestrator._resolve_agent_runtime(
        {
            "name": "competitor-analyst",
            "provider": "openrouter",
            "model": "perplexity/sonar",
            "thinking": "low",
        }
    )
    assert runtime["provider"] == "openrouter"
    assert runtime["model"] == "perplexity/sonar"
    assert runtime["thinking"] == "low"


def test_manager_reads_agent_registration_override() -> None:
    manager = AgentManager()
    overrides = manager._get_agent_registration_overrides("competitor-analyst", "research")
    assert overrides["provider"] == "openrouter"
    assert overrides["model"] == "perplexity/sonar"


def test_research_agent_routing_overrides_are_configured() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    project_runtime = orchestrator._resolve_agent_runtime(
        {"name": "project-analyst", "provider": "openrouter", "model": "openrouter/deepseek/deepseek-chat-v3", "thinking": "low"}
    )
    product_runtime = orchestrator._resolve_agent_runtime(
        {"name": "product-manager", "provider": "openrouter", "model": "openrouter/deepseek/deepseek-chat-v3", "thinking": "low"}
    )

    assert project_runtime["provider"] == "openrouter"
    assert project_runtime["model"] == "openrouter/deepseek/deepseek-chat-v3"
    assert project_runtime["thinking"] == "low"
    assert product_runtime["provider"] == "openrouter"
    assert product_runtime["model"] == "openrouter/deepseek/deepseek-chat-v3"
    assert product_runtime["thinking"] == "low"


def test_research_agents_use_expected_models() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    research_agents = {
        agent["name"]: agent["model"]
        for agent in orchestrator.config["phases"]["research"]["agents"]
    }

    assert research_agents["competitor-analyst"] == "perplexity/sonar"
    assert research_agents["innovation-scout"] == "perplexity/sonar"

    for name, model in research_agents.items():
        if name in {"competitor-analyst", "innovation-scout"}:
            continue
        assert model == "openrouter/deepseek/deepseek-chat-v3"


def test_no_active_agent_config_uses_deepseek_v4_pro() -> None:
    targets = [
        Path("workflow/config.yaml"),
        Path(".openclaw/config/settings.yaml"),
        Path(".openclaw/config/agents.yaml"),
        Path("manage_agents.py"),
    ]
    for target in targets:
        content = target.read_text(encoding="utf-8")
        assert "deepseek/deepseek-v4-pro" not in content
        assert "deepseek-v4-pro" not in content
