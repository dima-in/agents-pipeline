from workflow.orchestrator import WorkflowOrchestrator


def test_detect_agent_failure_recognizes_llm_request_timeout() -> None:
    reason = WorkflowOrchestrator._detect_agent_failure("", "LLM request timed out.", "")
    assert reason == "llm request timeout"


def test_detect_agent_failure_recognizes_idle_timeout() -> None:
    reason = WorkflowOrchestrator._detect_agent_failure(
        "",
        "The model did not produce a response before the LLM idle timeout.",
        "",
    )
    assert reason == "llm idle timeout"
