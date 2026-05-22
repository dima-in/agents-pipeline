from __future__ import annotations

from pathlib import Path
from typing import Any


def apply_operations(root: Path, operations: list[dict[str, Any]]) -> list[str]:
    changed_paths: list[str] = []
    workspace_root = root.resolve()
    for op in operations:
        op_type = str(op.get("type") or "").strip().lower()
        relative_path = str(op.get("path") or "").replace("\\", "/").strip()
        if not relative_path:
            continue
        candidate = (workspace_root / relative_path).resolve()
        candidate.relative_to(workspace_root)
        if op_type == "delete":
            if candidate.exists():
                candidate.unlink()
                changed_paths.append(relative_path)
            continue
        content = str(op.get("content") or "")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(content, encoding="utf-8")
        changed_paths.append(relative_path)
    return changed_paths

