# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

"""Full-text search across cmux session transcripts.

Searches the ``transcript.jsonl`` files that cmux tees for every session, so it
is self-contained and does not couple to copilot's internal session store.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from cmux.events import parse_line

_SNIPPET_BEFORE = 30
_SNIPPET_AFTER = 80


@dataclass
class Hit:
    """A single search match: which session, which role, and a text snippet."""

    label: str
    role: str
    snippet: str


def _messages(transcript_path: str | Path) -> list[tuple[str, str]]:
    path = Path(transcript_path)
    if not path.exists():
        return []

    out: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        ev = parse_line(line)
        if ev is None:
            continue
        typ = ev.get("type", "")
        data = ev.get("data") if isinstance(ev.get("data"), dict) else ev
        if typ == "user.message":
            out.append(("user", str(data.get("content", ""))))
        elif typ == "assistant.message":
            text = str(data.get("content", ""))
            if text:
                out.append(("assistant", text))

    return out


def _match_index(text: str, query: str, regex: bool) -> int:
    if regex:
        match = re.search(query, text, re.IGNORECASE)
        return match.start() if match else -1

    return text.lower().find(query.lower())


def _snippet(text: str, index: int) -> str:
    start = max(0, index - _SNIPPET_BEFORE)
    window = " ".join(text[start : index + _SNIPPET_AFTER].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if index + _SNIPPET_AFTER < len(text) else ""

    return f"{prefix}{window}{suffix}"


def search_transcripts(items: list[tuple[str, Path]], query: str, regex: bool = False) -> list[Hit]:
    """Return every transcript message matching ``query`` across the given sessions."""
    hits: list[Hit] = []
    for label, transcript_path in items:
        for role, text in _messages(transcript_path):
            index = _match_index(text, query, regex)
            if index >= 0:
                hits.append(Hit(label=label, role=role, snippet=_snippet(text, index)))

    return hits
