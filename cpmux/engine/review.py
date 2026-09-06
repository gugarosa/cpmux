# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from cpmux.config import ResolvedItem
from cpmux.engine.delivery import finalize_item
from cpmux.engine.interact import _operation, _run_followup
from cpmux.engine.store import RunManifest, RunPaths, SessionRecord
from cpmux.events import SessionState
from cpmux.vcs import git


class ReviewError(ValueError):
    """Raised when a review operation cannot safely use its source revision."""


class StaleRevisionError(ReviewError):
    """Raised when feedback no longer matches the current worktree revision."""


@dataclass(frozen=True)
class DiffSnapshot:
    """Immutable review text tied to the complete current Git state.

    Attributes:
        revision: Digest of HEAD, index, tracked worktree, and untracked contents.
        base_sha: Exact base commit from which the displayed changes start.
        head_sha: Exact commit checked out while the snapshot was captured.
        text: Binary-capable Git diff including non-ignored untracked files.

    """

    revision: str
    base_sha: str
    head_sha: str
    text: str


def _read_git(args: list[str], worktree: str | Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return git.run_git(
        ["--no-pager", "-c", "color.ui=false", *args],
        worktree,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        check=check,
    )


def _untracked(worktree: str | Path) -> list[str]:
    output = _read_git(
        ["ls-files", "--others", "--exclude-standard", "--no-directory", "-z"],
        worktree,
    ).stdout
    return sorted(path for path in output.split("\0") if path)


def _untracked_bytes(worktree: Path, relative: str) -> bytes:
    path = worktree / relative
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            contents = os.readlink(path).encode("utf-8", errors="surrogateescape")
        elif stat.S_ISREG(mode):
            contents = path.read_bytes()
        else:
            raise ReviewError(f"`{relative}` is not a regular file or symbolic link.")
    except OSError as exc:
        raise ReviewError(f"`{relative}` could not be read for review: {str(exc).removesuffix('.')}.") from exc

    return f"{mode:o}\0{relative}\0".encode("utf-8", errors="surrogateescape") + contents


def _revision_material(worktree: Path) -> tuple[str, bytes]:
    head = _read_git(["rev-parse", "--verify", "HEAD"], worktree).stdout.strip()
    commands = [
        ["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=no"],
        ["diff", "--cached", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
        ["diff", "--binary", "--full-index", "--no-ext-diff", "--no-textconv", "--"],
    ]
    digest = hashlib.sha256()
    digest.update(head.encode("ascii"))
    for command in commands:
        output = _read_git(command, worktree).stdout.encode("utf-8", errors="surrogateescape")
        digest.update(len(output).to_bytes(8, "big"))
        digest.update(output)
    for relative in _untracked(worktree):
        contents = _untracked_bytes(worktree, relative)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(contents)

    return head, digest.digest()


def _untracked_diff(worktree: Path, relative: str) -> str:
    proc = _read_git(
        [
            "diff",
            "--no-index",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "--",
            "/dev/null",
            relative,
        ],
        worktree,
        check=False,
    )
    if proc.returncode not in (0, 1):
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise git.GitError(f"`git diff` for `{relative}` failed: {detail.removesuffix('.')}.")

    return proc.stdout


def diff_snapshot(paths: RunPaths, record: SessionRecord) -> DiffSnapshot:
    """Capture the complete non-ignored source diff without mutating Git state.

    Args:
        paths: Storage paths identifying the run being reviewed.
        record: Session whose worktree and recorded base define the review.

    Returns:
        Immutable diff and revision identity for later feedback validation.

    Raises:
        FileNotFoundError: The recorded worktree is missing.
        ReviewError: The worktree changes while being read or a source file is unreadable.
        git.GitError: HEAD, the recorded base, status, or diff cannot be read.

    """

    paths.record_file(record.key)
    worktree = Path(record.worktree)
    if not worktree.is_dir():
        raise FileNotFoundError(f"`worktree={worktree}` does not exist or is not a directory.")
    if not record.base_sha:
        raise git.GitError(f"`session={record.key}` has no recorded base commit.")

    base = _read_git(["rev-parse", "--verify", f"{record.base_sha}^{{commit}}"], worktree).stdout.strip()
    head, before = _revision_material(worktree)
    tracked = _read_git(
        [
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            base,
            "--",
        ],
        worktree,
    ).stdout
    untracked = "".join(_untracked_diff(worktree, relative) for relative in _untracked(worktree))
    final_head, after = _revision_material(worktree)
    if (head, before) != (final_head, after):
        raise ReviewError(f"`worktree={worktree}` changed while its review snapshot was being read. Retry the review.")

    revision = hashlib.sha256(base.encode("ascii") + b"\0" + after).hexdigest()
    return DiffSnapshot(revision=revision, base_sha=base, head_sha=head, text=tracked + untracked)


def _feedback_message(message: str, revision: str, file_path: str | None, line: int | None) -> str:
    if not message.strip():
        raise ValueError("`message` must not be empty.")
    if not revision.strip():
        raise ValueError("`revision` must not be empty.")
    if line is not None and file_path is None:
        raise ValueError("`line` requires `file_path`.")
    target = ""
    if file_path is not None:
        path = PurePosixPath(file_path)
        if not file_path.strip() or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"`file_path` must be a relative source path, but got {file_path!r}.")
        if line is not None and line < 1:
            raise ValueError(f"`line` must be positive, but got {line}.")
        target = f"\nTarget: `{file_path}`" + (f", line {line}" if line is not None else "")

    return f"Review revision: `{revision}`{target}\n\n{message}"


async def run_feedback(
    paths: RunPaths,
    record: SessionRecord,
    message: str,
    revision: str,
    *,
    file_path: str | None = None,
    line: int | None = None,
) -> SessionState:
    """Apply feedback only while its reviewed source revision is still current.

    The revision is checked under the session mutation lease before any attempt
    or receipt mutation. Accepted feedback uses the shared follow-up engine and
    never finalizes or pushes Git changes.

    Args:
        paths: Storage paths for the existing run.
        record: Session to resume and update.
        message: Repair instructions.
        revision: Revision token returned by `diff_snapshot`.
        file_path: Optional relative source path receiving the feedback.
        line: Optional one-based line within `file_path`.

    Returns:
        Terminal native session state.

    Raises:
        StaleRevisionError: The reviewed source no longer matches the worktree.
        ValueError: The feedback or optional target is invalid.
        OwnershipError: Another operation owns the run or session.
        OSError: Git state, transcript, or record persistence fails.
        asyncio.CancelledError: The repair is cancelled after subprocess cleanup.

    """

    prompt = _feedback_message(message, revision, file_path, line)

    def require_current() -> None:
        current = diff_snapshot(paths, record)
        if current.revision != revision:
            raise StaleRevisionError(
                f"`revision={revision}` is stale; current worktree revision is `{current.revision}`. Review it again."
            )

    return await _run_followup(paths, record, prompt, before_start=require_current)


def _resolved_item(paths: RunPaths, record: SessionRecord) -> tuple[RunManifest, ResolvedItem]:
    manifest = RunManifest.model_validate_json(paths.manifest.read_text(encoding="utf-8"))
    item = next((entry for entry in manifest.resolved if entry.key == record.key), None)
    if item is None:
        raise ValueError(f"`session={record.key}` has no resolved item in `{paths.manifest}`.")

    return manifest, item


async def run_verification(paths: RunPaths, record: SessionRecord) -> None:
    """Commit and check the current candidate without delivering it.

    The operation reads persisted resolved configuration, owns the idle session,
    and records a durable attempt. No pull request or push is performed.

    Args:
        paths: Storage paths for the existing run.
        record: Terminal session whose candidate should be checked.

    Raises:
        OwnershipError: Another operation owns the run or session.
        ValueError: The manifest has no resolved configuration for the session.
        git.GitError: The candidate is missing, dirty, or changes during checks.
        pr.PRError: Committing the candidate fails.
        OSError: Manifest, evidence, or record persistence fails.
        asyncio.CancelledError: Verification is cancelled after child cleanup.

    """

    async with _operation(paths, record, "verify"):
        _, item = _resolved_item(paths, record)
        await finalize_item(paths, record, item, open_pr=False, verification_only=True)


async def run_finalization(paths: RunPaths, record: SessionRecord) -> None:
    """Commit, check, and deliver using the persisted run policy.

    The operation reads persisted resolved configuration, owns the idle session,
    and records a durable attempt. Delivery uses the manifest's pull-request and
    authentication policy and only pushes the checked candidate.

    Args:
        paths: Storage paths for the existing run.
        record: Terminal session whose candidate should be finalized.

    Raises:
        OwnershipError: Another operation owns the run or session.
        ValueError: The manifest has no resolved configuration for the session.
        git.GitError: The candidate is missing, dirty, or changes during checks.
        pr.PRError: Committing or pull-request delivery fails.
        OSError: Manifest, evidence, or record persistence fails.
        asyncio.CancelledError: Finalization is cancelled after child cleanup.

    """

    async with _operation(paths, record, "finalize"):
        manifest, item = _resolved_item(paths, record)
        await finalize_item(
            paths,
            record,
            item,
            open_pr=manifest.open_pr,
            strip_token=manifest.strip_github_token,
        )
