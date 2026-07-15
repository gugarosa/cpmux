# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import sys
import types

import pytest

from cpmux.voice.transcriber import VoiceError, transcribe


class _Segment:
    def __init__(self, text):
        self.text = text


def _raising_generator(error):
    if error is not None:
        raise error
    yield


def _install_fake_whisper(monkeypatch, segments=None, load_error=None, transcribe_error=None):
    module = types.ModuleType("faster_whisper")

    class _FakeModel:
        def __init__(self, model, device, compute_type):
            if load_error is not None:
                raise load_error

        def transcribe(self, path, **kwargs):
            if transcribe_error is not None:
                return _raising_generator(transcribe_error), object()
            return list(segments or []), object()

    module.WhisperModel = _FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", module)


def test_transcribe_joins_and_strips_segments(tmp_path, monkeypatch):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    _install_fake_whisper(monkeypatch, segments=[_Segment(" fix the bug ")])
    assert transcribe(audio) == "fix the bug"


def test_transcribe_missing_file_raises(tmp_path):
    with pytest.raises(VoiceError):
        transcribe(tmp_path / "gone.wav")


def test_transcribe_raises_when_backend_missing(tmp_path, monkeypatch):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    monkeypatch.setitem(sys.modules, "faster_whisper", None)

    with pytest.raises(VoiceError):
        transcribe(audio)


def test_transcribe_raises_on_empty_text(tmp_path, monkeypatch):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    _install_fake_whisper(monkeypatch, segments=[_Segment("   ")])

    with pytest.raises(VoiceError):
        transcribe(audio)


def test_transcribe_raises_on_load_failure(tmp_path, monkeypatch):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    _install_fake_whisper(monkeypatch, load_error=RuntimeError("x"))

    with pytest.raises(VoiceError):
        transcribe(audio)


def test_transcribe_raises_on_inference_failure(tmp_path, monkeypatch):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    _install_fake_whisper(monkeypatch, transcribe_error=RuntimeError("x"))

    with pytest.raises(VoiceError):
        transcribe(audio)
