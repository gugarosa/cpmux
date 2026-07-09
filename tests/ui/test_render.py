# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from cmux.events import Status
from cmux.ui.render import STATUS_COLOR, event_text


def test_status_color_maps_terminal_and_defaults_others():
    assert STATUS_COLOR[Status.DONE] == "green"
    assert STATUS_COLOR[Status.FAILED] == "red"
    assert STATUS_COLOR.get(Status.RUNNING, "cyan") == "cyan"


def test_event_text_renders_known_events():
    assert event_text({"type": "user.message", "data": {"content": "hi"}}).plain == "🧑 user hi"
    assert event_text({"type": "assistant.message", "data": {"content": "done"}}).plain == "🤖 assistant done"
    assert event_text({"type": "tool.execution_start", "data": {"toolName": "write"}}).plain == "🔧 tool write"
    assert event_text({"type": "result", "exitCode": 0}).plain == "— result exit=0"


def test_event_text_is_none_for_empty_or_unknown():
    assert event_text({"type": "assistant.message", "data": {"content": "   "}}) is None
    assert event_text({"type": "session.idle", "data": {}}) is None
