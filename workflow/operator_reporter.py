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
import urllib.parse
import urllib.request
from typing import Any

EVENT_KINDS = ("status", "blocker", "done")
_MAX_RECORDED_EVENTS = 50
# The chat caps an event at 8000 characters; keep a margin so a fence is never cut mid-way.
_MAX_REPLY_CHARS = 7000


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
        commands_url: str = "",
    ) -> None:
        self.url = str(url or "").strip()
        self.token = str(token or "").strip()
        self.project = str(project or "")
        self.run_id = str(run_id or "")
        self.timeout = float(timeout or 3.0)
        self.enabled = bool(enabled) and bool(self.url)
        self.commands_url = str(commands_url or "").strip() or self._sibling_url("commands")
        # Every event is recorded even while disabled, so a run can show what it WOULD have sent
        # before any chat half exists — the bridge is testable end-to-end without a server.
        self.sent: list[dict[str, Any]] = []

    def _sibling_url(self, leaf: str) -> str:
        """`.../operator/events` -> `.../operator/<leaf>`; the chat exposes both under one root."""
        if not self.url or "/" not in self.url:
            return ""
        return self.url.rsplit("/", 1)[0] + "/" + leaf

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
            commands_url=str(bridge.get("commands_url") or ""),
        )

    def build_event(
        self,
        kind: str,
        text: str,
        *,
        task: str = "",
        needs_human: bool = False,
        options: list[str] | None = None,
        reply_to: str = "",
    ) -> dict[str, Any]:
        reply_to = str(reply_to or "").strip()
        if reply_to:
            # An answer to an operator command is MARKDOWN — the chat renders it with ReactMarkdown —
            # so its newlines are its structure (a ```diff fence, a task list) and must survive.
            # Only trailing blanks go; leading indentation stays, it is meaningful in a diff.
            lines = [line.rstrip() for line in str(text or "").splitlines()]
            while lines and not lines[0]:
                lines.pop(0)
            while lines and not lines[-1]:
                lines.pop()
            body = "\n".join(lines)[:_MAX_REPLY_CHARS]
        else:
            # A blocker or a run summary is read at a glance: one line.
            body = " ".join(str(text or "").split())[:2000]
        event = {
            "run_id": self.run_id,
            "project": self.project,
            "task": str(task or ""),
            "kind": kind if kind in EVENT_KINDS else "status",
            "text": body,
            "needs_human": bool(needs_human),
            "options": [str(option).strip() for option in (options or []) if str(option).strip()][:6],
        }
        # The answer to a read command travels as an ordinary status event tagged with the command
        # id, so neither side needs a second endpoint (the chat threads it back to the question).
        if reply_to:
            event["reply_to"] = reply_to
        return event

    def send(
        self,
        kind: str,
        text: str,
        *,
        task: str = "",
        needs_human: bool = False,
        options: list[str] | None = None,
        reply_to: str = "",
    ) -> bool:
        """POST one event. Returns whether the chat accepted it; never raises."""
        event = self.build_event(
            kind, text, task=task, needs_human=needs_human, options=options, reply_to=reply_to
        )
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

    def poll_commands(self, wait: int = 25) -> list[dict[str, Any]]:
        """Long-poll the chat for pending operator commands. Returns [] on anything unexpected.

        The workstation cannot be reached from outside, so it asks instead: one outbound HTTPS
        request that the chat holds open for `wait` seconds. An empty list is the normal answer —
        it means no command was issued, not that anything failed. The socket timeout must outlive
        the server's hold or every poll would look like a failure.
        """
        if not self.enabled or not self.commands_url:
            return []
        wait = max(0, int(wait))
        query = urllib.parse.urlencode({"project": self.project, "wait": wait})
        request = urllib.request.Request(f"{self.commands_url}?{query}", method="GET")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=wait + 10) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
        except Exception:
            return []
        commands = payload.get("commands") if isinstance(payload, dict) else None
        if not isinstance(commands, list):
            return []
        # Commands are issued by the owner through the chat, but they arrive over the network:
        # keep only well-formed entries and let the caller decide which cmd names it serves.
        return [
            command
            for command in commands
            if isinstance(command, dict) and str(command.get("cmd") or "").strip()
        ]
