# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from cmux.config import ResolvedItem
from cmux.events import Status

CMUX_DIR = ".cmux"


def new_run_id() -> str:
    """Return a time-sortable run identifier.

    Returns:
        UTC timestamp plus a short random suffix.

    """

    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionRecord(BaseModel):
    """Persist session identity, git binding, status, and PR.

    Attributes:
        key: Stable config item identifier.
        name: Item name.
        slug: Branch- and worktree-safe slug.
        branch: Session git branch.
        base: Base branch.
        model: Copilot model.
        session_id: Copilot session identifier.
        worktree: Session git worktree path.
        base_sha: Base branch commit sha.
        permission_flags: Extra copilot permission flags.
        pid: Running session process id, if any.
        status: Current lifecycle status.
        exit_code: Process exit code after finish.
        pr_url: Opened pull request URL, if any.
        premium_requests: Premium request count.
        files_modified: Changed paths.
        error: Failure message.
        started_at: Session start ISO timestamp.
        ended_at: Session end ISO timestamp.

    """

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


class RunManifest(BaseModel):
    """Persist resolved run configuration as `manifest.json`.

    Attributes:
        run_id: Run identifier.
        created_at: Manifest creation ISO timestamp.
        repo_root: Repository root path.
        config_path: Resolved config file path.
        system: Shared system prompt.
        item_keys: Included item keys.
        resolved: Resolved config items.
        open_pr: Open pull requests when sessions finish.
        concurrency: Maximum concurrent sessions, if capped.
        strip_github_token: Strip the github token from sessions.
        deps_override: Dependency strategy override, if set.

    """

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
    """Filesystem paths for a single run, rooted at `<repo_root>/.cmux`."""

    def __init__(self, repo_root: str | Path, run_id: str) -> None:
        """Initialize run paths.

        Args:
            repo_root: Target git repository root.
            run_id: Run identifier.

        """

        self.repo_root = Path(repo_root)
        self.run_id = run_id
        self.root = self.repo_root / CMUX_DIR
        self.run_dir = self.root / "runs" / run_id
        self.sessions_dir = self.run_dir / "sessions"
        self.worktrees_dir = self.root / "worktrees" / run_id

    @property
    def manifest(self) -> Path:
        """Run manifest file path."""

        return self.run_dir / "manifest.json"

    @property
    def owner_file(self) -> Path:
        """Owner pid file path."""

        return self.run_dir / "owner.json"

    def session_dir(self, key: str) -> Path:
        """Item session artifact directory."""

        return self.sessions_dir / key

    def worktree(self, key: str) -> Path:
        """Item git worktree path."""

        return self.worktrees_dir / key

    def prompt_file(self, key: str) -> Path:
        """Item resolved prompt path."""

        return self.session_dir(key) / "prompt.md"

    def transcript(self, key: str) -> Path:
        """Item raw JSONL transcript path."""

        return self.session_dir(key) / "transcript.jsonl"

    def record_file(self, key: str) -> Path:
        """Item serialized session record path."""

        return self.session_dir(key) / "session.json"

    def copilot_log_dir(self, key: str) -> Path:
        """Item copilot `--log-dir` path."""

        return self.session_dir(key) / "copilot-logs"

    def ensure_session_dirs(self, key: str) -> None:
        """Create item session and copilot-log directories."""

        self.session_dir(key).mkdir(parents=True, exist_ok=True)
        self.copilot_log_dir(key).mkdir(parents=True, exist_ok=True)

    def write_manifest(self, manifest: RunManifest) -> None:
        """Persist the run manifest."""

        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text(manifest.model_dump_json(indent=2))

    def write_record(self, record: SessionRecord) -> None:
        """Persist a session record."""

        self.ensure_session_dirs(record.key)
        self.record_file(record.key).write_text(record.model_dump_json(indent=2))

    def read_record(self, key: str) -> SessionRecord:
        """Load an item session record."""

        return SessionRecord.model_validate_json(self.record_file(key).read_text())


def all_run_ids(repo_root: str | Path) -> list[str]:
    """List every run id under `<repo_root>/.cmux`, newest first.

    Args:
        repo_root: Repository root with the `.cmux` directory.

    Returns:
        Run ids sorted newest first, or empty if none exist.

    """

    runs = Path(repo_root) / CMUX_DIR / "runs"
    if not runs.is_dir():
        return []

    return sorted((entry.name for entry in runs.iterdir() if entry.is_dir()), reverse=True)


def latest_run_id(repo_root: str | Path) -> str | None:
    """Return the most recent run id under `<repo_root>/.cmux`.

    Args:
        repo_root: Repository root with the `.cmux` directory.

    Returns:
        Newest run id, or `None` when none exist.

    """

    ids = all_run_ids(repo_root)

    return ids[0] if ids else None


def load_run(repo_root: str | Path, run_id: str) -> tuple[RunManifest, list[SessionRecord]]:
    """Load a run manifest and referenced session records.

    Args:
        repo_root: Repository root with the `.cmux` directory.
        run_id: Run identifier.

    Returns:
        Run manifest and existing session records.

    """

    paths = RunPaths(repo_root, run_id)
    manifest = RunManifest.model_validate_json(paths.manifest.read_text())

    records: list[SessionRecord] = []
    for key in manifest.item_keys:
        record_path = paths.record_file(key)
        if record_path.exists():
            records.append(SessionRecord.model_validate_json(record_path.read_text()))

    return manifest, records
