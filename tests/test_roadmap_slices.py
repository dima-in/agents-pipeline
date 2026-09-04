import json
from pathlib import Path

import yaml
from start import build_parser
from workflow.orchestrator import WorkflowOrchestrator


def _write_config(config_path: Path, workspace: str = ".") -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "workflow": {
                    "executor": "direct_api",
                    "mode": "auto",
                    "console_language": "ru",
                    "max_phase_cost_usd": None,
                    "default_implementation_scope": "Implement one small, safe backend change.",
                    "require_registry_preflight": False,
                    "require_model_list_preflight": False,
                },
                "project": {"name": "agents-pipeline", "workspace": workspace, "default_branch": "main"},
                "paths": {
                    "agents_dir": ".openclaw/agents",
                    "logs_dir": ".openclaw/logs",
                    "feedback_dir": ".openclaw/feedback",
                },
                "phases": {},
                "runtime": {"provider": "openrouter", "model": "perplexity/sonar", "thinking": "low"},
                "git": {
                    "enabled": False,
                    "branch_prefix": "feature/",
                    "auto_rollback": True,
                    "rollback_dirty_strategy": "fail",
                },
                "logging": {"level": "INFO", "console": False, "file": False, "json": False},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _make_orch(tmp_path: Path) -> WorkflowOrchestrator:
    engine_root = tmp_path / "engine"
    target = tmp_path / "target"
    target.mkdir(parents=True)
    config_path = engine_root / "workflow" / "config.yaml"
    _write_config(config_path)
    return WorkflowOrchestrator(str(config_path), engine_root=str(engine_root), launch_cwd=str(target))


def _roadmap(slices: list[dict]) -> dict:
    return {"vision": "assistant for the business", "slices": slices}


# --------------------------------------------------------------------------
# JSON extraction
# --------------------------------------------------------------------------
def test_extract_first_json_object_ignores_prose_and_braces(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    text = (
        "Here is the roadmap you asked for:\n"
        '```json\n{"vision": "make it {great}", "slices": [{"id": "SLICE-001"}]}\n```\n'
        "That is all."
    )
    parsed = orch._extract_first_json_object(text)
    assert parsed is not None
    assert parsed["vision"] == "make it {great}"
    assert parsed["slices"][0]["id"] == "SLICE-001"


def test_extract_first_json_object_returns_none_for_prose(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    assert orch._extract_first_json_object("no json here at all") is None


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------
def test_normalize_roadmap_fills_defaults_and_dedups(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    payload = {
        "vision": "v",
        "slices": [
            {"id": "SLICE-001", "title": "A", "goal": "do A"},
            {"id": "SLICE-001", "title": "dup", "goal": "dup"},
            {"title": "no id", "goal": "do B"},
        ],
    }
    roadmap = orch._normalize_roadmap(payload)
    ids = [s["id"] for s in roadmap["slices"]]
    # dup SLICE-001 is dropped; the id-less slice is auto-named by its position (index 3).
    assert ids == ["SLICE-001", "SLICE-003"]
    first = roadmap["slices"][0]
    assert first["status"] == "pending"
    assert first["value"] == "medium"
    assert first["effort"] == "M"
    assert first["depends_on"] == []


def test_normalize_roadmap_preserves_progress(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    previous = _roadmap(
        [
            {"id": "SLICE-001", "title": "A", "goal": "do A", "status": "done"},
            {"id": "SLICE-002", "title": "B", "goal": "do B", "status": "in_progress"},
        ]
    )
    payload = {
        "slices": [
            {"id": "SLICE-001", "title": "A", "goal": "do A", "status": "pending"},
            {"id": "SLICE-002", "title": "B", "goal": "do B", "status": "pending"},
            {"id": "SLICE-003", "title": "C", "goal": "do C", "status": "pending"},
        ]
    }
    roadmap = orch._normalize_roadmap(payload, previous=previous)
    by_id = {s["id"]: s for s in roadmap["slices"]}
    assert by_id["SLICE-001"]["status"] == "done"
    assert by_id["SLICE-002"]["status"] == "in_progress"
    assert by_id["SLICE-003"]["status"] == "pending"


# --------------------------------------------------------------------------
# Slice selection
# --------------------------------------------------------------------------
def test_select_next_slice_resumes_in_progress(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    roadmap = _roadmap(
        [
            {"id": "SLICE-001", "status": "done", "depends_on": []},
            {"id": "SLICE-002", "status": "in_progress", "depends_on": ["SLICE-001"]},
            {"id": "SLICE-003", "status": "pending", "depends_on": ["SLICE-002"]},
        ]
    )
    assert orch._select_next_slice(roadmap)["id"] == "SLICE-002"


def test_select_next_slice_respects_dependencies(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    roadmap = _roadmap(
        [
            {"id": "SLICE-001", "status": "pending", "depends_on": []},
            {"id": "SLICE-002", "status": "pending", "depends_on": ["SLICE-001"]},
        ]
    )
    assert orch._select_next_slice(roadmap)["id"] == "SLICE-001"


def test_select_next_slice_blocked_returns_none(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    roadmap = _roadmap(
        [
            {"id": "SLICE-001", "status": "done", "depends_on": []},
            {"id": "SLICE-002", "status": "pending", "depends_on": ["SLICE-099"]},
        ]
    )
    assert orch._select_next_slice(roadmap) is None


def test_select_next_slice_all_done_returns_none(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    roadmap = _roadmap([{"id": "SLICE-001", "status": "done", "depends_on": []}])
    assert orch._select_next_slice(roadmap) is None


# --------------------------------------------------------------------------
# Capture from strategist report
# --------------------------------------------------------------------------
def test_capture_roadmap_from_strategist_persists(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    strategist_json = json.dumps(
        {
            "vision": "assistant for the business",
            "slices": [
                {"id": "SLICE-001", "title": "Q&A", "goal": "ask questions", "status": "done"},
                {"id": "SLICE-002", "title": "NL order", "goal": "enter orders in natural language",
                 "depends_on": ["SLICE-001"], "status": "pending"},
            ],
        }
    )
    orch._overwrite_agent_report(
        "research",
        "product-strategist",
        {"status": "success", "parsed_output": f"prose\n{strategist_json}\nmore prose"},
    )
    roadmap = orch._capture_roadmap_from_strategist()
    assert orch.roadmap_path.exists()
    assert len(roadmap["slices"]) == 2
    persisted = json.loads(orch.roadmap_path.read_text(encoding="utf-8"))
    assert persisted["slices"][1]["id"] == "SLICE-002"
    assert persisted["slices"][1]["depends_on"] == ["SLICE-001"]


def test_capture_roadmap_ignores_report_without_slices(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    orch._overwrite_agent_report(
        "research",
        "product-strategist",
        {"status": "success", "parsed_output": "I could not decide on slices."},
    )
    assert orch._capture_roadmap_from_strategist() == {}
    assert not orch.roadmap_path.exists()


# --------------------------------------------------------------------------
# Backlog completeness
# --------------------------------------------------------------------------
def test_active_backlog_is_complete(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    orch.canonical_backlog_path.parent.mkdir(parents=True, exist_ok=True)
    orch.canonical_backlog_path.write_text(
        json.dumps({"tasks": [{"id": "TASK-1"}, {"id": "TASK-2"}], "selected_task_id": "TASK-1"}),
        encoding="utf-8",
    )
    orch.project_settings["completed_implementation_tasks"] = ["TASK-1"]
    assert orch._active_backlog_is_complete() is False
    orch._canonical_backlog_payload = {}
    orch.project_settings["completed_implementation_tasks"] = ["TASK-1", "TASK-2"]
    assert orch._active_backlog_is_complete() is True


def test_active_backlog_is_complete_empty_backlog(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    assert orch._active_backlog_is_complete() is False


# --------------------------------------------------------------------------
# Slice driver
# --------------------------------------------------------------------------
def test_run_next_slice_without_roadmap_returns_false(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    assert orch.run_next_slice() is False


def test_run_next_slice_starts_first_pending(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    orch._persist_roadmap(
        orch._normalize_roadmap(
            _roadmap(
                [
                    {"id": "SLICE-001", "title": "Q&A", "goal": "ask questions", "status": "done"},
                    {"id": "SLICE-002", "title": "NL order", "goal": "enter orders naturally",
                     "depends_on": ["SLICE-001"], "status": "pending"},
                ]
            )
        )
    )
    calls: list[bool] = []
    orch.run_implementation_phase = lambda: (calls.append(True) or True)  # type: ignore[assignment]

    assert orch.run_next_slice() is True
    assert calls == [True]
    assert orch.user_goal == "enter orders naturally"
    assert orch.project_settings.get("active_slice_id") == "SLICE-002"
    persisted = json.loads(orch.roadmap_path.read_text(encoding="utf-8"))
    by_id = {s["id"]: s for s in persisted["slices"]}
    assert by_id["SLICE-002"]["status"] == "in_progress"


def test_run_next_slice_closes_completed_slice(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    orch._persist_roadmap(
        orch._normalize_roadmap(
            _roadmap(
                [
                    {"id": "SLICE-001", "title": "Q&A", "goal": "ask questions",
                     "status": "in_progress", "depends_on": []},
                ]
            )
        )
    )
    orch.project_settings["active_slice_id"] = "SLICE-001"
    orch.canonical_backlog_path.parent.mkdir(parents=True, exist_ok=True)
    orch.canonical_backlog_path.write_text(
        json.dumps({"tasks": [{"id": "TASK-1"}], "selected_task_id": "TASK-1"}), encoding="utf-8"
    )
    orch.project_settings["completed_implementation_tasks"] = ["TASK-1"]
    orch.run_implementation_phase = lambda: True  # type: ignore[assignment]

    assert orch.run_next_slice() is True
    persisted = json.loads(orch.roadmap_path.read_text(encoding="utf-8"))
    assert persisted["slices"][0]["status"] == "done"
    assert orch.project_settings.get("active_slice_id") in ("", None)


# --------------------------------------------------------------------------
# print_roadmap + CLI
# --------------------------------------------------------------------------
def test_print_roadmap_without_roadmap_returns_1(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    assert orch.print_roadmap() == 1


def test_print_roadmap_with_roadmap_returns_0(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    orch._persist_roadmap(orch._normalize_roadmap(_roadmap([{"id": "SLICE-001", "title": "A", "goal": "do A"}])))
    assert orch.print_roadmap() == 0


def test_parser_accepts_slice_flags() -> None:
    args = build_parser().parse_args(["--next-slice"])
    assert args.next_slice is True
    args = build_parser().parse_args(["--list-slices"])
    assert args.list_slices is True
    args = build_parser().parse_args(["--build-roadmap"])
    assert args.build_roadmap is True


def test_archive_slice_clears_completed_settings(tmp_path: Path) -> None:
    # Regression: generic task ids (TASK-001...) collide across slices, so a prior slice's
    # completed ids in settings.yaml must be parked+cleared when a new slice starts, else the
    # new slice's first tasks look already-done and selection skips to a mid-chain task.
    orch = _make_orch(tmp_path)
    orch.project_settings["completed_implementation_tasks"] = ["TASK-001", "TASK-002", "TASK-003"]
    orch.canonical_backlog_path.parent.mkdir(parents=True, exist_ok=True)
    orch.canonical_backlog_path.write_text(
        json.dumps({"tasks": [{"id": "TASK-001"}], "selected_task_id": "TASK-001"}), encoding="utf-8"
    )
    orch._archive_backlog_for_new_slice("SLICE-002")
    assert orch.project_settings["completed_implementation_tasks"] == []
    assert orch.project_settings["completed_implementation_tasks.SLICE-002"] == ["TASK-001", "TASK-002", "TASK-003"]
    # and the canonical backlog file was parked (fresh backlog for the new slice)
    assert not orch.canonical_backlog_path.exists()


def test_frontend_only_contract_passes_test_and_jsx_gates(tmp_path: Path) -> None:
    # Regression: a frontend-only React contract carries no pytest requirement and uses JSX/JS
    # anchors as must_contain. It must not be rejected for a stray test file or "vague" JSX.
    orch = _make_orch(tmp_path)
    item = {
        "id": "TASK-004",
        "title": "Add frontend NLP order input component",
        "scope": "frontend-only",
        "allowed_paths": ["frontend/src/components/NLPOrderInput.jsx"],
        "existing_paths": [],
        "new_files": ["frontend/src/components/NLPOrderInput.jsx"],
        "required_test_paths": [],
        "target_file": {"path": "frontend/src/components/NLPOrderInput.jsx", "action": "create"},
        "test_file": {"path": "frontend/src/components/NLPOrderInput.test.jsx", "action": "create"},
        "must_contain": ["<textarea", "export default NLPOrderInput"],
        "must_test": [],
        "acceptance_criteria": ["renders a textarea for the order text"],
        "_target_file_declared": True,
        "_test_file_declared": True,
        "_depends_on_declared": True,
        "_must_contain_declared": True,
        "_must_test_declared": True,
    }
    assert orch._task_requires_tests(item) is False
    errors = orch._validate_backend_task_contract(item)
    joined = " | ".join(errors)
    assert "test_file_path_not_declared" not in joined
    assert "missing_test_file_contract" not in joined
    assert "vague_must_contain" not in joined


def test_backend_contract_still_requires_tests(tmp_path: Path) -> None:
    # Guard: the test-less exemption must NOT leak to backend tasks — they still need a test.
    orch = _make_orch(tmp_path)
    item = {
        "id": "TASK-001",
        "title": "Add backend endpoint for natural language order parsing",
        "scope": "backend endpoint",
        "allowed_paths": ["main.py", "tests/test_nlp_order_parsing.py"],
        "existing_paths": ["main.py"],
        "new_files": ["tests/test_nlp_order_parsing.py"],
        "required_test_paths": ["tests/test_nlp_order_parsing.py"],
        "target_file": {"path": "main.py", "action": "update"},
        "test_file": {},
        "must_contain": ["def parse_nl_order(", "cursor.execute("],
        "must_test": [],
        "acceptance_criteria": ["parses an order string"],
        "_target_file_declared": True,
        "_test_file_declared": False,
        "_depends_on_declared": True,
        "_must_contain_declared": True,
        "_must_test_declared": False,
    }
    assert orch._task_requires_tests(item) is True
    errors = orch._validate_backend_task_contract(item)
    joined = " | ".join(errors)
    assert "missing_test_file_contract" in joined


def test_behavioral_test_imports_pass_gate(tmp_path: Path) -> None:
    # Behavioral endpoint tests import the app + TestClient + mock; the must_use_only gate must
    # allow these (and the target repo's own top-level modules) instead of flagging them.
    orch = _make_orch(tmp_path)
    (orch.target_workspace / "main.py").write_text("app = object()\n", encoding="utf-8")
    tdir = orch.target_workspace / "tests"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "test_endpoint.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "from unittest import mock\n"
        "sys.path.insert(0, str(Path(__file__).resolve().parent.parent))\n"
        "import main\n"
        "from fastapi.testclient import TestClient\n\n"
        "def test_x():\n"
        "    assert True\n",
        encoding="utf-8",
    )
    findings = orch._validate_test_developer_static_constraints(["tests/test_endpoint.py"])
    assert not any("import outside must_use_only" in f for f in findings), findings


def test_forbidden_test_imports_still_rejected(tmp_path: Path) -> None:
    # The behavioral relaxation must NOT open the door to sqlalchemy/alembic/pytest imports.
    orch = _make_orch(tmp_path)
    tdir = orch.target_workspace / "tests"
    tdir.mkdir(parents=True, exist_ok=True)
    (tdir / "test_bad.py").write_text("import sqlalchemy\n\ndef test_x():\n    assert True\n", encoding="utf-8")
    findings = orch._validate_test_developer_static_constraints(["tests/test_bad.py"])
    assert any("forbidden import: sqlalchemy" in f for f in findings), findings


def test_detect_db_cursor_row_shape_tuple(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    (orch.target_workspace / "db.py").write_text(
        "import mysql.connector\n"
        "def q():\n"
        "    conn = mysql.connector.connect()\n"
        "    cur = conn.cursor()\n"
        "    cur.execute('SELECT surname FROM customers')\n"
        "    return cur.fetchall()\n",
        encoding="utf-8",
    )
    assert orch._detect_db_cursor_row_shape() == "tuple"
    note = orch._build_raw_sql_schema_note() if orch._extract_raw_sql_schema() else ""
    # (schema note only builds when CREATE TABLE is present; the shape helper itself is the ground truth)


def test_detect_db_cursor_row_shape_dict(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    (orch.target_workspace / "db.py").write_text(
        "import mysql.connector\n"
        "def q():\n"
        "    conn = mysql.connector.connect()\n"
        "    cur = conn.cursor(dictionary=True)\n"
        "    cur.execute('SELECT surname FROM customers')\n"
        "    return cur.fetchall()\n",
        encoding="utf-8",
    )
    assert orch._detect_db_cursor_row_shape() == "dict"


def test_detect_db_cursor_row_shape_none(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    (orch.target_workspace / "app.py").write_text("def hello():\n    return 'hi'\n", encoding="utf-8")
    assert orch._detect_db_cursor_row_shape() == ""


def test_detect_app_entrypoint_from_fastapi_module(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    (orch.target_workspace / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    assert orch._detect_app_entrypoint() == "main"


def test_detect_app_entrypoint_from_dockerfile(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    (orch.target_workspace / "Dockerfile").write_text(
        'CMD ["uvicorn", "api:app", "--host", "0.0.0.0"]\n', encoding="utf-8"
    )
    assert orch._detect_app_entrypoint() == "api"


def test_detect_app_entrypoint_none(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    (orch.target_workspace / "util.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert orch._detect_app_entrypoint() == ""


def _verify_orch(tmp_path: Path, run_result, *, source="target_venv", verify_run=True):
    orch = _make_orch(tmp_path)
    orch.config.setdefault("workflow", {})["verify_run"] = verify_run
    orch._app_entrypoint_cache = "main"
    orch._implementation_completion_changed_files = lambda: ["main.py"]  # type: ignore[assignment]
    orch.resolve_python_executable = lambda: ("python", source, True)  # type: ignore[assignment]
    orch._run_local_command = lambda cmd, timeout=10, cwd=None: run_result  # type: ignore[assignment]
    return orch


def test_verify_gate_disabled_returns_true(tmp_path: Path) -> None:
    orch = _verify_orch(tmp_path, (1, "", "ModuleNotFoundError: No module named 'requests'"), verify_run=False)
    assert orch._run_app_boot_verification("t1") is True


def test_verify_gate_skips_without_target_venv(tmp_path: Path) -> None:
    orch = _verify_orch(tmp_path, (1, "", "ModuleNotFoundError: No module named 'requests'"), source="sys_executable")
    assert orch._run_app_boot_verification("t1") is True  # can't trust deps -> skip, don't cry wolf


def test_verify_gate_fails_on_code_error(tmp_path: Path) -> None:
    orch = _verify_orch(tmp_path, (1, "", "Traceback...\nModuleNotFoundError: No module named 'requests'"))
    assert orch._run_app_boot_verification("t1") is False
    assert orch._phase_failure_status == "verification_failed"
    report = orch._load_saved_agent_report("implementation", "developer-checks") or {}
    assert "boot verification failed" in str(report.get("result", "")).lower() or "requests" in str(report.get("parsed_output", ""))


def test_verify_gate_ignores_env_error(tmp_path: Path) -> None:
    orch = _verify_orch(tmp_path, (1, "", "mysql.connector.errors.OperationalError: 2003 Can't connect to MySQL server"))
    assert orch._run_app_boot_verification("t1") is True  # environment (DB) issue, not a code bug


def test_verify_gate_passes_on_boot_ok(tmp_path: Path) -> None:
    orch = _verify_orch(tmp_path, (0, "boot-ok", ""))
    assert orch._run_app_boot_verification("t1") is True


def test_build_roadmap_runs_only_strategist(tmp_path: Path) -> None:
    orch = _make_orch(tmp_path)
    orch.config["phases"]["research"] = {
        "name": "Research",
        "agents": [{"name": "product-strategist"}],
    }
    orch._ensure_user_goal = lambda phase: True  # type: ignore[assignment]
    orch._preflight_runtime = lambda phase=None: True  # type: ignore[assignment]
    orch._refresh_repo_map = lambda snapshot_path=None: True  # type: ignore[assignment]
    strategist_json = json.dumps(
        {"vision": "v", "slices": [{"id": "SLICE-001", "title": "A", "goal": "do A"}]}
    )
    ran: list[str] = []

    def fake_run_agent(agent, phase, index=None, total=None):
        ran.append(agent["name"])
        orch._overwrite_agent_report(
            "research", "product-strategist", {"status": "success", "parsed_output": strategist_json}
        )
        return True

    orch._run_agent = fake_run_agent  # type: ignore[assignment]
    assert orch.build_roadmap() is True
    assert ran == ["product-strategist"]
    assert orch.roadmap_path.exists()
