# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import sys
import wave

import pytest

from cmux.voice.recorder import record_to_file
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
