#!/usr/bin/env python3
from __future__ import annotations

import sys

import start
from tools import repo_map as repo_map_tool


PHASE_ALIASES = {
    "research": "research",
    "r": "research",
    "implementation": "implementation",
    "impl": "implementation",
    "i": "implementation",
    "deployment": "deployment",
    "deploy": "deployment",
    "d": "deployment",
}

MODE_ALIASES = {
    "auto": "auto",
    "interactive": "interactive",
}


def normalize_cli_args(argv: list[str]) -> list[str]:
    if not argv:
        return []

    first = argv[0].strip().lower()
    if first in PHASE_ALIASES:
        return ["--phase", PHASE_ALIASES[first], *argv[1:]]
    if first in MODE_ALIASES:
        return ["--mode", MODE_ALIASES[first], *argv[1:]]
    return list(argv)


def format_normalized_args(argv: list[str]) -> str:
    def _quote(value: str) -> str:
        if not value or any(ch.isspace() for ch in value) or '"' in value:
            return '"' + value.replace('"', '\\"') + '"'
        return value

    return " ".join(_quote(arg) for arg in argv)


def main(argv: list[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0].strip().lower() == "repo-map":
        forwarded = list(raw_args[1:])
        if "--workspace" not in forwarded:
            forwarded = ["--workspace", str(start.Path(start.os.environ.get("AGENTS_PIPELINE_LAUNCH_CWD") or start.os.getcwd()).resolve()), *forwarded]
        if "--project-id" not in forwarded:
            forwarded = ["--project-id", start.Path(start.os.environ.get("AGENTS_PIPELINE_LAUNCH_CWD") or start.os.getcwd()).resolve().name.lower().replace(" ", "-"), *forwarded]
        print(f"normalized_args=python tools/repo_map.py {' '.join(forwarded)}")
        return repo_map_tool.main(forwarded)
    normalized_args = normalize_cli_args(raw_args)
    print(f"normalized_args={format_normalized_args(normalized_args)}")
    return start.main(normalized_args)


if __name__ == "__main__":
    raise SystemExit(main())
