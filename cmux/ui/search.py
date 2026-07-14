# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import re
from dataclasses import dataclass
from pathlib import Path

from cmux.events import event_data, parse_line

_SNIPPET_BEFORE = 30
_SNIPPET_AFTER = 80


@dataclass
class TranscriptHit:
    """Transcript search match.

    Attributes:
        label: Session label.
        role: Message role.
        snippet: Matching text excerpt.

    """

    label: str
    role: str
    snippet: str


def _read_messages(transcript_path: str | Path) -> list[tuple[str, str]]:
    path = Path(transcript_path)
    if not path.exists():
        return []

    messages: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = parse_line(line)
        if event is None:
            continue
        event_type = event.get("type", "")
        data = event_data(event)
        if event_type == "user.message":
            messages.append(("user", str(data.get("content", ""))))
        elif event_type == "assistant.message":
            text = str(data.get("content", ""))
            if text:
                messages.append(("assistant", text))

    return messages


def _match_index(text: str, query: str, regex: bool) -> int:
    if regex:
        match = re.search(query, text, re.IGNORECASE)
        return match.start() if match else -1

    return text.lower().find(query.lower())


def _build_snippet(text: str, index: int) -> str:
    start = max(0, index - _SNIPPET_BEFORE)
    window = " ".join(text[start : index + _SNIPPET_AFTER].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if index + _SNIPPET_AFTER < len(text) else ""

    return f"{prefix}{window}{suffix}"


def search_transcripts(items: list[tuple[str, Path]], query: str, regex: bool = False) -> list[TranscriptHit]:
    """Find matching transcript messages across sessions.

    Args:
        items: Session labels and transcript paths.
        query: Text or pattern to find.
        regex: Whether query is a regular expression.

    Returns:
        Matching transcript messages.

    """

    hits: list[TranscriptHit] = []
    for label, transcript_path in items:
        for role, text in _read_messages(transcript_path):
            index = _match_index(text, query, regex)
            if index >= 0:
                hits.append(TranscriptHit(label=label, role=role, snippet=_build_snippet(text, index)))

    return hits
