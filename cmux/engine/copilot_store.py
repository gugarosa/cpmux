# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_STORE = Path.home() / ".copilot" / "session-store.db"
_CHUNK = 400


class CopilotStoreUnavailable(Exception):
    """Raised when copilot's session store cannot be queried."""


class InvalidFtsQuery(Exception):
    """Raised when a search query is not valid FTS5 syntax."""


@dataclass
class FtsHit:
    """A ranked full-text match from copilot's session store.

    Attributes:
        session_id: Session the match belongs to.
        snippet: Excerpt centered on the match.
        rank: FTS5 bm25 rank, smaller is more relevant.

    """

    session_id: str
    snippet: str
    rank: float


def search_sessions(session_ids: list[str], query: str, limit: int = 50, db_path: Path | None = None) -> list[FtsHit]:
    """Search copilot's full-text index for turns in the given sessions.

    Args:
        session_ids: Sessions to search within.
        query: FTS5 query, supporting operators like `OR`, `"phrase"`, and `term*`.
        limit: Maximum number of hits to return.
        db_path: Copilot session store, defaulting to `~/.copilot/session-store.db`.

    Returns:
        Hits ranked most-relevant first.

    Raises:
        InvalidFtsQuery: If the query is not valid FTS5 syntax.
        CopilotStoreUnavailable: If the store is missing or cannot be read.

    """

    if not session_ids or not query.strip():
        return []

    store = db_path or _DEFAULT_STORE
    if not store.exists():
        raise CopilotStoreUnavailable(f"`{store}` copilot session store does not exist.")

    try:
        connection = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.OperationalError as exc:
        raise CopilotStoreUnavailable(f"copilot session store could not be opened: {exc}.") from exc

    try:
        hits = _query(connection, sorted(set(session_ids)), query, limit)
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "fts5" in message or "unterminated" in message or "syntax" in message:
            raise InvalidFtsQuery(f"`{query}` is not a valid search query: {exc}.") from exc
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
