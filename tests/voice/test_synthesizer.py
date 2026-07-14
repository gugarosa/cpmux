# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import pytest

from cpmux.voice import synthesizer
from cpmux.voice.synthesizer import synthesize_plan
from cpmux.voice.transcriber import VoiceError


def _reply(text, monkeypatch):
    monkeypatch.setattr(synthesizer, "_run_copilot", lambda prompt, model: text)


def test_synthesize_plan_returns_validated_yaml(monkeypatch):
    _reply("```yaml\nitems:\n  - fix the bug\n```", monkeypatch)
    assert synthesize_plan("fix the bug") == "items:\n  - fix the bug"


def test_synthesize_plan_retries_then_fails_on_invalid(monkeypatch):
    _reply("```yaml\nitems: []\n```", monkeypatch)
    with pytest.raises(VoiceError):
        synthesize_plan("nothing")
