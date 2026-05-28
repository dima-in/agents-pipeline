from __future__ import annotations

import argparse
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXCLUDED_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
}

EXCLUDED_PREFIXES = {
    ".openclaw/logs",
}

RUNTIME_STATE_PREFIXES = (
    ".agents-pipeline/projects/",
    ".openclaw/feedback/",
)


def _normalize_rel(path: str) -> str:
    normalized = str(path).replace("\\", "/").strip("/")
    if normalized in {"", "."}:
        return ""
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _is_excluded(relative_path: str) -> bool:
    normalized = _normalize_rel(relative_path)
    if not normalized:
        return False
    parts = normalized.split("/")
    if any(part in EXCLUDED_DIR_NAMES for part in parts):
        return True
    if any(normalized == prefix or normalized.startswith(prefix + "/") for prefix in EXCLUDED_PREFIXES):
        return True
    if normalized.startswith(".agents-pipeline/projects/"):
        project_parts = normalized.split("/")
        if len(project_parts) >= 5 and project_parts[4] in {"logs", "memory", "summaries"}:
            return True
    return False


def is_excluded_path(relative_path: str) -> bool:
    return _is_excluded(relative_path)


def _run_git_capture(workspace: Path, args: list[str]) -> str:
    command = ["git", *args]
    try:
        process = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except Exception:
        return ""
    if process.returncode != 0:
        return ""
    return (process.stdout or "").strip()


def _collect_git_metadata(workspace: Path) -> dict[str, Any]:
    branch = _run_git_capture(workspace, ["branch", "--show-current"])
    status_short = _run_git_capture(workspace, ["status", "--short"])
    tracked_output = _run_git_capture(workspace, ["ls-files"])
    tracked_files = {
        _normalize_rel(line)
        for line in tracked_output.splitlines()
        if _normalize_rel(line)
    }
    return {
        "git_branch": branch,
        "git_status_short": status_short,
        "tracked_files": tracked_files,
    }


def _classify_file(relative_path: str) -> str:
    normalized = _normalize_rel(relative_path)
    name = Path(normalized).name.lower()
    suffix = Path(normalized).suffix.lower()
    parts = Path(normalized).parts
    if normalized.startswith("tests/") or normalized.startswith("test_") or name.startswith("test_"):
        return "test"
    if "alembic" in parts or "migrations" in parts:
        return "migration"
    if name in {
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        "pipfile",
        "pipfile.lock",
    }:
        return "config"
    if name == ".env" or name.startswith(".env."):
        return "config"
    if name.startswith("dockerfile") or name.startswith("docker-compose"):
        return "config"
    if suffix in {".md", ".rst", ".txt"}:
        return "docs"
    if suffix in {".yaml", ".yml", ".toml", ".ini", ".cfg", ".json", ".env"}:
        return "config"
    if suffix in {".py", ".js", ".jsx", ".ts", ".tsx", ".sh", ".bat", ".ps1"}:
        if name in {"run.bat", "run.sh", "start.py", "run_launcher.py", "manage_agents.py"}:
            return "script"
        return "code"
    if normalized.startswith(".agents-pipeline/") or normalized.startswith(".openclaw/feedback/"):
        return "runtime_state"
    return "unknown"


def _is_generated_or_state(relative_path: str) -> bool:
    normalized = _normalize_rel(relative_path)
    if any(normalized == prefix.rstrip("/") or normalized.startswith(prefix) for prefix in RUNTIME_STATE_PREFIXES):
        return True
    return Path(normalized).suffix.lower() == ".log"


def _top_level_tree(workspace: Path) -> list[str]:
    entries: list[str] = []
    for child in sorted(workspace.iterdir(), key=lambda item: item.name.lower()):
        relative = _normalize_rel(child.name)
        if _is_excluded(relative):
            continue
        entries.append(relative + ("/" if child.is_dir() else ""))
    return entries


def _build_repo_map(workspace: Path, project_id: str) -> dict[str, Any]:
    workspace = workspace.resolve()
    git_metadata = _collect_git_metadata(workspace)
    files: list[dict[str, Any]] = []
    directories: list[str] = []

    for root, dirnames, filenames in os.walk(workspace, topdown=True):
        root_path = Path(root)
        try:
            root_relative = _normalize_rel(root_path.relative_to(workspace))
        except ValueError:
            continue

        kept_dirs: list[str] = []
        for dirname in dirnames:
            candidate_relative = _normalize_rel(f"{root_relative}/{dirname}" if root_relative else dirname)
            if _is_excluded(candidate_relative):
                continue
            kept_dirs.append(dirname)
            directories.append(candidate_relative)
        dirnames[:] = kept_dirs

        for filename in filenames:
            relative = _normalize_rel(f"{root_relative}/{filename}" if root_relative else filename)
            if _is_excluded(relative):
                continue
            path = workspace / relative
            try:
                stat = path.stat()
            except OSError:
                continue
            files.append(
                {
                    "path": relative,
                    "size_bytes": stat.st_size,
                    "extension": Path(relative).suffix.lower(),
                    "kind": _classify_file(relative),
                    "is_tracked": relative in git_metadata["tracked_files"],
                    "is_generated_or_state": _is_generated_or_state(relative),
                }
            )

    directories = sorted(dict.fromkeys(directories))
    files = sorted(files, key=lambda item: item["path"])

    dependency_files = [item["path"] for item in files if Path(item["path"]).name.lower() in {
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        "pipfile",
        "pipfile.lock",
    }]
    config_files = [
        item["path"]
        for item in files
        if item["kind"] == "config"
    ]
    test_files = [item["path"] for item in files if item["kind"] == "test"]
    docker_files = [
        item["path"]
        for item in files
        if Path(item["path"]).name.lower().startswith("dockerfile")
        or Path(item["path"]).name.lower().startswith("docker-compose")
    ]
    entrypoints = [
        item["path"]
        for item in files
        if Path(item["path"]).name in {"run.bat", "run.sh", "start.py", "run_launcher.py", "manage_agents.py", "main.py", "app.py"}
    ]
    agent_relevant_files = [
        item["path"]
        for item in files
        if item["path"] in {
            "README.md",
            "README.ru.md",
            "start.py",
            "run.bat",
            "run_launcher.py",
            "manage_agents.py",
            "workflow/config.yaml",
            "workflow/orchestrator.py",
            "workflow/logger.py",
            "workflow/runtime.py",
        }
        or item["path"].startswith("tests/")
        or item["path"].startswith(".openclaw/agents/")
    ]

    return {
        "target_workspace": str(workspace),
        "project_id": project_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_branch": git_metadata["git_branch"],
        "git_status_short": git_metadata["git_status_short"],
        "top_level_tree": _top_level_tree(workspace),
        "directories": directories,
        "files": files,
        "entrypoints": entrypoints,
        "dependency_files": dependency_files,
        "config_files": config_files,
        "test_files": test_files,
        "docker_files": docker_files,
        "agent_relevant_files": sorted(dict.fromkeys(agent_relevant_files)),
    }


def generate_repo_map(workspace: Path, project_id: str, output_path: Path) -> dict[str, Any]:
    repo_map = _build_repo_map(workspace, project_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(repo_map, ensure_ascii=False, indent=2), encoding="utf-8")
    return repo_map


def validate_agent_paths(
    *,
    paths: list[str],
    repo_map: dict[str, Any],
    existing_paths: list[str] | None = None,
    new_directories: list[str] | None = None,
    new_files: list[str] | None = None,
    allowed_paths: list[str] | None = None,
    allow_restructuring: bool = False,
    known_files: list[str] | set[str] | None = None,
    known_directories: list[str] | set[str] | None = None,
) -> dict[str, Any]:
    existing_paths = [_normalize_rel(path) for path in (existing_paths or []) if _normalize_rel(path)]
    new_directories = [_normalize_rel(path) for path in (new_directories or []) if _normalize_rel(path)]
    new_files = [_normalize_rel(path) for path in (new_files or []) if _normalize_rel(path)]
    allowed_paths = [_normalize_rel(path) for path in (allowed_paths or paths) if _normalize_rel(path)]
    paths = [_normalize_rel(path) for path in paths if _normalize_rel(path)]

    repo_files = {item["path"] for item in repo_map.get("files", []) if isinstance(item, dict) and item.get("path")}
    repo_directories = set(repo_map.get("directories", []) or [])
    repo_directories.add("")
    effective_known_files = {_normalize_rel(path) for path in (known_files or repo_files) if _normalize_rel(path)}
    effective_known_directories = {_normalize_rel(path) for path in (known_directories or repo_directories) if _normalize_rel(path) or path == ""}
    effective_known_directories.add("")
    invalid_paths: list[str] = []

    def _outside(path: str) -> bool:
        return not path or path.startswith("..") or Path(path).is_absolute()

    def _generic_missing(path: str) -> bool:
        first = path.split("/", 1)[0]
        return first in {"src", "api", "services", "storage", "models", "config"} and first not in effective_known_directories and not allow_restructuring

    declared_directories = set(effective_known_directories)
    for directory in sorted(new_directories, key=lambda item: len(Path(item).parts)):
        if _outside(directory):
            invalid_paths.append(f"{directory}:outside_target_workspace")
            continue
        if _is_excluded(directory):
            invalid_paths.append(f"{directory}:excluded_runtime_path")
            continue
        if _generic_missing(directory):
            invalid_paths.append(f"{directory}:generic_nonexistent_directory")
            continue
        parent = _normalize_rel(str(Path(directory).parent))
        if parent not in declared_directories:
            invalid_paths.append(f"{directory}:new_directory_parent_missing")
            continue
        declared_directories.add(directory)

    if set(allowed_paths) != set(existing_paths).union(new_files):
        invalid_paths.append("allowed_paths_must_match_existing_paths_and_new_files")

    for path in existing_paths:
        if _outside(path):
            invalid_paths.append(f"{path}:outside_target_workspace")
            continue
        if _is_excluded(path):
            invalid_paths.append(f"{path}:excluded_runtime_path")
            continue
        if _generic_missing(path):
            invalid_paths.append(f"{path}:generic_nonexistent_directory")
            continue
        if path not in effective_known_files:
            invalid_paths.append(f"{path}:missing_existing_file")

    for path in new_files:
        if _outside(path):
            invalid_paths.append(f"{path}:outside_target_workspace")
            continue
        if _is_excluded(path):
            invalid_paths.append(f"{path}:excluded_runtime_path")
            continue
        if _generic_missing(path):
            invalid_paths.append(f"{path}:generic_nonexistent_directory")
            continue
        parent = _normalize_rel(str(Path(path).parent))
        if parent not in declared_directories:
            invalid_paths.append(f"{path}:new_file_parent_missing")

    for path in allowed_paths:
        if _outside(path):
            invalid_paths.append(f"{path}:outside_target_workspace")
            continue
        if _is_excluded(path):
            invalid_paths.append(f"{path}:excluded_runtime_path")
            continue
        if _generic_missing(path):
            invalid_paths.append(f"{path}:generic_nonexistent_directory")
            continue
        if path not in existing_paths and path not in new_files:
            invalid_paths.append(f"{path}:allowed_path_not_declared")

    validated_paths = [path for path in allowed_paths if not any(item.startswith(f"{path}:") for item in invalid_paths)]
    return {
        "valid": not invalid_paths,
        "invalid_paths": sorted(dict.fromkeys(invalid_paths)),
        "validated_paths": validated_paths,
        "validated_directories": sorted(path for path in new_directories if not any(item.startswith(f"{path}:") for item in invalid_paths)),
    }


def compare_repo_maps(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    before_files = {item["path"]: item for item in before.get("files", []) if isinstance(item, dict) and item.get("path")}
    after_files = {item["path"]: item for item in after.get("files", []) if isinstance(item, dict) and item.get("path")}
    new_files_created = sorted(path for path in after_files if path not in before_files)
    removed_files = sorted(path for path in before_files if path not in after_files)
    files_modified = sorted(
        path
        for path in after_files
        if path in before_files and (
            after_files[path].get("size_bytes") != before_files[path].get("size_bytes")
            or after_files[path].get("kind") != before_files[path].get("kind")
            or after_files[path].get("extension") != before_files[path].get("extension")
        )
    )
    return {
        "new_files_created": new_files_created,
        "files_modified": files_modified,
        "removed_files": removed_files,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate repo_map.json for a target workspace")
    parser.add_argument("--workspace", required=True, help="Target workspace path")
    parser.add_argument("--project-id", required=True, help="Stable project id")
    parser.add_argument("--output", default=None, help="Optional output path override")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    if args.output:
        output_path = Path(args.output).resolve()
    else:
        output_path = Path.cwd().resolve() / ".agents-pipeline" / "projects" / args.project_id / "context" / "repo_map.json"
    repo_map = generate_repo_map(workspace, args.project_id, output_path)
    print(json.dumps({
        "repo_map_path": str(output_path),
        "project_id": repo_map["project_id"],
        "file_count": len(repo_map["files"]),
        "directory_count": len(repo_map["directories"]),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
