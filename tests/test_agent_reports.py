import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import workflow.orchestrator as orchestrator_module
from workflow.logger import WorkflowLogger
from workflow.orchestrator import WorkflowOrchestrator


class _FakePopen:
    def __init__(self, *_args, **_kwargs) -> None:
        payload = {
            "output_text": (
                "English summary.\n\n"
                "Russian translation\n"
                "Русское резюме."
            ),
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 500,
                "total_tokens": 1500,
            },
        }
        self.stdout = io.StringIO(json.dumps(payload))
        self.stderr = io.StringIO("")
        self.returncode = 0

    def poll(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


class _FakePopenNoUsage:
    def __init__(self, *_args, **_kwargs) -> None:
        payload = {
            "output_text": (
                "English summary.\n\n"
                "Russian translation\n"
                "Русское резюме."
            )
        }
        self.stdout = io.StringIO(json.dumps(payload))
        self.stderr = io.StringIO("")
        self.returncode = 0

    def poll(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def _fake_agents_list_payload() -> str:
    return json.dumps(
        [
            {"id": "project-analyst", "model": "anthropic/claude-sonnet-4-6"},
            {"id": "competitor-analyst"},
        ]
    )


def _fake_models_list(*_args, **_kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        returncode=0,
        stdout=(
            "claude-sonnet-4-5-20250929\n"
            "anthropic/claude-sonnet-4-6\n"
            "openrouter/anthropic/claude-sonnet-4.6\n"
            "google/gemini-2.5-flash\n"
            "perplexity/sonar\n"
        ),
        stderr="",
    )


def _fake_run_with_agents_cache(args, **_kwargs) -> SimpleNamespace:
    if args[:4] == ["openclaw", "agents", "list", "--json"]:
        return SimpleNamespace(returncode=0, stdout=_fake_agents_list_payload(), stderr="")
    if args[:3] == ["openclaw", "agent", "--help"]:
        return SimpleNamespace(
            returncode=0,
            stdout="Usage: openclaw agent [options]\n  --thinking <level>\n  --timeout <seconds>\n  --json\n",
            stderr="",
        )
    if args[:3] == ["openclaw", "models", "list"]:
        return _fake_models_list()
    raise AssertionError(f"unexpected subprocess.run args: {args}")


def test_successful_agent_run_creates_report_files(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(subprocess, "run", _fake_run_with_agents_cache)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    ok = orchestrator._run_agent(
        {
            "name": "competitor-analyst",
            "description": "Analyze the current repository, active constraints, and the most important next questions",
            "timeout": 5,
            "provider": "openrouter",
            "model": "perplexity/sonar",
            "thinking": "low",
        },
        "research",
    )

    assert ok is True

    report_dir = orchestrator.logger.run_dir / "agents" / "research"
    md_path = report_dir / "competitor-analyst.md"
    json_path = report_dir / "competitor-analyst.json"

    assert md_path.exists()
    assert json_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["agent_name"] == "competitor-analyst"
    assert payload["phase"] == "research"
    assert payload["status"] == "success"
    assert payload["result"] == "completed"
    assert payload["returncode"] == 0
    assert payload["timestamp"]
    assert payload["message"]
    assert payload["stdout"]
    assert "Russian translation" in payload["parsed_output"]
    assert payload["usage"]["input_tokens"] == 1000
    assert payload["usage"]["output_tokens"] == 500
    assert payload["usage"]["total_tokens"] == 1500
    assert payload["usage"]["estimated_cost_usd"] == 0.0015
    assert payload["usage"]["usage_status"] == "captured"

    report_text = md_path.read_text(encoding="utf-8")
    assert "- agent_name: competitor-analyst" in report_text
    assert "## Prompt Message" in report_text
    assert "## Stdout" in report_text
    assert "## Usage" in report_text


def test_first_agent_message_has_no_previous_context(tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    _cmd, message, _stats = orchestrator._build_agent_command(
        "openclaw",
        "project-analyst",
        {"name": "project-analyst", "description": "Analyze repo"},
        Path(".openclaw/agents/research/project-analyst/prompt.md"),
        5,
        "research",
    )

    assert "Previous agent context:" not in message


def test_second_agent_message_includes_previous_agent_output(tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    orchestrator.logger.save_agent_report(
        "research",
        "project-analyst",
        {
            "status": "success",
            "result": "completed",
            "elapsed_s": 12.3,
            "returncode": 0,
            "message": "prompt",
            "stdout": "stdout text",
            "stderr": "",
            "parsed_output": "English summary.\n\nRussian translation\nРусский перевод.",
            "runtime": {"provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6", "thinking": "low"},
        },
    )

    _cmd, message, _stats = orchestrator._build_agent_command(
        "openclaw",
        "competitor-analyst",
        {"name": "competitor-analyst", "description": "Analyze competitors"},
        Path(".openclaw/agents/research/competitor-analyst/prompt.md"),
        5,
        "research",
    )

    assert "Previous agent context:" in message
    assert "[project-analyst]" in message
    assert "English summary." in message


def test_previous_context_is_limited_to_4000_characters(tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    long_text = "A" * 5000
    orchestrator.logger.save_agent_report(
        "research",
        "project-analyst",
        {
            "status": "success",
            "result": "completed",
            "elapsed_s": 12.3,
            "returncode": 0,
            "message": "prompt",
            "stdout": "",
            "stderr": "",
            "parsed_output": long_text,
            "runtime": {"provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6", "thinking": "low"},
        },
    )

    context = orchestrator._build_previous_agent_context("research", "competitor-analyst")

    assert "[project-analyst]" in context
    assert len(context) <= 4000


def test_failed_agents_are_excluded_from_previous_context_and_phase_summary_json(tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    orchestrator.logger.save_agent_report(
        "research",
        "project-analyst",
        {
            "status": "success",
            "result": "completed",
            "elapsed_s": 10,
            "returncode": 0,
            "message": "prompt",
            "stdout": "",
            "stderr": "",
            "parsed_output": "English summary.\n\nRussian translation\nРусский перевод.",
            "runtime": {"provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6", "thinking": "low"},
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "estimated_cost_usd": 0.00105, "usage_status": "captured"},
        },
    )
    orchestrator.logger.save_agent_report(
        "research",
        "market-analyst",
        {
            "status": "invalid_output",
            "result": "missing russian translation section",
            "elapsed_s": 20,
            "returncode": 0,
            "message": "prompt",
            "stdout": "bad output",
            "stderr": "",
            "parsed_output": "English only.",
            "runtime": {"provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6", "thinking": "low"},
            "usage": {"input_tokens": None, "output_tokens": None, "total_tokens": None, "estimated_cost_usd": None, "usage_status": "unavailable"},
        },
    )

    context = orchestrator._build_previous_agent_context("research", "competitor-analyst")
    assert "[project-analyst]" in context
    assert "[market-analyst]" not in context

    orchestrator.logger.save_phase_summary("research", "Research")
    summary_payload = json.loads((orchestrator.logger.run_dir / "phase_summary.json").read_text(encoding="utf-8"))
    assert summary_payload["phase"] == "research"
    assert summary_payload["completed_agents"] == 1
    assert summary_payload["failed_agents"] == 1
    assert summary_payload["total_elapsed_s"] == 30.0
    assert summary_payload["input_tokens"] == 100
    assert summary_payload["output_tokens"] == 50
    assert summary_payload["total_tokens"] == 150
    assert summary_payload["estimated_cost_usd"] == 0.00105


def test_invalid_model_blocks_agent_launch(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    effective_runtime = orchestrator._resolve_agent_runtime(
        {"name": "competitor-analyst", "provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"}
    )

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="google/gemini-2.5-flash\n", stderr=""),
    )

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("agent launch must not happen for an unavailable model")

    monkeypatch.setattr(subprocess, "Popen", fail_if_called)

    ok = orchestrator._run_agent(
        {
            "name": "competitor-analyst",
            "description": "Analyze repo",
            "timeout": 5,
            "provider": "openrouter",
            "model": "perplexity/sonar",
            "thinking": "low",
        },
        "research",
    )

    assert ok is False
    payload = json.loads(
        (orchestrator.logger.run_dir / "agents" / "research" / "competitor-analyst.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "failed"
    assert payload["result"].startswith("Configured model is not available:")
    assert effective_runtime["model"] in payload["result"]


def test_override_applied_when_cli_supports_it(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(subprocess, "run", _fake_run_with_agents_cache)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    monkeypatch.setattr(
        orchestrator,
        "_get_agent_cli_capabilities",
        lambda _runner: {"supports_model_override": True, "supports_provider_override": True},
    )

    ok = orchestrator._run_agent(
        {
            "name": "project-analyst",
            "description": "Analyze repo",
            "timeout": 5,
            "provider": "openrouter",
            "model": "google/gemini-2.5-flash",
            "thinking": "low",
        },
        "research",
    )

    assert ok is True
    payload = json.loads(
        (orchestrator.logger.run_dir / "agents" / "research" / "project-analyst.json").read_text(encoding="utf-8")
    )
    assert "--provider openrouter" in payload["command"]
    assert "--model google/gemini-2.5-flash" in payload["command"]


def test_mismatch_blocks_launch_when_override_cannot_be_applied(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(subprocess, "run", _fake_run_with_agents_cache)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("agent launch must not happen when override cannot be applied")

    monkeypatch.setattr(subprocess, "Popen", fail_if_called)
    monkeypatch.setattr(
        orchestrator,
        "_get_agent_cli_capabilities",
        lambda _runner: {"supports_model_override": False, "supports_provider_override": False},
    )

    ok = orchestrator._run_agent(
        {
            "name": "project-analyst",
            "description": "Analyze repo",
            "timeout": 5,
            "provider": "openrouter",
            "model": "google/gemini-2.5-flash",
            "thinking": "low",
        },
        "research",
    )

    assert ok is False
    payload = json.loads(
        (orchestrator.logger.run_dir / "agents" / "research" / "project-analyst.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "failed"
    assert payload["result"] == "Runtime override differs from registered agent model and cannot be applied"


def test_valid_model_allows_agent_launch(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(subprocess, "run", _fake_run_with_agents_cache)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    ok = orchestrator._run_agent(
        {
            "name": "competitor-analyst",
            "description": "Analyze repo",
            "timeout": 5,
            "provider": "openrouter",
            "model": "perplexity/sonar",
            "thinking": "low",
        },
        "research",
    )

    assert ok is True


def test_usage_status_is_unavailable_when_token_data_is_missing(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(subprocess, "run", _fake_run_with_agents_cache)
    monkeypatch.setattr(subprocess, "Popen", _FakePopenNoUsage)

    ok = orchestrator._run_agent(
        {
            "name": "project-analyst",
            "description": "Analyze repo",
            "timeout": 5,
        },
        "research",
    )

    assert ok is True
    payload = json.loads(
        (orchestrator.logger.run_dir / "agents" / "research" / "project-analyst.json").read_text(encoding="utf-8")
    )
    assert payload["usage"]["usage_status"] == "unavailable"
    assert payload["usage"]["estimated_cost_usd"] is None


def test_run_summary_totals_are_calculated_correctly(tmp_path: Path) -> None:
    logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    logger.save_agent_report(
        "research",
        "project-analyst",
        {
            "status": "success",
            "result": "completed",
            "elapsed_s": 10,
            "returncode": 0,
            "message": "prompt",
            "stdout": "",
            "stderr": "",
            "parsed_output": "ok",
            "runtime": {"provider": "anthropic", "model": "anthropic/claude-sonnet-4-6", "thinking": "low"},
            "usage": {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140, "estimated_cost_usd": 0.0009, "usage_status": "captured"},
        },
    )
    logger.save_agent_report(
        "implementation",
        "developer",
        {
            "status": "success",
            "result": "completed",
            "elapsed_s": 20,
            "returncode": 0,
            "message": "prompt",
            "stdout": "",
            "stderr": "",
            "parsed_output": "ok",
            "runtime": {"provider": "anthropic", "model": "anthropic/claude-sonnet-4-6", "thinking": "low"},
            "usage": {"input_tokens": 200, "output_tokens": 60, "total_tokens": 260, "estimated_cost_usd": 0.0015, "usage_status": "captured"},
        },
    )

    summary_path = logger.save_run_summary()
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    assert payload["completed_agents"] == 2
    assert payload["failed_agents"] == 0
    assert payload["total_elapsed_s"] == 30.0
    assert payload["input_tokens"] == 300
    assert payload["output_tokens"] == 100
    assert payload["total_tokens"] == 400
    assert payload["estimated_cost_usd"] == 0.0024


def test_agents_list_cache_validates_without_models_list_per_agent(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    calls = {"agents": 0, "models": 0}

    def fake_run(args, **_kwargs):
        if args[:4] == ["openclaw", "agents", "list", "--json"]:
            calls["agents"] += 1
            return SimpleNamespace(returncode=0, stdout=_fake_agents_list_payload(), stderr="")
        if args[:3] == ["openclaw", "models", "list"]:
            calls["models"] += 1
            raise AssertionError("models list should not be called for global registry validation")
        raise AssertionError(f"unexpected subprocess.run args: {args}")

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    first_ok = orchestrator._run_agent({"name": "project-analyst", "description": "Analyze repo", "timeout": 5}, "research")
    second_ok = orchestrator._run_agent({"name": "project-analyst", "description": "Analyze repo", "timeout": 5}, "research")

    assert first_ok is True
    assert second_ok is True
    assert calls["agents"] == 1
    assert calls["models"] == 0


def test_models_list_timeout_does_not_block_if_registry_model_exists(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    def fake_run(args, **_kwargs):
        if args[:4] == ["openclaw", "agents", "list", "--json"]:
            return SimpleNamespace(returncode=0, stdout=_fake_agents_list_payload(), stderr="")
        if args[:3] == ["openclaw", "models", "list"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=20)
        raise AssertionError(f"unexpected subprocess.run args: {args}")

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    ok = orchestrator._run_agent(
        {
            "name": "project-analyst",
            "description": "Analyze repo",
            "timeout": 5,
            "provider": "anthropic",
            "model": "anthropic/claude-sonnet-4-6",
        },
        "research",
    )

    assert ok is True


def test_invalid_model_fails_when_neither_registry_nor_models_list_can_validate(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    def fake_run(args, **_kwargs):
        if args[:4] == ["openclaw", "agents", "list", "--json"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"id": "project-analyst"}]), stderr="")
        if args[:3] == ["openclaw", "models", "list"]:
            return SimpleNamespace(returncode=0, stdout="perplexity/sonar\n", stderr="")
        raise AssertionError(f"unexpected subprocess.run args: {args}")

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("agent launch must not happen for an unavailable model")

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fail_if_called)

    ok = orchestrator._run_agent(
        {
            "name": "project-analyst",
            "description": "Analyze repo",
            "timeout": 5,
            "provider": "anthropic",
            "model": "anthropic/claude-sonnet-4-6",
        },
        "research",
    )

    assert ok is False
    payload = json.loads(
        (orchestrator.logger.run_dir / "agents" / "research" / "project-analyst.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "failed"
    assert payload["result"] == "Configured model is not available: anthropic/claude-sonnet-4-6"
