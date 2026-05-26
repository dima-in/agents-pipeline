import http.client
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import git
import yaml

import workflow.orchestrator as orchestrator_module
import start as start_module
from workflow.logger import WorkflowLogger
from workflow.orchestrator import WorkflowOrchestrator


class _FakePopen:
    def __init__(self, *_args, **_kwargs) -> None:
        payload = {
            "output_text": (
                "English summary.\n\n"
                "Russian translation\n"
                "Р СѓСЃСЃРєРѕРµ СЂРµР·СЋРјРµ."
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
                "Р СѓСЃСЃРєРѕРµ СЂРµР·СЋРјРµ."
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
            "claude-sonnet-4-6-20251001\n"
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


def _seed_research_run(log_root: Path, run_id: str, reports: list[dict[str, object]]) -> Path:
    run_dir = log_root / f"run_{run_id}" / "agents" / "research"
    run_dir.mkdir(parents=True, exist_ok=True)
    for report in reports:
        agent_name = str(report["agent_name"])
        payload = {
            "agent_name": agent_name,
            "agent": agent_name,
            "status": "success",
            "handoff_summary": report.get("handoff_summary", ""),
            "parsed_output": report.get("parsed_output", ""),
        }
        (run_dir / f"{agent_name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return run_dir.parent.parent


def _seed_implementation_report(
    run_dir: Path,
    agent_name: str,
    *,
    parsed_output: str = "",
    handoff_summary: str = "",
    status: str = "success",
) -> None:
    agent_dir = run_dir / "agents" / "implementation"
    agent_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "agent_name": agent_name,
        "agent": agent_name,
        "status": status,
        "handoff_summary": handoff_summary,
        "parsed_output": parsed_output,
    }
    (agent_dir / f"{agent_name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_temp_agent_prompt(engine_root: Path, phase: str, agent_name: str) -> None:
    prompt_dir = engine_root / ".openclaw" / "agents" / phase / agent_name
    prompt_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / "prompt.md").write_text(f"# {agent_name}\n\nFollow the task.\n", encoding="utf-8")

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
                            "content": "English summary.\n\nRussian translation\nР СѓСЃСЃРєРёР№ РїРµСЂРµРІРѕРґ."
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
    assert captured["body"]["max_tokens"] == 1800


def test_implementation_planner_direct_api_does_not_require_russian_translation(
    monkeypatch, tmp_path: Path
) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    def fake_urlopen(_request, timeout=0):
        return _FakeHTTPResponse(
            {
                "model": "anthropic/claude-4.5-sonnet-20250929",
                "choices": [
                    {
                        "message": {
                            "content": "tasks:\n  - id: TASK-001\n    title: Example\n    priority: P1\n    scope: backend-only\n    existing_paths:\n      - gateway-v4/app/main.py\n    new_directories: []\n    new_files:\n      - gateway-v4/tests/test_example.py\n    allowed_paths:\n      - gateway-v4/app/main.py\n      - gateway-v4/tests/test_example.py\n    forbidden_paths: []\n    required_test_paths:\n      - gateway-v4/tests/test_example.py\n    acceptance_criteria:\n      - test exists\n    reason_each_path_is_needed:\n      gateway-v4/app/main.py: reference\n      gateway-v4/tests/test_example.py: test\n    target_file:\n      path: gateway-v4/tests/test_example.py\n      action: create\n      purpose: Create test\n    reference_files:\n      - gateway-v4/app/main.py\n    depends_on: []\n    risk_level: low\n    estimated_effort: S"
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
            "name": "implementation-planner",
            "description": "Convert plan to backlog",
            "timeout": 5,
            "provider": "openrouter",
            "model": "openrouter/anthropic/claude-sonnet-4.5",
        },
        "implementation",
    )

    assert ok is True


def test_implementation_planner_direct_api_omits_max_tokens(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout=0):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeHTTPResponse(
            {
                "model": "anthropic/claude-4.5-sonnet-20250929",
                "choices": [
                    {
                        "message": {
                            "content": "tasks:\n  - id: TASK-001\n    title: Example\n    priority: P1\n    scope: backend-only\n    existing_paths:\n      - gateway-v4/app/main.py\n    new_directories: []\n    new_files:\n      - gateway-v4/tests/test_example.py\n    allowed_paths:\n      - gateway-v4/app/main.py\n      - gateway-v4/tests/test_example.py\n    forbidden_paths: []\n    required_test_paths:\n      - gateway-v4/tests/test_example.py\n    acceptance_criteria:\n      - test exists\n    reason_each_path_is_needed:\n      gateway-v4/app/main.py: reference\n      gateway-v4/tests/test_example.py: test\n    target_file:\n      path: gateway-v4/tests/test_example.py\n      action: create\n      purpose: Create test\n    reference_files:\n      - gateway-v4/app/main.py\n    depends_on: []\n    risk_level: low\n    estimated_effort: S"
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
            "name": "implementation-planner",
            "description": "Convert plan to backlog",
            "timeout": 5,
            "provider": "openrouter",
            "model": "openrouter/anthropic/claude-sonnet-4.5",
        },
        "implementation",
    )

    assert ok is True
    assert "max_tokens" not in captured["body"]


def test_parse_direct_api_retrieval_request_supports_multiple_json_tool_calls() -> None:
    payload = WorkflowOrchestrator._parse_direct_api_retrieval_request(
        """I'll inspect and update files.

{"tool":"read_file","path":"gateway-v4/app/models.py"}

{"tool":"read_file","path":"gateway-v4/app/database.py"}

{"tool":"write_file","path":"gateway-v4/tests/test_provider_metrics_model.py","content":"ok"}
"""
    )

    assert payload is not None
    assert payload["tool"] == "tool_batch"
    assert [request["tool"] for request in payload["requests"]] == ["read_file", "read_file", "write_file"]


def test_openrouter_model_normalization_works() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    assert orchestrator._normalize_openrouter_model("openrouter/deepseek/deepseek-chat-v3") == "deepseek/deepseek-chat-v3"
    assert orchestrator._normalize_openrouter_model("openrouter/anthropic/claude-sonnet-4.6") == "anthropic/claude-sonnet-4.6"
    assert orchestrator._normalize_openrouter_model("perplexity/sonar") == "perplexity/sonar"


def test_direct_api_404_no_endpoints_becomes_model_not_found() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    assert (
        orchestrator._classify_direct_api_error(
            404,
            '{"error":{"message":"No endpoints found for anthropic/claude-sonnet-4.6.","code":404}}',
        )
        == "model_not_found"
    )


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
                "model": "anthropic/claude-sonnet-4.6",
                "choices": [
                    {
                        "message": {
                            "content": "English summary.\n\nRussian translation\nР СѓСЃСЃРєРёР№ РїРµСЂРµРІРѕРґ."
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
            "model": "openrouter/anthropic/claude-sonnet-4.6",
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
    assert payload["handoff_summary"].startswith("agent: competitor-analyst")
    assert "findings:" in payload["handoff_summary"]
    assert payload["context_profile"] == "external_comparison"
    assert payload["retrieval_enabled"] is False
    assert payload["repository_context_chars"] > 0
    assert payload["handoff_summary_chars"] == 0
    assert payload["retrieval_rounds"] == 0
    assert payload["usage"]["input_tokens"] == 21
    assert payload["usage"]["output_tokens"] == 13
    assert payload["usage"]["total_tokens"] == 34


def test_project_context_includes_workflow_config() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    context = orchestrator._build_direct_api_repository_context(agent_name="project-analyst", limit=12000)

    assert "## workflow/config.yaml" in context
    assert "workflow:" in context


def test_project_context_reads_readme_from_target_workspace(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("target workspace readme", encoding="utf-8")
    (engine_root / "README.md").write_text("engine readme", encoding="utf-8")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )

    context = orchestrator._build_direct_api_repository_context(agent_name="project-analyst", limit=12000)

    assert "target workspace readme" in context
    assert "engine readme" not in context
    assert "workflow/orchestrator.py" not in context
    assert orchestrator.context_mode == "external_project_analysis"


def test_project_context_is_capped(monkeypatch) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    monkeypatch.setattr(orchestrator, "_run_local_capture", lambda *_args, **_kwargs: "A" * 20000)
    monkeypatch.setattr(orchestrator, "_build_top_level_tree", lambda *args, **kwargs: "B" * 20000)
    monkeypatch.setattr(orchestrator, "_read_file_excerpt", lambda *args, **kwargs: "C" * 20000)
    monkeypatch.setattr(orchestrator, "_build_python_outline", lambda *args, **kwargs: "D" * 20000)
    monkeypatch.setattr(orchestrator, "_build_tests_file_list", lambda: "E" * 20000)
    monkeypatch.setattr(orchestrator, "_build_fallback_project_summary", lambda: "F" * 20000)
    monkeypatch.setattr(orchestrator, "_build_compact_architecture_summary", lambda: "G" * 20000)
    monkeypatch.setattr(orchestrator, "_build_positioning_summary", lambda: "H" * 20000)
    monkeypatch.setattr(orchestrator, "_build_workflow_goals_summary", lambda: "I" * 20000)
    monkeypatch.setattr(orchestrator, "_build_target_users_summary", lambda: "J" * 20000)
    monkeypatch.setattr(orchestrator, "_build_execution_architecture_summary", lambda: "K" * 20000)
    monkeypatch.setattr(orchestrator, "_build_known_constraints_summary", lambda: "L" * 20000)
    monkeypatch.setattr(orchestrator, "_build_tests_subset", lambda *args, **kwargs: "M" * 20000)
    monkeypatch.setattr(orchestrator, "_read_python_sections", lambda *args, **kwargs: "N" * 20000)

    context = orchestrator._build_direct_api_repository_context(agent_name="project-analyst", limit=12000)

    assert len(context) <= 12000


def test_project_analyst_receives_full_repo_profile() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    bundle = orchestrator._build_agent_message_bundle(
        "project-analyst",
        {"name": "project-analyst", "description": "Analyze repo"},
        Path(".openclaw/agents/research/project-analyst/prompt.md"),
        "research",
    )

    assert "Repository context collected locally:" in bundle["system_message"]
    assert "## workflow/config.yaml" in bundle["system_message"]
    assert "## workflow/orchestrator.py outline" in bundle["system_message"]
    assert "Repository context collected locally:" in bundle["combined_message"]
    assert bundle["context_profile"] == "repo_overview_full"
    assert orchestrator.context_mode == "engine_self_analysis"


def test_external_project_analyst_excludes_engine_files(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("external product readme", encoding="utf-8")
    (target_workspace / "package.json").write_text('{"name":"external-app"}', encoding="utf-8")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    bundle = orchestrator._build_agent_message_bundle(
        "project-analyst",
        {"name": "project-analyst", "description": "Analyze repo"},
        Path(".openclaw/agents/research/project-analyst/prompt.md"),
        "research",
    )

    assert "external product readme" in bundle["system_message"]
    assert "workflow/orchestrator.py outline" not in bundle["system_message"]
    assert "manage_agents.py outline" not in bundle["system_message"]
    assert "package.json" in bundle["system_message"]


def test_competitor_analyst_does_not_receive_full_orchestrator_outline(tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    orchestrator.logger.save_agent_report(
        "research",
        "project-analyst",
        {
            "status": "success",
            "result": "completed",
            "elapsed_s": 1,
            "returncode": 0,
            "message": "prompt",
            "stdout": "",
            "stderr": "",
            "parsed_output": "Repo findings.\n\nRussian translation\nР РµРїРѕ.",
                "handoff_summary": "agent: project-analyst\nfindings:\n- Repo findings.\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none",
                "runtime": {"provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6", "thinking": "low"},
            },
        )

    bundle = orchestrator._build_agent_message_bundle(
        "competitor-analyst",
        {"name": "competitor-analyst", "description": "Analyze competitors"},
        Path(".openclaw/agents/research/competitor-analyst/prompt.md"),
        "research",
    )

    assert "Repository context collected locally:" in bundle["system_message"]
    assert "## workflow/orchestrator.py outline" not in bundle["system_message"]
    assert "Default competitors" in bundle["system_message"]
    assert "[project-analyst]" in bundle["system_message"]
    assert bundle["context_profile"] == "external_comparison"
    assert bundle["retrieval_enabled"] is False


def test_market_analyst_receives_no_code_context_by_default() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    bundle = orchestrator._build_agent_message_bundle(
        "market-analyst",
        {"name": "market-analyst", "description": "Analyze market"},
        Path(".openclaw/agents/research/market-analyst/prompt.md"),
        "research",
    )

    assert "Positioning" in bundle["system_message"]
    assert "Target users and use cases" in bundle["system_message"]
    assert "workflow/orchestrator.py outline" not in bundle["system_message"]
    assert "workflow/runtime.py" not in bundle["system_message"]
    assert "workflow/config.yaml" not in bundle["system_message"]
    assert bundle["context_profile"] == "market_positioning"
    assert bundle["retrieval_enabled"] is False


def test_market_analyst_external_mode_receives_no_agents_pipeline_positioning(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("AI Getaway is a gateway for model routing and chat delivery.", encoding="utf-8")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    bundle = orchestrator._build_agent_message_bundle(
        "market-analyst",
        {"name": "market-analyst", "description": "Analyze market"},
        Path(".openclaw/agents/research/market-analyst/prompt.md"),
        "research",
    )

    assert "AI Getaway is a gateway for model routing and chat delivery." in bundle["system_message"]
    assert "agents-pipeline is a local-first multi-agent workflow" not in bundle["system_message"]


def test_tech_analyst_receives_direct_api_and_retrieval_context() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    bundle = orchestrator._build_agent_message_bundle(
        "tech-analyst",
        {"name": "tech-analyst", "description": "Review technical options"},
        Path(".openclaw/agents/research/tech-analyst/prompt.md"),
        "research",
    )

    assert "direct_api implementation" in bundle["system_message"]
    assert "retrieval-loop implementation" in bundle["system_message"]
    assert "def _run_direct_api_agent(" in bundle["system_message"]
    assert bundle["context_profile"] == "technical_architecture"
    assert bundle["retrieval_enabled"] is True


def test_tech_analyst_external_mode_receives_target_files_not_engine_internals(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    (target_workspace / "frontend").mkdir(parents=True)
    (target_workspace / "gateway-v4").mkdir(parents=True)
    target_workspace.mkdir(parents=True, exist_ok=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "frontend" / "package.json").write_text('{"name":"frontend"}', encoding="utf-8")
    (target_workspace / "gateway-v4" / "requirements.txt").write_text("fastapi", encoding="utf-8")
    (target_workspace / "docker-compose.yml").write_text("services: {}", encoding="utf-8")
    (target_workspace / "test_api.py").write_text("def test_ok(): pass", encoding="utf-8")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    bundle = orchestrator._build_agent_message_bundle(
        "tech-analyst",
        {"name": "tech-analyst", "description": "Review technical options"},
        Path(".openclaw/agents/research/tech-analyst/prompt.md"),
        "research",
    )

    assert "frontend/package.json" in bundle["system_message"]
    assert "gateway-v4/requirements.txt" in bundle["system_message"]
    assert "docker-compose.yml" in bundle["system_message"]
    assert "direct_api implementation" not in bundle["system_message"]
    assert "retrieval-loop implementation" not in bundle["system_message"]


def test_innovation_scout_receives_compressed_context_without_repo_dump() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    bundle = orchestrator._build_agent_message_bundle(
        "innovation-scout",
        {"name": "innovation-scout", "description": "Scout ideas"},
        Path(".openclaw/agents/research/innovation-scout/prompt.md"),
        "research",
    )

    assert "Compact architecture summary" in bundle["system_message"]
    assert "Known constraints and problems" in bundle["system_message"]
    assert "Top-level file tree up to depth 3" not in bundle["system_message"]
    assert "workflow/orchestrator.py outline" not in bundle["system_message"]
    assert bundle["retrieval_enabled"] is False


def test_product_manager_receives_summaries_from_previous_agents(tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))

    for agent_name, summary in [
        ("project-analyst", "agent: project-analyst\nfindings:\n- repo summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"),
        ("competitor-analyst", "agent: competitor-analyst\nfindings:\n- competitor summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"),
        ("market-analyst", "agent: market-analyst\nfindings:\n- market summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"),
    ]:
        orchestrator.logger.save_agent_report(
            "research",
            agent_name,
            {
                "status": "success",
                "result": "completed",
                "elapsed_s": 1,
                "returncode": 0,
                "message": "prompt",
                "stdout": "",
                "stderr": "",
                "parsed_output": summary,
                "handoff_summary": summary,
                "runtime": {"provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6", "thinking": "low"},
            },
        )

    bundle = orchestrator._build_agent_message_bundle(
        "product-manager",
        {"name": "product-manager", "description": "Summarize and prepare requirements"},
        Path(".openclaw/agents/research/product-manager/prompt.md"),
        "research",
    )

    assert "[project-analyst]" in bundle["system_message"]
    assert "[competitor-analyst]" in bundle["system_message"]
    assert "[market-analyst]" in bundle["system_message"]
    assert "Repository context collected locally:" not in bundle["system_message"]
    assert bundle["context_profile"] == "research_synthesis"
    assert bundle["retrieval_enabled"] is False


def test_no_memory_keeps_current_run_handoff_summaries(tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml", no_memory=True)
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    orchestrator.logger.save_agent_report(
        "research",
        "project-analyst",
        {
            "status": "success",
            "result": "completed",
            "elapsed_s": 1,
            "returncode": 0,
            "message": "prompt",
            "stdout": "",
            "stderr": "",
            "parsed_output": "summary",
            "handoff_summary": "agent: project-analyst\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none",
            "runtime": {"provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6", "thinking": "low"},
        },
    )

    bundle = orchestrator._build_agent_message_bundle(
        "competitor-analyst",
        {"name": "competitor-analyst", "description": "Analyze competitors"},
        Path(".openclaw/agents/research/competitor-analyst/prompt.md"),
        "research",
    )

    assert "Previous agent context:" in bundle["system_message"]
    assert "[project-analyst]" in bundle["system_message"]


def test_direct_api_retries_on_incomplete_read(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    attempts = {"count": 0}

    def fake_urlopen(_request, timeout=0):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise http.client.IncompleteRead(b'{"partial":true}', 100)
        return _FakeHTTPResponse(
            {
                "model": "anthropic/claude-sonnet-4.6",
                "choices": [
                    {
                        "message": {
                            "content": "English summary.\n\nRussian translation\nРџРµСЂРµРІРѕРґ."
                        }
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            }
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setattr(orchestrator_module.urllib_request, "urlopen", fake_urlopen)

    ok = orchestrator._run_agent(
        {
            "name": "product-manager",
            "description": "Summarize and prepare requirements",
            "timeout": 5,
            "provider": "openrouter",
            "model": "openrouter/anthropic/claude-sonnet-4.6",
        },
        "research",
    )

    assert ok is True
    assert attempts["count"] == 3


def test_task_scope_override_is_used_for_implementation(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        task_scope="Ship observability only.",
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120002",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )

    bundle = orchestrator._build_agent_message_bundle(
        "architect",
        {"name": "architect", "description": "Prepare technical plan"},
        Path(".openclaw/agents/implementation/architect/prompt.md"),
        "implementation",
    )

    assert bundle["selected_task_scope"] == "Ship observability only."
    assert "Ship observability only." in bundle["system_message"]


def test_default_implementation_scope_is_loaded_from_config(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    default_scope = "Implement backend-only monitoring only."
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {
                    "executor": "direct_api",
                    "mode": "auto",
                    "default_implementation_scope": default_scope,
                    "require_registry_preflight": False,
                    "require_model_list_preflight": False,
                },
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    monitoring_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    monitoring_file.parent.mkdir(parents=True, exist_ok=True)
    monitoring_file.write_text("pass\n", encoding="utf-8")
    monitoring_test = target_workspace / "tests" / "test_monitoring.py"
    monitoring_test.parent.mkdir(parents=True, exist_ok=True)
    monitoring_test.write_text("def test_monitoring():\n    assert True\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120004",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )

    bundle = orchestrator._build_agent_message_bundle(
        "architect",
        {"name": "architect", "description": "Prepare technical plan"},
        Path(".openclaw/agents/implementation/architect/prompt.md"),
        "implementation",
    )

    assert bundle["selected_task_scope"] == default_scope


def test_agents_pipeline_self_analysis_uses_reliability_default_scope() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    reports = [{"agent_name": "product-manager", "status": "success", "handoff_summary": "summary"}]

    scope = orchestrator._select_implementation_scope(reports)

    assert "agents-pipeline orchestration reliability" in scope
    assert "status/resume/doctor/repo-map/validation improvements" in scope
    assert "provider marketplace" in scope


def test_architect_prompt_includes_no_generic_root_warning_for_self_analysis() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    bundle = orchestrator._build_agent_message_bundle(
        "architect",
        {"name": "architect", "description": "Prepare technical plan"},
        Path(".openclaw/agents/implementation/architect/prompt.md"),
        "implementation",
    )

    assert "Do not propose src/, api/, services/, models/, or config/ root directories unless they already exist." in bundle["system_message"]


def test_implementation_planner_does_not_require_russian_translation() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    bundle = orchestrator._build_agent_message_bundle(
        "implementation-planner",
        {"name": "implementation-planner", "description": "Convert the architect plan into a prioritized implementation backlog"},
        Path(".openclaw/agents/implementation/implementation-planner/prompt.md"),
        "implementation",
    )

    assert "Верни только структурированный YAML или JSON для backlog outline." in bundle["system_message"]
    assert "Russian translation" not in bundle["system_message"]


def test_planner_can_translate_invalid_architect_path_into_valid_workflow_path(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    workflow_file = target_workspace / "workflow" / "orchestrator.py"
    workflow_file.parent.mkdir(parents=True, exist_ok=True)
    workflow_file.write_text("pass\n", encoding="utf-8")
    workflow_test = target_workspace / "tests" / "test_workflow.py"
    workflow_test.parent.mkdir(parents=True, exist_ok=True)
    workflow_test.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120016",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- improve repo map\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )
    _seed_implementation_report(
        orchestrator.logger.run_dir,
        "architect",
        parsed_output="Architect plan: add services/provider_monitor.py",
    )
    _seed_implementation_report(
        orchestrator.logger.run_dir,
        "implementation-planner",
        parsed_output=json.dumps(
            [
                {
                    "id": "workflow-fix",
                    "title": "Workflow reliability fix",
                    "priority": "P0",
                    "scope": "Improve orchestration validation in workflow/orchestrator.py.",
                    "existing_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                    "allowed_paths": ["workflow/orchestrator.py", "tests/test_workflow.py"],
                    "forbidden_paths": [],
                    "required_test_paths": ["tests/test_workflow.py"],
                    "depends_on": [],
                    "acceptance_criteria": ["Workflow validation improved."],
                    "reason_each_path_is_needed": {"workflow/orchestrator.py": "Needed.", "tests/test_workflow.py": "Needed."},
                    "target_file": {"path": "workflow/orchestrator.py", "action": "update", "purpose": "Improve workflow validation."},
                    "test_file": {"path": "tests/test_workflow.py", "action": "update"},
                    "must_contain": ["def _validate_implementation_planner_output(", "return {"],
                    "must_import": ["from pathlib import Path"],
                    "integration": ["Planner validation must align with implementation flow."],
                    "reference_files": ["workflow/orchestrator.py"],
                    "reference_excerpts": {"workflow/orchestrator.py": "pass"},
                    "must_test": ["test_planner_validation_accepts_valid_workflow_path: assert diagnostics are valid"],
                    "forbidden": ["Do not edit frontend files."],
                    "risk_level": "low",
                    "estimated_effort": "S",
                }
            ]
        ),
    )

    diagnostics = orchestrator._validate_implementation_planner_output()

    assert diagnostics["valid"] is True


def test_developer_write_tools_are_enabled_for_scoped_implementation(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    monitoring_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    monitoring_file.parent.mkdir(parents=True, exist_ok=True)
    monitoring_file.write_text("pass\n", encoding="utf-8")
    monitoring_test = target_workspace / "tests" / "test_monitoring.py"
    monitoring_test.parent.mkdir(parents=True, exist_ok=True)
    monitoring_test.write_text("def test_monitoring():\n    assert True\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120003",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )
    _seed_implementation_report(
        orchestrator.logger.run_dir,
        "implementation-planner",
        parsed_output=json.dumps(
            [
                {
                    "id": "planner-backend-safe-task",
                    "title": "Implement monitoring service changes",
                    "priority": "P0",
                    "scope": "Edit monitoring service only.",
                    "existing_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
                    "allowed_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
                    "forbidden_paths": ["frontend/*"],
                    "required_test_paths": ["tests/test_monitoring.py"],
                    "depends_on": [],
                    "acceptance_criteria": ["Monitoring service is updated."],
                    "reason_each_path_is_needed": {
                        "gateway-v4/app/services/monitoring.py": "Monitoring implementation target.",
                        "tests/test_monitoring.py": "Required regression test.",
                    },
                    "target_file": {"path": "gateway-v4/app/services/monitoring.py", "action": "update", "purpose": "Add monitoring behavior."},
                    "test_file": {"path": "tests/test_monitoring.py", "action": "update"},
                    "must_contain": ["def record_request(", "response_time_ms"],
                    "must_import": ["from typing import Any"],
                    "integration": ["Provider calls must record metrics."],
                    "reference_files": ["gateway-v4/app/services/monitoring.py"],
                    "reference_excerpts": {"gateway-v4/app/services/monitoring.py": "pass"},
                    "must_test": ["test_record_request_writes_to_db: assert metrics row exists"],
                    "forbidden": ["Do not modify frontend files."],
                    "risk_level": "low",
                    "estimated_effort": "S",
                }
            ]
        ),
    )

    bundle = orchestrator._build_agent_message_bundle(
        "developer",
        {"name": "developer", "description": "Implement the task"},
        Path(".openclaw/agents/implementation/developer/prompt.md"),
        "implementation",
    )

    assert bundle["implementation_retrieval_enabled"] is True
    assert bundle["selected_task_scope"] == "Edit monitoring service only."
    assert bundle["selected_task_scope"] in bundle["system_message"]
    assert bundle["selected_task_id"] == "planner-backend-safe-task"
    assert bundle["selected_task_allowed_paths"] == ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"]
    assert bundle["backlog_task_count"] == 1
    assert bundle["implementation_planner_output_chars"] > 0
    assert '"tool":"write_file"' in bundle["system_message"]
    assert '"tool":"apply_patch"' in bundle["system_message"]
    assert "Developer must produce real file edits via write_file/apply_patch" in bundle["system_message"]
    assert "Не пиши повествовательный текст, объяснения, планы или переводы." in bundle["system_message"]
    assert "`status=implemented`" in bundle["system_message"]
    assert "Russian translation" not in bundle["system_message"]
    assert "Do not implement marketplace." in bundle["system_message"]
    assert "Do not change Stripe or billing flows." in bundle["system_message"]
    assert "Do not make broad frontend changes." in bundle["system_message"]
    assert "[selected-task-contract]" in bundle["system_message"]
    assert "target_file.path: gateway-v4/app/services/monitoring.py" in bundle["system_message"]
    assert "test_file.path: tests/test_monitoring.py" in bundle["system_message"]
    assert "depends_on:" in bundle["system_message"]
    assert "must_contain:" in bundle["system_message"]
    assert "must_test:" in bundle["system_message"]
    assert "Selected task file excerpts" in bundle["system_message"]
    assert "## gateway-v4/app/services/monitoring.py" in bundle["system_message"]
    assert "## tests/test_monitoring.py" in bundle["system_message"]
    assert 'Supported requests are: {"tool":"read_file","path":"relative/path.py"}, {"tool":"read_files","paths":["relative/path.py"]}.' in bundle["system_message"]
    assert "Do not use search_text or list_files in developer implementation mode." in bundle["system_message"]


def test_qa_bundle_blocks_search_and_list_retrieval() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    bundle = orchestrator._build_agent_message_bundle(
        "qa",
        {"name": "qa", "description": "Run checks and report regressions"},
        Path(".openclaw/agents/implementation/qa/prompt.md"),
        "implementation",
    )

    assert "Формат ответа обязателен. Пиши ответ полностью на русском языке." in bundle["system_message"]
    assert "Do not use search_text or list_files in QA mode." in bundle["system_message"]
    assert "Вердикт QA:" in bundle["system_message"]


def test_developer_context_includes_reference_files_for_new_file_parent_directory(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir()
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    main_file = target_workspace / "gateway-v4" / "app" / "main.py"
    main_file.parent.mkdir(parents=True, exist_ok=True)
    main_file.write_text("app = object()\n", encoding="utf-8")
    versions_dir = target_workspace / "gateway-v4" / "alembic" / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)
    (versions_dir / "000_base.py").write_text("def upgrade():\n    pass\n", encoding="utf-8")
    (versions_dir / "000_other.py").write_text("def downgrade():\n    pass\n", encoding="utf-8")
    model_test = target_workspace / "gateway-v4" / "tests" / "test_provider_metrics_model.py"
    model_test.parent.mkdir(parents=True, exist_ok=True)
    model_test.write_text("def test_model():\n    assert True\n", encoding="utf-8")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120099",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- add migration\nrisks:\n- none\ndecisions:\n- backend only\nrecommended_next_tasks:\n- add migration"}],
    )
    _seed_implementation_report(
        orchestrator.logger.run_dir,
        "implementation-planner",
        parsed_output=json.dumps(
            [
                {
                    "id": "task-migration",
                    "title": "Add migration",
                    "priority": "P0",
                    "scope": "Add a migration file.",
                    "existing_paths": ["gateway-v4/app/main.py", "gateway-v4/tests/test_provider_metrics_model.py"],
                    "new_files": ["gateway-v4/alembic/versions/001_add_provider_metrics.py"],
                    "allowed_paths": [
                        "gateway-v4/app/main.py",
                        "gateway-v4/alembic/versions/001_add_provider_metrics.py",
                        "gateway-v4/tests/test_provider_metrics_model.py",
                    ],
                    "forbidden_paths": ["frontend/*"],
                    "required_test_paths": ["gateway-v4/tests/test_provider_metrics_model.py"],
                    "depends_on": [],
                    "reason_each_path_is_needed": {
                        "gateway-v4/app/main.py": "Reference app wiring.",
                        "gateway-v4/alembic/versions/001_add_provider_metrics.py": "Migration target.",
                        "gateway-v4/tests/test_provider_metrics_model.py": "Migration regression test.",
                    },
                    "target_file": {"path": "gateway-v4/alembic/versions/001_add_provider_metrics.py", "action": "create", "purpose": "Add provider metrics migration."},
                    "test_file": {"path": "gateway-v4/tests/test_provider_metrics_model.py", "action": "update"},
                    "must_contain": ["def upgrade()", "def downgrade()"],
                    "must_import": ["alembic.op", "sqlalchemy as sa"],
                    "integration": ["Migration must align with provider metrics data model."],
                    "reference_files": ["gateway-v4/app/main.py"],
                    "reference_excerpts": {"gateway-v4/app/main.py": "app = object()"},
                    "must_test": ["test_provider_metrics_model_migration: assert migration metadata is valid"],
                    "forbidden": ["Do not edit frontend files."],
                    "acceptance_criteria": ["Migration added."],
                    "risk_level": "low",
                    "estimated_effort": "S",
                }
            ]
        ),
    )

    bundle = orchestrator._build_agent_message_bundle(
        "developer",
        {"name": "developer", "description": "Implement the task"},
        Path(".openclaw/agents/implementation/developer/prompt.md"),
        "implementation",
    )

    assert "Selected task file excerpts" in bundle["system_message"]
    assert "### sibling files in gateway-v4/alembic/versions" in bundle["system_message"]
    assert "gateway-v4/alembic/versions/000_base.py" in bundle["system_message"]
    assert "### reference file excerpts from gateway-v4/alembic/versions" in bundle["system_message"]
    assert "## gateway-v4/alembic/versions/000_base.py" in bundle["system_message"]


def test_backlog_is_generated_from_implementation_planner_output(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    monitoring_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    monitoring_file.parent.mkdir(parents=True, exist_ok=True)
    monitoring_file.write_text("pass\n", encoding="utf-8")
    monitoring_test = target_workspace / "tests" / "test_monitoring.py"
    monitoring_test.parent.mkdir(parents=True, exist_ok=True)
    monitoring_test.write_text("def test_monitoring():\n    assert True\n", encoding="utf-8")
    monitoring_test = target_workspace / "tests" / "test_monitoring.py"
    monitoring_test.parent.mkdir(parents=True, exist_ok=True)
    monitoring_test.write_text("def test_monitoring():\n    assert True\n", encoding="utf-8")
    frontend_file = target_workspace / "frontend" / "src" / "App.jsx"
    frontend_file.parent.mkdir(parents=True, exist_ok=True)
    frontend_file.write_text("export default null;\n", encoding="utf-8")
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120010",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )
    _seed_implementation_report(
        orchestrator.logger.run_dir,
        "implementation-planner",
        parsed_output=json.dumps(
            [
                {
                    "id": "p1-frontend",
                    "title": "Frontend follow-up",
                    "priority": "P1",
                    "scope": "Touch frontend.",
                    "existing_paths": ["frontend/src/App.jsx"],
                    "allowed_paths": ["frontend/src/App.jsx"],
                    "forbidden_paths": [],
                    "acceptance_criteria": ["Frontend updated."],
                    "risk_level": "high",
                    "estimated_effort": "M",
                },
                {
                    "id": "p0-backend",
                    "title": "Backend first task",
                    "priority": "P0",
                    "scope": "Touch backend only.",
                    "existing_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
                    "allowed_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
                    "forbidden_paths": [],
                    "required_test_paths": ["tests/test_monitoring.py"],
                    "depends_on": [],
                    "acceptance_criteria": ["Backend updated."],
                    "reason_each_path_is_needed": {
                        "gateway-v4/app/services/monitoring.py": "Backend implementation target.",
                        "tests/test_monitoring.py": "Required regression test.",
                    },
                    "target_file": {"path": "gateway-v4/app/services/monitoring.py", "action": "update", "purpose": "Backend update."},
                    "test_file": {"path": "tests/test_monitoring.py", "action": "update"},
                    "must_contain": ["def record_request(", "response_time_ms"],
                    "must_import": ["from typing import Any"],
                    "integration": ["Provider flow must call record_request."],
                    "reference_files": ["gateway-v4/app/services/monitoring.py"],
                    "reference_excerpts": {"gateway-v4/app/services/monitoring.py": "pass"},
                    "must_test": ["test_record_request_updates_metrics: assert metrics update succeeds"],
                    "forbidden": ["Do not edit frontend files."],
                    "risk_level": "low",
                    "estimated_effort": "S",
                },
            ]
        ),
    )

    selection = orchestrator._prepare_implementation_backlog_selection(require_backlog=True)

    assert selection["error"] == ""
    assert [item["id"] for item in selection["backlog"]] == ["p0-backend", "p1-frontend"]
    assert selection["backlog_source"] == "implementation-planner"
    assert selection["selected_item"]["id"] == "p0-backend"


def test_developer_prompt_receives_only_selected_task_not_raw_architect_plan(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    monitoring_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    monitoring_file.parent.mkdir(parents=True, exist_ok=True)
    monitoring_file.write_text("pass\n", encoding="utf-8")
    monitoring_test = target_workspace / "tests" / "test_monitoring.py"
    monitoring_test.parent.mkdir(parents=True, exist_ok=True)
    monitoring_test.write_text("def test_monitoring():\n    assert True\n", encoding="utf-8")
    monitoring_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    monitoring_file.parent.mkdir(parents=True, exist_ok=True)
    monitoring_file.write_text("pass\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    monitoring_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    monitoring_file.parent.mkdir(parents=True, exist_ok=True)
    monitoring_file.write_text("pass\n", encoding="utf-8")
    frontend_file = target_workspace / "frontend" / "src" / "App.jsx"
    frontend_file.parent.mkdir(parents=True, exist_ok=True)
    frontend_file.write_text("export default null;\n", encoding="utf-8")
    monitoring_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    monitoring_file.parent.mkdir(parents=True, exist_ok=True)
    monitoring_file.write_text("pass\n", encoding="utf-8")
    frontend_file = target_workspace / "frontend" / "src" / "App.jsx"
    frontend_file.parent.mkdir(parents=True, exist_ok=True)
    frontend_file.write_text("export default null;\n", encoding="utf-8")
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120011",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )
    _seed_implementation_report(
        orchestrator.logger.run_dir,
        "architect",
        parsed_output="Broad architect plan: touch frontend/src/App.jsx and gateway-v4/app/services/marketplace.py",
    )
    _seed_implementation_report(
        orchestrator.logger.run_dir,
        "implementation-planner",
        parsed_output=json.dumps(
            [
                {
                    "id": "safe-backend-task",
                    "title": "Safe backend task",
                    "priority": "P0",
                    "scope": "Touch gateway-v4/app/services/monitoring.py only.",
                    "existing_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
                    "allowed_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
                    "forbidden_paths": ["frontend/*", "gateway-v4/app/services/marketplace.py"],
                    "required_test_paths": ["tests/test_monitoring.py"],
                    "depends_on": [],
                    "acceptance_criteria": ["Monitoring file updated."],
                    "reason_each_path_is_needed": {
                        "gateway-v4/app/services/monitoring.py": "Backend implementation target.",
                        "tests/test_monitoring.py": "Required regression test.",
                    },
                    "target_file": {"path": "gateway-v4/app/services/monitoring.py", "action": "update", "purpose": "Implement monitoring changes."},
                    "test_file": {"path": "tests/test_monitoring.py", "action": "update"},
                    "must_contain": ["def record_request(", "response_time_ms"],
                    "must_import": ["from typing import Any"],
                    "integration": ["Provider flow must call record_request after each request."],
                    "reference_files": ["gateway-v4/app/services/monitoring.py"],
                    "reference_excerpts": {"gateway-v4/app/services/monitoring.py": "pass"},
                    "must_test": ["test_record_request_updates_metrics: assert metrics update succeeds"],
                    "forbidden": ["Do not edit frontend files."],
                    "risk_level": "low",
                    "estimated_effort": "S",
                }
            ]
        ),
    )

    bundle = orchestrator._build_agent_message_bundle(
        "developer",
        {"name": "developer", "description": "Implement the task"},
        Path(".openclaw/agents/implementation/developer/prompt.md"),
        "implementation",
    )

    assert "safe-backend-task" in bundle["system_message"]
    assert "gateway-v4/app/services/monitoring.py" in bundle["system_message"]
    assert "Broad architect plan:" not in bundle["system_message"]
    assert "developer_contract:" in bundle["system_message"]


def test_planner_task_normalization_populates_developer_contract() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    normalized, errors = orchestrator._normalize_planner_task(
        {
            "id": "TASK-001",
            "title": "Add migration",
            "priority": "P0",
            "scope": "Create migration.",
            "existing_paths": ["gateway-v4/app/main.py"],
            "new_files": ["gateway-v4/alembic/versions/001_add_provider_metrics.py"],
            "allowed_paths": ["gateway-v4/app/main.py", "gateway-v4/alembic/versions/001_add_provider_metrics.py"],
            "required_test_paths": ["gateway-v4/tests/test_provider_metrics_model.py"],
            "target_file": {
                "path": "gateway-v4/alembic/versions/001_add_provider_metrics.py",
                "action": "create",
                "purpose": "Add alembic migration for provider metrics",
            },
            "must_contain": ["def upgrade()", "def downgrade()"],
            "must_import": ["alembic.op", "sqlalchemy as sa"],
            "integration": ["Migration must align with the provider metrics ORM model."],
            "reference_files": ["gateway-v4/app/main.py"],
            "reference_excerpts": {"gateway-v4/app/main.py": "from fastapi import FastAPI"},
            "test_file": {"path": "gateway-v4/tests/test_provider_metrics_model.py"},
            "must_test": ["Migration table columns match the model."],
            "forbidden": ["modify billing code"],
            "acceptance_criteria": ["Migration exists."],
            "reason_each_path_is_needed": {
                "gateway-v4/app/main.py": "Reference wiring.",
                "gateway-v4/alembic/versions/001_add_provider_metrics.py": "Target migration file.",
            },
            "risk_level": "low",
            "estimated_effort": "S",
        },
        item_index=1,
    )

    assert errors == []
    assert normalized is not None
    assert normalized["target_file"]["path"] == "gateway-v4/alembic/versions/001_add_provider_metrics.py"
    assert normalized["test_file"]["path"] == "gateway-v4/tests/test_provider_metrics_model.py"
    assert normalized["must_contain"] == ["def upgrade()", "def downgrade()"]
    assert normalized["contract_completeness"] is True


def test_implementation_planner_receives_architect_output_after_architect(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    monitoring_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    monitoring_file.parent.mkdir(parents=True, exist_ok=True)
    monitoring_file.write_text("pass\n", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120012",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- PM summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )
    _seed_implementation_report(
        orchestrator.logger.run_dir,
        "architect",
        parsed_output="Architect plan: update gateway-v4/app/services/monitoring.py and add tests.",
    )

    bundle = orchestrator._build_agent_message_bundle(
        "implementation-planner",
        {"name": "implementation-planner", "description": "Convert plan to backlog"},
        Path(".openclaw/agents/implementation/implementation-planner/prompt.md"),
        "implementation",
    )

    assert "Architect plan: update gateway-v4/app/services/monitoring.py and add tests." in bundle["system_message"]
    assert "[product-manager]" in bundle["system_message"]


def test_fallback_backlog_is_generated_from_product_manager_summary_when_planner_missing(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120013",
        [
            {
                "agent_name": "product-manager",
                "handoff_summary": (
                    "agent: product-manager\n"
                    "findings:\n- summary\n"
                    "risks:\n- none\n"
                    "decisions:\n- backend first\n"
                    "recommended_next_tasks:\n"
                    "- [P0] Add monitoring foundation\n"
                    "  scope: Implement monitoring service only\n"
                    "  files: gateway-v4/app/services/monitoring.py\n"
                    "  risk: low\n"
                    "  effort: S\n"
                ),
            }
        ],
    )

    backlog, backlog_source = orchestrator._build_implementation_backlog(
        orchestrator._load_latest_project_research_reports()[0],
        allow_research_fallback=True,
    )

    assert backlog_source == "product-manager-fallback"
    assert backlog[0]["id"] == "product-manager-add-monitoring-foundation"
    assert backlog[0]["scope"] == "Implement monitoring service only"


def test_task_id_selects_planner_backlog_item_non_interactively(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "interactive", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
        selected_task_ref="second-task",
    )
    monitoring_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    monitoring_file.parent.mkdir(parents=True, exist_ok=True)
    monitoring_file.write_text("pass\n", encoding="utf-8")
    proxy_file = target_workspace / "gateway-v4" / "app" / "services" / "proxy.py"
    proxy_file.parent.mkdir(parents=True, exist_ok=True)
    proxy_file.write_text("pass\n", encoding="utf-8")
    first_test = target_workspace / "tests" / "test_monitoring.py"
    first_test.parent.mkdir(parents=True, exist_ok=True)
    first_test.write_text("def test_monitoring():\n    assert True\n", encoding="utf-8")
    second_test = target_workspace / "tests" / "test_proxy.py"
    second_test.write_text("def test_proxy():\n    assert True\n", encoding="utf-8")
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120014",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )
    _seed_implementation_report(
        orchestrator.logger.run_dir,
        "implementation-planner",
        parsed_output=json.dumps(
            [
                {
                        "id": "first-task",
                        "title": "First task",
                        "priority": "P0",
                        "scope": "First scope",
                        "existing_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
                        "allowed_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_monitoring.py"],
                        "depends_on": [],
                        "acceptance_criteria": ["First done"],
                        "reason_each_path_is_needed": {
                            "gateway-v4/app/services/monitoring.py": "First target.",
                            "tests/test_monitoring.py": "First test.",
                        },
                        "target_file": {"path": "gateway-v4/app/services/monitoring.py", "action": "update", "purpose": "First backend update."},
                        "test_file": {"path": "tests/test_monitoring.py", "action": "update"},
                        "must_contain": ["def record_request(", "response_time_ms"],
                        "must_import": ["from typing import Any"],
                        "integration": ["Provider flow uses monitoring."],
                        "reference_files": ["gateway-v4/app/services/monitoring.py"],
                        "reference_excerpts": {"gateway-v4/app/services/monitoring.py": "pass"},
                        "must_test": ["test_record_request_updates_metrics: assert metrics update succeeds"],
                        "forbidden": ["Do not edit frontend files."],
                        "risk_level": "low",
                        "estimated_effort": "S",
                    },
                    {
                        "id": "second-task",
                        "title": "Second task",
                        "priority": "P1",
                        "scope": "Second scope",
                        "existing_paths": ["gateway-v4/app/services/proxy.py", "tests/test_proxy.py"],
                        "allowed_paths": ["gateway-v4/app/services/proxy.py", "tests/test_proxy.py"],
                        "forbidden_paths": [],
                        "required_test_paths": ["tests/test_proxy.py"],
                        "depends_on": [],
                        "acceptance_criteria": ["Second done"],
                        "reason_each_path_is_needed": {
                            "gateway-v4/app/services/proxy.py": "Second target.",
                            "tests/test_proxy.py": "Second test.",
                        },
                        "target_file": {"path": "gateway-v4/app/services/proxy.py", "action": "update", "purpose": "Second backend update."},
                        "test_file": {"path": "tests/test_proxy.py", "action": "update"},
                        "must_contain": ["async def call_provider(", "time.monotonic()"],
                        "must_import": ["import time"],
                        "integration": ["Proxy flow must measure provider timing."],
                        "reference_files": ["gateway-v4/app/services/proxy.py"],
                        "reference_excerpts": {"gateway-v4/app/services/proxy.py": "pass"},
                        "must_test": ["test_call_provider_records_metrics: assert metrics call happens"],
                        "forbidden": ["Do not edit frontend files."],
                        "risk_level": "low",
                        "estimated_effort": "S",
                    },
            ]
        ),
    )

    selection = orchestrator._prepare_implementation_backlog_selection(require_backlog=True)

    assert selection["selected_item"]["id"] == "second-task"
    assert selection["selected_item"]["scope"] == "Second scope"


def test_list_tasks_prints_planner_backlog(capsys, monkeypatch, tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "interactive", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    orchestrator = WorkflowOrchestrator(
        str(config_path),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    monitoring_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    monitoring_file.parent.mkdir(parents=True, exist_ok=True)
    monitoring_file.write_text("pass\n", encoding="utf-8")
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120015",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )
    _seed_implementation_report(
        orchestrator.logger.run_dir,
        "implementation-planner",
        parsed_output=json.dumps(
            [
                {
                    "id": "planner-task",
                    "title": "Planner task",
                    "priority": "P0",
                    "scope": "Planner scope",
                    "allowed_paths": ["gateway-v4/app/services/monitoring.py"],
                    "forbidden_paths": [],
                    "required_test_paths": ["tests/test_monitoring.py"],
                    "acceptance_criteria": ["Planner done"],
                    "risk_level": "low",
                    "estimated_effort": "S",
                }
            ]
        ),
    )

    monkeypatch.setattr(start_module, "WorkflowOrchestrator", lambda **kwargs: orchestrator)

    exit_code = start_module.main(["--config", str(config_path), "--list-tasks"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "Бэклог реализации (источник=implementation-planner)" in output
    assert "planner-task" in output


def test_english_backlog_labels_print_when_console_language_en(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "console_language": "en", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    orchestrator = WorkflowOrchestrator(str(engine_root / "workflow" / "config.yaml"), engine_root=str(engine_root), launch_cwd=str(target_workspace))

    text = orchestrator._format_implementation_backlog(
        [
            {
                "id": "TASK-001",
                "title": "Planner task",
                "priority": "P0",
                "scope": "Backend only.",
                "existing_paths": ["gateway-v4/app/services/proxy.py"],
                "new_directories": [],
                "new_files": ["gateway-v4/tests/test_proxy_metrics.py"],
                "allowed_paths": ["gateway-v4/app/services/proxy.py", "gateway-v4/tests/test_proxy_metrics.py"],
                "required_test_paths": ["gateway-v4/tests/test_proxy_metrics.py"],
                "acceptance_criteria": ["done"],
                "risk_level": "low",
                "estimated_effort": "S",
            }
        ],
        "implementation-planner",
    )

    assert "Implementation backlog (source=implementation-planner)" in text
    assert "existing files: gateway-v4/app/services/proxy.py" in text
    assert "new files: gateway-v4/tests/test_proxy_metrics.py" in text
    assert "required tests: gateway-v4/tests/test_proxy_metrics.py" in text
    assert "TASK-001" in text
    assert "gateway-v4/app/services/proxy.py" in text


def test_russian_backlog_labels_keep_ids_and_paths_unchanged(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "console_language": "ru", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    orchestrator = WorkflowOrchestrator(str(engine_root / "workflow" / "config.yaml"), engine_root=str(engine_root), launch_cwd=str(target_workspace))

    text = orchestrator._format_implementation_backlog(
        [
            {
                "id": "TASK-007",
                "title": "Planner task",
                "priority": "P2",
                "scope": "Backend only.",
                "existing_paths": ["frontend/src/lib/api.js"],
                "new_directories": [],
                "new_files": [],
                "allowed_paths": ["frontend/src/lib/api.js"],
                "required_test_paths": [],
                "acceptance_criteria": ["done"],
                "risk_level": "low",
                "estimated_effort": "1 час",
            }
        ],
        "implementation-planner",
    )

    assert "Бэклог реализации (источник=implementation-planner)" in text
    assert "существующие файлы: frontend/src/lib/api.js" in text
    assert "разрешённые пути: frontend/src/lib/api.js" in text
    assert "обязательные тесты: none" in text
    assert "TASK-007" in text
    assert "frontend/src/lib/api.js" in text


def test_research_handoff_summary_is_capped() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    summary = orchestrator._build_research_handoff_summary("project-analyst", "A" * 5000)
    assert summary.startswith("agent: project-analyst")
    assert "findings:" in summary
    assert "recommended_next_tasks:" in summary
    assert len(summary) <= 2000


def test_research_handoff_summary_uses_deterministic_sections() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    summary = orchestrator._build_research_handoff_summary(
        "tech-analyst",
        "\n".join(
            [
                "Findings:",
                "- The direct_api path is the active executor.",
                "Risks:",
                "- Retrieval rounds can grow prompt size.",
                "Decisions:",
                "- Keep repository tools bounded to the target workspace.",
                "Recommended next tasks:",
                "- Add diagnostics for retrieval rounds.",
                "",
                "Russian translation",
                "РџРµСЂРµРІРѕРґ.",
            ]
        ),
    )

    assert summary == "\n".join(
        [
            "agent: tech-analyst",
            "findings:",
            "- The direct_api path is the active executor.",
            "risks:",
            "- Retrieval rounds can grow prompt size.",
            "decisions:",
            "- Keep repository tools bounded to the target workspace.",
            "recommended_next_tasks:",
            "- Add diagnostics for retrieval rounds.",
        ]
    )


def test_architect_receives_product_manager_summary_from_previous_research_run(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    (target_workspace / "frontend").mkdir()
    (target_workspace / "frontend" / "package.json").write_text('{"name":"frontend"}', encoding="utf-8")
    (target_workspace / "gateway-v4").mkdir()
    (target_workspace / "gateway-v4" / "requirements.txt").write_text("fastapi", encoding="utf-8")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120000",
        [
            {"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- P0 deliver monitoring foundation\nrisks:\n- none\ndecisions:\n- Build provider metrics first\nrecommended_next_tasks:\n- Implement provider performance monitoring"},
            {"agent_name": "project-analyst", "handoff_summary": "agent: project-analyst\nfindings:\n- Target has frontend and gateway-v4\nrisks:\n- no tests\ndecisions:\n- keep scope narrow\nrecommended_next_tasks:\n- add observability"},
            {"agent_name": "tech-analyst", "handoff_summary": "agent: tech-analyst\nfindings:\n- FastAPI backend with React frontend\nrisks:\n- missing monitoring\ndecisions:\n- add routing metrics\nrecommended_next_tasks:\n- add provider telemetry"},
        ],
    )

    bundle = orchestrator._build_agent_message_bundle(
        "architect",
        {"name": "architect", "description": "Prepare technical plan"},
        Path(".openclaw/agents/implementation/architect/prompt.md"),
        "implementation",
    )

    assert "Repository context collected locally:" in bundle["system_message"]
    assert "[product-manager]" in bundle["system_message"]
    assert "[project-analyst]" in bundle["system_message"]
    assert "[tech-analyst]" in bundle["system_message"]
    assert "Build provider metrics first" in bundle["system_message"]
    assert bundle["implementation_context_chars"] > 0
    assert "Do not implement marketplace." in bundle["system_message"]
    assert "Do not change Stripe or billing flows." in bundle["system_message"]


def test_architect_receives_target_project_context(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    (target_workspace / "frontend").mkdir()
    (target_workspace / "frontend" / "package.json").write_text('{"name":"frontend"}', encoding="utf-8")
    (target_workspace / "gateway-v4").mkdir()
    (target_workspace / "gateway-v4" / "requirements.txt").write_text("fastapi", encoding="utf-8")
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120001",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- summary\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )

    bundle = orchestrator._build_agent_message_bundle(
        "architect",
        {"name": "architect", "description": "Prepare technical plan"},
        Path(".openclaw/agents/implementation/architect/prompt.md"),
        "implementation",
    )

    assert "target readme" in bundle["system_message"]
    assert "frontend/package.json" in bundle["system_message"]
    assert "gateway-v4/requirements.txt" in bundle["system_message"]
    assert "Selected implementation scope" in bundle["system_message"]



def test_direct_api_retrieval_reads_local_file_and_requeries(monkeypatch, tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    requests: list[dict[str, object]] = []
    responses = [
        {
            "choices": [
                {
                    "message": {
                        "content": '{"tool":"read_files","paths":["workflow/config.yaml"]}'
                    }
                }
            ],
            "model": "deepseek/deepseek-chat-v3",
        },
        {
            "choices": [
                {
                    "message": {
                        "content": "English summary.\n\nRussian translation\nР СѓСЃСЃРєРёР№ РїРµСЂРµРІРѕРґ."
                    }
                }
            ],
            "model": "deepseek/deepseek-chat-v3",
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        },
    ]

    def fake_urlopen(request, timeout=0):
        requests.append(json.loads(request.data.decode("utf-8")))
        return _FakeHTTPResponse(responses[len(requests) - 1])

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
    assert len(requests) == 2
    assert "workflow/config.yaml" in requests[1]["messages"][-1]["content"]
    payload = json.loads(
        (orchestrator.logger.run_dir / "agents" / "research" / "project-analyst.json").read_text(encoding="utf-8")
    )
    assert payload["retrieval_enabled"] is True
    assert payload["retrieval_rounds"] == 1
    assert payload["repository_context_chars"] > 0
    assert payload["target_workspace"] == str(orchestrator.target_workspace)
    assert payload["project_id"] == orchestrator.project_id


def test_direct_api_retrieval_parses_fenced_json_with_preface() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    payload = orchestrator._parse_direct_api_retrieval_request(
        "I'll inspect the file first.\n\n```json\n{\"tool\":\"read_file\",\"path\":\"gateway-v4/app/main.py\"}\n```"
    )

    assert payload == {"tool": "read_file", "path": "gateway-v4/app/main.py"}


def test_direct_api_retrieval_parses_multiple_xml_read_file_tags() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    payload = orchestrator._parse_direct_api_retrieval_request(
        "I'll inspect both files first.\n\n"
        "<read_file path=\"gateway-v4/tests/test_provider_metrics_model.py\"/>\n"
        "<read_file path=\"gateway-v4/app/main.py\"/>"
    )

    assert payload == {
        "tool": "read_files",
        "paths": [
            "gateway-v4/tests/test_provider_metrics_model.py",
            "gateway-v4/app/main.py",
        ],
    }


def test_direct_api_retrieval_parses_xml_read_files_block() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    payload = orchestrator._parse_direct_api_retrieval_request(
        "<read_files>\n"
        "<paths>\n"
        "<path>gateway-v4/alembic/versions/20240801_add_provider_metrics.py</path>\n"
        "<path>gateway-v4/tests/__init__.py</path>\n"
        "<path>gateway-v4/tests/test_provider_metrics_migration.py</path>\n"
        "</paths>\n"
        "</read_files>"
    )

    assert payload == {
        "tool": "read_files",
        "paths": [
            "gateway-v4/alembic/versions/20240801_add_provider_metrics.py",
            "gateway-v4/tests/__init__.py",
            "gateway-v4/tests/test_provider_metrics_migration.py",
        ],
    }


def test_direct_api_retrieval_parses_function_style_tool_calls() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    payload = orchestrator._parse_direct_api_retrieval_request(
        "I'll inspect the reference files first.\n"
        'read_file({"path": "gateway-v4/app/main.py"})\n'
        'read_file({"path": "gateway-v4/tests/test_provider_metrics_model.py"})\n'
        'list_files({"directory": "gateway-v4/alembic", "max_depth": 2})'
    )

    assert payload == {
        "tool": "read_files",
        "paths": [
            "gateway-v4/app/main.py",
            "gateway-v4/tests/test_provider_metrics_model.py",
        ],
    }


def test_direct_api_retrieval_parses_tool_call_prefixed_calls() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    payload = orchestrator._parse_direct_api_retrieval_request(
        'I will inspect first.\n'
        '<tool_call>read_file(path="gateway-v4/app/main.py")\n'
        '<tool_call>read_file(path="gateway-v4/tests/test_provider_metrics_model.py")\n'
        '<tool_call>list_files(directory="gateway-v4/alembic", max_depth="3")'
    )

    assert payload == {
        "tool": "read_files",
        "paths": [
            "gateway-v4/app/main.py",
            "gateway-v4/tests/test_provider_metrics_model.py",
        ],
    }


def test_direct_api_retrieval_parses_markdown_wrapped_apply_patch() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    payload = orchestrator._parse_direct_api_retrieval_request(
        "Applying the fix now.\n\n```json\n{\"tool\":\"apply_patch\",\"path\":\"gateway-v4/tests/test_provider_metrics_migration.py\",\"search\":\"old\",\"replace\":\"new\"}\n```"
    )

    assert payload == {
        "tool": "apply_patch",
        "path": "gateway-v4/tests/test_provider_metrics_migration.py",
        "search": "old",
        "replace": "new",
    }


def test_direct_api_retrieval_parses_prose_and_escaped_apply_patch() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    payload = orchestrator._parse_direct_api_retrieval_request(
        'I will patch the file now. "{\\"tool\\":\\"apply_patch\\",\\"path\\":\\"gateway-v4/tests/test_provider_metrics_migration.py\\",\\"search\\":\\"old\\",\\"replace\\":\\"new\\"}"'
    )

    assert payload == {
        "tool": "apply_patch",
        "path": "gateway-v4/tests/test_provider_metrics_migration.py",
        "search": "old",
        "replace": "new",
    }


def test_direct_api_retrieval_returns_tool_batch_when_write_is_mixed_with_reads() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    payload = orchestrator._parse_direct_api_retrieval_request(
        'First inspect then patch.\n'
        '{"tool":"read_file","path":"gateway-v4/tests/test_provider_metrics_migration.py"}\n'
        '```json\n{"tool":"apply_patch","path":"gateway-v4/tests/test_provider_metrics_migration.py","search":"old","replace":"new"}\n```'
    )

    assert payload == {
        "tool": "tool_batch",
        "requests": [
            {
                "tool": "read_file",
                "path": "gateway-v4/tests/test_provider_metrics_migration.py",
            },
            {
                "tool": "apply_patch",
                "path": "gateway-v4/tests/test_provider_metrics_migration.py",
                "search": "old",
                "replace": "new",
            },
        ],
    }


def test_extract_write_operations_finds_apply_patch_inside_mixed_output() -> None:
    payloads = WorkflowOrchestrator._extract_write_operations(
        'Done.\n```json\n{"tool":"apply_patch","path":"gateway-v4/tests/test_provider_metrics_migration.py","search":"old","replace":"new"}\n```\nstatus=implemented'
    )

    assert payloads == [
        {
            "tool": "apply_patch",
            "path": "gateway-v4/tests/test_provider_metrics_migration.py",
            "search": "old",
            "replace": "new",
        }
    ]


def test_direct_api_retrieval_cannot_escape_target_workspace(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    outside_file = tmp_path / "secret.txt"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    outside_file.write_text("secret", encoding="utf-8")
    (target_workspace / "inside.txt").write_text("inside", encoding="utf-8")
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )

    assert orchestrator._direct_api_read_files(["../secret.txt"], limit=2000) == ""
    assert orchestrator._direct_api_list_files("..", max_depth=2) == ""
    assert "inside.txt" in orchestrator._direct_api_list_files(".", max_depth=2)


def test_developer_search_text_is_blocked_when_exact_task_context_exists(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir()
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator._selected_implementation_item = {
        "existing_paths": ["gateway-v4/app/main.py"],
        "new_files": ["gateway-v4/alembic/versions/001_add_provider_metrics.py"],
        "allowed_paths": ["gateway-v4/app/main.py", "gateway-v4/alembic/versions/001_add_provider_metrics.py"],
    }

    result = orchestrator._execute_direct_api_retrieval_request(
        {"tool": "search_text", "pattern": "alembic"},
        phase="implementation",
        agent_name="developer",
    )

    assert "search_text is disabled for developer in implementation mode" in result


def test_developer_list_files_is_blocked_in_implementation_mode(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir()
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator._selected_implementation_item = {
        "existing_paths": ["gateway-v4/app/main.py"],
        "new_files": ["gateway-v4/alembic/versions/001_add_provider_metrics.py"],
        "allowed_paths": ["gateway-v4/app/main.py", "gateway-v4/alembic/versions/001_add_provider_metrics.py"],
    }

    result = orchestrator._execute_direct_api_retrieval_request(
        {"tool": "list_files", "directory": "gateway-v4/alembic", "max_depth": 2},
        phase="implementation",
        agent_name="developer",
    )

    assert "list_files is disabled for developer in implementation mode" in result


def test_developer_write_file_changes_target_file(monkeypatch, tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    target_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("initial\n", encoding="utf-8")
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    _ensure_temp_agent_prompt(engine_root, "implementation", "developer")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120010",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- implement monitoring\nrisks:\n- none\ndecisions:\n- backend only\nrecommended_next_tasks:\n- edit monitoring file"}],
    )

    responses = [
        {"choices": [{"message": {"content": '{"tool":"write_file","path":"gateway-v4/app/services/monitoring.py","content":"updated\\n"}'}}], "model": "anthropic/claude-sonnet-4.6"},
        {"choices": [{"message": {"content": "status=implemented"}}], "model": "anthropic/claude-sonnet-4.6", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]
    calls: list[dict[str, object]] = []

    def fake_urlopen(request, timeout=0):
        calls.append(json.loads(request.data.decode("utf-8")))
        return _FakeHTTPResponse(responses[len(calls) - 1])

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setattr(orchestrator_module.urllib_request, "urlopen", fake_urlopen)

    ok = orchestrator._run_agent(
        {"name": "developer", "description": "Implement the task", "timeout": 5, "provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6"},
        "implementation",
    )

    assert ok is True
    assert target_file.read_text(encoding="utf-8") == "updated\n"
    payload = json.loads((orchestrator.logger.run_dir / "agents" / "implementation" / "developer.json").read_text(encoding="utf-8"))
    assert payload["write_tools_used"] == ["write_file"]
    assert payload["parsed_output"] == "status=implemented"


def test_developer_apply_patch_changes_target_file(monkeypatch, tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    target_file = target_workspace / "gateway-v4" / "app" / "services" / "proxy.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("return old_value\n", encoding="utf-8")
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    _ensure_temp_agent_prompt(engine_root, "implementation", "developer")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120011",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- patch proxy\nrisks:\n- none\ndecisions:\n- backend only\nrecommended_next_tasks:\n- patch proxy file"}],
    )

    responses = [
        {"choices": [{"message": {"content": '{"tool":"apply_patch","path":"gateway-v4/app/services/proxy.py","search":"old_value","replace":"new_value"}'}}], "model": "anthropic/claude-sonnet-4.6"},
        {"choices": [{"message": {"content": "status=implemented"}}], "model": "anthropic/claude-sonnet-4.6", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]
    calls: list[dict[str, object]] = []

    def fake_urlopen(request, timeout=0):
        calls.append(json.loads(request.data.decode("utf-8")))
        return _FakeHTTPResponse(responses[len(calls) - 1])

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setattr(orchestrator_module.urllib_request, "urlopen", fake_urlopen)

    ok = orchestrator._run_agent(
        {"name": "developer", "description": "Implement the task", "timeout": 5, "provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6"},
        "implementation",
    )

    assert ok is True
    assert "new_value" in target_file.read_text(encoding="utf-8")
    payload = json.loads((orchestrator.logger.run_dir / "agents" / "implementation" / "developer.json").read_text(encoding="utf-8"))
    assert payload["write_tools_used"] == ["apply_patch"]
    assert payload["parsed_output"] == "status=implemented"


def test_developer_prose_without_write_tools_becomes_no_changes(monkeypatch, tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    target_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("initial\n", encoding="utf-8")
    _ensure_temp_agent_prompt(engine_root, "implementation", "developer")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120010",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- implement monitoring\nrisks:\n- none\ndecisions:\n- backend only\nrecommended_next_tasks:\n- edit monitoring file"}],
    )

    response = {"choices": [{"message": {"content": "Implementation plan: inspect code, then update monitoring."}}], "model": "anthropic/claude-sonnet-4.6", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setattr(orchestrator_module.urllib_request, "urlopen", lambda request, timeout=0: _FakeHTTPResponse(response))

    ok = orchestrator._run_agent(
        {"name": "developer", "description": "Implement the task", "timeout": 5, "provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6"},
        "implementation",
    )

    assert ok is False
    payload = json.loads((orchestrator.logger.run_dir / "agents" / "implementation" / "developer.json").read_text(encoding="utf-8"))
    assert payload["status"] == "no_changes"
    assert payload["result"] == "Developer must use write_file/apply_patch or return status=no_changes: <reason>."
    assert payload["parsed_output"] == "status=no_changes: protocol_violation"


def test_developer_status_implemented_without_write_triggers_repair(monkeypatch, tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    target_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("initial\n", encoding="utf-8")
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    _ensure_temp_agent_prompt(engine_root, "implementation", "developer")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120012",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- implement monitoring\nrisks:\n- none\ndecisions:\n- backend only\nrecommended_next_tasks:\n- edit monitoring file"}],
    )

    responses = [
        {"choices": [{"message": {"content": "status=implemented"}}], "model": "anthropic/claude-sonnet-4.6", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
        {"choices": [{"message": {"content": '{"tool":"write_file","path":"gateway-v4/app/services/monitoring.py","content":"updated\\n"}'}}], "model": "anthropic/claude-sonnet-4.6", "usage": {"prompt_tokens": 12, "completion_tokens": 6, "total_tokens": 18}},
    ]
    calls: list[dict[str, object]] = []

    def fake_urlopen(request, timeout=0):
        calls.append(json.loads(request.data.decode("utf-8")))
        return _FakeHTTPResponse(responses[len(calls) - 1])

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setattr(orchestrator_module.urllib_request, "urlopen", fake_urlopen)

    ok = orchestrator._run_agent(
        {"name": "developer", "description": "Implement the task", "timeout": 5, "provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6"},
        "implementation",
    )

    assert ok is True
    assert target_file.read_text(encoding="utf-8") == "updated\n"
    payload = json.loads((orchestrator.logger.run_dir / "agents" / "implementation" / "developer.json").read_text(encoding="utf-8"))
    assert payload["write_tools_used"] == ["write_file"]
    assert payload["parsed_output"] == "status=implemented"
    assert len(calls) == 2


def test_developer_failed_write_gets_repair_turn_with_validator_error(monkeypatch, tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    target_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("initial\n", encoding="utf-8")
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    _ensure_temp_agent_prompt(engine_root, "implementation", "developer")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120013",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- implement monitoring\nrisks:\n- none\ndecisions:\n- backend only\nrecommended_next_tasks:\n- edit monitoring file"}],
    )

    responses = [
        {"choices": [{"message": {"content": '{"tool":"read_file","path":"gateway-v4/app/services/monitoring.py"}'}}], "model": "anthropic/claude-sonnet-4.6"},
        {"choices": [{"message": {"content": '{"tool":"read_file","path":"README.md"}'}}], "model": "anthropic/claude-sonnet-4.6"},
        {"choices": [{"message": {"content": '{"tool":"read_files","paths":["gateway-v4/app/services/monitoring.py","README.md"]}'}}], "model": "anthropic/claude-sonnet-4.6"},
        {"choices": [{"message": {"content": '{"tool":"write_file","path":"gateway-v4/app/services/not_allowed.py","content":"bad\\n"}'}}], "model": "anthropic/claude-sonnet-4.6"},
        {"choices": [{"message": {"content": '{"tool":"write_file","path":"gateway-v4/app/services/monitoring.py","content":"updated\\n"}'}}], "model": "anthropic/claude-sonnet-4.6"},
        {"choices": [{"message": {"content": "status=implemented"}}], "model": "anthropic/claude-sonnet-4.6", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]
    calls: list[dict[str, object]] = []

    def fake_urlopen(request, timeout=0):
        calls.append(json.loads(request.data.decode("utf-8")))
        return _FakeHTTPResponse(responses[len(calls) - 1])

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setattr(orchestrator_module.urllib_request, "urlopen", fake_urlopen)

    ok = orchestrator._run_agent(
        {"name": "developer", "description": "Implement the task", "timeout": 5, "provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6"},
        "implementation",
    )

    assert ok is True
    assert target_file.read_text(encoding="utf-8") == "updated\n"
    payload = json.loads((orchestrator.logger.run_dir / "agents" / "implementation" / "developer.json").read_text(encoding="utf-8"))
    assert payload["write_tools_used"] == ["write_file"]
    assert payload["failed_write_attempts"] == 1
    repair_messages = [message["content"] for message in calls[4]["messages"] if message["role"] == "user"]
    assert any("Validator error:" in str(message) for message in repair_messages)
    assert any("not_allowed.py" in str(message) for message in repair_messages)


def test_developer_list_files_triggers_exact_path_repair_turn(monkeypatch, tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    target_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_text("initial\n", encoding="utf-8")
    test_file = target_workspace / "tests" / "test_monitoring.py"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    test_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _ensure_temp_agent_prompt(engine_root, "implementation", "developer")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120014",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- implement monitoring\nrisks:\n- none\ndecisions:\n- backend only\nrecommended_next_tasks:\n- edit monitoring file"}],
    )
    orchestrator._selected_implementation_item = {
        "id": "TASK-001",
        "allowed_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
        "existing_paths": ["gateway-v4/app/services/monitoring.py", "tests/test_monitoring.py"],
        "target_file": {"path": "gateway-v4/app/services/monitoring.py"},
        "test_file": {"path": "tests/test_monitoring.py"},
    }

    responses = [
        {"choices": [{"message": {"content": '{"tool":"read_file","path":"gateway-v4/app/services/monitoring.py"}'}}], "model": "anthropic/claude-sonnet-4.6"},
        {"choices": [{"message": {"content": '{"tool":"list_files","directory":"gateway-v4/app/services","max_depth":2}'}}], "model": "anthropic/claude-sonnet-4.6"},
        {"choices": [{"message": {"content": '{"tool":"write_file","path":"gateway-v4/app/services/monitoring.py","content":"updated\\n"}'}}], "model": "anthropic/claude-sonnet-4.6"},
        {"choices": [{"message": {"content": "status=implemented"}}], "model": "anthropic/claude-sonnet-4.6", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    ]
    calls: list[dict[str, object]] = []

    def fake_urlopen(request, timeout=0):
        calls.append(json.loads(request.data.decode("utf-8")))
        return _FakeHTTPResponse(responses[len(calls) - 1])

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setattr(orchestrator_module.urllib_request, "urlopen", fake_urlopen)

    ok = orchestrator._run_agent(
        {"name": "developer", "description": "Implement the task", "timeout": 5, "provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6"},
        "implementation",
    )

    assert ok is True
    assert target_file.read_text(encoding="utf-8") == "updated\n"
    repair_messages = [message["content"] for message in calls[2]["messages"] if message["role"] == "user"]
    assert any("Do not use list_files or search_text again." in str(message) for message in repair_messages)
    assert any("gateway-v4/app/services/monitoring.py" in str(message) for message in repair_messages)


def test_developer_write_cannot_escape_target_workspace(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    outside_file = tmp_path / "secret.txt"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    outside_file.write_text("secret", encoding="utf-8")
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )

    result = orchestrator._execute_direct_api_retrieval_request(
        {"tool": "write_file", "path": "../secret.txt", "content": "changed"},
        phase="implementation",
        agent_name="developer",
    )

    assert "escapes target_workspace" in result
    assert outside_file.read_text(encoding="utf-8") == "secret"


def test_developer_write_forbidden_path_is_blocked(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )

    result = orchestrator._execute_direct_api_retrieval_request(
        {"tool": "write_file", "path": "frontend/src/App.jsx", "content": "changed"},
        phase="implementation",
        agent_name="developer",
    )

    assert "violates implementation scope policy" in result


def test_no_git_diff_after_developer_marks_no_changes(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow").mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    repo = git.Repo.init(target_workspace)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test")
        writer.set_value("user", "email", "test@example.com")
    file_path = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("pass\n", encoding="utf-8")
    repo.index.add([str(file_path.relative_to(target_workspace)).replace("\\", "/")])
    repo.index.commit("init")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    orchestrator.logger.save_agent_report(
        "implementation",
        "developer",
        {"status": "success", "result": "completed", "elapsed_s": 1, "returncode": 0, "message": "prompt", "stdout": "", "stderr": "", "parsed_output": "status=no_changes", "usage": {}},
    )

    ok = orchestrator._enforce_implementation_scope_diff()

    assert ok is False
    payload = json.loads((orchestrator.logger.run_dir / "agents" / "implementation" / "developer.json").read_text(encoding="utf-8"))
    assert payload["status"] == "no_changes"
    assert payload["no_changes_detected"] is True


def test_qa_fails_if_no_diff_exists(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    _ensure_temp_agent_prompt(engine_root, "implementation", "qa")
    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120012",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- qa should inspect diff\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )

    ok = orchestrator._run_agent(
        {"name": "qa", "description": "Run checks", "timeout": 5, "provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6"},
        "implementation",
    )

    assert ok is False
    payload = json.loads((orchestrator.logger.run_dir / "agents" / "implementation" / "qa.json").read_text(encoding="utf-8"))
    assert payload["status"] == "no_changes"


def test_qa_receives_git_diff_when_changes_exist(tmp_path: Path) -> None:
    engine_root = tmp_path / "engine"
    target_workspace = tmp_path / "target"
    (engine_root / "workflow").mkdir(parents=True)
    target_workspace.mkdir(parents=True)
    (engine_root / "workflow" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {"executor": "direct_api", "mode": "auto", "require_registry_preflight": False, "require_model_list_preflight": False},
                "project": {"name": "agents-pipeline", "workspace": ".", "default_branch": "main"},
                "paths": {"agents_dir": ".openclaw/agents", "logs_dir": ".openclaw/logs", "feedback_dir": ".openclaw/feedback"},
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {"enabled": False, "branch_prefix": "feature/", "auto_rollback": True},
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    repo = git.Repo.init(target_workspace)
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Test")
        writer.set_value("user", "email", "test@example.com")
    diff_file = target_workspace / "gateway-v4" / "app" / "services" / "monitoring.py"
    diff_file.parent.mkdir(parents=True, exist_ok=True)
    diff_file.write_text("before\n", encoding="utf-8")
    repo.index.add([str(diff_file.relative_to(target_workspace)).replace("\\", "/")])
    repo.index.commit("init")
    diff_file.write_text("after\n", encoding="utf-8")
    (target_workspace / "README.md").write_text("target readme", encoding="utf-8")
    _ensure_temp_agent_prompt(engine_root, "implementation", "qa")

    orchestrator = WorkflowOrchestrator(
        str(engine_root / "workflow" / "config.yaml"),
        engine_root=str(engine_root),
        launch_cwd=str(target_workspace),
    )
    _seed_research_run(
        orchestrator.logger.log_dir,
        "20260101_120013",
        [{"agent_name": "product-manager", "handoff_summary": "agent: product-manager\nfindings:\n- qa should inspect diff\nrisks:\n- none\ndecisions:\n- none\nrecommended_next_tasks:\n- none"}],
    )

    bundle = orchestrator._build_agent_message_bundle(
        "qa",
        {"name": "qa", "description": "Run checks and report regressions"},
        Path(".openclaw/agents/implementation/qa/prompt.md"),
        "implementation",
    )

    assert "Target git diff" in bundle["system_message"]
    assert "-before" in bundle["system_message"] or "+after" in bundle["system_message"]


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
            "parsed_output": "English summary.\n\nRussian translation\nР СѓСЃСЃРєРёР№ РїРµСЂРµРІРѕРґ.",
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


def test_project_analyst_context_is_prioritized_for_later_research_agents(tmp_path: Path) -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")
    orchestrator.runtime.executor = "openclaw"
    orchestrator.logger = WorkflowLogger(log_dir=str(tmp_path / "logs"))
    long_text = "B" * 3800

    for agent_name in ("competitor-analyst", "innovation-scout", "market-analyst"):
        orchestrator.logger.save_agent_report(
            "research",
            agent_name,
            {
                "status": "success",
                "result": "completed",
                "elapsed_s": 12.3,
                "returncode": 0,
                "message": "prompt",
                "stdout": "",
                "stderr": "",
                "parsed_output": long_text,
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
            },
        )

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
            "parsed_output": "Critical repository findings.\n\nRussian translation\nРљР»СЋС‡РµРІС‹Рµ РІС‹РІРѕРґС‹ РїРѕ СЂРµРїРѕР·РёС‚РѕСЂРёСЋ.",
            "runtime": {
                "provider": "openrouter",
                "model": "openrouter/anthropic/claude-sonnet-4.6",
                "thinking": "low",
            },
        },
    )

    context = orchestrator._build_previous_agent_context("research", "product-manager")

    assert "[project-analyst]" in context
    assert "Critical repository findings." in context
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
            "parsed_output": "English summary.\n\nRussian translation\nР СѓСЃСЃРєРёР№ РїРµСЂРµРІРѕРґ.",
            "runtime": {"provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.6", "thinking": "low"},
            "usage": {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150, "estimated_cost_usd": 0.00105, "usage_status": "captured"},
        },
    )
    orchestrator.logger.save_agent_report(
        "research",
        "market-analyst",
        {
            "status": "invalid_output",
            "result": "malformed structured output",
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


def test_extract_usage_estimates_cost_for_dated_model_alias() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    stdout = json.dumps(
        {
            "output_text": "English summary.\n\nRussian translation\nПеревод.",
            "model": "deepseek/deepseek-v4-pro-20260423",
            "provider": "openrouter",
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        },
        ensure_ascii=False,
    )

    usage = orchestrator._extract_usage(
        stdout,
        {"provider": "openrouter", "model": "openrouter/deepseek/deepseek-v4-pro", "thinking": "low"},
    )

    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 500
    assert usage["total_tokens"] == 1500
    assert usage["estimated_cost_usd"] == 0.00087
    assert usage["usage_status"] == "captured"


def test_extract_usage_estimates_cost_for_dated_claude_45_alias() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    stdout = json.dumps(
        {
            "output_text": "outline",
            "model": "anthropic/claude-4.5-sonnet-20250929",
            "provider": "openrouter",
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
        },
        ensure_ascii=False,
    )

    usage = orchestrator._extract_usage(
        stdout,
        {"provider": "openrouter", "model": "openrouter/anthropic/claude-sonnet-4.5", "thinking": "low"},
    )

    assert usage["input_tokens"] == 1000
    assert usage["output_tokens"] == 500
    assert usage["total_tokens"] == 1500
    assert usage["estimated_cost_usd"] == 0.0105
    assert usage["usage_status"] == "captured"


def test_parse_implementation_planner_output_salvages_complete_tasks_from_truncated_fenced_yaml() -> None:
    orchestrator = WorkflowOrchestrator("workflow/config.yaml")

    text = """```yaml
tasks:
  - id: TASK-001
    title: Example one
    priority: P0
    scope: backend-only
    existing_paths:
      - gateway-v4/app/main.py
    new_directories:
      - gateway-v4/tests
    new_files:
      - gateway-v4/tests/__init__.py
      - gateway-v4/tests/test_one.py
    allowed_paths:
      - gateway-v4/app/main.py
      - gateway-v4/tests/__init__.py
      - gateway-v4/tests/test_one.py
    forbidden_paths: []
    required_test_paths:
      - gateway-v4/tests/test_one.py
    acceptance_criteria:
      - test exists
    reason_each_path_is_needed:
      gateway-v4/app/main.py: reference
      gateway-v4/tests/__init__.py: marker
      gateway-v4/tests/test_one.py: test
    target_file:
      path: gateway-v4/tests/test_one.py
      action: create
      purpose: Create test
    reference_files:
      - gateway-v4/app/main.py
    depends_on: []
    risk_level: low
    estimated_effort: S
  - id: TASK-002
    title: Broken
    priority: P1
    scope: backend-only
    existing_paths:
      - gateway-v4/app/main.py
    new_files:
      - gateway-v4/tests/test_two.py
    allowed_paths:
      - gateway-v4/app/main.py
      - gateway-v4/tests/test_two"""

    parsed = orchestrator._parse_implementation_planner_output(text)

    assert parsed["parse_error"] == ""
    assert [item["id"] for item in parsed["items"]][:1] == ["TASK-001"]


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

