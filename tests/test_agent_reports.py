import io
import json
import subprocess
from pathlib import Path

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
            )
        }
        self.stdout = io.StringIO(json.dumps(payload))
        self.stderr = io.StringIO("")
        self.returncode = 0

    def poll(self) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def test_successful_agent_run_creates_report_files(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    ok = orchestrator._run_agent(
        {
            "name": "project-analyst",
            "description": "Analyze the current repository, active constraints, and the most important next questions",
            "timeout": 5,
        },
        "research",
    )

    assert ok is True

    report_dir = orchestrator.logger.run_dir / "agents" / "research"
    md_path = report_dir / "project-analyst.md"
    json_path = report_dir / "project-analyst.json"

    assert md_path.exists()
    assert json_path.exists()

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["agent_name"] == "project-analyst"
    assert payload["phase"] == "research"
    assert payload["status"] == "success"
    assert payload["result"] == "completed"
    assert payload["returncode"] == 0
    assert payload["timestamp"]
    assert payload["message"]
    assert payload["stdout"]
    assert "Russian translation" in payload["parsed_output"]

    report_text = md_path.read_text(encoding="utf-8")
    assert "- agent_name: project-analyst" in report_text
    assert "## Prompt Message" in report_text
    assert "## Stdout" in report_text


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
            "runtime": {"provider": "claude", "model": "claude-sonnet-4-20250514", "thinking": "low"},
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
            "runtime": {"provider": "claude", "model": "claude-sonnet-4-20250514", "thinking": "low"},
        },
    )

    context = orchestrator._build_previous_agent_context("research", "competitor-analyst")

    assert "[project-analyst]" in context
    assert len(context) <= 4000
