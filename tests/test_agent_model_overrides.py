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
    assert overrides["model"] == "openrouter/anthropic/claude-sonnet-5"


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

    assert research_agents["project-analyst"] == "openrouter/anthropic/claude-sonnet-5"
    assert research_agents["competitor-analyst"] == "openrouter/anthropic/claude-sonnet-5"
    assert research_agents["market-analyst"] == "openrouter/anthropic/claude-sonnet-5"
    assert research_agents["tech-analyst"] == "openrouter/anthropic/claude-sonnet-5"
    assert research_agents["innovation-scout"] == "openrouter/anthropic/claude-sonnet-5"
    assert research_agents["product-manager"] == "openrouter/anthropic/claude-sonnet-5"


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

    assert implementation_agents["architect"] == "openrouter/anthropic/claude-sonnet-5"
    assert implementation_agents["implementation-planner"] == "openrouter/anthropic/claude-sonnet-5"
    assert implementation_agents["task-designer"] == "openrouter/anthropic/claude-sonnet-5"
    # Attempt-1 code producer runs on the cheap-but-capable tier; the retry loop escalates it.
    assert implementation_agents["developer"] == "openrouter/openai/gpt-5.6-luna"
    assert implementation_agents["qa"] == "openrouter/openai/gpt-5.6-sol"
    # Tertiary structural reviewer runs on the cheap tier (its vetoes are arbiter-overridable).
    assert implementation_agents["template-validator"] == "openrouter/openai/gpt-5.6-luna"
    assert deployment_agents["production-readiness-checker"] == "openrouter/deepseek/deepseek-v4-pro"
    assert deployment_agents["launch-strategist"] == "openrouter/deepseek/deepseek-v4-pro"

    # Multi-developer edit agents: cheap code/infra producers, strong FIXED test author.
    multi = {
        agent["name"]: agent["model"]
        for agent in orchestrator.config["phases"]["implementation"]["multi_developer_agents"]
    }
    assert multi["code-developer"] == "openrouter/openai/gpt-5.6-luna"
    assert multi["infra-developer"] == "openrouter/openai/gpt-5.6-luna"
    assert multi["test-developer"] == "openrouter/anthropic/claude-sonnet-5"

    # Escalation ladder: cheap -> sonnet-5 -> opus-5, and test-developer is NOT escalated (anti-gaming).
    esc = orchestrator.config["workflow"]["developer_model_escalation"]
    assert esc["enabled"] is True
    assert "test-developer" not in esc["agents"]
    tiers = {t["min_attempt"]: t["model"] for t in esc["tiers"]}
    assert tiers[2] == "openrouter/anthropic/claude-sonnet-5"
    assert tiers[3] == "openrouter/anthropic/claude-opus-5"


def test_config_uses_deepseek_v4_pro_for_selected_agents() -> None:
    content = Path("workflow/config.yaml").read_text(encoding="utf-8")
    assert "openrouter/deepseek/deepseek-v4-pro" in content
