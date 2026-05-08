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
        {"name": "project-analyst", "provider": "openrouter", "model": "google/gemini-2.5-flash", "thinking": "low"}
    )
    product_runtime = orchestrator._resolve_agent_runtime(
        {"name": "product-manager", "provider": "anthropic", "model": "anthropic/claude-sonnet-4-6", "thinking": "low"}
    )

    assert project_runtime["provider"] == "openrouter"
    assert project_runtime["model"] == "google/gemini-2.5-flash"
    assert project_runtime["thinking"] == "low"
    assert product_runtime["provider"] == "anthropic"
    assert product_runtime["model"] == "anthropic/claude-sonnet-4-6"
    assert product_runtime["thinking"] == "low"
