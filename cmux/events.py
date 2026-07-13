# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

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
    FINALIZING = "finalizing"
    OPENING_PR = "opening_pr"
    DONE = "done"
    NO_CHANGES = "no_changes"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    KILLED = "killed"


ACTIVE = frozenset({Status.STARTING, Status.RUNNING, Status.TOOL, Status.IDLE, Status.FINALIZING})
TERMINAL = frozenset({Status.DONE, Status.NO_CHANGES, Status.FAILED, Status.TIMED_OUT, Status.KILLED})
SUCCESS = frozenset({Status.DONE, Status.NO_CHANGES})
TERMINAL_FAILURE = TERMINAL - SUCCESS


def _first_str(data: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value

    return default


def event_data(event: dict[str, Any]) -> dict[str, Any]:
    """Return a JSONL event's nested payload or the event itself."""

    data = event.get("data")

    return data if isinstance(data, dict) else event


@dataclass
class SessionState:
    """Live state of one copilot session."""

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
        """Latest assistant text, truncated for single-line display."""

        text = (self.last_text or self._delta_buf).strip().replace("\n", " ")

        return text[:80]


def apply_event(state: SessionState, event: dict[str, Any]) -> SessionState:
    """Fold a decoded JSONL event into the session state."""

    event_type = event.get("type", "")
    data = event_data(event)

    if event_type == "user.message":
        state.status = Status.STARTING
    elif event_type == "assistant.turn_start":
        state.status = Status.RUNNING
    elif event_type == "assistant.message_delta":
        state._delta_buf += _first_str(data, "deltaContent", "delta", "content", "text")
        state.status = Status.RUNNING
    elif event_type == "assistant.message":
        content = _first_str(data, "content", "text")
        if content:
            state.last_text = content
        state._delta_buf = ""
        state.status = Status.RUNNING
    elif event_type == "tool.execution_start":
        state.current_tool = _first_str(data, "toolName", "name", "tool")
        state.tool_count += 1
        state.status = Status.TOOL
    elif event_type == "tool.execution_complete":
        state.current_tool = ""
        state.status = Status.RUNNING
    elif event_type == "assistant.idle":
        state.status = Status.IDLE
    elif event_type == "session.error":
        state.error = _first_str(data, "message", "error") or "session error"
        state.status = Status.FAILED
    elif event_type == "result":
        state.session_id = event.get("sessionId") or state.session_id
        state.exit_code = event.get("exitCode", state.exit_code)
        usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        if "premiumRequests" in usage:
            state.premium_requests = usage.get("premiumRequests")
        changes = usage.get("codeChanges") if isinstance(usage.get("codeChanges"), dict) else {}
        files = changes.get("filesModified")
        if isinstance(files, list):
            state.files_modified = [str(path) for path in files]

        state.status = Status.DONE if state.exit_code == 0 else Status.FAILED

    return state


def parse_line(line: str) -> dict[str, Any] | None:
    """Decode a JSONL line, returning `None` for invalid input."""

    line = line.strip()
    if not line:
        return None

    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None

    return obj if isinstance(obj, dict) else None
