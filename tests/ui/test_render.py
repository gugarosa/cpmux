# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import pytest

from cpmux.events import Status
from cpmux.ui.render import deps_cell, event_text


def test_deps_cell_is_dash_without_dependencies():
    cell = deps_cell([], {})
    assert cell.plain == "-"


def test_deps_cell_colors_each_dependency_by_status():
    status_by_key = {"a": Status.DONE, "b": Status.RUNNING, "c": Status.FAILED}
    cell = deps_cell(["a", "b", "c", "gone"], status_by_key)
    assert cell.plain == "a, b, c, gone"
    styles = {span.style for span in cell.spans if cell.plain[span.start : span.end] != ", "}
    assert {"green", "yellow", "red", "magenta"} <= styles


@pytest.mark.parametrize(
    ("event", "expected_plain"),
    [
        pytest.param(
            {"type": "user.message", "data": {"content": "hi"}},
            "🧑 user hi",
            id="user-message",
        ),
        pytest.param(
            {"type": "assistant.message", "data": {"content": "done"}},
            "🤖 assistant done",
            id="assistant-message",
        ),
        pytest.param(
            {"type": "tool.execution_start", "data": {"toolName": "write"}},
            "🔧 tool write",
            id="tool-execution-start",
        ),
        pytest.param(
            {"type": "result", "exitCode": 0},
            "— result exit=0",
            id="successful-result",
        ),
        pytest.param(
            {"type": "session.error", "data": {"message": "token expired"}},
            "✖ error token expired",
            id="session-error",
        ),
    ],
)
def test_event_text_renders(event, expected_plain):
    assert event_text(event).plain == expected_plain


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(
            {"type": "assistant.message", "data": {"content": "   "}},
            id="empty-assistant-message",
        ),
        pytest.param({"type": "session.idle", "data": {}}, id="unknown-event"),
    ],
)
def test_event_text_is_none_for_empty_or_unknown(event):
    assert event_text(event) is None


@pytest.mark.parametrize(
    ("exit_code", "expected_style"),
    [
        pytest.param(1, "red", id="failed-result"),
        pytest.param(0, "dim", id="successful-result"),
    ],
)
def test_event_text_styles_results(exit_code, expected_style):
    assert event_text({"type": "result", "exitCode": exit_code}).style == expected_style


def test_event_text_falls_back_to_ascii_role_glyphs(monkeypatch):
    monkeypatch.setenv("CPMUX_ASCII", "1")
    assert event_text({"type": "user.message", "data": {"content": "hi"}}).plain == "> user hi"
