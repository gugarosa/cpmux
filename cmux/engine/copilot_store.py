# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_STORE = Path.home() / ".copilot" / "session-store.db"
_CHUNK = 400


class CopilotStoreUnavailable(Exception):
    """Copilot session store query failure."""


class InvalidFtsQuery(Exception):
    """Invalid FTS5 query."""


@dataclass
class FtsHit:
    """Ranked match from Copilot's session store.

    Attributes:
        session_id: Copilot session identifier.
        snippet: Matched text excerpt.
        rank: Relevance rank.

    """

    session_id: str
    snippet: str
    rank: float


def search_sessions(session_ids: list[str], query: str, limit: int = 50, db_path: Path | None = None) -> list[FtsHit]:
    """Search turns in selected Copilot sessions.

    Args:
        session_ids: Session IDs.
        query: FTS5 query.
        limit: Hit limit.
        db_path: Session store path.

    Returns:
        Relevance-ranked hits.

    Raises:
        InvalidFtsQuery: Query has invalid FTS5 syntax.
        CopilotStoreUnavailable: Store is missing or unreadable.

    """

    if not session_ids or not query.strip():
        return []

    store = db_path or _DEFAULT_STORE
    if not store.exists():
        raise CopilotStoreUnavailable(f"`{store}` Copilot session store not found.")

    try:
        connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.OperationalError as exc:
        raise CopilotStoreUnavailable(f"`{store}` Copilot session store open failed: {exc}.") from exc

    try:
        hits = _query(connection, sorted(set(session_ids)), query, limit)
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "fts5" in message or "unterminated" in message or "syntax" in message:
            raise InvalidFtsQuery(f"`{query}` is invalid: {exc}.") from exc
        raise CopilotStoreUnavailable(f"copilot session store query failed: {exc}.") from exc
    finally:
        connection.close()

    hits.sort(key=lambda hit: hit.rank)

    return hits[:limit]


def _query(connection: sqlite3.Connection, session_ids: list[str], query: str, limit: int) -> list[FtsHit]:
    hits: list[FtsHit] = []
    for start in range(0, len(session_ids), _CHUNK):
        chunk = session_ids[start : start + _CHUNK]
        placeholders = ",".join("?" * len(chunk))
        rows = connection.execute(
            f"SELECT session_id, snippet(search_index, 0, '', '', '…', 10), rank "
            f"FROM search_index WHERE session_id IN ({placeholders}) "
            f"AND source_type = 'turn' AND content MATCH ? ORDER BY rank LIMIT ?",
            (*chunk, query, limit),
        ).fetchall()
        hits.extend(FtsHit(session_id, " ".join(snippet.split()), rank) for session_id, snippet, rank in rows)

    return hits
