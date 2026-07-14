# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import struct
import sys

import pytest

from cpmux.voice.recorder import (
    _common_word_count,
    _elapsed,
    _Level,
    _meter,
    _Partial,
    record_and_transcribe,
)
from cpmux.voice.transcriber import VoiceError


def test_record_and_transcribe_reports_missing_backend(monkeypatch):
    monkeypatch.setitem(sys.modules, "numpy", None)
    with pytest.raises(VoiceError):
        record_and_transcribe("base")


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        pytest.param(["a", "b", "c"], ["a", "b", "x"], 2, id="common_prefix"),
        pytest.param([], ["a"], 0, id="empty_previous"),
        pytest.param(["a", "b"], ["a", "b"], 2, id="identical"),
        pytest.param(["a"], ["b"], 0, id="no_overlap"),
    ],
)
def test_common_word_count(previous, current, expected):
    assert _common_word_count(previous, current) == expected


def test_partial_commits_agreed_prefix():
    partial = _Partial()
    partial.update("fix the login")
    assert partial.snapshot() == ("", "fix the login")
    partial.update("fix the login bug now")
    assert partial.snapshot() == ("fix the login", "bug now")


def test_partial_commit_is_monotonic():
    partial = _Partial()
    partial.update("alpha beta gamma")
    partial.update("alpha beta gamma delta")
    assert partial.snapshot()[0] == "alpha beta gamma"
    partial.update("alpha beta")
    assert partial.snapshot()[0] == "alpha beta gamma"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        pytest.param(0, "0:00", id="zero_seconds"),
        pytest.param(7.9, "0:07", id="fractional_seconds"),
        pytest.param(65, "1:05", id="minutes_and_seconds"),
    ],
)
def test_elapsed_formats_time(seconds, expected):
    assert _elapsed(seconds) == expected


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        pytest.param([struct.pack("<8h", *([16384] * 8))], pytest.approx(0.5, abs=0.01), id="tracks_peak"),
        pytest.param(
            [struct.pack("<8h", *([16384] * 8)), b"\x00\x00" * 8],
            pytest.approx(0.4, abs=0.01),
            id="decays",
        ),
    ],
)
def test_level_tracks_peak_and_decay(samples, expected):
    level = _Level()
    for sample in samples:
        level.update(sample)
    assert level.value == expected


@pytest.mark.parametrize(
    "expected",
    [
        pytest.param("0:07", id="elapsed_time"),
        pytest.param("█" * 24, id="filled_level"),
    ],
)
def test_meter_shows_level_and_time(expected):
    text = _meter(7, 1.0).plain
    assert expected in text
