# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from cmux.events import SessionState, Status, apply_event, parse_line

# A real copilot 1.0.70 JSONL sequence (from the "PONG" probe), trimmed
SAMPLE = [
    {"type": "session.tools_updated", "data": {"model": "gpt-5.5"}},
    {"type": "user.message", "data": {"content": "Say PONG"}},
    {"type": "assistant.turn_start", "turnId": "t1"},
    {"type": "assistant.message_delta", "data": {"deltaContent": "PO"}},
    {"type": "assistant.message_delta", "data": {"deltaContent": "NG"}},
    {"type": "assistant.message", "data": {"content": "PONG"}},
    {"type": "assistant.turn_end", "turnId": "t1"},
    {"type": "assistant.idle", "data": {}},
    {
        "type": "result",
        "sessionId": "abc-123",
        "exitCode": 0,
        "usage": {"premiumRequests": 3, "codeChanges": {"filesModified": ["a.ts"]}},
    },
]


def test_reduce_full_sequence():
    st = SessionState()
    for ev in SAMPLE:
        apply_event(st, ev)
    assert st.status == Status.DONE
    assert st.session_id == "abc-123"
    assert st.last_text == "PONG"
    assert st.premium_requests == 3
    assert st.files_modified == ["a.ts"]


def test_tool_events_update_status():
    st = SessionState()
    apply_event(st, {"type": "tool.execution_start", "data": {"toolName": "write"}})
    assert st.status == Status.TOOL
    assert st.current_tool == "write"
    apply_event(st, {"type": "tool.execution_complete", "data": {"success": True}})
    assert st.status == Status.RUNNING


def test_failure_exit_code():
    st = SessionState()
    apply_event(st, {"type": "result", "sessionId": "z", "exitCode": 1, "usage": {}})
    assert st.status == Status.FAILED


def test_parse_line_tolerant():
    assert parse_line("") is None
    assert parse_line("   ") is None
    assert parse_line("not json") is None
    assert parse_line('{"type":"x"}') == {"type": "x"}
