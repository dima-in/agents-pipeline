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
