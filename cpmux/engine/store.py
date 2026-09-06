# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import os
import time
from datetime import datetime, timezone
from pathlib import Path
from shutil import rmtree
from tempfile import NamedTemporaryFile
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from cpmux.config import IssueSource, ResolvedItem, validate_identifier
from cpmux.events import Status

CPMUX_DIR = ".cpmux"


def new_run_id() -> str:
    """Create a time-sortable run identifier.

    Returns:
        A time-sortable run identifier.

    """

    return f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid4().hex[:6]}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_model(path: Path, model: BaseModel) -> None:
    """Atomically replace a JSON model using a unique sibling temporary file.

    Args:
        path: Target JSON file.
        model: Model to serialize without mutation.

    Raises:
        OSError: Directory creation, writing, or replacement fails.

    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as out:
            temporary = Path(out.name)
            out.write(model.model_dump_json(indent=2))
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class CommandResult(BaseModel):
    """Persisted outcome of a configured setup or acceptance command.

    Attributes:
        name: Human-readable command label.
        command: Executed shell command retained in local run history.
        phase: Setup or acceptance-check phase.
        status: Command lifecycle outcome.
        exit_code: Shell exit status when available.
        log_path: Local combined-output log path.
        started_at: Command start timestamp.
        ended_at: Command completion timestamp.
        duration_seconds: Monotonic elapsed execution time.
        error: Actionable execution failure detail.

    """

    name: str
    command: str
    phase: Literal["setup", "check"]
    status: Literal["running", "passed", "failed", "timed_out", "cancelled"] = "running"
    exit_code: int | None = None
    log_path: str
    started_at: str = Field(default_factory=_now)
    ended_at: str | None = None
    duration_seconds: float = 0.0
    error: str | None = None


class AttemptRecord(BaseModel):
    """One execution or delivery attempt for a persistent item.

    Attributes:
        number: One-based attempt number.
        mode: Explicit reason for the attempt.
        phase: Current or last execution phase.
        status: Latest attempt outcome.
        session_id: Native Copilot session used by the attempt.
        started_at: Attempt start timestamp.
        ended_at: Attempt completion timestamp.
        exit_code: Agent exit status when available.
        error: Attempt failure detail.
        base_sha: Source commit at the start of the attempt.
        candidate_sha: Candidate commit produced by this attempt.
        commands: Setup and acceptance-command outcomes.
        agent_started: Whether a native agent process was spawned in this attempt.
        premium_requests: Latest reported usage for this attempt, not an estimate.

    """

    number: int = Field(ge=1)
    mode: Literal["initial", "retry", "resume", "fresh", "followup", "verify", "finalize", "interactive"]
    phase: Literal["pending", "setup", "agent", "verification", "delivery", "complete"] = "pending"
    status: Status = Status.STARTING
    session_id: str
    started_at: str = Field(default_factory=_now)
    ended_at: str | None = None
    exit_code: int | None = None
    error: str | None = None
    base_sha: str = ""
    candidate_sha: str | None = None
    commands: list[CommandResult] = Field(default_factory=list)
    agent_started: bool = False
    premium_requests: int | float | None = Field(default=None, ge=0, allow_inf_nan=False)


class VerificationReceipt(BaseModel):
    """Successful configured checks tied to an immutable Git source revision.

    Attributes:
        attempt: Attempt containing the command outcomes.
        commit_sha: Exact candidate commit checked.
        tree_sha: Git tree belonging to the checked commit.
        check_fingerprint: Digest of configured checks and item environment overrides.
        checked_at: Completion timestamp.

    """

    attempt: int = Field(ge=1)
    commit_sha: str
    tree_sha: str
    check_fingerprint: str
    checked_at: str = Field(default_factory=_now)


class SessionRecord(BaseModel):
    """Persisted session state and git metadata.

    Attributes:
        key: Configured item key.
        name: Human-readable session name.
        slug: Normalized slug used for naming.
        branch: Git branch name.
        base: Base branch name.
        model: Copilot model name.
        session_id: Copilot session identifier.
        worktree: Git worktree path.
        permission_flags: Copilot permission flags.
        env: Session environment variables.
        status: Current session status.
        pid: Active session process identifier, or None after cleanup.
        started_at: ISO-formatted start time.
        ended_at: ISO-formatted end time.
        exit_code: Session process exit code.
        error: Session error message.
        pr_url: Pull request URL.
        premium_requests: Premium request count.
        files_modified: Modified file paths.
        base_sha: Base commit SHA.
        pid_created_at: Operating-system creation time for the active child.
        pid_is_group: Whether the active child leads an isolated process group.
        phase: Current execution phase.
        attempts: Persistent execution and delivery attempts.
        setup_complete: Whether the configured setup has succeeded.
        agent_complete: Whether the latest required agent execution succeeded.
        native_started: Whether native events or a successful interactive resume confirmed the session.
        last_activity_at: Last observed process or command activity.
        candidate_sha: Latest local candidate commit.
        delivery_sha: Exact commit most recently delivered to the remote.
        verification: Successful source-bound acceptance-check receipt.
        pr_title: Persisted agent-authored or configured PR title.
        pr_body: Persisted agent-authored or configured PR description.
        timeout_seconds: Optional agent execution limit carried into follow-ups.
        base_from: Explicit predecessor used when this worktree was created.
        source: Imported issue provenance, when configured.
        recovery_mode: Requested next execution mode persisted before a recovery starts.

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
    env: dict[str, str] = Field(default_factory=dict)
    pid: int | None = Field(default=None, gt=0, strict=True)
    status: Status = Status.PENDING
    exit_code: int | None = None
    pr_url: str | None = None
    premium_requests: int | float | None = Field(default=None, ge=0, allow_inf_nan=False)
    files_modified: list[str] = Field(default_factory=list)
    error: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    pid_created_at: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    pid_is_group: bool = True
    phase: Literal["pending", "setup", "agent", "verification", "delivery", "complete"] = "pending"
    attempts: list[AttemptRecord] = Field(default_factory=list)
    setup_complete: bool = False
    agent_complete: bool = False
    native_started: bool = False
    last_activity_at: str | None = None
    candidate_sha: str | None = None
    delivery_sha: str | None = None
    verification: VerificationReceipt | None = None
    pr_title: str | None = None
    pr_body: str | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    base_from: str | None = None
    source: IssueSource | None = None
    recovery_mode: Literal["retry", "resume", "fresh"] | None = None

    def mark_started(self) -> None:
        """Stamp the session's start time."""

        self.started_at = _now()

    def mark_ended(self) -> None:
        """Stamp the session's end time."""

        self.ended_at = _now()

    def begin_attempt(
        self, mode: Literal["initial", "retry", "resume", "fresh", "followup", "verify", "finalize", "interactive"]
    ) -> AttemptRecord:
        """Start an attempt without discarding prior attempts or accumulated usage.

        Args:
            mode: Explicit execution or delivery operation.

        Returns:
            Newly appended mutable attempt record.

        """

        if self.started_at is None:
            self.mark_started()
        self.ended_at = None
        self.exit_code = None
        self.error = None
        self.pid = None
        self.pid_created_at = None
        self.status = Status.STARTING
        self.last_activity_at = _now()
        attempt = AttemptRecord(
            number=len(self.attempts) + 1,
            mode=mode,
            session_id=self.session_id,
            base_sha=self.base_sha,
        )
        self.attempts.append(attempt)
        return attempt

    def record_usage(self, premium_requests: int | float) -> None:
        """Accumulate the latest attempt usage without counting repeated events twice.

        Args:
            premium_requests: Validated native usage for the current attempt.

        Raises:
            ValueError: No attempt has been started.

        """

        if not self.attempts:
            raise ValueError("`usage` requires an active attempt.")
        attempt = self.attempts[-1]
        self.premium_requests = (self.premium_requests or 0) - (attempt.premium_requests or 0) + premium_requests
        attempt.premium_requests = premium_requests

    def finish_attempt(self) -> None:
        """Finish the latest attempt using the current item outcome."""

        self.pid = None
        self.pid_created_at = None
        self.mark_ended()
        self.last_activity_at = self.ended_at
        if self.attempts:
            attempt = self.attempts[-1]
            attempt.phase = self.phase
            attempt.status = self.status
            attempt.exit_code = self.exit_code
            attempt.error = self.error
            attempt.candidate_sha = self.candidate_sha
            attempt.base_sha = self.base_sha
            attempt.ended_at = self.ended_at

    @property
    def elapsed_seconds(self) -> float | None:
        """Seconds from start to end, or to now while still running."""

        if not self.started_at:
            return None

        end = datetime.fromisoformat(self.ended_at) if self.ended_at else datetime.now(timezone.utc)

        return (end - datetime.fromisoformat(self.started_at)).total_seconds()


class RunManifest(BaseModel):
    """Resolved run configuration in `manifest.json`.

    Attributes:
        run_id: Run identifier.
        created_at: ISO-formatted creation time.
        repo_root: Repository root path.
        config_path: Configuration file path.
        system: System prompt.
        item_keys: Resolved item keys.
        resolved: Resolved run items.
        open_pr: Whether to open pull requests.
        concurrency: Maximum concurrent sessions.
        strip_github_token: Whether GitHub delivery removes ambient authentication tokens.
        deps_override: Worktree dependency provisioning strategy override.
        premium_budget: Soft admission ceiling for the run's reported premium requests.

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
    premium_budget: int | None = Field(default=None, ge=1)


class RunPaths:
    """Paths for one run under `<repo_root>/.cpmux`."""

    def __init__(self, repo_root: str | Path, run_id: str) -> None:
        """Build paths for a run.

        Args:
            repo_root: Repository root for the run.
            run_id: Run identifier.

        Raises:
            ValueError: The run identifier is not a normalized relative name.

        """

        run_id = validate_identifier(run_id, "run_id")
        self.repo_root = Path(repo_root)
        self.run_id = run_id

        self.root = self.repo_root / CPMUX_DIR
        self.run_dir = self.root / "runs" / run_id
        self.sessions_dir = self.run_dir / "sessions"
        self.worktrees_dir = self.root / "worktrees" / run_id

    @property
    def manifest(self) -> Path:
        """Path to the run's `manifest.json`."""

        return self.run_dir / "manifest.json"

    @property
    def owner_file(self) -> Path:
        """Path to the run owner's process identity metadata."""

        return self.run_dir / "owner.json"

    @property
    def owner_lock(self) -> Path:
        """Path to the run coordination lock."""

        return self.run_dir / "owner.lock"

    @property
    def ready_file(self) -> Path:
        """Path to the detached-owner startup marker."""

        return self.run_dir / "ready"

    @property
    def pause_file(self) -> Path:
        """Path to the queue-admission pause marker."""

        return self.run_dir / "paused"

    def session_owner(self, key: str) -> Path:
        """Build the session-operation owner record path.

        Args:
            key: Session identifier.

        Returns:
            Session owner metadata path.

        """

        return self.session_dir(key) / "owner.json"

    def session_lock(self, key: str) -> Path:
        """Build the session-operation lock path.

        Args:
            key: Session identifier.

        Returns:
            Exclusive session lock path.

        """

        return self.session_dir(key) / "session.lock"

    def stop_file(self, key: str | None = None) -> Path:
        """Build an owner-consumed cancellation marker path.

        Args:
            key: Session identifier, or None for the whole run.

        Returns:
            Cancellation marker path.

        """

        return (self.run_dir if key is None else self.session_dir(key)) / "stop"

    def attempt_dir(self, key: str, number: int) -> Path:
        """Build an attempt's local artifact directory.

        Args:
            key: Session identifier.
            number: One-based attempt number.

        Returns:
            Attempt artifact directory without creating it.

        Raises:
            ValueError: The attempt number is not positive.

        """

        if number < 1:
            raise ValueError("`attempt` must be positive.")
        return self.session_dir(key) / "attempts" / f"{number:04d}"

    def session_dir(self, key: str) -> Path:
        """Build the session artifact directory path.

        Args:
            key: Normalized relative session identifier.

        Returns:
            Session directory path without creating it.

        Raises:
            ValueError: The session identifier is invalid.

        """

        return self.sessions_dir / validate_identifier(key, "key")

    def worktree(self, key: str) -> Path:
        """Build the session's Git worktree path.

        Args:
            key: Normalized relative session identifier.

        Returns:
            Worktree path without creating it.

        Raises:
            ValueError: The session identifier is invalid.

        """

        return self.worktrees_dir / validate_identifier(key, "key")

    def prompt_file(self, key: str) -> Path:
        """Build the stored prompt path.

        Args:
            key: Normalized relative session identifier.

        Returns:
            Path to the session's `prompt.md`.

        Raises:
            ValueError: The session identifier is invalid.

        """

        return self.session_dir(key) / "prompt.md"

    def transcript(self, key: str) -> Path:
        """Build the raw JSONL transcript path.

        Args:
            key: Normalized relative session identifier.

        Returns:
            Path to the session's `transcript.jsonl`.

        Raises:
            ValueError: The session identifier is invalid.

        """

        return self.session_dir(key) / "transcript.jsonl"

    def record_file(self, key: str) -> Path:
        """Build the persisted session record path.

        Args:
            key: Normalized relative session identifier.

        Returns:
            Path to the session's `session.json`.

        Raises:
            ValueError: The session identifier is invalid.

        """

        return self.session_dir(key) / "session.json"

    def copilot_log_dir(self, key: str) -> Path:
        """Build the Copilot diagnostic log directory path.

        Args:
            key: Normalized relative session identifier.

        Returns:
            Copilot log directory path without creating it.

        Raises:
            ValueError: The session identifier is invalid.

        """

        return self.session_dir(key) / "copilot-logs"

    def ensure_session_dirs(self, key: str) -> None:
        """Create session and Copilot log directories.

        Args:
            key: Normalized relative session identifier.

        Raises:
            OSError: A required directory cannot be created.
            ValueError: The session identifier is invalid.

        """

        self.session_dir(key).mkdir(parents=True, exist_ok=True)
        self.copilot_log_dir(key).mkdir(parents=True, exist_ok=True)

    def write_manifest(self, manifest: RunManifest) -> None:
        """Write the manifest after creating the run directory.

        Args:
            manifest: Resolved run configuration to serialize.

        Raises:
            OSError: Directory creation or manifest writing fails.

        """

        write_model(self.manifest, manifest)

    def write_record(self, record: SessionRecord) -> None:
        """Replace the stored record atomically after creating its directories.

        This protects readers from partial JSON, but does not coordinate competing
        writers or merge their changes.

        Args:
            record: Session record to serialize without mutating it.

        Raises:
            OSError: Directory creation, writing, or replacement fails.
            ValueError: The record key is not a valid storage identifier.

        """

        self.ensure_session_dirs(record.key)
        write_model(self.record_file(record.key), record)

    def read_record(self, key: str) -> SessionRecord:
        """Read and validate the persisted record without updating it.

        Args:
            key: Session identifier.

        Returns:
            A new record populated from stored JSON.

        Raises:
            OSError: The record file cannot be read.
            ValueError: The identifier or stored record is invalid.

        """

        path = self.record_file(key)
        record = SessionRecord.model_validate_json(path.read_text(encoding="utf-8"))
        if record.key != key:
            raise ValueError(f"`{path}` contains key `{record.key}`, expected `{key}`.")
        return record

    def refresh_record(self, record: SessionRecord) -> None:
        """Refresh a caller's record in place while its operation holds a lease.

        Args:
            record: Record to replace from its persisted state.

        Raises:
            OSError: The record cannot be read.
            ValueError: Persisted state is invalid.

        """

        current = self.read_record(record.key)
        for name in SessionRecord.model_fields:
            setattr(record, name, getattr(current, name))


def all_run_ids(repo_root: str | Path) -> list[str]:
    """List run IDs newest first.

    Args:
        repo_root: Repository root containing run history.

    Returns:
        Run identifiers ordered newest first.

    """

    runs = Path(repo_root) / CPMUX_DIR / "runs"
    if not runs.is_dir():
        return []

    return sorted(
        (entry.name for entry in runs.iterdir() if entry.is_dir() and (entry / "manifest.json").is_file()),
        reverse=True,
    )


def latest_run_id(repo_root: str | Path) -> str | None:
    """Return the latest run ID.

    Args:
        repo_root: Repository root containing run history.

    Returns:
        Latest run identifier, or None when no runs exist.

    """

    ids = all_run_ids(repo_root)

    return ids[0] if ids else None


def load_run(repo_root: str | Path, run_id: str) -> tuple[RunManifest, list[SessionRecord]]:
    """Load a manifest and existing session records.

    Args:
        repo_root: Repository root containing run history.
        run_id: Run identifier.

    Returns:
        Manifest and existing records in manifest order, omitting missing records.

    Raises:
        OSError: The manifest or a present record cannot be read.
        ValueError: Run identifiers or stored data are invalid.

    """

    paths = RunPaths(repo_root, run_id)
    manifest = RunManifest.model_validate_json(paths.manifest.read_text(encoding="utf-8"))

    records: list[SessionRecord] = []
    for key in manifest.item_keys:
        record_path = paths.record_file(key)
        if record_path.exists():
            records.append(paths.read_record(key))

    return manifest, records


def delete_run(repo_root: str | Path, run_id: str) -> None:
    """Delete a run's on-disk history and worktree directory.

    Args:
        repo_root: Repository root containing run history.
        run_id: Run identifier.

    """

    paths = RunPaths(repo_root, run_id)
    for directory in (paths.worktrees_dir, paths.run_dir):
        try:
            rmtree(directory)
        except FileNotFoundError:
            if directory.exists():
                raise
