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
