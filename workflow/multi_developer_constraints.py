from __future__ import annotations

import json
from typing import Any

from workflow.multi_developer_dispatcher import classify_path


REQUIRED_FIELDS = ("agent", "task_id", "reasoning", "operations")


def validate_operation(op: dict[str, Any], allowed_files: list[str], agent_name: str, index: int) -> list[str]:
    errors: list[str] = []
    prefix = f"operation[{index}]"
    for field in ("type", "path", "reason"):
        if field not in op:
            errors.append(f"{prefix}: missing field {field}")
            return errors
    op_type = str(op.get("type") or "").strip().lower()
    if op_type not in {"create", "modify", "delete"}:
        errors.append(f"{prefix}: invalid type {op.get('type')}")
    if op_type in {"create", "modify"} and not isinstance(op.get("content"), str):
        errors.append(f"{prefix}: content required for {op_type}")
    path = str(op.get("path") or "").replace("\\", "/").strip()
    if path not in allowed_files:
        errors.append(f"{prefix}: file {path} not in allowed_files")
    actual_agent = classify_path(path)
    if actual_agent != agent_name:
        errors.append(f"{prefix}: file {path} belongs to {actual_agent}, not {agent_name}")
    return errors


def validate_agent_output(agent_output: dict[str, Any], allowed_files: list[str], agent_name: str) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_FIELDS:
        if field not in agent_output:
            errors.append(f"missing required field: {field}")
    if errors:
        return errors
    if str(agent_output.get("agent") or "") != agent_name:
        errors.append(f"wrong agent name: expected {agent_name}, got {agent_output.get('agent')}")
    operations = agent_output.get("operations")
    if not isinstance(operations, list):
        return [*errors, "operations must be a list"]
    for index, op in enumerate(operations):
        if not isinstance(op, dict):
            errors.append(f"operation[{index}]: must be an object")
            continue
        errors.extend(validate_operation(op, allowed_files, agent_name, index))
    return errors


def parse_and_validate(raw_output: str, allowed_files: list[str], agent_name: str) -> tuple[dict[str, Any] | None, list[str]]:
    cleaned = str(raw_output or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return None, [f"invalid JSON: {exc}"]
    if not isinstance(payload, dict):
        return None, ["agent output must be a JSON object"]
    errors = validate_agent_output(payload, allowed_files, agent_name)
    return payload, errors

