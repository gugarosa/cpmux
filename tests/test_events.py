# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from cmux.events import SessionState, Status, apply_event, event_data, parse_line

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


def test_apply_event_marks_done_and_captures_session_and_usage():
    state = SessionState()
    for event in SAMPLE:
        apply_event(state, event)

    assert state.status == Status.DONE
    assert state.session_id == "abc-123"
    assert state.last_text == "PONG"
    assert state.premium_requests == 3
    assert state.files_modified == ["a.ts"]


def test_tool_events_update_status():
    state = SessionState()
    apply_event(state, {"type": "tool.execution_start", "data": {"toolName": "write"}})
    assert state.status == Status.TOOL
    assert state.current_tool == "write"
    apply_event(state, {"type": "tool.execution_complete", "data": {"success": True}})
    assert state.status == Status.RUNNING


def test_failure_exit_code():
    state = SessionState()
    apply_event(state, {"type": "result", "sessionId": "z", "exitCode": 1, "usage": {}})
    assert state.status == Status.FAILED


def test_parse_line_tolerant():
    assert parse_line("") is None
    assert parse_line("   ") is None
    assert parse_line("not json") is None
    assert parse_line('{"type":"x"}') == {"type": "x"}


def test_event_data_unwraps_nested_payload_or_returns_event():
    assert event_data({"type": "x", "data": {"content": "hi"}}) == {"content": "hi"}
    bare = {"type": "result", "exitCode": 0}
    assert event_data(bare) is bare
    assert event_data({"type": "x", "data": "not-a-dict"}) == {"type": "x", "data": "not-a-dict"}
