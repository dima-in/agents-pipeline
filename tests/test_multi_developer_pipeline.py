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


def test_multi_developer_editable_paths_include_existing_allowed_paths(tmp_path) -> None:
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

    assert "gateway-v4/app/models.py" in editable_paths
    assert route_paths(editable_paths) == ["code-developer", "infra-developer", "test-developer"]


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
