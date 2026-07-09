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
        A run id combining a UTC timestamp and a short random suffix.

    """

    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SessionRecord(BaseModel):
    """Durable per-session record: identity, git binding, status, and PR.

    Attributes:
        key: Stable identifier of the config item this session runs.
        name: Human-readable item name.
        slug: Filesystem-safe form of the name.
        branch: Git branch created for the session.
        base: Base branch the work branches from.
        model: Copilot model driving the session.
        session_id: Copilot session identifier.
        worktree: Path to the session's git worktree.
        base_sha: Commit sha the base branch pointed at.
        permission_flags: Extra permission flags passed to copilot.
        pid: Process id of the running session, if any.
        status: Current lifecycle status of the session.
        exit_code: Process exit code once the session finished.
        pr_url: Url of the opened pull request, if any.
        premium_requests: Count of premium requests consumed.
        files_modified: Paths changed by the session.
        error: Failure message when the session did not succeed.
        started_at: Iso timestamp when the session started.
        ended_at: Iso timestamp when the session ended.

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
    """Immutable snapshot of a run's resolved configuration (reconstructable from disk).

    Attributes:
        run_id: Identifier of the run.
        created_at: Iso timestamp when the manifest was created.
        repo_root: Absolute path to the repository root.
        config_path: Path to the config file the run resolved.
        system: Shared system prompt applied to every item.
        item_keys: Keys of the items included in the run.
        resolved: Fully resolved config items for the run.
        open_pr: Whether sessions open a pull request when done.
        concurrency: Maximum sessions to run at once, if capped.
        strip_github_token: Whether to strip the github token from sessions.
        deps_override: Command overriding dependency installation.

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
    """Filesystem paths for a single run, rooted at `<repo_root>/.cmux`.

    Attributes:
        repo_root: Absolute path to the repository root.
        run_id: Identifier of the run these paths belong to.
        root: The `.cmux` directory under the repository root.
        run_dir: Directory holding this run's artifacts.
        sessions_dir: Directory holding per-session artifacts.
        worktrees_dir: Directory holding this run's git worktrees.

    """

    def __init__(self, repo_root: str | Path, run_id: str) -> None:
        self.repo_root = Path(repo_root)
        self.run_id = run_id
        self.root = self.repo_root / CMUX_DIR
        self.run_dir = self.root / "runs" / run_id
        self.sessions_dir = self.run_dir / "sessions"
        self.worktrees_dir = self.root / "worktrees" / run_id

    @property
    def manifest(self) -> Path:
        """Path to the run manifest file."""

        return self.run_dir / "manifest.json"

    @property
    def owner_file(self) -> Path:
        """Path to the run's owner pid file."""

        return self.run_dir / "owner.json"

    def session_dir(self, key: str) -> Path:
        """Directory holding an item's session artifacts."""

        return self.sessions_dir / key

    def worktree(self, key: str) -> Path:
        """Path to an item's git worktree."""

        return self.worktrees_dir / key

    def prompt_file(self, key: str) -> Path:
        """Path to an item's resolved prompt."""

        return self.session_dir(key) / "prompt.md"

    def transcript(self, key: str) -> Path:
        """Path to an item's raw JSONL transcript."""

        return self.session_dir(key) / "transcript.jsonl"

    def record_file(self, key: str) -> Path:
        """Path to an item's serialized session record."""

        return self.session_dir(key) / "session.json"

    def copilot_log_dir(self, key: str) -> Path:
        """Path to an item's copilot `--log-dir`."""

        return self.session_dir(key) / "copilot-logs"

    def ensure_session_dirs(self, key: str) -> None:
        """Create an item's session and copilot-log directories."""

        self.session_dir(key).mkdir(parents=True, exist_ok=True)
        self.copilot_log_dir(key).mkdir(parents=True, exist_ok=True)

    def write_manifest(self, manifest: RunManifest) -> None:
        """Persist the run manifest to disk."""

        self.run_dir.mkdir(parents=True, exist_ok=True)

        self.manifest.write_text(manifest.model_dump_json(indent=2))

    def write_record(self, record: SessionRecord) -> None:
        """Persist a single session record to disk."""

        self.ensure_session_dirs(record.key)

        self.record_file(record.key).write_text(record.model_dump_json(indent=2))

    def read_record(self, key: str) -> SessionRecord:
        """Load and return an item's session record."""

        return SessionRecord.model_validate_json(self.record_file(key).read_text())


def all_run_ids(repo_root: str | Path) -> list[str]:
    """List every run id under `<repo_root>/.cmux`, newest first.

    Args:
        repo_root: Repository root containing the `.cmux` directory.

    Returns:
        Run ids sorted newest first, or empty when none exist.

    """

    runs = Path(repo_root) / CMUX_DIR / "runs"
    if not runs.is_dir():
        return []

    return sorted((entry.name for entry in runs.iterdir() if entry.is_dir()), reverse=True)


def latest_run_id(repo_root: str | Path) -> str | None:
    """Return the most recent run id under `<repo_root>/.cmux`.

    Args:
        repo_root: Repository root containing the `.cmux` directory.

    Returns:
        The newest run id, or `None` when there are no runs.

    """

    ids = all_run_ids(repo_root)

    return ids[0] if ids else None


def load_run(repo_root: str | Path, run_id: str) -> tuple[RunManifest, list[SessionRecord]]:
    """Load a run's manifest and every session record it references.

    Args:
        repo_root: Repository root containing the `.cmux` directory.
        run_id: Identifier of the run to load.

    Returns:
        The run manifest paired with its existing session records.

    """

    paths = RunPaths(repo_root, run_id)
    manifest = RunManifest.model_validate_json(paths.manifest.read_text())

    records: list[SessionRecord] = []
    for key in manifest.item_keys:
        record_path = paths.record_file(key)
        if record_path.exists():
            records.append(SessionRecord.model_validate_json(record_path.read_text()))

    return manifest, records
