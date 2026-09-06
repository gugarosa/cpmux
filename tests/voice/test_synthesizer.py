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


def test_synthesize_plan_retries_a_template_that_cannot_resolve(monkeypatch):
    replies = iter(["defaults:\n  branch_template: 'feature/{}'\nitems: [x]", "items: [x]"])
    monkeypatch.setattr(synthesizer, "_run_copilot", lambda *args: next(replies))

    assert synthesize_plan("x") == "items: [x]"


def test_synthesize_plan_retry_includes_actionable_validation_feedback(monkeypatch):
    prompts = []
    replies = iter(["items: []", "items: [x]"])

    def reply(prompt, model):
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr(synthesizer, "_run_copilot", reply)

    assert synthesize_plan("x") == "items: [x]"
    assert len(prompts) == 2
    assert "items:" in prompts[1]
    assert "at least 1 item" in prompts[1]
    assert "validation error for Plan" not in prompts[1]


def test_synthesize_plan_preserves_literal_references_and_explicit_execution_contracts(monkeypatch):
    prompts = []
    response = (
        "profiles:\n  python:\n    checks: ['uv run pytest']\n"
        "defaults:\n  profile: python\n  premium_budget: 5\n"
        "items:\n  - id: base\n    prompt: 'Document $${TOKEN}'\n"
        "  - id: child\n    prompt: Extend it\n    base_from: base\n"
    )

    def reply(prompt, model):
        prompts.append(prompt)
        return response

    monkeypatch.setattr(synthesizer, "_run_copilot", reply)

    assert synthesize_plan("document ${TOKEN} then extend; run uv run pytest") == response.strip()
    assert "do not invent them" in prompts[0]
    assert "`depends_on` only orders tasks" in prompts[0]
    assert "code is inherited" in prompts[0]
