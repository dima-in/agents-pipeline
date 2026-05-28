from pathlib import Path

from tools.repo_map import compare_repo_maps, generate_repo_map, validate_agent_paths


def test_repo_map_excludes_runtime_and_detects_core_files(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (workspace / "tests").mkdir(parents=True, exist_ok=True)
    (workspace / ".git").mkdir(parents=True, exist_ok=True)
    (workspace / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)
    (workspace / "frontend" / "node_modules" / "pkg").mkdir(parents=True, exist_ok=True)
    (workspace / "frontend" / "src").mkdir(parents=True, exist_ok=True)
    (workspace / ".openclaw" / "logs").mkdir(parents=True, exist_ok=True)

    (workspace / "README.md").write_text("# Repo\n", encoding="utf-8")
    (workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    (workspace / "run.bat").write_text("@echo off\n", encoding="utf-8")
    (workspace / "tests" / "test_example.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (workspace / "requirements.txt").write_text("pytest\n", encoding="utf-8")
    (workspace / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (workspace / ".env").write_text("MODE=test\n", encoding="utf-8")
    (workspace / ".git" / "ignored.txt").write_text("ignore\n", encoding="utf-8")
    (workspace / "node_modules" / "pkg" / "index.js").write_text("console.log('x')\n", encoding="utf-8")
    (workspace / "frontend" / "node_modules" / "pkg" / "index.js").write_text("console.log('nested')\n", encoding="utf-8")
    (workspace / "frontend" / "src" / "App.jsx").write_text("export default function App() {}\n", encoding="utf-8")
    (workspace / ".openclaw" / "logs" / "run.log").write_text("log\n", encoding="utf-8")

    output_path = tmp_path / "repo_map.json"
    repo_map = generate_repo_map(workspace, "demo-project", output_path)

    files = {item["path"]: item for item in repo_map["files"]}
    assert output_path.exists()
    assert "README.md" in files
    assert "workflow/orchestrator.py" in files
    assert "run.bat" in files
    assert "tests/test_example.py" in files
    assert all(not path.startswith(".git/") for path in files)
    assert all(not path.startswith("node_modules/") for path in files)
    assert all("/node_modules/" not in path for path in files)
    assert all(not path.startswith(".openclaw/logs/") for path in files)
    assert "frontend/src/App.jsx" in files
    assert files["README.md"]["kind"] == "docs"
    assert files["workflow/orchestrator.py"]["kind"] == "code"
    assert files["run.bat"]["kind"] == "script"
    assert files["tests/test_example.py"]["kind"] == "test"
    assert "requirements.txt" in repo_map["dependency_files"]
    assert ".env" in repo_map["config_files"]
    assert "tests/test_example.py" in repo_map["test_files"]
    assert "docker-compose.yml" in repo_map["docker_files"]
    assert "run.bat" in repo_map["entrypoints"]
    assert "workflow/orchestrator.py" in repo_map["agent_relevant_files"]


def test_validate_existing_path_passes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "workflow").mkdir(parents=True, exist_ok=True)
    (workspace / "workflow" / "orchestrator.py").write_text("pass\n", encoding="utf-8")
    repo_map = generate_repo_map(workspace, "demo-project", tmp_path / "repo_map.json")

    result = validate_agent_paths(
        paths=["workflow/orchestrator.py"],
        repo_map=repo_map,
        existing_paths=["workflow/orchestrator.py"],
        new_files=[],
        allowed_paths=["workflow/orchestrator.py"],
    )

    assert result["valid"] is True


def test_validate_nonexistent_existing_path_and_generic_dirs_fail(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    repo_map = generate_repo_map(workspace, "demo-project", tmp_path / "repo_map.json")

    missing_result = validate_agent_paths(
        paths=["missing.py"],
        repo_map=repo_map,
        existing_paths=["missing.py"],
        new_files=[],
        allowed_paths=["missing.py"],
    )
    generic_result = validate_agent_paths(
        paths=["src/main.py"],
        repo_map=repo_map,
        existing_paths=["src/main.py"],
        new_files=[],
        allowed_paths=["src/main.py"],
    )

    assert missing_result["valid"] is False
    assert any("missing_existing_file" in item for item in missing_result["invalid_paths"])
    assert generic_result["valid"] is False
    assert any("generic_nonexistent_directory" in item for item in generic_result["invalid_paths"])


def test_validate_new_file_under_existing_directory_passes_and_missing_src_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True, exist_ok=True)
    repo_map = generate_repo_map(workspace, "demo-project", tmp_path / "repo_map.json")

    valid_result = validate_agent_paths(
        paths=["docs/new-plan.md"],
        repo_map=repo_map,
        existing_paths=[],
        new_files=["docs/new-plan.md"],
        allowed_paths=["docs/new-plan.md"],
    )
    invalid_result = validate_agent_paths(
        paths=["src/new-plan.py"],
        repo_map=repo_map,
        existing_paths=[],
        new_files=["src/new-plan.py"],
        allowed_paths=["src/new-plan.py"],
    )

    assert valid_result["valid"] is True
    assert invalid_result["valid"] is False
    assert any("new_file_parent_missing" in item or "generic_nonexistent_directory" in item for item in invalid_result["invalid_paths"])


def test_validate_new_directory_and_file_passes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True, exist_ok=True)
    repo_map = generate_repo_map(workspace, "demo-project", tmp_path / "repo_map.json")

    result = validate_agent_paths(
        paths=["docs/guides/setup.md"],
        repo_map=repo_map,
        existing_paths=[],
        new_directories=["docs/guides"],
        new_files=["docs/guides/setup.md"],
        allowed_paths=["docs/guides/setup.md"],
    )

    assert result["valid"] is True
    assert result["validated_directories"] == ["docs/guides"]


def test_validate_path_traversal_fails(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    repo_map = generate_repo_map(workspace, "demo-project", tmp_path / "repo_map.json")

    result = validate_agent_paths(
        paths=["../secret.py"],
        repo_map=repo_map,
        existing_paths=["../secret.py"],
        new_files=[],
        allowed_paths=["../secret.py"],
    )

    assert result["valid"] is False
    assert any("outside_target_workspace" in item for item in result["invalid_paths"])


def test_compare_repo_maps_detects_new_file(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True, exist_ok=True)
    before = generate_repo_map(workspace, "demo-project", tmp_path / "before.json")
    (workspace / "docs" / "new-plan.md").write_text("plan\n", encoding="utf-8")
    after = generate_repo_map(workspace, "demo-project", tmp_path / "after.json")

    delta = compare_repo_maps(before, after)

    assert delta["new_files_created"] == ["docs/new-plan.md"]
    assert delta["removed_files"] == []
    assert delta["files_modified"] == []
