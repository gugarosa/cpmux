# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import struct
import sys
import wave

import pytest

from cmux.voice.recorder import _elapsed, _Level, _meter, record_to_file
from cmux.voice.transcriber import VoiceError


class _FakeStream:
    def __init__(self, callback, **kwargs):
        self._callback = callback

    def __enter__(self):
        self._callback(b"\x01\x00" * 320, 320, None, None)
        return self

    def __exit__(self, *args):
        return False


class _FakeSoundDevice:
    RawInputStream = _FakeStream


def test_record_to_file_writes_wav(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", _FakeSoundDevice())
    monkeypatch.setattr("builtins.input", lambda *a: "")
    out = record_to_file(tmp_path / "rec.wav")
    assert out.exists()
    with wave.open(str(out), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getframerate() == 16000
        assert wav.getnframes() > 0


def test_record_to_file_without_backend_raises(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "sounddevice", None)
    with pytest.raises(VoiceError):
        record_to_file(tmp_path / "rec.wav")


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
