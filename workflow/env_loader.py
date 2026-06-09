from __future__ import annotations

import os
from pathlib import Path


def _parse_env_text(text: str) -> dict[str, str]:
    """Parse simple KEY=VALUE lines (optionally `export KEY=VALUE`), ignoring blanks/comments."""
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def load_env_files(directories, *, filename: str = ".env") -> list[str]:
    """Load `<dir>/.env` files into os.environ WITHOUT overriding existing variables.

    Real environment variables always win — a `.env` only fills what is missing — so a key
    exported on the server/CI is never shadowed by a stale file. No third-party dependency.
    Returns the list of files actually applied (for logging).
    """
    loaded: list[str] = []
    seen: set[str] = set()
    for directory in directories:
        if not directory:
            continue
        path = Path(directory) / filename
        try:
            resolved = str(path.resolve())
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        applied = False
        for key, value in _parse_env_text(text).items():
            if key not in os.environ:
                os.environ[key] = value
                applied = True
        if applied:
            loaded.append(str(path))
    return loaded
