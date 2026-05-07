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
