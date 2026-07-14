# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import pytest

from cpmux import theme
from cpmux.events import Status


def test_status_visual_covers_every_status():
    assert set(theme.STATUS_VISUAL) == set(Status)


@pytest.mark.parametrize(
    ("status", "expected_plain"),
    [
        pytest.param(Status.OPENING_PR, "⇪ opening PR", id="opening-pr"),
        pytest.param(Status.KILLED, "■ stopped", id="killed"),
    ],
)
def test_status_text_shows_glyph_and_human_label(status, expected_plain):
    assert theme.status_text(status).plain == expected_plain


@pytest.mark.parametrize(
    ("status", "expected_style"),
    [
        pytest.param(Status.DONE, "green", id="done"),
        pytest.param(Status.FAILED, "red", id="failed"),
    ],
)
def test_status_text_is_styled_by_severity(status, expected_style):
    assert theme.status_text(status).style == expected_style


def test_status_text_falls_back_to_ascii_glyphs(monkeypatch):
    monkeypatch.setenv("CPMUX_ASCII", "1")
    assert theme.status_text(Status.DONE).plain == "v done"


def test_print_error_writes_to_stderr(capsys):
    theme.print_error("`copilot` is not on PATH.")
    captured = capsys.readouterr()
    assert "copilot" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        pytest.param(7, "0:07", id="seconds-only"),
        pytest.param(65, "1:05", id="minutes"),
        pytest.param(3725, "1:02:05", id="hours"),
    ],
)
def test_format_duration(seconds, expected):
    assert theme.format_duration(seconds) == expected
