# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import pytest

from cmux.voice import synthesizer
from cmux.voice.synthesizer import _extract_yaml, synthesize_plan
from cmux.voice.transcriber import VoiceError


def _reply(text, monkeypatch):
    monkeypatch.setattr(synthesizer, "_run_copilot", lambda prompt, model: text)


def test_synthesize_plan_returns_validated_yaml(monkeypatch):
    _reply("```yaml\nitems:\n  - fix the bug\n```", monkeypatch)
    assert synthesize_plan("fix the bug") == "items:\n  - fix the bug"


def test_synthesize_plan_retries_then_fails_on_invalid(monkeypatch):
    _reply("```yaml\nitems: []\n```", monkeypatch)
    with pytest.raises(VoiceError):
        synthesize_plan("nothing")


def test_extract_yaml_prefers_fenced_block():
    assert _extract_yaml("prose\n```yaml\nitems:\n  - x\n```\ntail") == "items:\n  - x"


def test_extract_yaml_falls_back_to_raw():
    assert _extract_yaml("  items:\n  - x  ") == "items:\n  - x"


def test_schema_documents_branch_scoping():
    assert "branch_template" in synthesizer._SCHEMA
    assert "branch" in synthesizer._SCHEMA
