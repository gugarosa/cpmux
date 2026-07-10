# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import httpx
import pytest

from cmux.voice.transcriber import VoiceError, _resolve_endpoint, transcribe


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_transcribe_returns_text_from_endpoint(tmp_path, monkeypatch):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response({"text": "  fix the bug  "}))
    assert transcribe(audio, endpoint="http://localhost:1234/v1") == "fix the bug"


def test_transcribe_missing_file_raises(tmp_path):
    with pytest.raises(VoiceError):
        transcribe(tmp_path / "gone.wav", endpoint="http://localhost:1234/v1")


def test_transcribe_empty_text_raises(tmp_path, monkeypatch):
    audio = tmp_path / "clip.wav"
    audio.write_bytes(b"RIFF")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Response({"text": "   "}))
    with pytest.raises(VoiceError):
        transcribe(audio, endpoint="http://localhost:1234/v1")


def test_resolve_endpoint_prefers_explicit_endpoint():
    assert _resolve_endpoint("whisper", "http://host:9/v1/") == ("http://host:9/v1", None)
