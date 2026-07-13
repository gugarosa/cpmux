# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import sqlite3

import pytest

from cmux.engine.copilot_store import (
    CopilotStoreUnavailable,
    InvalidFtsQuery,
    search_sessions,
)


def _store(tmp_path, rows):
    db = tmp_path / "store.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE VIRTUAL TABLE search_index USING fts5(content, session_id, source_type, source_id)")
    connection.executemany(
        "INSERT INTO search_index(content, session_id, source_type, source_id) VALUES (?, ?, ?, ?)", rows
    )
    connection.commit()
    connection.close()

    return db


def test_search_sessions_returns_ranked_turn_hits(tmp_path):
    db = _store(
        tmp_path,
        [
            ("fix the flaky login test", "s1", "turn", "0"),
            ("paginate the notifications list", "s2", "turn", "0"),
            ("login mentioned in a checkpoint", "s1", "checkpoint_overview", "0"),
        ],
    )
    hits = search_sessions(["s1", "s2"], "login OR paginate", db_path=db)
    assert {hit.session_id for hit in hits} == {"s1", "s2"}
    assert all("checkpoint" not in hit.snippet for hit in hits)


def test_search_sessions_is_empty_for_empty_inputs(tmp_path):
    db = _store(tmp_path, [("x", "s1", "turn", "0")])
    assert search_sessions([], "login", db_path=db) == []
    assert search_sessions(["s1"], "   ", db_path=db) == []


def test_search_sessions_raises_on_invalid_query(tmp_path):
    db = _store(tmp_path, [("x", "s1", "turn", "0")])
    with pytest.raises(InvalidFtsQuery):
        search_sessions(["s1"], "foo AND", db_path=db)


def test_search_sessions_raises_when_store_missing(tmp_path):
    with pytest.raises(CopilotStoreUnavailable):
        search_sessions(["s1"], "login", db_path=tmp_path / "absent.db")
