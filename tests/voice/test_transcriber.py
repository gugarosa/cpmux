# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import sys
import types

import pytest

from cmux.voice.transcriber import VoiceError, transcribe


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

        def transcribe(self, path):
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


@pytest.mark.parametrize(
    "install",
    [
        pytest.param(lambda mp: mp.setitem(sys.modules, "faster_whisper", None), id="no_backend"),
        pytest.param(lambda mp: _install_fake_whisper(mp, segments=[_Segment("   ")]), id="empty_text"),
        pytest.param(lambda mp: _install_fake_whisper(mp, load_error=RuntimeError("x")), id="load_failure"),
        pytest.param(
            lambda mp: _install_fake_whisper(mp, transcribe_error=RuntimeError("x")),
            id="inference_failure",
        ),
    ],
)
def test_transcribe_raises_voice_error(tmp_path, monkeypatch, install):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    install(monkeypatch)
    with pytest.raises(VoiceError):
        transcribe(audio)
