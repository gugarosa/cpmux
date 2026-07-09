# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import json

from cmux.search import search_transcripts


def _write(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def test_search_finds_user_and_assistant_text(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    _write(
        transcript,
        [
            {"type": "user.message", "data": {"content": "fix the timezone bug"}},
            {"type": "assistant.message", "data": {"content": "used parseISO to fix tz"}},
        ],
    )

    hits = search_transcripts([("k", transcript)], "timezone")

    assert len(hits) == 1
    assert hits[0].role == "user"
    assert hits[0].label == "k"


def test_search_case_insensitive(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write(transcript, [{"type": "assistant.message", "data": {"content": "ParseISO Helper"}}])
    assert search_transcripts([("k", transcript)], "parseiso")


def test_search_regex(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write(transcript, [{"type": "assistant.message", "data": {"content": "error code 429"}}])
    assert len(search_transcripts([("k", transcript)], r"\d{3}", regex=True)) == 1


def test_search_ignores_deltas_and_missing_files(tmp_path):
    transcript = tmp_path / "t.jsonl"
    _write(transcript, [{"type": "assistant.message_delta", "data": {"deltaContent": "foo"}}])
    assert search_transcripts([("k", transcript), ("missing", tmp_path / "nope.jsonl")], "foo") == []


def test_search_snippet_marks_truncation(tmp_path):
    transcript = tmp_path / "t.jsonl"
    content = "x" * 200 + " NEEDLE " + "y" * 200
    _write(transcript, [{"type": "assistant.message", "data": {"content": content}}])

    hit = search_transcripts([("k", transcript)], "NEEDLE")[0]

    assert "NEEDLE" in hit.snippet
    assert hit.snippet.startswith("…")
