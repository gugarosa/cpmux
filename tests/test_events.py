# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import pytest

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


@pytest.mark.parametrize(
    ("event_stream", "expected_status"),
    [
        pytest.param(SAMPLE, Status.DONE, id="successful-result"),
        pytest.param(
            [{"type": "tool.execution_start", "data": {"toolName": "write"}}],
            Status.TOOL,
            id="tool-start",
        ),
        pytest.param(
            [
                {"type": "tool.execution_start", "data": {"toolName": "write"}},
                {"type": "tool.execution_complete", "data": {"success": True}},
            ],
            Status.RUNNING,
            id="tool-complete",
        ),
        pytest.param(
            [{"type": "result", "sessionId": "z", "exitCode": 1, "usage": {}}],
            Status.FAILED,
            id="failed-result",
        ),
    ],
)
def test_apply_event_updates_status(event_stream, expected_status):
    state = SessionState()
    for event in event_stream:
        apply_event(state, event)

    assert state.status == expected_status


def test_apply_event_captures_session_and_usage():
    state = SessionState()
    for event in SAMPLE:
        apply_event(state, event)

    assert state.session_id == "abc-123"
    assert state.last_text == "PONG"
    assert state.premium_requests == 3
    assert state.files_modified == ["a.ts"]


def test_apply_event_captures_current_tool():
    state = SessionState()
    apply_event(state, {"type": "tool.execution_start", "data": {"toolName": "write"}})
    assert state.current_tool == "write"


@pytest.mark.parametrize(
    "line",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="whitespace"),
        pytest.param("not json", id="invalid-json"),
    ],
)
def test_parse_line_returns_none_for_invalid_input(line):
    assert parse_line(line) is None


def test_parse_line_returns_decoded_object():
    assert parse_line('{"type":"x"}') == {"type": "x"}


@pytest.mark.parametrize(
    ("event", "expected"),
    [
        pytest.param(
            {"type": "x", "data": {"content": "hi"}},
            {"content": "hi"},
            id="nested-payload",
        ),
        pytest.param(
            {"type": "x", "data": "not-a-dict"},
            {"type": "x", "data": "not-a-dict"},
            id="non-dict-payload",
        ),
    ],
)
def test_event_data_selects_mapping_payload(event, expected):
    assert event_data(event) == expected


def test_event_data_returns_bare_event_identity():
    bare = {"type": "result", "exitCode": 0}
    assert event_data(bare) is bare
