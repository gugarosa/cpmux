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


def test_elapsed_formats_minutes_and_seconds():
    assert _elapsed(0) == "0:00"
    assert _elapsed(7.9) == "0:07"
    assert _elapsed(65) == "1:05"


def test_level_style_tracks_peak_then_decays():
    level = _Level()
    level.update(struct.pack("<8h", *([16384] * 8)))
    assert level.value == pytest.approx(0.5, abs=0.01)

    level.update(b"\x00\x00" * 8)
    assert level.value == pytest.approx(0.4, abs=0.01)


def test_meter_fills_with_level_and_shows_time():
    text = _meter(7, 1.0).plain
    assert "0:07" in text
    assert "█" * 24 in text
