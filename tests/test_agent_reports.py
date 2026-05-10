import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import yaml

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
            {"id": "project-analyst", "model": "openrouter/deepseek/deepseek-chat-v3"},
            {"id": "competitor-analyst"},
        ]
    )


def _fake_models_list(*_args, **_kwargs) -> SimpleNamespace:
    return SimpleNamespace(
        returncode=0,
        stdout=(
            "openrouter/deepseek/deepseek-chat-v3\n"
            "claude-sonnet-4-5-20250929\n"
            "anthropic/claude-sonnet-4-6\n"
            "openrouter/anthropic/claude-sonnet-4.6\n"
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


class _FakeHTTPResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


def test_openrouter_request_includes_authorization_bearer_header(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=0):
        headers = {key.lower(): value for key, value in request.header_items()}
        captured["authorization"] = headers.get("authorization")
        captured["content_type"] = headers.get("content-type")
        captured["referer"] = headers.get("http-referer")
        captured["title"] = headers.get("x-title")
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHTTPResponse(
            {
                "model": "deepseek/deepseek-chat-v3",
                "choices": [
                    {
                        "message": {
                            "content": "English summary.\n\nRussian translation\nРусский перевод."
                        }
                    }
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            }
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setattr(orchestrator_module.urllib_request, "urlopen", fake_urlopen)

    ok = orchestrator._run_agent(
        {
            "name": "project-analyst",
            "description": "Analyze repo",
            "timeout": 5,
            "provider": "openrouter",
            "model": "openrouter/deepseek/deepseek-chat-v3",
        },
        "research",
    )

    assert ok is True
    assert captured["authorization"] == "Bearer test-openrouter-key"
    assert captured["content_type"] == "application/json"
    assert captured["referer"] == "http://localhost/agents-pipeline"
    assert captured["title"] == "agents-pipeline"
    assert captured["body"]["model"] == "deepseek/deepseek-chat-v3"


def test_openrouter_model_normalization_works() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    assert orchestrator._normalize_openrouter_model("openrouter/deepseek/deepseek-chat-v3") == "deepseek/deepseek-chat-v3"
    assert orchestrator._normalize_openrouter_model("openrouter/anthropic/claude-3.7-sonnet") == "anthropic/claude-3.7-sonnet"
    assert orchestrator._normalize_openrouter_model("perplexity/sonar") == "perplexity/sonar"


def test_direct_api_401_becomes_auth_failed(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    def fake_urlopen(request, timeout=0):
        raise HTTPError(
            request.full_url,
            401,
            "Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b'{"error":{"message":"Missing Authentication header"}}'),
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setattr(orchestrator_module.urllib_request, "urlopen", fake_urlopen)

    ok = orchestrator._run_agent(
        {
            "name": "project-analyst",
            "description": "Analyze repo",
            "timeout": 5,
            "provider": "openrouter",
            "model": "openrouter/deepseek/deepseek-chat-v3",
        },
        "research",
    )

    assert ok is False
    payload = json.loads(
        (orchestrator.logger.run_dir / "agents" / "research" / "project-analyst.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "auth_failed"
    assert payload["returncode"] == 1


def test_direct_api_successful_response_saves_report(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    def fake_urlopen(_request, timeout=0):
        return _FakeHTTPResponse(
            {
                "model": "anthropic/claude-3.7-sonnet",
                "choices": [
                    {
                        "message": {
                            "content": "English summary.\n\nRussian translation\nРусский перевод."
                        }
                    }
                ],
                "usage": {"prompt_tokens": 21, "completion_tokens": 13, "total_tokens": 34},
            }
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setattr(orchestrator_module.urllib_request, "urlopen", fake_urlopen)

    ok = orchestrator._run_agent(
        {
            "name": "competitor-analyst",
            "description": "Analyze competitors",
            "timeout": 5,
            "provider": "openrouter",
            "model": "openrouter/anthropic/claude-3.7-sonnet",
        },
        "research",
    )

    assert ok is True
    payload = json.loads(
        (orchestrator.logger.run_dir / "agents" / "research" / "competitor-analyst.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "success"
    assert payload["returncode"] == 0
    assert "Russian translation" in payload["parsed_output"]
    assert payload["usage"]["input_tokens"] == 21
    assert payload["usage"]["output_tokens"] == 13
    assert payload["usage"]["total_tokens"] == 34


def test_project_context_includes_workflow_config() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    context = orchestrator._build_direct_api_repository_context(limit=12000)

    assert "## workflow/config.yaml" in context
    assert "workflow:" in context


def test_project_context_is_capped(monkeypatch) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    monkeypatch.setattr(orchestrator, "_run_local_capture", lambda *_args, **_kwargs: "A" * 20000)
    monkeypatch.setattr(orchestrator, "_build_top_level_tree", lambda *args, **kwargs: "B" * 20000)
    monkeypatch.setattr(orchestrator, "_read_file_excerpt", lambda *args, **kwargs: "C" * 20000)
    monkeypatch.setattr(orchestrator, "_build_python_outline", lambda *args, **kwargs: "D" * 20000)
    monkeypatch.setattr(orchestrator, "_build_tests_file_list", lambda: "E" * 20000)

    context = orchestrator._build_direct_api_repository_context(limit=12000)

    assert len(context) <= 12000


def test_project_analyst_direct_api_prompt_includes_repository_context() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    bundle = orchestrator._build_agent_message_bundle(
        "project-analyst",
        {"name": "project-analyst", "description": "Analyze repo"},
        Path(".openclaw/agents/research/project-analyst/prompt.md"),
        "research",
    )

    assert "Repository context collected locally:" in bundle["system_message"]
    assert "## workflow/config.yaml" in bundle["system_message"]
    assert "Repository context collected locally:" in bundle["combined_message"]


def test_non_project_analyst_prompt_is_unchanged() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    bundle = orchestrator._build_agent_message_bundle(
        "competitor-analyst",
        {"name": "competitor-analyst", "description": "Analyze competitors"},
        Path(".openclaw/agents/research/competitor-analyst/prompt.md"),
        "research",
    )

    assert "Repository context collected locally:" not in bundle["system_message"]
    assert "Repository context collected locally:" not in bundle["combined_message"]


def test_successful_agent_run_creates_report_files(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.runtime.executor = "openclaw"
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
    orchestrator.runtime.executor = "openclaw"
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
    orchestrator.runtime.executor = "openclaw"
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
    orchestrator.runtime.executor = "openclaw"
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
    orchestrator.runtime.executor = "openclaw"
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
    orchestrator.runtime.executor = "openclaw"
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    effective_runtime = orchestrator._resolve_agent_runtime(
        {"name": "competitor-analyst", "provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"}
    )

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="openrouter/deepseek/deepseek-chat-v3\n", stderr=""),
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
    orchestrator.runtime.executor = "openclaw"
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
            "model": "openrouter/deepseek/deepseek-chat-v3",
            "thinking": "low",
        },
        "research",
    )

    assert ok is True
    payload = json.loads(
        (orchestrator.logger.run_dir / "agents" / "research" / "project-analyst.json").read_text(encoding="utf-8")
    )
    assert "--provider openrouter" in payload["command"]
    assert "--model openrouter/deepseek/deepseek-chat-v3" in payload["command"]


def test_mismatch_blocks_launch_when_override_cannot_be_applied(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.runtime.executor = "openclaw"
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda args, **_kwargs: (
            SimpleNamespace(returncode=0, stdout=json.dumps([{"id": "project-analyst", "model": "perplexity/sonar"}]), stderr="")
            if args[:4] == ["openclaw", "agents", "list", "--json"]
            else SimpleNamespace(returncode=0, stdout="", stderr="")
        ),
    )

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
            "model": "openrouter/deepseek/deepseek-chat-v3",
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
    orchestrator.runtime.executor = "openclaw"
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
    orchestrator.runtime.executor = "openclaw"
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    orchestrator._global_registry_models = {"project-analyst": "openrouter/deepseek/deepseek-chat-v3"}

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
    orchestrator.runtime.executor = "openclaw"
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    orchestrator._global_registry_models = {"project-analyst": "openrouter/deepseek/deepseek-chat-v3"}
    calls = {"agents": 0, "models": 0, "help": 0}

    def fake_run(args, **_kwargs):
        if args[:4] == ["openclaw", "agents", "list", "--json"]:
            calls["agents"] += 1
            return SimpleNamespace(returncode=0, stdout=_fake_agents_list_payload(), stderr="")
        if args[:3] == ["openclaw", "agent", "--help"]:
            calls["help"] += 1
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[:3] == ["openclaw", "models", "list"]:
            calls["models"] += 1
            return SimpleNamespace(returncode=0, stdout="openrouter/deepseek/deepseek-chat-v3\n", stderr="")
        raise AssertionError(f"unexpected subprocess.run args: {args}")

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    first_ok = orchestrator._run_agent({"name": "project-analyst", "description": "Analyze repo", "timeout": 5}, "research")
    second_ok = orchestrator._run_agent({"name": "project-analyst", "description": "Analyze repo", "timeout": 5}, "research")

    assert first_ok is True
    assert second_ok is True
    assert calls["agents"] == 1
    assert calls["help"] == 1
    assert calls["models"] == 0


def test_models_list_timeout_does_not_block_if_registry_model_exists(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.runtime.executor = "openclaw"
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
            "provider": "openrouter",
            "model": "openrouter/deepseek/deepseek-chat-v3",
        },
        "research",
    )

    assert ok is True


def test_invalid_model_fails_when_neither_registry_nor_models_list_can_validate(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.runtime.executor = "openclaw"
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
            "provider": "openrouter",
            "model": "openrouter/deepseek/deepseek-chat-v3",
        },
        "research",
    )

    assert ok is False
    payload = json.loads(
        (orchestrator.logger.run_dir / "agents" / "research" / "project-analyst.json").read_text(encoding="utf-8")
    )
    assert payload["status"] == "failed"
    assert payload["result"] == "Configured model is not available: openrouter/deepseek/deepseek-chat-v3"


def test_preflight_fails_fast_when_startup_model_is_not_configured(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.runtime.executor = "openclaw"
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    def fake_run(args, **_kwargs):
        if args[:4] == ["openclaw", "agents", "list", "--json"]:
            payload = [{"id": agent["name"], "model": "openrouter/deepseek/deepseek-chat-v3"} for phase in orchestrator.config["phases"].values() for agent in phase["agents"]]
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")
        if args[:3] == ["openclaw", "models", "list"]:
            return SimpleNamespace(returncode=0, stdout="perplexity/sonar\n", stderr="")
        raise AssertionError(f"unexpected subprocess.run args: {args}")

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(orchestrator_module, "has_provider_credentials", lambda _provider: True)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert orchestrator._preflight_runtime() is False


def test_agents_list_json_timeout_does_not_fail_if_local_agent_dirs_exist(monkeypatch, tmp_path: Path) -> None:
    agents_root = tmp_path / "agents"
    (agents_root / "research" / "local-only-agent").mkdir(parents=True)
    config_path = tmp_path / "workflow.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "workflow": {"require_registry_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": "."},
                "paths": {"agents_dir": str(agents_root)},
                "phases": {
                    "research": {
                        "name": "Research",
                        "agents": [{"name": "local-only-agent", "provider": "openrouter", "model": "perplexity/sonar"}],
                    }
                },
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    orchestrator = WorkflowOrchestrator(str(config_path))
    orchestrator.runtime.executor = "openclaw"
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    orchestrator.runtime.require_registry_preflight = False
    warnings: list[str] = []

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(orchestrator_module, "has_provider_credentials", lambda _provider: True)
    monkeypatch.setattr(orchestrator, "_get_required_startup_models", lambda: {})
    monkeypatch.setattr(orchestrator.logger, "warning", warnings.append)

    def fake_run(args, **_kwargs):
        if args[:4] == ["openclaw", "agents", "list", "--json"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=60)
        raise AssertionError(f"unexpected subprocess.run args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert orchestrator._preflight_runtime() is True
    assert orchestrator._registered_agents_cache == {}
    assert any("timed out after 60 seconds" in message for message in warnings)


def test_missing_local_agent_dir_still_fails_when_registry_json_times_out(monkeypatch, tmp_path: Path) -> None:
    agents_root = tmp_path / "agents"
    config_path = tmp_path / "workflow.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "workflow": {"require_registry_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": "."},
                "paths": {"agents_dir": str(agents_root)},
                "phases": {
                    "research": {
                        "name": "Research",
                        "agents": [{"name": "missing-agent", "provider": "openrouter", "model": "perplexity/sonar"}],
                    }
                },
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    orchestrator = WorkflowOrchestrator(str(config_path))
    orchestrator.runtime.executor = "openclaw"
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    orchestrator.runtime.require_registry_preflight = False

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(orchestrator_module, "has_provider_credentials", lambda _provider: True)
    monkeypatch.setattr(orchestrator, "_get_required_startup_models", lambda: {})

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("registry lookup should not matter once the local agent directory is missing")

    monkeypatch.setattr(subprocess, "run", fail_if_called)

    assert orchestrator._preflight_runtime() is False


def test_models_list_timeout_does_not_fail_startup_when_preflight_not_required(monkeypatch, tmp_path: Path) -> None:
    agents_root = tmp_path / "agents"
    (agents_root / "research" / "local-only-agent").mkdir(parents=True)
    config_path = tmp_path / "workflow.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "workflow": {
                    "require_registry_preflight": False,
                    "require_model_list_preflight": False,
                },
                "project": {"name": "agents-pipeline", "workspace": "."},
                "paths": {"agents_dir": str(agents_root)},
                "phases": {
                    "research": {
                        "name": "Research",
                        "agents": [{"name": "local-only-agent", "provider": "openrouter", "model": "perplexity/sonar"}],
                    }
                },
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    orchestrator = WorkflowOrchestrator(str(config_path))
    orchestrator.runtime.executor = "openclaw"
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    orchestrator.runtime.require_model_list_preflight = False
    warnings: list[str] = []

    def fake_run(args, **_kwargs):
        if args[:4] == ["openclaw", "agents", "list", "--json"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"id": "local-only-agent"}]), stderr="")
        if args[:3] == ["openclaw", "models", "list"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=20)
        raise AssertionError(f"unexpected subprocess.run args: {args}")

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(orchestrator_module, "has_provider_credentials", lambda _provider: True)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(orchestrator.logger, "warning", warnings.append)

    assert orchestrator._preflight_runtime() is True
    assert any(
        message == "OpenClaw model list check timed out; continuing with configured effective models."
        for message in warnings
    )


def test_models_list_timeout_fails_startup_when_preflight_required(monkeypatch, tmp_path: Path) -> None:
    agents_root = tmp_path / "agents"
    (agents_root / "research" / "local-only-agent").mkdir(parents=True)
    config_path = tmp_path / "workflow.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "workflow": {
                    "require_registry_preflight": False,
                    "require_model_list_preflight": True,
                },
                "project": {"name": "agents-pipeline", "workspace": "."},
                "paths": {"agents_dir": str(agents_root)},
                "phases": {
                    "research": {
                        "name": "Research",
                        "agents": [{"name": "local-only-agent", "provider": "openrouter", "model": "perplexity/sonar"}],
                    }
                },
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    orchestrator = WorkflowOrchestrator(str(config_path))
    orchestrator.runtime.executor = "openclaw"
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    orchestrator.runtime.require_model_list_preflight = True

    def fake_run(args, **_kwargs):
        if args[:4] == ["openclaw", "agents", "list", "--json"]:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"id": "local-only-agent"}]), stderr="")
        if args[:3] == ["openclaw", "models", "list"]:
            raise subprocess.TimeoutExpired(cmd=args, timeout=20)
        raise AssertionError(f"unexpected subprocess.run args: {args}")

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(orchestrator_module, "has_provider_credentials", lambda _provider: True)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert orchestrator._preflight_runtime() is False


def test_models_list_is_not_called_repeatedly_after_first_timeout(monkeypatch, tmp_path: Path) -> None:
    agents_root = tmp_path / "agents"
    (agents_root / "research" / "local-only-agent").mkdir(parents=True)
    config_path = tmp_path / "workflow.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "workflow": {
                    "require_registry_preflight": False,
                    "require_model_list_preflight": False,
                },
                "project": {"name": "agents-pipeline", "workspace": "."},
                "paths": {"agents_dir": str(agents_root)},
                "phases": {
                    "research": {
                        "name": "Research",
                        "agents": [{"name": "local-only-agent", "provider": "openrouter", "model": "perplexity/sonar"}],
                    }
                },
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    orchestrator = WorkflowOrchestrator(str(config_path))
    orchestrator.runtime.executor = "openclaw"
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    orchestrator.runtime.require_model_list_preflight = False
    calls = {"agents": 0, "models": 0}

    def fake_run(args, **_kwargs):
        if args[:4] == ["openclaw", "agents", "list", "--json"]:
            calls["agents"] += 1
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"id": "local-only-agent"}]), stderr="")
        if args[:3] == ["openclaw", "models", "list"]:
            calls["models"] += 1
            raise subprocess.TimeoutExpired(cmd=args, timeout=20)
        raise AssertionError(f"unexpected subprocess.run args: {args}")

    monkeypatch.setattr(orchestrator_module, "resolve_runner_path", lambda _runner: "openclaw")
    monkeypatch.setattr(orchestrator_module, "has_provider_credentials", lambda _provider: True)
    monkeypatch.setattr(subprocess, "run", fake_run)

    assert orchestrator._preflight_runtime() is True

    validation = orchestrator._verify_model_available(
        "openclaw",
        "local-only-agent",
        {
            "provider": "openrouter",
            "model": "perplexity/sonar",
            "profile": "default",
            "thinking": "low",
            "model_source": "agent override",
        },
    )

    assert validation["error"] == ""
    assert calls["models"] == 1
