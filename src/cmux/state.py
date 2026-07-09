"""On-disk run state for cmux: the repo-local ``.cmux/`` control plane.

Layout (repo-local, gitignored)::

    .cmux/
      runs/<run_id>/
        manifest.json                 resolved run config
        sessions/<key>/
          prompt.md                   the exact prompt sent to copilot
          transcript.jsonl            raw tee of copilot --output-format json
          session.json                per-session record (status, branch, PR...)
          copilot-logs/               copilot's own --log-dir
      worktrees/<run_id>/<key>/       one git worktree per item

copilot keeps its own transcripts/FTS index in ``~/.copilot``; cmux only owns
the orchestration bookkeeping above.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from .events import Status

CMUX_DIR = ".cmux"


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionRecord(BaseModel):
    key: str
    name: str
    slug: str
    branch: str
    base: str
    model: str
    session_id: str
    worktree: str
    status: Status = Status.PENDING
    exit_code: int | None = None
    pr_url: str | None = None
    premium_requests: int | None = None
    files_modified: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: str | None = None
    ended_at: str | None = None

    def mark_started(self) -> None:
        self.started_at = _now()

    def mark_ended(self) -> None:
        self.ended_at = _now()


class RunManifest(BaseModel):
    run_id: str
    created_at: str = Field(default_factory=_now)
    repo_root: str
    config_path: str
    system: str = ""
    item_keys: list[str] = Field(default_factory=list)


class RunPaths:
    """Filesystem paths for a single run, rooted at ``<repo_root>/.cmux``."""

    def __init__(self, repo_root: str | Path, run_id: str) -> None:
        self.repo_root = Path(repo_root)
        self.run_id = run_id
        self.root = self.repo_root / CMUX_DIR
        self.run_dir = self.root / "runs" / run_id
        self.sessions_dir = self.run_dir / "sessions"
        self.worktrees_dir = self.root / "worktrees" / run_id

    @property
    def manifest(self) -> Path:
        return self.run_dir / "manifest.json"

    def session_dir(self, key: str) -> Path:
        return self.sessions_dir / key

    def worktree(self, key: str) -> Path:
        return self.worktrees_dir / key

    def prompt_file(self, key: str) -> Path:
        return self.session_dir(key) / "prompt.md"

    def transcript(self, key: str) -> Path:
        return self.session_dir(key) / "transcript.jsonl"

    def record_file(self, key: str) -> Path:
        return self.session_dir(key) / "session.json"

    def copilot_log_dir(self, key: str) -> Path:
        return self.session_dir(key) / "copilot-logs"

    def ensure_session_dirs(self, key: str) -> None:
        self.session_dir(key).mkdir(parents=True, exist_ok=True)
        self.copilot_log_dir(key).mkdir(parents=True, exist_ok=True)

    def write_manifest(self, manifest: RunManifest) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(manifest.model_dump_json(indent=2))

    def write_record(self, record: SessionRecord) -> None:
        self.ensure_session_dirs(record.key)
        self.record_file(record.key).write_text(record.model_dump_json(indent=2))

    def read_record(self, key: str) -> SessionRecord:
        return SessionRecord.model_validate_json(self.record_file(key).read_text())


def latest_run_id(repo_root: str | Path) -> str | None:
    runs = Path(repo_root) / CMUX_DIR / "runs"
    if not runs.is_dir():
        return None
    candidates = sorted((p.name for p in runs.iterdir() if p.is_dir()), reverse=True)
    return candidates[0] if candidates else None


def load_run(repo_root: str | Path, run_id: str) -> tuple[RunManifest, list[SessionRecord]]:
    paths = RunPaths(repo_root, run_id)
    manifest = RunManifest.model_validate_json(paths.manifest.read_text())
    records: list[SessionRecord] = []
    for key in manifest.item_keys:
        f = paths.record_file(key)
        if f.exists():
            records.append(SessionRecord.model_validate_json(f.read_text()))
    return manifest, records
