from __future__ import annotations

from pathlib import Path
from typing import Literal


AgentName = Literal["code-developer", "test-developer", "infra-developer"]


def classify_path(path: str) -> AgentName:
    normalized = str(path or "").replace("\\", "/").lower()
    name = Path(normalized).name
    if "/tests/" in f"/{normalized}" or normalized.startswith("tests/"):
        return "test-developer"
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return "test-developer"
    if "/alembic/versions/" in f"/{normalized}" or "/migrations/" in f"/{normalized}":
        return "infra-developer"
    if name in {"requirements.txt", "pyproject.toml", "poetry.lock", "dockerfile", "makefile"}:
        return "infra-developer"
    if any(token in normalized for token in ("docker-compose", ".github/", ".env", ".yaml", ".yml", ".toml", ".ini", ".sh")):
        return "infra-developer"
    return "code-developer"


def route_paths(paths: list[str]) -> list[AgentName]:
    agents: set[AgentName] = set()
    for path in paths:
        normalized = str(path or "").strip()
        if normalized:
            agents.add(classify_path(normalized))
    ordered: list[AgentName] = []
    for agent in ("code-developer", "infra-developer", "test-developer"):
        if agent in agents:
            ordered.append(agent)
    return ordered


def filter_paths_for_agent(paths: list[str], agent_name: AgentName) -> list[str]:
    filtered: list[str] = []
    for path in paths:
        normalized = str(path or "").strip()
        if normalized and classify_path(normalized) == agent_name:
            filtered.append(normalized)
    return filtered
