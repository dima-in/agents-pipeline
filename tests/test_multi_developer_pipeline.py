from workflow.multi_developer_constraints import parse_and_validate
from workflow.multi_developer_dispatcher import classify_path, route_paths


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
