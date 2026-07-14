# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import re
import subprocess

import yaml
from pydantic import ValidationError

from cpmux.config import Plan
from cpmux.voice.transcriber import VoiceError

_SCHEMA = """\
cpmux YAML fields:
- `system` (optional): a shared instruction prepended to every task.
- `defaults` (optional): `model`, `effort` (none|minimal|low|medium|high|xhigh|max),
  `permissions` (readonly|edit|full), `base` (branch to fork from, default `main`),
  `branch_template` (each item's branch name, default `cpmux/{slug}`; keep the `{slug}`
  placeholder, e.g. `alice/feature/{slug}`), `concurrency`, `pr` (`draft`, `labels`).
- `items` (required, non-empty): task list. Each item is EITHER a plain string
  (the task prompt) OR a mapping with `prompt` plus optional `name`, `model`, `effort`,
  `permissions`, `branch`, `base`, `paths`, `depends_on`."""

_FENCE = re.compile(r"```(?:ya?ml)?\s*\n(.*?)```", re.DOTALL)


def synthesize_plan(transcript: str, model: str = "gpt-5.5") -> str:
    """Build a validated cpmux plan from spoken tasks.

    Args:
        transcript: Spoken task transcript.
        model: Copilot model used for synthesis.

    Returns:
        The validated plan YAML.

    Raises:
        VoiceError: If synthesis or validation fails.

    """

    prompt = _build_prompt(transcript)
    error = ""

    for _ in range(2):
        instruction = (
            prompt
            if not error
            else f"{prompt}\n\nYour previous reply was invalid: {error}\nReturn corrected YAML only."
        )
        yaml_text = _extract_yaml(_run_copilot(instruction, model))

        try:
            Plan.model_validate(yaml.safe_load(yaml_text) or {})
            return yaml_text
        except (yaml.YAMLError, ValidationError) as exc:
            error = str(exc).splitlines()[0]

    raise VoiceError(f"plan synthesis failed: {error}.")


def _build_prompt(transcript: str) -> str:
    return (
        "Convert this spoken task list into a cpmux YAML plan.\n\n"
        f"{_SCHEMA}\n\n"
        "How to build it:\n"
        "- First, privately list every distinct task the speaker described, and under each list every "
        "specific they attached to it (files, paths, libraries, versions, constraints, acceptance "
        "criteria, ordering). Then write the YAML from that list.\n"
        "- One item per distinct task. Each item's `prompt` is a self-contained imperative that keeps "
        "every specific the speaker gave; prefer copying their wording over paraphrasing, and "
        "multi-sentence prompts are fine.\n"
        "- Do not summarize, generalize, merge, or drop any detail. If the speaker said it, it must "
        "appear in the plan.\n"
        "- Put shared guidance (style, testing, language, model, effort, branch naming) in `system` or "
        "`defaults`; put per-task files in `paths` and stated ordering in `depends_on`.\n"
        "- Before answering, re-read the transcript and confirm every task and constraint is covered.\n\n"
        "Reply with ONLY a single ```yaml code block.\n\n"
        f"Spoken task list:\n{transcript}"
    )


def _run_copilot(prompt: str, model: str) -> str:
    argv = ["copilot", "-p", prompt, "-s", "--model", model, "--no-ask-user", "--deny-tool=write", "--deny-tool=shell"]

    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise VoiceError("`copilot` is not on `PATH`.") from exc

    if proc.returncode != 0:
        raise VoiceError(f"`copilot` failed: {proc.stderr.strip() or proc.stdout.strip()}.")

    return proc.stdout


def _extract_yaml(text: str) -> str:
    fence = _FENCE.search(text)

    return (fence.group(1) if fence else text).strip()
