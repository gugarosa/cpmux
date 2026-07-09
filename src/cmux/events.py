# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

"""Parse and reduce the JSONL event stream emitted by ``copilot --output-format json``.

A session emits, in order, ``session.*`` setup events, ``user.message``,
``assistant.turn_start``, streamed ``assistant.message_delta``, a final
``assistant.message`` (with ``toolRequests``/``outputTokens``),
``assistant.turn_end``, ``assistant.idle``, and a terminal ``result`` object
carrying ``sessionId``, ``exitCode`` and ``usage``. The session id and reliable
token usage appear only in ``result``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    """Lifecycle status of a single cmux session."""

    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    TOOL = "tool"
    IDLE = "idle"
    COMMITTING = "committing"
    PUSHING = "pushing"
    OPENING_PR = "opening_pr"
    DONE = "done"
    NO_CHANGES = "no_changes"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    KILLED = "killed"


ACTIVE = frozenset({Status.STARTING, Status.RUNNING, Status.TOOL, Status.IDLE})
TERMINAL = frozenset({Status.DONE, Status.NO_CHANGES, Status.FAILED, Status.TIMED_OUT, Status.KILLED})


def _first(data: dict[str, Any], *keys: str, default: str = "") -> str:
    for k in keys:
        v = data.get(k)
        if isinstance(v, str) and v:
            return v
    return default


@dataclass
class SessionState:
    """Live, reduced view of one copilot session, updated event by event."""

    status: Status = Status.PENDING
    last_text: str = ""
    _delta_buf: str = ""
    current_tool: str = ""
    tool_count: int = 0
    exit_code: int | None = None
    session_id: str | None = None
    premium_requests: int | None = None
    files_modified: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def summary_line(self) -> str:
        """The latest assistant text, truncated for single-line display."""
        text = (self.last_text or self._delta_buf).strip().replace("\n", " ")
        return text[:80]


def apply_event(state: SessionState, ev: dict[str, Any]) -> SessionState:
    """Fold a single decoded JSONL event into ``state`` and return it."""
    typ = ev.get("type", "")
    data = ev.get("data") if isinstance(ev.get("data"), dict) else ev

    if typ == "user.message":
        state.status = Status.STARTING
    elif typ == "assistant.turn_start":
        state.status = Status.RUNNING
    elif typ == "assistant.message_delta":
        state._delta_buf += _first(data, "deltaContent", "delta", "content", "text")
        state.status = Status.RUNNING
    elif typ == "assistant.message":
        content = _first(data, "content", "text")
        if content:
            state.last_text = content
        state._delta_buf = ""
        state.status = Status.RUNNING
    elif typ == "tool.execution_start":
        state.current_tool = _first(data, "toolName", "name", "tool")
        state.tool_count += 1
        state.status = Status.TOOL
    elif typ == "tool.execution_complete":
        state.current_tool = ""
        state.status = Status.RUNNING
    elif typ == "assistant.idle":
        state.status = Status.IDLE
    elif typ == "session.error":
        state.error = _first(data, "message", "error") or "session error"
        state.status = Status.FAILED
    elif typ == "result":
        state.session_id = ev.get("sessionId") or state.session_id
        state.exit_code = ev.get("exitCode", state.exit_code)
        usage = ev.get("usage") if isinstance(ev.get("usage"), dict) else {}
        if "premiumRequests" in usage:
            state.premium_requests = usage.get("premiumRequests")
        changes = usage.get("codeChanges") if isinstance(usage.get("codeChanges"), dict) else {}
        files = changes.get("filesModified")
        if isinstance(files, list):
            state.files_modified = [str(f) for f in files]
        state.status = Status.DONE if state.exit_code == 0 else Status.FAILED

    return state


def parse_line(line: str) -> dict[str, Any] | None:
    """Decode one JSONL line, returning ``None`` for blank or non-JSON lines."""
    line = line.strip()
    if not line:
        return None

    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    return obj if isinstance(obj, dict) else None
