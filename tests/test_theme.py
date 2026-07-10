# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from cmux import theme
from cmux.events import Status


def test_status_visual_covers_every_status():
    assert set(theme.STATUS_VISUAL) == set(Status)


def test_status_text_shows_glyph_and_human_label():
    assert theme.status_text(Status.OPENING_PR).plain == "⇪ opening PR"
    assert theme.status_text(Status.KILLED).plain == "■ stopped"


def test_status_text_is_styled_by_severity():
    assert theme.status_text(Status.DONE).style == "green"
    assert theme.status_text(Status.FAILED).style == "red"


def test_ascii_fallback_swaps_glyphs(monkeypatch):
    monkeypatch.setenv("CMUX_ASCII", "1")
    assert theme.status_text(Status.DONE).plain == "v done"


def test_print_error_writes_to_stderr(capsys):
    theme.print_error("`copilot` is not on PATH.")
    captured = capsys.readouterr()
    assert "copilot" in captured.err
    assert captured.out == ""
