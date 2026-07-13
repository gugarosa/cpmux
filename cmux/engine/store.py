# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from cmux.config import ResolvedItem
from cmux.events import Status

CMUX_DIR = ".cmux"


def new_run_id() -> str:
    """Return a time-sortable run identifier."""

    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionRecord(BaseModel):
    """Persisted session state and git metadata."""

    key: str
    name: str
    slug: str
    branch: str
    base: str
    model: str
    session_id: str
    worktree: str
    base_sha: str = ""
    permission_flags: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    pid: int | None = None
    status: Status = Status.PENDING
    exit_code: int | None = None
    pr_url: str | None = None
    premium_requests: int | None = None
    files_modified: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: str | None = None
    ended_at: str | None = None

    def mark_started(self) -> None:
        """Stamp the session's start time."""

        self.started_at = _now()

    def mark_ended(self) -> None:
        """Stamp the session's end time."""

        self.ended_at = _now()

    @property
    def elapsed_seconds(self) -> float | None:
        """Seconds from start to end, or to now while still running."""

        if not self.started_at:
            return None

        end = datetime.fromisoformat(self.ended_at) if self.ended_at else datetime.now(timezone.utc)

        return (end - datetime.fromisoformat(self.started_at)).total_seconds()


class RunManifest(BaseModel):
    """Resolved run configuration in `manifest.json`."""

    run_id: str
    created_at: str = Field(default_factory=_now)
    repo_root: str
    config_path: str
    system: str = ""
    item_keys: list[str] = Field(default_factory=list)
    resolved: list[ResolvedItem] = Field(default_factory=list)
    open_pr: bool = True
    concurrency: int | None = None
    strip_github_token: bool = True
    deps_override: str | None = None


class RunPaths:
    """Paths for one run under `<repo_root>/.cmux`."""

    def __init__(self, repo_root: str | Path, run_id: str) -> None:
        self.repo_root = Path(repo_root)
        self.run_id = run_id

        self.root = self.repo_root / CMUX_DIR
        self.run_dir = self.root / "runs" / run_id
        self.sessions_dir = self.run_dir / "sessions"
        self.worktrees_dir = self.root / "worktrees" / run_id

    @property
    def manifest(self) -> Path:
        """`manifest.json` path."""

        return self.run_dir / "manifest.json"

    @property
    def owner_file(self) -> Path:
        """Owner PID path."""

        return self.run_dir / "owner.json"

    def session_dir(self, key: str) -> Path:
        """Session artifact directory."""

        return self.sessions_dir / key

    def worktree(self, key: str) -> Path:
        """Git worktree path."""

        return self.worktrees_dir / key

    def prompt_file(self, key: str) -> Path:
        """Resolved prompt path."""

        return self.session_dir(key) / "prompt.md"

    def transcript(self, key: str) -> Path:
        """Raw JSONL transcript path."""

        return self.session_dir(key) / "transcript.jsonl"

    def record_file(self, key: str) -> Path:
        """Session record path."""

        return self.session_dir(key) / "session.json"

    def copilot_log_dir(self, key: str) -> Path:
        """Copilot log directory."""

        return self.session_dir(key) / "copilot-logs"

    def ensure_session_dirs(self, key: str) -> None:
        """Create session and Copilot log directories."""

        self.session_dir(key).mkdir(parents=True, exist_ok=True)
        self.copilot_log_dir(key).mkdir(parents=True, exist_ok=True)

    def write_manifest(self, manifest: RunManifest) -> None:
        """Persist the manifest."""

        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(manifest.model_dump_json(indent=2))

    def write_record(self, record: SessionRecord) -> None:
        """Persist the record atomically."""

        self.ensure_session_dirs(record.key)
        target = self.record_file(record.key)
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(record.model_dump_json(indent=2))
        os.replace(tmp, target)

    def read_record(self, key: str) -> SessionRecord:
        """Load a session record."""

        return SessionRecord.model_validate_json(self.record_file(key).read_text())


def all_run_ids(repo_root: str | Path) -> list[str]:
    """List run IDs newest first."""

    runs = Path(repo_root) / CMUX_DIR / "runs"
    if not runs.is_dir():
        return []

    return sorted((entry.name for entry in runs.iterdir() if entry.is_dir()), reverse=True)


def latest_run_id(repo_root: str | Path) -> str | None:
    """Return the latest run ID."""

    ids = all_run_ids(repo_root)

    return ids[0] if ids else None


def load_run(repo_root: str | Path, run_id: str) -> tuple[RunManifest, list[SessionRecord]]:
    """Load a manifest and existing session records."""

    paths = RunPaths(repo_root, run_id)
    manifest = RunManifest.model_validate_json(paths.manifest.read_text())

    records: list[SessionRecord] = []
    for key in manifest.item_keys:
        record_path = paths.record_file(key)
        if record_path.exists():
            records.append(SessionRecord.model_validate_json(record_path.read_text()))

    return manifest, records
