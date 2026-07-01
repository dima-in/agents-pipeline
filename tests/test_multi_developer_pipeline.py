import types

from workflow.multi_developer_constraints import parse_and_validate
from workflow.multi_developer_dispatcher import classify_path, filter_paths_for_agent, route_paths
from workflow.orchestrator import WorkflowOrchestrator


def test_classify_path_routes_code_test_and_infra() -> None:
    assert classify_path("gateway-v4/app/models.py") == "code-developer"
    assert classify_path("gateway-v4/tests/test_models.py") == "test-developer"
    assert classify_path("gateway-v4/alembic/versions/20240801_add_provider_metrics.py") == "infra-developer"


def test_route_paths_orders_agents_deterministically() -> None:
    routes = route_paths(
        [
            "gateway-v4/tests/test_provider_metrics_migration.py",
            "gateway-v4/alembic/versions/20240801_add_provider_metrics.py",
            "gateway-v4/app/models.py",
        ]
    )
    assert routes == ["code-developer", "infra-developer", "test-developer"]


def test_filter_paths_for_agent_keeps_only_matching_scope() -> None:
    paths = [
        "gateway-v4/app/models.py",
        "gateway-v4/alembic/versions/20240801_add_provider_metrics.py",
        "gateway-v4/tests/test_provider_metrics_migration.py",
    ]
    assert filter_paths_for_agent(paths, "code-developer") == ["gateway-v4/app/models.py"]
    assert filter_paths_for_agent(paths, "infra-developer") == ["gateway-v4/alembic/versions/20240801_add_provider_metrics.py"]
    assert filter_paths_for_agent(paths, "test-developer") == ["gateway-v4/tests/test_provider_metrics_migration.py"]


def test_multi_developer_editable_paths_exclude_reference_only_allowed_paths(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator._selected_implementation_item = {
        "allowed_paths": [
            "gateway-v4/app/database.py",
            "gateway-v4/app/models.py",
            "gateway-v4/alembic/versions/20240801_add_provider_metrics.py",
            "gateway-v4/tests/__init__.py",
            "gateway-v4/tests/test_provider_metrics_migration.py",
        ],
        "existing_paths": ["gateway-v4/app/database.py", "gateway-v4/app/models.py"],
        "new_files": [
            "gateway-v4/alembic/versions/20240801_add_provider_metrics.py",
            "gateway-v4/tests/__init__.py",
            "gateway-v4/tests/test_provider_metrics_migration.py",
        ],
        "required_test_paths": ["gateway-v4/tests/test_provider_metrics_migration.py"],
    }

    editable_paths = orchestrator._build_multi_developer_editable_paths()

    assert "gateway-v4/app/database.py" not in editable_paths
    assert "gateway-v4/app/models.py" not in editable_paths
    assert route_paths(editable_paths) == ["infra-developer", "test-developer"]


def test_multi_developer_editable_paths_include_code_target_file(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator._selected_implementation_item = {
        "allowed_paths": [
            "gateway-v4/app/database.py",
            "gateway-v4/app/models.py",
            "gateway-v4/tests/test_provider_metrics_model.py",
        ],
        "existing_paths": ["gateway-v4/app/database.py", "gateway-v4/app/models.py"],
        "new_files": ["gateway-v4/tests/test_provider_metrics_model.py"],
        "required_test_paths": ["gateway-v4/tests/test_provider_metrics_model.py"],
        "target_file": {"path": "gateway-v4/app/models.py", "action": "update"},
        "test_file": {"path": "gateway-v4/tests/test_provider_metrics_model.py", "action": "create"},
    }

    editable_paths = orchestrator._build_multi_developer_editable_paths()

    assert "gateway-v4/app/database.py" not in editable_paths
    assert "gateway-v4/app/models.py" in editable_paths
    assert route_paths(editable_paths) == ["code-developer", "test-developer"]


def test_multi_developer_agents_receive_selected_task_contract_context(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator._selected_implementation_item = {
        "id": "TASK-001",
        "title": "Add provider metrics migration",
        "scope": "backend-only",
        "allowed_paths": ["gateway-v4/tests/test_provider_metrics_migration.py"],
        "target_file": {"path": "gateway-v4/alembic/versions/20240801_add_provider_metrics.py", "action": "create"},
        "test_file": {"path": "gateway-v4/tests/test_provider_metrics_migration.py", "action": "create"},
        "must_contain": ["def upgrade():", "def downgrade():"],
        "must_test": ["test_provider_metrics_migration_upgrade: assert migration columns"],
        "contract_completeness": True,
    }

    context = orchestrator._build_implementation_same_phase_context("test-developer")

    assert "[selected-task-contract]" in context
    assert "developer_contract:" in context
    assert "target_file.path: gateway-v4/alembic/versions/20240801_add_provider_metrics.py" in context
    assert "test_file.path: gateway-v4/tests/test_provider_metrics_migration.py" in context


def test_multi_developer_scope_instruction_marks_other_context_read_only(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator._selected_implementation_item = {
        "allowed_paths": ["gateway-v4/tests/test_provider_metrics_migration.py"],
    }
    orchestrator.user_goal = ""
    orchestrator.context_mode = ""
    orchestrator.config = {"project": {"name": "ai-getaway"}}
    orchestrator.project_id = "github.com-dima-in-ai_getaway"

    instruction = orchestrator._build_implementation_scope_instruction("backend-only", agent_name="test-developer")

    assert "Agent-scoped allowed files for this invocation:" in instruction
    assert "Write only the files listed directly above." in instruction
    assert "sibling agent reports as read-only context" in instruction


def test_multi_developer_task_override_scopes_reasons_and_contract_fields(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator._selected_implementation_item = {
        "id": "TASK-001",
        "allowed_paths": [
            "gateway-v4/app/models.py",
            "gateway-v4/alembic/versions/20240801_add_provider_metrics.py",
            "gateway-v4/tests/test_provider_metrics_migration.py",
        ],
        "existing_paths": ["gateway-v4/app/models.py"],
        "new_files": ["gateway-v4/alembic/versions/20240801_add_provider_metrics.py"],
        "required_test_paths": ["gateway-v4/tests/test_provider_metrics_migration.py"],
        "reason_each_path_is_needed": {
            "gateway-v4/app/models.py": "Add the application model.",
            "gateway-v4/alembic/versions/20240801_add_provider_metrics.py": "Create the migration.",
            "gateway-v4/tests/test_provider_metrics_migration.py": "Cover the migration statically.",
        },
        "target_file": {"path": "gateway-v4/alembic/versions/20240801_add_provider_metrics.py", "action": "create"},
        "test_file": {"path": "gateway-v4/tests/test_provider_metrics_migration.py", "action": "create"},
        "must_contain": ["def upgrade():", "def downgrade():"],
        "must_import": ["sqlalchemy as sa"],
        "must_test": ["assert migration columns"],
        "contract_completeness": True,
    }
    editable_paths = orchestrator._build_multi_developer_editable_paths()

    code_item = orchestrator._build_multi_developer_task_override("code-developer", editable_paths)
    test_item = orchestrator._build_multi_developer_task_override("test-developer", editable_paths)

    assert code_item["allowed_paths"] == []
    assert code_item["target_file"] == {}
    assert code_item["test_file"] == {}
    assert code_item["must_contain"] == []
    assert code_item["must_import"] == []
    assert code_item["must_test"] == []
    assert code_item["reason_each_path_is_needed"] == {}

    assert test_item["allowed_paths"] == ["gateway-v4/tests/test_provider_metrics_migration.py"]
    assert test_item["target_file"]["path"] == "gateway-v4/alembic/versions/20240801_add_provider_metrics.py"
    assert test_item["test_file"]["path"] == "gateway-v4/tests/test_provider_metrics_migration.py"
    assert test_item["reason_each_path_is_needed"] == {
        "gateway-v4/tests/test_provider_metrics_migration.py": "Cover the migration statically.",
    }

    orchestrator._selected_implementation_item = test_item
    context = orchestrator._build_selected_task_contract_context()

    assert "gateway-v4/app/models.py" not in context
    assert "target_file.path: gateway-v4/alembic/versions/20240801_add_provider_metrics.py" in context


def test_parse_and_validate_accepts_allowed_agent_output() -> None:
    payload, errors = parse_and_validate(
        """
        {
          "agent": "test-developer",
          "task_id": "TASK-001",
          "reasoning": "Added tests.",
          "operations": [
            {
              "type": "create",
              "path": "gateway-v4/tests/test_provider_metrics_migration.py",
              "content": "def test_ok():\\n    assert True\\n",
              "reason": "required test"
            }
          ],
          "dependencies_added": [],
          "warnings": []
        }
        """,
        ["gateway-v4/tests/test_provider_metrics_migration.py"],
        "test-developer",
    )
    assert payload is not None
    assert errors == []


def test_parse_and_validate_rejects_wrong_agent_scope() -> None:
    payload, errors = parse_and_validate(
        """
        {
          "agent": "code-developer",
          "task_id": "TASK-001",
          "reasoning": "Tried to write tests.",
          "operations": [
            {
              "type": "create",
              "path": "gateway-v4/tests/test_provider_metrics_migration.py",
              "content": "def test_bad():\\n    assert True\\n",
              "reason": "wrong scope"
            }
          ]
        }
        """,
        ["gateway-v4/tests/test_provider_metrics_migration.py"],
        "code-developer",
    )
    assert payload is not None
    assert any("belongs to test-developer" in error for error in errors)


def test_parse_and_validate_extracts_json_from_wrapped_output() -> None:
    payload, errors = parse_and_validate(
        """
        Here is the result:
        {
          "agent": "infra-developer",
          "task_id": "TASK-001",
          "reasoning": "Added migration.",
          "operations": [
            {
              "type": "create",
              "path": "gateway-v4/alembic/versions/20240801_add_provider_metrics.py",
              "content": "from alembic import op\\n",
              "reason": "required migration"
            }
          ],
          "dependencies_added": [],
          "warnings": []
        }
        """,
        ["gateway-v4/alembic/versions/20240801_add_provider_metrics.py"],
        "infra-developer",
    )
    assert payload is not None
    assert errors == []


def make_orchestrator_with_workspace(tmp_path):
    orchestrator = WorkflowOrchestrator.__new__(WorkflowOrchestrator)
    orchestrator.target_workspace = tmp_path
    return orchestrator


def test_target_relative_path_normalizes_absolute_windows_path(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    absolute_path = tmp_path / "gateway-v4" / "tests" / "test_provider_metrics_migration.py"

    assert (
        orchestrator._normalize_target_relative_path(str(absolute_path))
        == "gateway-v4/tests/test_provider_metrics_migration.py"
    )


def test_multi_developer_write_detection_matches_absolute_written_path(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    written = tmp_path / "gateway-v4" / "alembic" / "versions" / "20240801_add_provider_metrics.py"

    diagnostics = orchestrator._build_multi_developer_write_detection(
        ["gateway-v4/alembic/versions/20240801_add_provider_metrics.py"],
        [],
        [str(written)],
        ["write_file"],
    )

    assert diagnostics["write_operation_detected"] is True
    assert diagnostics["scoped_path_match_result"] is True
    assert diagnostics["diff_detection_stage"] == "write_tool"
    assert diagnostics["actual_changed_files"] == ["gateway-v4/alembic/versions/20240801_add_provider_metrics.py"]


def test_multi_developer_write_detection_matches_backslash_git_diff(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)

    diagnostics = orchestrator._build_multi_developer_write_detection(
        ["gateway-v4/tests/test_provider_metrics_migration.py"],
        ["gateway-v4\\tests\\test_provider_metrics_migration.py"],
        [],
        [],
    )

    assert diagnostics["scoped_path_match_result"] is True
    assert diagnostics["diff_detection_stage"] == "git_diff"
    assert diagnostics["actual_changed_files"] == ["gateway-v4/tests/test_provider_metrics_migration.py"]


def test_multi_developer_write_detection_survives_empty_diff_after_write(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)

    diagnostics = orchestrator._build_multi_developer_write_detection(
        ["gateway-v4/tests/test_provider_metrics_migration.py"],
        [],
        ["gateway-v4/tests/test_provider_metrics_migration.py"],
        ["write_file"],
    )

    assert diagnostics["scoped_path_match_result"] is True
    assert diagnostics["write_operation_detected"] is True
    assert diagnostics["actual_changed_files"] == ["gateway-v4/tests/test_provider_metrics_migration.py"]


def test_recover_multi_developer_write_metadata_parses_embedded_apply_patch_markdown(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)

    tools, paths = orchestrator._recover_multi_developer_write_metadata(
        {
            "parsed_output": 'Done.\n```json\n{"tool":"apply_patch","path":"gateway-v4/tests/test_provider_metrics_migration.py","search":"old","replace":"new"}\n```'
        }
    )

    assert tools == ["apply_patch"]
    assert paths == ["gateway-v4/tests/test_provider_metrics_migration.py"]


def test_recover_multi_developer_write_metadata_parses_embedded_write_file_json(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)

    tools, paths = orchestrator._recover_multi_developer_write_metadata(
        {
            "stdout": '{"output_text":"Applying fix\\n```json\\n{\\"tool\\":\\"write_file\\",\\"path\\":\\"gateway-v4/alembic/versions/20240801_add_provider_metrics.py\\",\\"content\\":\\"ok\\\\n\\"}\\n```"}'
        }
    )

    assert tools == ["write_file"]
    assert paths == ["gateway-v4/alembic/versions/20240801_add_provider_metrics.py"]


def test_recover_multi_developer_write_metadata_normalizes_absolute_windows_paths(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    absolute_path = tmp_path / "gateway-v4" / "tests" / "test_provider_metrics_migration.py"

    tools, paths = orchestrator._recover_multi_developer_write_metadata(
        {
            "parsed_output": (
                '{"tool":"apply_patch","path":"'
                + str(absolute_path).replace("\\", "\\\\")
                + '","search":"old","replace":"new"}'
            )
        }
    )

    assert tools == ["apply_patch"]
    assert paths == ["gateway-v4/tests/test_provider_metrics_migration.py"]


def test_recover_multi_developer_write_metadata_normalizes_backslash_paths(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)

    tools, paths = orchestrator._recover_multi_developer_write_metadata(
        {
            "parsed_output": '{"tool":"write_file","path":"gateway-v4\\\\tests\\\\test_provider_metrics_migration.py","content":"ok"}'
        }
    )

    assert tools == ["write_file"]
    assert paths == ["gateway-v4/tests/test_provider_metrics_migration.py"]


def test_multi_developer_no_changes_rejects_code_scope_without_target_contract(tmp_path) -> None:
    models_path = tmp_path / "gateway-v4" / "app" / "models.py"
    models_path.parent.mkdir(parents=True)
    models_path.write_text("class ExistingModel:\n    pass\n", encoding="utf-8")

    orchestrator = make_orchestrator_with_workspace(tmp_path)

    ok, detail = orchestrator._validate_multi_developer_no_changes(
        "code-developer",
        {
            "allowed_paths": ["gateway-v4/app/models.py"],
            "target_file": {},
            "test_file": {},
            "must_contain": [],
        },
        ["gateway-v4/app/models.py"],
        "status=no_changes: already valid",
    )

    assert ok is False
    assert "no scoped target_file contract" in detail


def test_test_developer_static_constraints_reject_format_sensitive_migration_assertions(tmp_path) -> None:
    test_path = tmp_path / "gateway-v4" / "tests" / "test_provider_metrics_migration.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "def test_brittle_formatting():\n"
        "    text = read_text()\n"
        "    assert \"op.create_table('provider_metrics'\" in text\n"
        "    assert 'sa.Column(\"id\", sa.Integer(), nullable=False)' in text\n",
        encoding="utf-8",
    )
    orchestrator = make_orchestrator_with_workspace(tmp_path)

    findings = orchestrator._validate_test_developer_static_constraints(
        ["gateway-v4/tests/test_provider_metrics_migration.py"]
    )

    assert any("format-sensitive raw string assertions" in finding for finding in findings)


def test_test_developer_static_constraints_reject_revision_regex_assertions(tmp_path) -> None:
    test_path = tmp_path / "gateway-v4" / "tests" / "test_provider_metrics_migration.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "import re\n\n"
        "def test_brittle_down_revision(source):\n"
        "    assert re.search(r\"down_revision\\s*=\\s*\\\"0005\\\"\", source)\n",
        encoding="utf-8",
    )
    orchestrator = make_orchestrator_with_workspace(tmp_path)

    findings = orchestrator._validate_test_developer_static_constraints(
        ["gateway-v4/tests/test_provider_metrics_migration.py"]
    )

    assert any("migration revision assignments" in finding for finding in findings)


def test_multi_developer_changed_scope_rejects_code_syntax_error(tmp_path) -> None:
    models_path = tmp_path / "gateway-v4" / "app" / "models.py"
    models_path.parent.mkdir(parents=True)
    models_path.write_text("class ProviderMetrics:\n    broken = )\n", encoding="utf-8")
    orchestrator = make_orchestrator_with_workspace(tmp_path)

    ok, detail = orchestrator._validate_multi_developer_changed_scope(
        "code-developer",
        {"forbidden": []},
        ["gateway-v4/app/models.py"],
    )

    assert ok is False
    assert "py_compile failed for scoped changed files" in detail


def test_multi_developer_changed_scope_rejects_brittle_test_file(tmp_path) -> None:
    test_path = tmp_path / "gateway-v4" / "tests" / "test_provider_metrics_migration.py"
    test_path.parent.mkdir(parents=True)
    test_path.write_text(
        "def test_brittle_formatting():\n"
        "    assert \"op.create_table('provider_metrics'\" in source\n",
        encoding="utf-8",
    )
    orchestrator = make_orchestrator_with_workspace(tmp_path)

    ok, detail = orchestrator._validate_multi_developer_changed_scope(
        "test-developer",
        {"forbidden": []},
        ["gateway-v4/tests/test_provider_metrics_migration.py"],
    )

    assert ok is False
    assert "format-sensitive raw string assertions" in detail


def strict_message_bundle(paths=None, agent_scope="backend-only"):
    return {
        "selected_task_scope": agent_scope,
        "selected_task_allowed_paths": paths
        or [
            "gateway-v4/alembic/versions/20240801_add_provider_metrics.py",
        ],
        "contract_completeness": True,
        "implementation_context_chars": 500,
        "repository_context_chars": 500,
        "execution_mode": "balanced",
        "system_message": "system",
        "combined_message": "combined",
        "prompt_stats": {"message_chars": 8, "message_lines": 1},
    }


def test_strict_mode_auto_activation_for_migration_task(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    bundle = strict_message_bundle()

    assert orchestrator.should_enable_strict_mode("infra-developer", "implementation", bundle) is True

    orchestrator._apply_execution_policy("infra-developer", "implementation", bundle)

    assert bundle["execution_mode"] == "strict"
    assert bundle["strict_execution_mode"] is True
    assert bundle["retrieval_budget"] == 2


def test_strict_mode_not_auto_enabled_for_code_agent_with_two_files(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    bundle = strict_message_bundle(
        paths=[
            "gateway-v4/app/database.py",
            "gateway-v4/app/models.py",
        ]
    )

    assert orchestrator.should_enable_strict_mode("code-developer", "implementation", bundle) is False

    orchestrator._apply_execution_policy("code-developer", "implementation", bundle)

    assert bundle["execution_mode"] == "balanced"
    assert bundle["strict_execution_mode"] is False
    assert bundle["retrieval_budget"] == 6


def test_strict_mode_still_auto_enabled_for_single_code_file(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    bundle = strict_message_bundle(paths=["gateway-v4/app/models.py"])

    assert orchestrator.should_enable_strict_mode("code-developer", "implementation", bundle) is True

    orchestrator._apply_execution_policy("code-developer", "implementation", bundle)

    assert bundle["execution_mode"] == "strict"
    assert bundle["strict_execution_mode"] is True
    assert bundle["retrieval_budget"] == 2


def test_strict_mode_disabled_during_repair_so_developer_can_read_to_fix(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    bundle = strict_message_bundle()
    # First attempt: strict is on (no repair in progress).
    assert orchestrator.should_enable_strict_mode("infra-developer", "implementation", bundle) is True

    # A repair retry (attempt > 1) must relax to balanced retrieval, otherwise the
    # developer is hard-stopped after one read and can never fix the failing file.
    orchestrator._implementation_attempt = 2
    assert orchestrator.should_enable_strict_mode("infra-developer", "implementation", bundle) is False

    # The explicit developer-repair flag relaxes it too, even on attempt 1.
    orchestrator._implementation_attempt = 1
    orchestrator._implementation_retry_from_agent = "developer"
    assert orchestrator.should_enable_strict_mode("infra-developer", "implementation", bundle) is False
    orchestrator._apply_execution_policy("infra-developer", "implementation", bundle)
    assert bundle["execution_mode"] == "balanced"
    assert bundle["retrieval_budget"] == 6


def test_retrieval_blocked_in_strict_mode_after_one_read(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    bundle = strict_message_bundle()
    orchestrator._apply_execution_policy("infra-developer", "implementation", bundle)

    result = orchestrator._evaluate_retrieval_budget(
        bundle,
        "read_file",
        retrieval_operations_used=1,
        read_file_operations_used=1,
    )

    assert result["allowed"] is False
    assert result["hard_stop"] is True
    assert "only one read_file" in result["reason"]


def test_list_files_forbidden_in_strict_mode(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    bundle = strict_message_bundle()
    orchestrator._apply_execution_policy("infra-developer", "implementation", bundle)

    result = orchestrator._evaluate_retrieval_budget(
        bundle,
        "list_files",
        retrieval_operations_used=0,
        read_file_operations_used=0,
    )

    assert result["allowed"] is False
    assert result["hard_stop"] is True
    assert "forbids list_files" in result["reason"]


def test_retrieval_budget_exhaustion_in_strict_mode(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    bundle = strict_message_bundle()
    orchestrator._apply_execution_policy("infra-developer", "implementation", bundle)

    result = orchestrator._evaluate_retrieval_budget(
        bundle,
        "read_files",
        retrieval_operations_used=2,
        read_file_operations_used=0,
    )

    assert result["allowed"] is False
    assert result["remaining"] == 0
    assert "budget exhausted" in result["reason"]


def test_write_file_allowed_without_spending_strict_retrieval_budget(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    bundle = strict_message_bundle()
    orchestrator._apply_execution_policy("infra-developer", "implementation", bundle)

    result = orchestrator._evaluate_retrieval_budget(
        bundle,
        "write_file",
        retrieval_operations_used=2,
        read_file_operations_used=1,
    )

    assert result["allowed"] is True
    assert result["hard_stop"] is False
    assert result["remaining"] == 0


def test_selected_task_excerpts_inject_editable_target_file_in_full(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)

    models_path = tmp_path / "gateway-v4" / "app" / "models.py"
    models_path.parent.mkdir(parents=True, exist_ok=True)
    # Large existing file whose final class sits well beyond the old shared excerpt budget
    # (limit // 2 == 2100 chars), so the previous behaviour truncated it away.
    head = "\n".join(
        f"class Model{i}(Base):\n    __tablename__ = 'm{i}'\n    col = Column(String)\n"
        for i in range(60)
    )
    sentinel = (
        "class ChatMessageSentinel(Base):\n"
        "    __tablename__ = 'chat_messages'\n"
        "    keep_me = Column(String)\n"
    )
    models_path.write_text(head + "\n" + sentinel, encoding="utf-8")
    assert len(models_path.read_text(encoding="utf-8")) > 2100

    database_path = tmp_path / "gateway-v4" / "app" / "database.py"
    database_path.write_text("Base = object()\n", encoding="utf-8")

    # The contract redundantly lists the editable target file in reference_files too —
    # this previously excluded it from the full-injection set and truncated it again.
    orchestrator._selected_implementation_item = {
        "allowed_paths": ["gateway-v4/app/models.py", "gateway-v4/app/database.py"],
        "existing_paths": ["gateway-v4/app/models.py", "gateway-v4/app/database.py"],
        "reference_files": ["gateway-v4/app/models.py", "gateway-v4/app/database.py"],
        "new_files": [],
        "required_test_paths": [],
        "target_file": {"path": "gateway-v4/app/models.py", "action": "update"},
    }

    excerpts = orchestrator._build_selected_task_file_excerpts(limit=4200)

    # The editable target file must reach the developer in full — including its final
    # class — so a write_file developer never reconstructs unseen classes from memory.
    assert "ChatMessageSentinel" in excerpts
    models_portion = excerpts.split("gateway-v4/app/database.py")[0]
    assert "[retrieval truncated by total limit]" not in models_portion
    assert "[file truncated" not in models_portion
    # Reference-only files are still surfaced under the remaining budget.
    assert "gateway-v4/app/database.py" in excerpts


def _write_provider_metrics_migration(tmp_path):
    migration = tmp_path / "gateway-v4" / "alembic" / "versions" / "0006_provider_metrics.py"
    migration.parent.mkdir(parents=True, exist_ok=True)
    migration.write_text(
        "from alembic import op\n"
        "import sqlalchemy as sa\n\n"
        "def upgrade():\n"
        "    op.create_table('provider_metrics', sa.Column('id', sa.String()))\n\n"
        "def downgrade():\n"
        "    op.drop_table('provider_metrics')\n",
        encoding="utf-8",
    )


def _provider_metrics_contract():
    return {
        "id": "TASK-001",
        "_target_file_declared": True,
        "_test_file_declared": True,
        "_depends_on_declared": True,
        "_must_contain_declared": True,
        "_must_test_declared": True,
        "allowed_paths": [
            "gateway-v4/app/models.py",
            "gateway-v4/alembic/versions/0006_provider_metrics.py",
        ],
        "existing_paths": [
            "gateway-v4/app/models.py",
            "gateway-v4/alembic/versions/0006_provider_metrics.py",
        ],
        "reference_files": ["gateway-v4/alembic/versions/0006_provider_metrics.py"],
        "new_files": ["gateway-v4/tests/test_provider_metrics_migration.py"],
        "required_test_paths": ["gateway-v4/tests/test_provider_metrics_migration.py"],
        "target_file": {"path": "gateway-v4/app/models.py", "action": "update"},
        "test_file": {"path": "gateway-v4/tests/test_provider_metrics_migration.py", "action": "create"},
        "must_contain": [
            "class ProviderMetrics(Base):",
            '__tablename__ = "provider_metrics"',
        ],
        "must_test": [
            "test_provider_metrics_table_exists: verify provider_metrics table is created by migration",
            "test_provider_metrics_indexes: verify provider and model indexes exist",
        ],
    }


def test_contract_rejects_migration_test_for_table_absent_from_migration(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    _write_provider_metrics_migration(tmp_path)

    # The planning agents invented a provider_health model + a test asserting the
    # migration creates that table, but migration 0006 only creates provider_metrics.
    item = _provider_metrics_contract()
    item["must_contain"] += ["class ProviderHealth(Base):", '__tablename__ = "provider_health"']
    item["must_test"].append(
        "test_provider_health_table_exists: verify provider_health table is created by migration"
    )

    errors = orchestrator._validate_backend_task_contract(item)

    assert any("table 'provider_health' is created by the migration" in e for e in errors)
    # The real table must not be flagged.
    assert not any("table 'provider_metrics' is created by the migration" in e for e in errors)


def test_contract_allows_migration_test_for_real_table(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    _write_provider_metrics_migration(tmp_path)

    errors = orchestrator._validate_backend_task_contract(_provider_metrics_contract())

    assert not any("created by the migration" in e for e in errors)


def test_migration_ground_truth_note_lists_only_real_tables(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    _write_provider_metrics_migration(tmp_path)
    orchestrator._selected_implementation_item = {
        "existing_paths": ["gateway-v4/alembic/versions/0006_provider_metrics.py"],
        "reference_files": ["gateway-v4/alembic/versions/0006_provider_metrics.py"],
        "new_files": [],
    }

    note = orchestrator._build_migration_ground_truth_note()

    assert "provider_metrics" in note
    assert "provider_health" not in note


def test_migration_ground_truth_note_empty_without_existing_migration(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator._selected_implementation_item = {
        "existing_paths": ["gateway-v4/app/models.py"],
        # The migration is declared as a new file (does not exist yet) → not ground truth.
        "new_files": ["gateway-v4/alembic/versions/0007_new.py"],
        "reference_files": ["gateway-v4/alembic/versions/0007_new.py"],
    }

    assert orchestrator._build_migration_ground_truth_note() == ""


def test_migration_ground_truth_note_includes_columns_pk_and_indexes(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    migration = tmp_path / "gateway-v4" / "alembic" / "versions" / "0006_provider_metrics.py"
    migration.parent.mkdir(parents=True, exist_ok=True)
    migration.write_text(
        "from alembic import op\n"
        "import sqlalchemy as sa\n\n"
        "def upgrade():\n"
        "    op.create_table('provider_metrics',\n"
        "        sa.Column('id', sa.String(), primary_key=True),\n"
        "        sa.Column('provider', sa.String(), nullable=False),\n"
        "        sa.Column('model', sa.String(), nullable=False),\n"
        "    )\n"
        "    op.create_index('ix_pm_provider', 'provider_metrics', ['provider'])\n\n"
        "def downgrade():\n"
        "    op.drop_table('provider_metrics')\n",
        encoding="utf-8",
    )
    orchestrator._selected_implementation_item = {
        "existing_paths": ["gateway-v4/alembic/versions/0006_provider_metrics.py"],
        "reference_files": ["gateway-v4/alembic/versions/0006_provider_metrics.py"],
        "new_files": [],
    }

    note = orchestrator._build_migration_ground_truth_note()

    assert "primary key: id" in note
    assert "provider" in note and "model" in note
    assert "[provider]" in note  # parsed index columns
    # The real PK is a single `id` column — the invented composite key must not appear.
    assert "primary key: provider, model" not in note


def test_task_designer_contract_retry_regenerates_on_invalid(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator.logger = types.SimpleNamespace(info=lambda *a, **k: None)
    orchestrator._task_designer_feedback_for_prompt = False
    calls = {"run": 0, "apply": 0, "flag_during_run": []}

    def fake_run_agent(agent, phase, *, index, total):
        calls["run"] += 1
        calls["flag_during_run"].append(orchestrator._task_designer_feedback_for_prompt)
        return True

    def fake_apply(report):
        calls["apply"] += 1
        return calls["apply"] >= 2  # invalid first, valid on regeneration

    orchestrator._run_agent = fake_run_agent
    orchestrator._load_saved_agent_report = lambda phase, name, run_dir=None: {"agent_name": "task-designer"}
    orchestrator._apply_task_designer_contract_from_report = fake_apply

    ok = orchestrator._apply_task_designer_contract_with_retry(
        {"name": "task-designer"}, index=3, total=6, max_retries=2
    )

    assert ok is True
    assert calls["run"] == 1  # one regeneration after the initial apply failed
    assert calls["apply"] == 2
    assert calls["flag_during_run"] == [True]  # feedback injected on the regeneration run
    assert orchestrator._task_designer_feedback_for_prompt is False  # reset afterwards


def test_scaffold_package_markers_creates_missing_init(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    (tmp_path / "gateway-v4" / "tests").mkdir(parents=True, exist_ok=True)
    paths = [
        "gateway-v4/tests/__init__.py",
        "gateway-v4/tests/test_provider_metrics_migration.py",  # not a marker — ignored
        "gateway-v4/app/models.py",  # not a marker — ignored
    ]

    created = orchestrator._scaffold_package_markers(paths)

    assert created == ["gateway-v4/tests/__init__.py"]
    init_file = tmp_path / "gateway-v4" / "tests" / "__init__.py"
    assert init_file.exists()
    assert init_file.read_text(encoding="utf-8") == ""
    # Idempotent: an existing marker is neither recreated nor re-reported.
    assert orchestrator._scaffold_package_markers(paths) == []


def test_task_designer_contract_retry_gives_up_after_max(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator.logger = types.SimpleNamespace(info=lambda *a, **k: None)
    orchestrator._task_designer_feedback_for_prompt = False
    orchestrator._run_agent = lambda agent, phase, *, index, total: True
    orchestrator._load_saved_agent_report = lambda phase, name, run_dir=None: {}
    orchestrator._apply_task_designer_contract_from_report = lambda report: False

    ok = orchestrator._apply_task_designer_contract_with_retry(
        {"name": "task-designer"}, index=3, total=6, max_retries=2
    )

    assert ok is False
    assert orchestrator._task_designer_feedback_for_prompt is False


def _write_sync_db_stack(tmp_path):
    database_path = tmp_path / "gateway-v4" / "app" / "database.py"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database_path.write_text(
        "from sqlalchemy import create_engine\n"
        "from sqlalchemy.orm import declarative_base, sessionmaker, Session\n"
        "engine = create_engine('sqlite://')\n"
        "SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)\n"
        "Base = declarative_base()\n"
        "def get_db():\n"
        "    db = SessionLocal()\n"
        "    try:\n"
        "        yield db\n"
        "    finally:\n"
        "        db.close()\n",
        encoding="utf-8",
    )
    return database_path


def _write_async_db_stack(tmp_path):
    database_path = tmp_path / "gateway-v4" / "app" / "database.py"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database_path.write_text(
        "from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession\n"
        "engine = create_async_engine('sqlite+aiosqlite://')\n"
        "AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession)\n"
        "async def get_async_session():\n"
        "    async with AsyncSessionLocal() as session:\n"
        "        yield session\n",
        encoding="utf-8",
    )
    return database_path


def test_extract_db_architecture_detects_sync_stack(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    _write_sync_db_stack(tmp_path)

    arch = orchestrator._extract_db_architecture()

    assert arch["found"] is True
    assert arch["is_async"] is False
    assert arch["engine_call"] == "create_engine"
    assert arch["session_factory"] == "SessionLocal"
    assert arch["session_dependency"] == "get_db"
    assert arch["dependency_is_async"] is False
    assert arch["db_module"] == "gateway-v4/app/database.py"


def test_extract_db_architecture_detects_async_stack(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    _write_async_db_stack(tmp_path)

    arch = orchestrator._extract_db_architecture()

    assert arch["found"] is True
    assert arch["is_async"] is True
    assert arch["engine_call"] == "create_async_engine"
    assert arch["session_dependency"] == "get_async_session"
    assert arch["dependency_is_async"] is True


def test_architecture_ground_truth_note_sync_warns_against_async_db(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    _write_sync_db_stack(tmp_path)

    note = orchestrator._build_architecture_ground_truth_note()

    assert "SYNCHRONOUS" in note
    assert "get_db" in note
    assert "SessionLocal" in note
    # Must steer agents away from the exact contradiction that blocked TASK-002.
    assert "async def" in note
    assert "asyncio.to_thread" in note


def test_contract_demands_async_db_flags_async_on_sync_stack(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    _write_sync_db_stack(tmp_path)

    # The exact shape of the TASK-002 contract QA could never accept.
    bad_contract = {
        "must_contain": [
            "class PerformanceMonitor:",
            "async def record_request_metrics(",
            "def __init__(self, db_session",
        ],
        "must_import": [
            "from sqlalchemy.orm import Session",
            "from app.models import ProviderMetrics",
        ],
        "integration": ["All database operations must be asynchronous"],
        "forbidden": ["Do not add synchronous blocking database calls"],
        "must_test": ["test_record_request_metrics_creates_new_entry: assert row created"],
    }

    reason = orchestrator._contract_demands_async_db(bad_contract)

    assert reason
    assert "synchronous" in reason.lower()


def test_contract_demands_async_db_passes_clean_sync_contract(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    _write_sync_db_stack(tmp_path)

    good_contract = {
        "must_contain": [
            "class PerformanceMonitor:",
            "def record_request_metrics(self, db: Session",
        ],
        "must_import": ["from sqlalchemy.orm import Session"],
        "integration": ["Use the injected db session passed by the caller"],
        "forbidden": ["Do not modify billing routes"],
        "must_test": ["test_record_request_metrics: assert provider_metrics row is created"],
    }

    assert orchestrator._contract_demands_async_db(good_contract) == ""


def test_contract_demands_async_db_noop_when_stack_unknown(tmp_path) -> None:
    # No database module in the workspace -> detection finds nothing -> guardrail must not fire.
    orchestrator = make_orchestrator_with_workspace(tmp_path)

    bad_contract = {
        "must_contain": ["async def record_request_metrics("],
        "must_import": ["from sqlalchemy.orm import Session"],
        "integration": ["All database operations must be asynchronous"],
    }

    assert orchestrator._contract_demands_async_db(bad_contract) == ""


def test_default_scope_policy_is_project_agnostic() -> None:
    # The engine default must name no project. Only universal infra is fenced; sensitive
    # files come from keyword-derivation; a project declares its own paths.
    policy = WorkflowOrchestrator._default_implementation_scope_policy()
    assert "gateway-v4" not in repr(policy)
    assert policy["allowed_paths"] == []
    assert any("Dockerfile" in pattern for pattern in policy["forbidden_paths"])
    assert "billing" in policy["forbidden_keywords"]


def test_scope_policy_per_project_override_wins_and_unions_forbidden(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator.config = {"workflow": {}}
    orchestrator.project_settings = {
        "implementation_scope_policy": {
            "allowed_paths": ["app/models.py"],
            "forbidden_paths": ["frontend/*"],
        }
    }

    policy = orchestrator._get_implementation_scope_policy()

    # Project defines its editable surface -> replace.
    assert policy["allowed_paths"] == ["app/models.py"]
    # Project ADDS to the safety fence; the universal infra forbid stays (union).
    assert "frontend/*" in policy["forbidden_paths"]
    assert any("Dockerfile" in pattern for pattern in policy["forbidden_paths"])


def test_forbidden_paths_derived_from_repo_map_keywords(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator.implementation_scope_policy = {
        "forbidden_paths": ["Dockerfile"],
        "forbidden_keywords": ["billing", "payment", "marketplace"],
    }
    orchestrator._repo_map_cache = {
        "files": [
            {"path": "gateway-v4/app/routers/billing.py"},
            {"path": "app/services/payment_gateway.py"},
            {"path": "app/models.py"},
        ]
    }

    effective = orchestrator._effective_forbidden_paths()

    assert "Dockerfile" in effective  # declared kept
    assert "gateway-v4/app/routers/billing.py" in effective  # derived from 'billing'
    assert "app/services/payment_gateway.py" in effective  # derived from 'payment'
    assert "app/models.py" not in effective  # no sensitive keyword -> editable


def test_getaway_protection_now_comes_from_project_settings(tmp_path) -> None:
    # Regression: getaway's exact protections survive, but declared by the project, not baked
    # into the engine. Same mechanism would carry any other project's truth.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator.config = {"workflow": {}}
    orchestrator.project_settings = {
        "implementation_scope_policy": {
            "allowed_paths": ["gateway-v4/app/models.py", "gateway-v4/app/database.py"],
            "forbidden_paths": ["gateway-v4/app/services/auth.py", "frontend/*"],
        }
    }

    policy = orchestrator._get_implementation_scope_policy()

    assert policy["allowed_paths"] == ["gateway-v4/app/models.py", "gateway-v4/app/database.py"]
    assert "gateway-v4/app/services/auth.py" in policy["forbidden_paths"]


def test_architecture_profile_verified_for_sync_sqlalchemy(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    _write_sync_db_stack(tmp_path)  # gateway-v4/app/database.py, sync SQLAlchemy
    (tmp_path / "gateway-v4" / "requirements.txt").write_text("fastapi\nsqlalchemy\n", encoding="utf-8")
    orchestrator._repo_map_cache = {
        "files": [
            {"path": "gateway-v4/app/database.py"},
            {"path": "gateway-v4/requirements.txt"},
            {"path": "gateway-v4/alembic/versions/0006_x.py"},
            {"path": "gateway-v4/tests/test_x.py"},
        ]
    }
    orchestrator.implementation_scope_policy = {"forbidden_paths": ["frontend/*"], "forbidden_keywords": ["billing"]}

    profile = orchestrator._build_architecture_profile()

    assert profile["persistence"]["concurrency"]["value"] == "sync"
    assert profile["persistence"]["concurrency"]["confidence"] == "verified"
    assert profile["persistence"]["session_dependency"]["value"] == "get_db"
    assert profile["persistence"]["migrations"]["value"]["tool"] == "alembic"
    assert "python" in profile["language"]["value"]
    assert profile["layout"]["tests_root"]["value"] == "gateway-v4/tests"
    assert profile["target_architecture"] is None  # reserved for the architect
    # Rendered note surfaces the injected-session rule.
    note = orchestrator._render_architecture_profile_note()
    assert "get_db" in note and "sync" in note


def test_architecture_profile_unknown_persistence_for_raw_dbapi(tmp_path) -> None:
    # Oil-like: raw mysql-connector, no SQLAlchemy -> the deterministic detector can't tell,
    # so persistence is left unknown for a profiler agent to fill (as inferred) later.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    (tmp_path / "requirements.txt").write_text("fastapi\nmysql-connector-python\n", encoding="utf-8")
    (tmp_path / "Database.py").write_text("import mysql.connector\n\ndef q(cur):\n    cur.execute('SELECT 1')\n", encoding="utf-8")
    orchestrator._repo_map_cache = {"files": [{"path": "requirements.txt"}, {"path": "Database.py"}]}
    orchestrator.implementation_scope_policy = {"forbidden_paths": [], "forbidden_keywords": []}

    profile = orchestrator._build_architecture_profile()

    assert profile["persistence"]["concurrency"]["confidence"] == "unknown"
    assert profile["persistence"]["migrations"]["value"]["tool"] == "none"
    assert "python" in profile["language"]["value"]
    assert any("profiler agent" in question for question in profile["open_questions"])
    assert any("test harness" in question for question in profile["open_questions"])


def test_profiler_agent_fills_gaps_without_overriding_verified(tmp_path) -> None:
    # Oil-like repo: deterministic profile leaves persistence unknown -> agent worth running.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    (tmp_path / "requirements.txt").write_text("fastapi\nmysql-connector-python\n", encoding="utf-8")
    (tmp_path / "Database.py").write_text("import mysql.connector\n", encoding="utf-8")
    orchestrator._repo_map_cache = {"files": [{"path": "requirements.txt"}, {"path": "Database.py"}]}
    orchestrator.implementation_scope_policy = {"forbidden_paths": [], "forbidden_keywords": []}

    profile = orchestrator._build_architecture_profile()
    assert orchestrator._architecture_profile_has_gaps(profile) is True

    merged = orchestrator._merge_profiler_agent_output(
        profile,
        {
            "persistence": {
                "access": {"value": "raw_dbapi", "source": "cursor.execute in Database.py"},
                "concurrency": {"value": "sync", "source": "no await on DB calls"},
                "session_pattern": "UseDatabase context manager",
            },
            "conventions": "Flat module layout; FastAPI in main.py; no ORM.",
        },
    )

    # Agent-filled gaps are now present but marked inferred (not verified).
    assert merged["persistence"]["access"]["value"] == "raw_dbapi"
    assert merged["persistence"]["access"]["confidence"] == "inferred"
    assert merged["persistence"]["concurrency"]["value"] == "sync"
    assert merged["persistence"]["concurrency"]["confidence"] == "inferred"
    assert merged["persistence"]["session_pattern"]["value"] == "UseDatabase context manager"
    assert merged["conventions"]["confidence"] == "inferred"
    # The answered open-question is dropped.
    assert not any("concurrency/access" in question for question in merged["open_questions"])


def test_profiler_merge_never_overrides_verified_facts(tmp_path) -> None:
    # Sync SQLAlchemy repo: concurrency is verified -> a contradicting agent claim is ignored.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    _write_sync_db_stack(tmp_path)
    (tmp_path / "gateway-v4" / "requirements.txt").write_text("sqlalchemy\n", encoding="utf-8")
    orchestrator._repo_map_cache = {"files": [{"path": "gateway-v4/app/database.py"}, {"path": "gateway-v4/requirements.txt"}]}
    orchestrator.implementation_scope_policy = {"forbidden_paths": [], "forbidden_keywords": []}

    profile = orchestrator._build_architecture_profile()
    assert orchestrator._architecture_profile_has_gaps(profile) is False  # all verified -> skip agent

    merged = orchestrator._merge_profiler_agent_output(
        profile, {"persistence": {"concurrency": {"value": "async", "source": "hallucinated"}}}
    )
    # Verified ground truth is preserved; the agent cannot flip it.
    assert merged["persistence"]["concurrency"]["value"] == "sync"
    assert merged["persistence"]["concurrency"]["confidence"] == "verified"


def test_render_profile_note_includes_sync_directive(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    _write_sync_db_stack(tmp_path)
    (tmp_path / "gateway-v4" / "requirements.txt").write_text("sqlalchemy\n", encoding="utf-8")
    orchestrator._repo_map_cache = {"files": [{"path": "gateway-v4/app/database.py"}, {"path": "gateway-v4/requirements.txt"}]}
    orchestrator.implementation_scope_policy = {"forbidden_paths": [], "forbidden_keywords": []}

    note = orchestrator._render_architecture_profile_note()

    assert "Project Architecture Profile" in note
    assert "MATCH THIS" in note
    assert "async" in note  # the directive warns against introducing async DB
    assert "get_db" in note


def test_agent_handoff_card_is_russian(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    bundle = {
        "repository_context_chars": 1200,
        "previous_context": "architect plan...",
        "selected_task_id": "TASK-002",
        "selected_task_scope": "Add performance monitoring service",
        "developer_feedback_source": "",
    }

    title, lines = orchestrator._build_agent_handoff_card("task-designer", "implementation", bundle)

    assert title == "Передача: Планировщик -> Конструктор задачи"
    assert any(line.startswith("Должен:") for line in lines)
    assert any("архитектурный профиль" in line for line in lines)
    assert any("TASK-002" in line for line in lines)


def test_agent_result_summary_is_russian(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator._selected_implementation_item = {
        "id": "TASK-002",
        "contract_source": "task-designer",
        "must_contain": ["class PerformanceMonitor:", "def record("],
        "must_test": ["test_record"],
    }
    assert orchestrator._build_agent_result_summary("task-designer") == "контракт для TASK-002: 2 требований, 1 тестов"

    orchestrator._validated_backlog_task_count = 3
    assert orchestrator._build_agent_result_summary("implementation-planner") == "бэклог: 3 задач"
    assert orchestrator._build_agent_result_summary("architect") == "архитектурный план готов"
    # Unknown/blank cases stay silent (no card).
    orchestrator._selected_implementation_item = {"id": "TASK-009", "contract_source": ""}
    assert orchestrator._build_agent_result_summary("task-designer") == ""


def test_codebase_profiler_step_skips_when_no_gaps(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    _write_sync_db_stack(tmp_path)  # verified sync SQLAlchemy -> no gaps
    (tmp_path / "gateway-v4" / "requirements.txt").write_text("sqlalchemy\n", encoding="utf-8")
    orchestrator._repo_map_cache = {"files": [{"path": "gateway-v4/app/database.py"}, {"path": "gateway-v4/requirements.txt"}]}
    orchestrator.implementation_scope_policy = {"forbidden_paths": [], "forbidden_keywords": []}
    orchestrator.project_state_dir = tmp_path / "state"
    orchestrator.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    calls: list[int] = []
    orchestrator._run_agent = lambda *a, **k: calls.append(1) or True

    orchestrator._run_codebase_profiler_step({"name": "codebase-profiler"}, index=1, total=7)

    assert calls == []  # agent not invoked when there is nothing to fill
    assert (tmp_path / "state" / "context" / "architecture_profile.json").exists()


def test_codebase_profiler_step_runs_and_merges_on_gaps(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    (tmp_path / "requirements.txt").write_text("fastapi\nmysql-connector-python\n", encoding="utf-8")
    (tmp_path / "Database.py").write_text("import mysql.connector\n", encoding="utf-8")
    orchestrator._repo_map_cache = {"files": [{"path": "requirements.txt"}, {"path": "Database.py"}]}
    orchestrator.implementation_scope_policy = {"forbidden_paths": [], "forbidden_keywords": []}
    orchestrator.project_state_dir = tmp_path / "state"
    orchestrator.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    orchestrator._run_agent = lambda *a, **k: True
    orchestrator._load_saved_agent_report = lambda phase, name, run_dir=None: {
        "parsed_output": '{"persistence": {"access": {"value": "raw_dbapi", "source": "Database.py"}, "concurrency": {"value": "sync", "source": "no await"}}}'
    }

    orchestrator._run_codebase_profiler_step({"name": "codebase-profiler"}, index=1, total=7)

    text = (tmp_path / "state" / "context" / "architecture_profile.json").read_text(encoding="utf-8")
    assert '"value": "raw_dbapi"' in text
    assert '"confidence": "inferred"' in text  # agent fills as inferred, never verified


def _sync_stack_orchestrator(tmp_path):
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    _write_sync_db_stack(tmp_path)
    (tmp_path / "gateway-v4" / "requirements.txt").write_text("sqlalchemy\n", encoding="utf-8")
    orchestrator._repo_map_cache = {"files": [{"path": "gateway-v4/app/database.py"}, {"path": "gateway-v4/requirements.txt"}]}
    orchestrator.implementation_scope_policy = {"forbidden_paths": [], "forbidden_keywords": []}
    orchestrator.project_state_dir = tmp_path / "state"
    return orchestrator


def test_target_architecture_flags_async_on_sync(tmp_path) -> None:
    orchestrator = _sync_stack_orchestrator(tmp_path)
    warnings: list[str] = []
    orchestrator.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda m, *a, **k: warnings.append(m))
    orchestrator._load_saved_agent_report = lambda phase, name, run_dir=None: {
        "parsed_output": "## Target architecture\nAdd asynchronous database operations to the monitor service."
    }

    orchestrator._capture_target_architecture_from_architect()

    target = orchestrator._build_architecture_profile()["target_architecture"]
    assert target and target["fits_current"] is False  # async target on a verified-sync stack
    assert warnings  # the design-level guard warned
    note = orchestrator._render_architecture_profile_note()
    assert "Target architecture" in note and "WARNING" in note


def test_target_architecture_compatible_is_carried(tmp_path) -> None:
    orchestrator = _sync_stack_orchestrator(tmp_path)
    orchestrator.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None)
    orchestrator._load_saved_agent_report = lambda phase, name, run_dir=None: {
        "parsed_output": "## Target architecture\nPerformanceMonitor service uses the injected synchronous db session."
    }

    orchestrator._capture_target_architecture_from_architect()

    assert orchestrator._build_architecture_profile()["target_architecture"]["fits_current"] is True
    # Persisted -> survives a fresh rebuild (carried across runs).
    orchestrator._architecture_profile_cache = None
    reloaded = orchestrator._build_architecture_profile()["target_architecture"]
    assert reloaded and "PerformanceMonitor" in reloaded["summary"]


def test_research_agents_force_final_instead_of_giving_up(tmp_path) -> None:
    # Regression: a research agent that spends its whole retrieval budget reading must be forced
    # to finalize (not return "Exceeded retrieval rounds"). Surfaced by a live Oil run.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    assert orchestrator._should_force_final_after_retrieval_limit("research", "project-analyst") is True
    assert orchestrator._should_force_final_after_retrieval_limit("research", "competitor-analyst") is True
    assert orchestrator._should_force_final_after_retrieval_limit("implementation", "qa") is False
    assert orchestrator._should_force_final_after_retrieval_limit("deployment", "x") is False


def test_safe_is_file_never_raises(tmp_path) -> None:
    real = tmp_path / "real.txt"
    real.write_text("x", encoding="utf-8")
    assert WorkflowOrchestrator._safe_is_file(real) is True
    assert WorkflowOrchestrator._safe_is_file(tmp_path / "missing") is False


def test_target_tests_file_list_excludes_db_data_and_survives(tmp_path) -> None:
    # Regression: a MySQL db_data/ dir in the target (with special/socket files) crashed the
    # rglob+is_file scan on Windows. Now db_data is excluded and is_file is stat-safe.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_orders.py").write_text("x", encoding="utf-8")
    (tmp_path / "db_data").mkdir()
    (tmp_path / "db_data" / "test_lookalike.bin").write_text("x", encoding="utf-8")

    out = orchestrator._build_target_tests_file_list()

    assert "tests/test_orders.py" in out
    assert "db_data" not in out  # MySQL data dir excluded


def test_network_failure_stops_phase_fast(tmp_path) -> None:
    # Regression: a DNS/connection failure made every agent fail and the phase auto-continued
    # through all of them. Now the first connectivity error stops the phase immediately.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator._network_unreachable = False
    orchestrator._phase_failure_status = None
    orchestrator.logger = types.SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, error=lambda *a, **k: None)
    orchestrator._phase_cost_limit_exceeded = lambda phase: False
    orchestrator._wait_for_user = lambda prompt: True
    ran: list[str] = []

    def fake_run_agent(agent, phase, *, index=None, total=None):
        ran.append(agent["name"])
        orchestrator._network_unreachable = True  # simulate a connectivity failure inside the agent
        return False

    orchestrator._run_agent = fake_run_agent
    phase = {"agents": [{"name": "project-analyst"}, {"name": "competitor-analyst"}, {"name": "market-analyst"}]}

    ok = orchestrator._run_phase_agents(phase, "research")

    assert ok is False
    assert ran == ["project-analyst"]  # aborted after the first; did not grind the rest into the wall
    assert orchestrator._phase_failure_status == "network_unreachable"


def test_default_branch_prefers_detected_over_config(tmp_path) -> None:
    # Regression: git rollback hardcoded "main"; a repo on "master" (Oil) crashed the rollback.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator.config = {"project": {"default_branch": "main"}}
    orchestrator._default_branch_cache = ""
    orchestrator._detect_default_branch = lambda: "master"
    assert orchestrator._default_branch() == "master"  # real repo branch wins over config default


def test_default_branch_falls_back_to_config_then_main(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator.config = {"project": {"default_branch": "develop"}}
    orchestrator._default_branch_cache = ""
    orchestrator._detect_default_branch = lambda: ""
    assert orchestrator._default_branch() == "develop"

    orchestrator2 = make_orchestrator_with_workspace(tmp_path)
    orchestrator2.config = {}
    orchestrator2._default_branch_cache = ""
    orchestrator2._detect_default_branch = lambda: ""
    assert orchestrator2._default_branch() == "main"


def test_missing_must_contain_flags_absent_symbols_robustly() -> None:
    content = "def get_customer_analytics(cfg):\n    return []\n\nclass Foo:\n    pass\n"
    must = [
        "def get_customer_analytics(config: dict) -> list[dict]:",  # present by NAME despite different signature
        "def get_product_analytics(config: dict) -> list[dict]:",   # ABSENT
        "class Foo:",                                                # present
        "class Bar:",                                                # ABSENT
        "with UseDatabase(config) as cursor:",                       # free-form line -> NOT gated (no false positive)
        "profit = revenue - cost",                                   # free-form line -> NOT gated
    ]
    missing = WorkflowOrchestrator._missing_must_contain(content, must)
    assert "def get_product_analytics(config: dict) -> list[dict]:" in missing
    assert "class Bar:" in missing
    assert "def get_customer_analytics(config: dict) -> list[dict]:" not in missing  # name defined
    assert "class Foo:" not in missing
    assert "with UseDatabase(config) as cursor:" not in missing  # free-form -> ignored
    assert "profit = revenue - cost" not in missing


def test_missing_must_contain_findings_gives_exact_repair_lines(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    (tmp_path / "main.py").write_text("def get_customer_analytics(cfg):\n    return []\n", encoding="utf-8")
    item = {
        "target_file": {"path": "main.py"},
        "must_contain": [
            "def get_customer_analytics(config: dict) -> list[dict]:",
            "def get_summary_analytics(config: dict) -> dict:",  # absent
        ],
    }

    findings = orchestrator._missing_must_contain_findings(item, ["main.py"])

    assert any("must_contain" in line for line in findings)
    assert any("get_summary_analytics" in line for line in findings)
    assert not any(line.strip() == "- def get_customer_analytics(config: dict) -> list[dict]:" for line in findings)

    # Only gates a file the developer changed this attempt.
    assert orchestrator._missing_must_contain_findings(item, []) == []

    # All symbols present -> no findings.
    (tmp_path / "main.py").write_text(
        "def get_customer_analytics(c):\n    pass\n\ndef get_summary_analytics(c):\n    pass\n", encoding="utf-8"
    )
    assert orchestrator._missing_must_contain_findings(item, ["main.py"]) == []


def test_parse_create_tables_extracts_real_columns_skips_constraints() -> None:
    sql = (
        'cursor.execute("""CREATE TABLE IF NOT EXISTS order_details (\n'
        '    id INT PRIMARY KEY AUTO_INCREMENT,\n'
        '    order_id INT,\n'
        '    oil_name VARCHAR(255),\n'
        '    volume DECIMAL(10,2),\n'
        '    count INT,\n'
        '    price DECIMAL(10,2),\n'
        '    FOREIGN KEY (order_id) REFERENCES orders(id)\n'
        ')""")\n'
        'cursor.execute("""CREATE TABLE production_profiles (\n'
        '    id INT, oil_name VARCHAR(100), batch_seed_weight_kg DECIMAL(10,2), yield_percent FLOAT)""")\n'
    )
    tables = dict(WorkflowOrchestrator._parse_create_tables(sql))
    assert tables["order_details"] == ["id", "order_id", "oil_name", "volume", "count", "price"]  # FK line skipped, DECIMAL(10,2) not split
    assert "batch_seed_weight_kg" in tables["production_profiles"]
    assert "yield_percent" in tables["production_profiles"]
    assert "seed_weight_kg" not in tables["production_profiles"]  # the hallucinated name is genuinely absent


def test_raw_sql_schema_note_lists_real_tables(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    (tmp_path / "Database.py").write_text(
        'def create(cur):\n    cur.execute("""CREATE TABLE IF NOT EXISTS order_details (id INT, count INT, price DECIMAL(10,2))""")\n',
        encoding="utf-8",
    )
    orchestrator._repo_map_cache = {"files": [{"path": "Database.py"}]}
    orchestrator._raw_sql_schema_cache = None
    orchestrator._raw_sql_relations_cache = None

    note = orchestrator._build_raw_sql_schema_note()

    assert "order_details" in note
    assert "count" in note
    assert "EXACT" in note


def test_raw_sql_schema_note_lists_fk_join_paths_and_unlinked_tables(tmp_path) -> None:
    # Regression: the architect planned per-customer cost because the ground truth showed
    # production cost COLUMNS but hid that production_batches has NO join path to orders.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    (tmp_path / "Database.py").write_text(
        'def create(cur):\n'
        '    cur.execute("""CREATE TABLE IF NOT EXISTS customers (id INT PRIMARY KEY, name VARCHAR(50))""")\n'
        '    cur.execute("""CREATE TABLE IF NOT EXISTS orders (\n'
        '        id INT PRIMARY KEY,\n'
        '        customer_id INT NOT NULL,\n'
        '        FOREIGN KEY (customer_id) REFERENCES customers(id)\n'
        '    )""")\n'
        '    cur.execute("""CREATE TABLE IF NOT EXISTS production_batches (id INT, labor_cost FLOAT)""")\n',
        encoding="utf-8",
    )
    orchestrator._repo_map_cache = {"files": [{"path": "Database.py"}]}
    orchestrator._raw_sql_schema_cache = None
    orchestrator._raw_sql_relations_cache = None

    note = orchestrator._build_raw_sql_schema_note()

    assert "orders.customer_id -> customers.id" in note
    assert "NO declared foreign-key link" in note
    assert "production_batches" in note.split("NO declared foreign-key link", 1)[1]
    assert "do NOT invent a join" in note


def test_parse_foreign_keys_handles_constraint_and_inline_forms() -> None:
    sql = (
        'CREATE TABLE order_details (\n'
        '    id INT PRIMARY KEY,\n'
        '    order_id INT,\n'
        '    FOREIGN KEY (order_id) REFERENCES orders(id)\n'
        ');\n'
        'CREATE TABLE payments (\n'
        '    id INT,\n'
        '    order_id INT REFERENCES orders (id)\n'
        ');\n'
    )
    relations = WorkflowOrchestrator._parse_foreign_keys(sql)
    assert ("order_details", "order_id", "orders", "id") in relations
    assert ("payments", "order_id", "orders", "id") in relations


def test_qa_verdict_recognizes_rejection_phrasings() -> None:
    # Regression: QA said "Не принято" but the engine only knew "не пройдено" -> false pass -> merge.
    verdict = WorkflowOrchestrator._extract_qa_verdict
    assert verdict("Вердикт QA:\nНе принято. Обнаружены критические регрессии.") == "failed"
    assert verdict("Вердикт QA: Не пройдено.") == "failed"
    assert verdict("Вердикт QA: Отклонено") == "failed"
    assert verdict("Вердикт QA:\nПринято. Всё ок.") == "passed"
    assert verdict("Вердикт QA: Пройдено") == "passed"
    assert verdict("no verdict line here") == ""
    # run_20260611_164017: this phrasing was not recognized -> qa marked [успех] on a rejected result.
    assert verdict("Вердикт QA:\nБлокирующие замечания. Реализация частичная и контракт TASK-002 не выполнен.") == "failed"
    assert verdict("Вердикт QA: Контракт не выполнен") == "failed"
    # Negated-clean phrasing must stay a pass despite containing the 'блокирующ' stem.
    assert verdict("Вердикт QA: Принято, блокирующих замечаний нет.") == "passed"


def test_reference_symbol_ground_truth_confirms_existing_symbols(tmp_path) -> None:
    # run_20260701_181702: task-designer refused TASK-003 claiming fetchCustomerAnalytics/etc.
    # were missing from api.js, though they exist past the excerpt cut-off. The engine now
    # greps the real file and injects a deterministic EXISTS/not-found confirmation.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    (tmp_path / "frontend" / "src" / "lib").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src" / "components").mkdir(parents=True, exist_ok=True)
    api = "\n".join(["export const api = {}"] + ["// filler"] * 200 + [
        "export const fetchCustomerAnalytics = async () => {}",
        "export const fetchProductAnalytics = async () => {}",
        "export const fetchSummaryAnalytics = async () => {}",
    ])
    (tmp_path / "frontend" / "src" / "lib" / "api.js").write_text(api, encoding="utf-8")
    (tmp_path / "frontend" / "src" / "components" / "AdminAnalytics.jsx").write_text(
        "import { getAnalytics } from '../lib/api'\n", encoding="utf-8"
    )
    orchestrator._selected_implementation_item = {
        "id": "TASK-003",
        "target_file": {"path": "frontend/src/components/AdminAnalytics.jsx", "action": "update"},
        "allowed_paths": ["frontend/src/components/AdminAnalytics.jsx"],
        "existing_paths": ["frontend/src/components/AdminAnalytics.jsx"],
        "reference_files": ["frontend/src/lib/api.js"],
        "acceptance_criteria": [
            "AdminAnalytics.jsx imports fetchCustomerAnalytics, fetchProductAnalytics, fetchSummaryAnalytics from ../lib/api",
            "AdminAnalytics.jsx calls fetchCustomerAnalytics, fetchProductAnalytics and fetchSummaryAnalytics",
        ],
    }
    note = orchestrator._build_reference_symbol_ground_truth()
    assert "fetchCustomerAnalytics: EXISTS in frontend/src/lib/api.js" in note
    assert "fetchProductAnalytics: EXISTS" in note
    assert "fetchSummaryAnalytics: EXISTS" in note
    assert "Do NOT declare an EXISTS symbol missing" in note


def test_planner_outline_flags_multi_file_task_for_split(tmp_path) -> None:
    # Treat the cause one step earlier than the contract guard: the planner must emit one
    # edited source file per task. A bundled outline (TASK-003: api.js + AdminAnalytics.jsx)
    # is rejected at planner validation so the planner re-emits split, depends_on-linked tasks.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    (tmp_path / "frontend" / "src" / "lib").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src" / "components").mkdir(parents=True, exist_ok=True)
    (tmp_path / "frontend" / "src" / "lib" / "api.js").write_text("export const api = {}\n", encoding="utf-8")
    (tmp_path / "frontend" / "src" / "components" / "AdminAnalytics.jsx").write_text("export default function A(){}\n", encoding="utf-8")
    repo_map = {
        "files": [
            {"path": "frontend/src/lib/api.js"},
            {"path": "frontend/src/components/AdminAnalytics.jsx"},
        ],
        "directories": ["frontend", "frontend/src", "frontend/src/lib", "frontend/src/components"],
    }
    bundled = {
        "id": "TASK-003",
        "allowed_paths": ["frontend/src/lib/api.js", "frontend/src/components/AdminAnalytics.jsx"],
        "existing_paths": ["frontend/src/lib/api.js", "frontend/src/components/AdminAnalytics.jsx"],
        "new_files": [],
        "required_test_paths": [],
        "target_file": {"path": "frontend/src/lib/api.js", "action": "update"},
        "test_file": {},
        "acceptance_criteria": [
            "api.js exports fetchCustomerAnalytics",
            "AdminAnalytics.jsx calls the new API functions",
        ],
    }
    errors = orchestrator._validate_planner_task_outline(bundled, repo_map=repo_map)
    assert any("multi_file_task_must_be_split" in e and "AdminAnalytics.jsx" in e for e in errors)

    # Split single-file task (only api.js named in its criterion) passes the guard.
    single = dict(bundled)
    single["allowed_paths"] = ["frontend/src/lib/api.js"]
    single["existing_paths"] = ["frontend/src/lib/api.js"]
    single["acceptance_criteria"] = ["api.js exports fetchCustomerAnalytics, fetchProductAnalytics"]
    errors = orchestrator._validate_planner_task_outline(single, repo_map=repo_map)
    assert not any("multi_file_task_must_be_split" in e for e in errors)


def test_contract_validation_flags_multi_file_edit_task(tmp_path) -> None:
    # run_20260613_131603: TASK-003 needed edits to BOTH api.js and AdminAnalytics.jsx, but a
    # single contract edits one target file; the second file was never editable, so QA rejected
    # it on every attempt until the run died. The guard now fails fast with an actionable reason.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    base = {
        "id": "TASK-003",
        "scope": "frontend-only",
        "target_file": {"path": "frontend/src/lib/api.js", "action": "update"},
        "test_file": {},
        "existing_paths": ["frontend/src/lib/api.js", "frontend/src/components/AdminAnalytics.jsx"],
        "new_files": [],
        "required_test_paths": [],
        "must_contain": ["export const fetchCustomerAnalytics =", "const response = await fetch("],
        "must_test": [],
        "_target_file_declared": True,
        "_depends_on_declared": True,
        "_must_contain_declared": True,
    }
    # A criterion names a SECOND allowed file as needing changes, but only api.js is the
    # editable target -> the second file is unreachable -> flagged.
    two_file = dict(base)
    two_file["allowed_paths"] = ["frontend/src/lib/api.js", "frontend/src/components/AdminAnalytics.jsx"]
    two_file["acceptance_criteria"] = [
        "api.js exports fetchCustomerAnalytics",
        "AdminAnalytics.jsx calls the new API functions",
    ]
    errors = orchestrator._validate_backend_task_contract(two_file)
    assert any("task_needs_split_multiple_editable_files" in e and "AdminAnalytics.jsx" in e for e in errors)

    # A passive dependency artifact in allowed_paths that NO criterion asks to change
    # (e.g. a package marker) is NOT flagged.
    passive = dict(base)
    passive["allowed_paths"] = ["frontend/src/lib/api.js", "frontend/src/components/AdminAnalytics.jsx"]
    passive["acceptance_criteria"] = ["api.js exports the analytics fetch functions"]
    errors = orchestrator._validate_backend_task_contract(passive)
    assert not any("task_needs_split_multiple_editable_files" in e for e in errors)


def test_contract_validation_allows_testless_frontend_task(tmp_path) -> None:
    # run_20260611_215559: a frontend-only task has no required_test_paths, but the contract
    # validator demanded a test_file whose path must come from that EMPTY list - unsatisfiable,
    # so the designer could never produce a valid contract.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    base = {
        "id": "TASK-003",
        "title": "Add AdminAnalytics component API client stub",
        "scope": "frontend-only",
        "allowed_paths": ["frontend/src/lib/api.js", "frontend/src/components/AdminAnalytics.jsx"],
        "existing_paths": ["frontend/src/lib/api.js", "frontend/src/components/AdminAnalytics.jsx"],
        "new_files": [],
        "required_test_paths": [],
        "target_file": {"path": "frontend/src/lib/api.js", "action": "update"},
        "test_file": {},
        "must_contain": ["fetchCustomerAnalytics(", "fetchProductAnalytics("],
        "must_test": [],
        "_target_file_declared": True,
        "_test_file_declared": False,
        "_depends_on_declared": True,
        "_must_contain_declared": True,
        "_must_test_declared": False,
    }
    assert orchestrator._validate_backend_task_contract(dict(base)) == []

    # A backend task still demands the test contract.
    backend = dict(base)
    backend.update(
        {
            "title": "Add analytics helpers",
            "scope": "backend-only",
            "allowed_paths": ["main.py"],
            "existing_paths": ["main.py"],
            "target_file": {"path": "main.py", "action": "update"},
        }
    )
    backend_errors = orchestrator._validate_backend_task_contract(backend)
    assert any("missing_test_file_contract" in error for error in backend_errors)
    assert any("must_test_empty" in error for error in backend_errors)


def test_obsolescence_claim_disproven_when_endpoints_absent(tmp_path) -> None:
    # run_20260611_213539: designer declared TASK-003 obsolete because getAnalytics() exists,
    # but it calls /admin/analytics - the required /api/analytics/* paths appear nowhere.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    (tmp_path / "api.js").write_text(
        "export const api = { getAnalytics: () => request('/admin/analytics') }\n", encoding="utf-8"
    )
    item = {
        "id": "TASK-003",
        "title": "Wire AdminAnalytics to analytics endpoints",
        "acceptance_criteria": [
            "fetch /api/analytics/customers with date filters",
            "fetch /api/analytics/products",
        ],
        "allowed_paths": ["api.js"],
    }
    supported, missing = orchestrator._check_obsolescence_claim(item)
    assert supported is False
    assert "/api/analytics/customers" in missing and "/api/analytics/products" in missing

    # And when the file really wires those paths, the claim stands.
    (tmp_path / "api.js").write_text(
        "request('/api/analytics/customers'); request('/api/analytics/products')\n", encoding="utf-8"
    )
    supported, missing = orchestrator._check_obsolescence_claim(item)
    assert supported is True and missing == []


def test_obsolescence_claim_disproven_for_identifier_criteria(tmp_path) -> None:
    # run_20260611_214950: TASK-003's criteria name FUNCTIONS, not URLs
    # ("api.js exports fetchCustomerAnalytics, ..."), and the URL-only extractor found
    # nothing checkable -> the claim could not be disproven -> designer refused 3x again.
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    (tmp_path / "api.js").write_text(
        "export const api = { getAnalytics: () => request('/admin/analytics') }\n", encoding="utf-8"
    )
    item = {
        "id": "TASK-003",
        "title": "Add AdminAnalytics component API client stub",
        "acceptance_criteria": [
            "api.js exports fetchCustomerAnalytics, fetchProductAnalytics, fetchSummaryAnalytics",
            "AdminAnalytics.jsx calls the new API functions",
        ],
        "allowed_paths": ["api.js"],
    }
    tokens = orchestrator._extract_task_evidence_tokens(item)
    assert {"fetchCustomerAnalytics", "fetchProductAnalytics", "fetchSummaryAnalytics"}.issubset(set(tokens))
    supported, missing = orchestrator._check_obsolescence_claim(item)
    assert supported is False
    assert "fetchCustomerAnalytics" in missing


def test_handoff_card_includes_task_essence(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    orchestrator._selected_implementation_item = {
        "id": "TASK-003",
        "title": "Add AdminAnalytics component API client stub",
        "target_file": {"path": "frontend/src/lib/api.js"},
        "acceptance_criteria": ["GET /api/analytics/customers returns customer analytics"],
    }
    title, lines = orchestrator._build_agent_handoff_card("code-developer", "implementation", {})
    joined = "\n".join(lines)
    assert "Суть: Add AdminAnalytics component API client stub -> frontend/src/lib/api.js" in joined
    assert "Критерий: GET /api/analytics/customers" in joined


def test_digest_output_lines_extracts_meaningful_summary() -> None:
    digest = WorkflowOrchestrator._digest_output_lines
    qa_output = "Вердикт QA:\nТребуется доработка.\n\n```python\ncode\n```\n- деталь раз\n"
    assert digest(qa_output, 3) == ["Вердикт QA:", "Требуется доработка.", "деталь раз"]
    assert digest("status=implemented", 3) == []  # protocol echo carries no info
    assert digest("", 3) == []


def test_must_contain_gates_decorators_quote_insensitively() -> None:
    # run_20260611_173952: qa/validator read a truncated excerpt and falsely reported the
    # registered FastAPI routes missing. Decorators are now gated deterministically by the
    # engine against the FULL file, quote- and whitespace-normalized.
    gate = WorkflowOrchestrator._missing_must_contain
    content = (
        "@app.get('/api/analytics/customers')\n"
        "def get_customer_analytics(start_date=None, end_date=None):\n"
        "    return []\n"
    )
    requirements = [
        '@app.get("/api/analytics/customers")',  # double quotes vs single in file -> still found
        '@app.get("/api/analytics/products")',   # genuinely absent -> reported
        "def get_customer_analytics(start_date: Optional[str] = None, end_date: Optional[str] = None):",
    ]
    missing = gate(content, requirements)
    assert missing == ['@app.get("/api/analytics/products")']


def test_qa_verdict_fail_closed_on_unrecognized_phrasing() -> None:
    # run_20260611_173952: 'Требуется доработка' matched no marker -> qa passed -> merged.
    verdict = WorkflowOrchestrator._extract_qa_verdict
    assert verdict("Вердикт QA:\nТребуется доработка.") == "failed"
    assert verdict("Вердикт QA: Какая-то новая формулировка отказа") == "failed"  # fail-closed
    assert verdict("Вердикт QA: ПРИНЯТО") == "passed"
    assert verdict("Итог: строки вердикта нет вовсе") == ""


def test_validator_verdict_falls_back_to_body_tokens() -> None:
    # run_20260611_173952: the validator skipped the status line; its report was full of
    # '**FAILED** - ...' sections yet was treated as success.
    verdict = WorkflowOrchestrator._extract_validator_verdict
    report_failed = "# Отчет валидации\n\n#### Эндпоинты\n**FAILED** - Отсутствуют декораторы\n\n**PASSED** - Сигнатуры верны"
    assert verdict(report_failed) == "failed"  # FAILED wins over section-level PASSED
    report_passed = "# Отчет\n**PASSED** - всё на месте"
    assert verdict(report_passed) == "passed"
    assert verdict("просто текст без вердикта") == ""


def test_dropped_tool_request_detection_and_repair_instruction() -> None:
    # run_20260611_164017: a whole-file write_file JSON was truncated by max_tokens, failed to
    # parse, and was silently accepted as a successful final answer (the write never happened).
    looks = WorkflowOrchestrator._looks_like_dropped_tool_request
    truncated = '{"tool":"write_file","path":"main.py","content":"import math\\nimport secrets'  # no closing brace
    assert looks(truncated) is True
    assert looks('```json\n{"tool":"apply_patch","path":"main.py"') is True
    assert looks("status=implemented") is False
    assert looks("") is False
    assert looks("Обычный текстовый ответ без JSON") is False
    instruction = WorkflowOrchestrator._build_truncated_write_repair_instruction()
    assert "apply_patch" in instruction
    assert "status=implemented" in instruction


def test_request_is_read_only_classifies_tools_and_batches() -> None:
    # Regression run_20260611_162021: 3 reads consumed all turns, the write never happened,
    # and the run died with no_changes. Edit agents now get write-reserved turns where
    # read-only requests are bounced with a force-write instruction.
    is_read_only = WorkflowOrchestrator._request_is_read_only
    assert is_read_only({"tool": "read_file", "path": "main.py"}) is True
    assert is_read_only({"tool": "search_text", "pattern": "def"}) is True
    assert is_read_only({"tool": "apply_patch", "path": "main.py"}) is False
    assert is_read_only({"tool": "write_file", "path": "main.py"}) is False
    assert is_read_only({"tool": "tool_batch", "requests": [{"tool": "read_file"}, {"tool": "read_files"}]}) is True
    assert is_read_only({"tool": "tool_batch", "requests": [{"tool": "read_file"}, {"tool": "apply_patch"}]}) is False


def test_force_write_instruction_names_allowed_paths_and_write_tools() -> None:
    instruction = WorkflowOrchestrator._build_force_write_instruction(
        {"selected_task_allowed_paths": ["main.py", "tests/test_analytics_api.py"]}
    )
    assert "main.py" in instruction
    assert "apply_patch" in instruction
    assert "write_file" in instruction
    assert "status=implemented" in instruction
    fallback = WorkflowOrchestrator._build_force_write_instruction({})
    assert "allowed contract paths" in fallback


def test_template_validator_verdict_is_parsed() -> None:
    # Regression: template-validator returned "## Статус: FAILED" but was marked success -> bad output merged.
    verdict = WorkflowOrchestrator._extract_validator_verdict
    assert verdict("## Статус: ❌ FAILED\n\nИмпорт внутри функции.") == "failed"
    assert verdict("## Статус: ✅ PASSED") == "passed"
    assert verdict("Status: FAILED - wrong file") == "failed"
    assert verdict("Статус проверки: ПРОЙДЕНО") == "passed"
    assert verdict("Статус: не пройден") == "failed"
    assert verdict("no status line at all") == ""


def test_ide_dirs_excluded_from_repo_map() -> None:
    from tools.repo_map import is_excluded_path
    assert is_excluded_path(".idea/Oil.iml") is True
    assert is_excluded_path(".vs/Oil/v17/.wsuo") is True
    assert is_excluded_path(".vscode/settings.json") is True
    assert is_excluded_path("main.py") is False
    assert is_excluded_path("frontend/src/App.jsx") is False


def test_within_turn_must_contain_gate_detects_missing_then_clears(tmp_path) -> None:
    orchestrator = make_orchestrator_with_workspace(tmp_path)
    # Only an import written so far (exactly the Oil failure: code-developer added UseDatabase only).
    (tmp_path / "main.py").write_text("from Database import UseDatabase\n", encoding="utf-8")
    orchestrator._selected_implementation_item = {
        "target_file": {"path": "main.py"},
        "must_contain": [
            "def get_customer_analytics(start_date=None, end_date=None):",
            "with UseDatabase(config) as cursor:",  # free-form -> not gated
        ],
    }

    missing = orchestrator._within_turn_missing_must_contain()
    assert any("get_customer_analytics" in item for item in missing)
    assert not any("UseDatabase(config)" in item for item in missing)  # free-form line not gated

    instruction = WorkflowOrchestrator._build_must_contain_repair_instruction(missing)
    assert "get_customer_analytics" in instruction
    assert ("write_file" in instruction) or ("apply_patch" in instruction)

    # Once the function is actually defined, the gate clears.
    (tmp_path / "main.py").write_text("def get_customer_analytics(s=None, e=None):\n    return []\n", encoding="utf-8")
    assert orchestrator._within_turn_missing_must_contain() == []
