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
    research_agents = {
        agent["name"]: agent
        for agent in orchestrator.config["phases"]["research"]["agents"]
    }

    project_runtime = orchestrator._resolve_agent_runtime(
        research_agents["project-analyst"]
    )
    product_runtime = orchestrator._resolve_agent_runtime(
        research_agents["product-manager"]
    )

    assert project_runtime["provider"] == "openrouter"
    assert project_runtime["model"] == research_agents["project-analyst"]["model"]
    assert project_runtime["thinking"] == "low"
    assert product_runtime["provider"] == "openrouter"
    assert product_runtime["model"] == research_agents["product-manager"]["model"]
    assert product_runtime["thinking"] == "low"


def test_research_agents_use_expected_models() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    research_agents = {
        agent["name"]: agent["model"]
        for agent in orchestrator.config["phases"]["research"]["agents"]
    }

    assert research_agents["project-analyst"] == "openrouter/deepseek/deepseek-v4-pro"
    assert research_agents["competitor-analyst"] == "perplexity/sonar"
    assert research_agents["market-analyst"] == "openrouter/deepseek/deepseek-v4-pro"
    assert research_agents["tech-analyst"] == "openrouter/deepseek/deepseek-v4-pro"
    assert research_agents["innovation-scout"] == "perplexity/sonar"
    assert research_agents["product-manager"] == "openrouter/deepseek/deepseek-v4-pro"


def test_implementation_and_deployment_agents_use_expected_models() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    implementation_agents = {
        agent["name"]: agent["model"]
        for agent in orchestrator.config["phases"]["implementation"]["agents"]
    }
    deployment_agents = {
        agent["name"]: agent["model"]
        for agent in orchestrator.config["phases"]["deployment"]["agents"]
    }

    assert implementation_agents["architect"] == "openrouter/anthropic/claude-sonnet-4.5"
    assert implementation_agents["implementation-planner"] == "openrouter/anthropic/claude-sonnet-4.5"
    assert implementation_agents["developer"] == "openrouter/deepseek/deepseek-chat-v4"
    assert implementation_agents["qa"] == "openrouter/deepseek/deepseek-v4-pro"
    assert implementation_agents["template-validator"] == "openrouter/deepseek/deepseek-v4-pro"
    assert deployment_agents["production-readiness-checker"] == "openrouter/deepseek/deepseek-v4-pro"
    assert deployment_agents["launch-strategist"] == "openrouter/deepseek/deepseek-v4-pro"


def test_config_uses_deepseek_v4_pro_for_selected_agents() -> None:
    content = Path("workflow/config.yaml").read_text(encoding="utf-8")
    assert "openrouter/deepseek/deepseek-v4-pro" in content
