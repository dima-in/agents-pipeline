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


def test_detect_agent_failure_requires_russian_translation_section() -> None:
    reason = WorkflowOrchestrator._detect_agent_failure(
        "",
        "",
        "English section only without the required translated block.",
    )
    assert reason == "missing russian translation section"


def test_classify_failure_status_timeout() -> None:
    status = WorkflowOrchestrator._classify_failure_status("llm idle timeout")
    assert status == "timeout"


def test_classify_failure_status_invalid_output() -> None:
    status = WorkflowOrchestrator._classify_failure_status("missing russian translation section")
    assert status == "invalid_output"


def test_run_phase_agents_continues_after_failure_when_fail_fast_false(monkeypatch) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    outcomes = iter([False, True])

    monkeypatch.setattr(orchestrator, "_wait_for_user", lambda _prompt: True)
    monkeypatch.setattr(orchestrator, "_run_agent", lambda *_args, **_kwargs: next(outcomes))

    phase = {
        "agents": [
            {"name": "first"},
            {"name": "second"},
        ],
        "fail_fast": False,
    }

    ok = orchestrator._run_phase_agents(phase, "research")
    assert ok is False
