# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import pytest

from cpmux.voice import synthesizer
from cpmux.voice.synthesizer import _extract_yaml, synthesize_plan
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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("prose\n```yaml\nitems:\n  - x\n```\ntail", "items:\n  - x", id="fenced_block"),
        pytest.param("  items:\n  - x  ", "items:\n  - x", id="raw_text"),
    ],
)
def test_extract_yaml_returns_content(text, expected):
    assert _extract_yaml(text) == expected


@pytest.mark.parametrize(
    "key",
    [
        pytest.param("branch_template", id="branch_template"),
        pytest.param("branch", id="branch"),
    ],
)
def test_schema_documents_branch_scoping(key):
    assert key in synthesizer._SCHEMA
