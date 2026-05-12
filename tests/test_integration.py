from pathlib import Path


def test_project_tree_exists() -> None:
    expected = [
        Path("workflow/config.yaml"),
        Path("workflow/orchestrator.py"),
        Path("workflow/logger.py"),
        Path(".openclaw/agents/research/competitor-analyst/config.yaml"),
        Path(".openclaw/agents/implementation/implementation-planner/config.yaml"),
        Path(".openclaw/agents/implementation/qa/config.yaml"),
        Path("start.py"),
    ]
    for file_path in expected:
        assert file_path.exists(), f"Missing {file_path}"
