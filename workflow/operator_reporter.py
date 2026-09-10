"""Outbound-only bridge from a pipeline run to an operator chat.

The pipeline runs on a workstation behind NAT, so the chat can never reach IN: every event is
POSTed OUT and the run never waits for an answer. Delivery is therefore fire-and-forget — an
unreachable, misconfigured or slow bridge must never fail a run that is otherwise fine, so every
transport error is swallowed and reported as a boolean.

The event contract is deliberately tiny, because the engine already computes every field: an
escalation card is exactly `kind="blocker"` + `text` + the operator's decision `options`.

    {"run_id", "project", "task", "kind": "status|blocker|done", "text", "needs_human", "options"}

`needs_human=True` is the only signal the chat needs to decide whether to push to a phone.
"""

from __future__ import annotations

import json
import os
import urllib.request
from typing import Any

EVENT_KINDS = ("status", "blocker", "done")
_MAX_RECORDED_EVENTS = 50


class OperatorReporter:
    """Posts run events to the operator chat. Inert until a url is configured."""

    def __init__(
        self,
        *,
        url: str = "",
        token: str = "",
        project: str = "",
        run_id: str = "",
        enabled: bool = True,
        timeout: float = 3.0,
    ) -> None:
        self.url = str(url or "").strip()
        self.token = str(token or "").strip()
        self.project = str(project or "")
        self.run_id = str(run_id or "")
        self.timeout = float(timeout or 3.0)
        self.enabled = bool(enabled) and bool(self.url)
        # Every event is recorded even while disabled, so a run can show what it WOULD have sent
        # before any chat half exists — the bridge is testable end-to-end without a server.
        self.sent: list[dict[str, Any]] = []

    @classmethod
    def from_config(cls, config: dict[str, Any] | None, *, project: str, run_id: str) -> "OperatorReporter":
        bridge = ((config or {}).get("workflow") or {}).get("operator_bridge") or {}
        token_env = str(bridge.get("token_env") or "OPERATOR_BRIDGE_TOKEN").strip()
        return cls(
            url=str(bridge.get("url") or ""),
            token=os.environ.get(token_env, ""),
            project=project,
            run_id=run_id,
            enabled=bool(bridge.get("enabled", False)),
            timeout=float(bridge.get("timeout_seconds") or 3.0),
        )

    def build_event(
        self,
        kind: str,
        text: str,
        *,
        task: str = "",
        needs_human: bool = False,
        options: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project": self.project,
            "task": str(task or ""),
            "kind": kind if kind in EVENT_KINDS else "status",
            "text": " ".join(str(text or "").split())[:2000],
            "needs_human": bool(needs_human),
            "options": [str(option).strip() for option in (options or []) if str(option).strip()][:6],
        }

    def send(
        self,
        kind: str,
        text: str,
        *,
        task: str = "",
        needs_human: bool = False,
        options: list[str] | None = None,
    ) -> bool:
        """POST one event. Returns whether the chat accepted it; never raises."""
        event = self.build_event(kind, text, task=task, needs_human=needs_human, options=options)
        self.sent.append(event)
        del self.sent[:-_MAX_RECORDED_EVENTS]
        if not self.enabled:
            return False
        payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=payload, method="POST")
        request.add_header("Content-Type", "application/json; charset=utf-8")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return 200 <= int(getattr(response, "status", 0) or 0) < 300
        except Exception:
            return False  # the chat is a side channel: a dead bridge never fails a run
