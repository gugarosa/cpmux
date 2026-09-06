# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import hashlib
import json
from collections.abc import Callable
from typing import Literal

from cpmux.config import CommandSpec, ResolvedItem
from cpmux.engine.commands import run_command
from cpmux.engine.ownership import process_created_at
from cpmux.engine.store import (
    CommandResult,
    RunPaths,
    SessionRecord,
    VerificationReceipt,
    write_model,
)
from cpmux.events import Status
from cpmux.process import run_sync
from cpmux.vcs import git, pr


def check_fingerprint(item: ResolvedItem, record: SessionRecord) -> str:
    """Identify configured checks and explicit environment overrides.

    Args:
        item: Resolved item configuration.
        record: Session carrying the actual environment overrides.

    Returns:
        Stable digest without exposing environment values in a report.

    """

    data = {"checks": [step.model_dump(mode="json") for step in item.checks], "env": record.env}
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def verified_candidate(item: ResolvedItem, record: SessionRecord, commit_sha: str, tree_sha: str) -> bool:
    """Match a receipt to the candidate, configuration, and completed command outcomes.

    Args:
        item: Resolved acceptance configuration.
        record: Session containing the receipt and its execution history.
        commit_sha: Current candidate commit.
        tree_sha: Tree belonging to the candidate.

    Returns:
        Whether every required command has a matching successful recorded outcome.

    """

    receipt = record.verification
    if (
        receipt is None
        or not item.checks
        or receipt.attempt > len(record.attempts)
        or receipt.commit_sha != commit_sha
        or receipt.tree_sha != tree_sha
        or receipt.check_fingerprint != check_fingerprint(item, record)
    ):
        return False
    attempt = record.attempts[receipt.attempt - 1]
    commands = [command for command in attempt.commands if command.phase == "check"]
    return (
        attempt.number == receipt.attempt
        and len(commands) == len(item.checks)
        and all(
            command.status == "passed"
            and command.exit_code == 0
            and command.ended_at is not None
            and command.command == spec.command
            for command, spec in zip(commands, item.checks)
        )
    )


async def run_steps(
    paths: RunPaths,
    record: SessionRecord,
    steps: list[CommandSpec],
    phase: Literal["setup", "check"],
    on_change: Callable[[], None] | None = None,
) -> bool:
    """Execute approved commands sequentially and persist their real outcomes.

    The caller must hold run-wide or session ownership.

    Args:
        paths: Run storage paths.
        record: Item whose current attempt receives the command outcomes.
        steps: Explicitly configured commands.
        phase: Setup or acceptance-check phase.
        on_change: Optional presentation refresh callback.

    Returns:
        Whether every configured command succeeded.

    Raises:
        OSError: Command logs or state cannot be persisted.
        asyncio.CancelledError: Execution is cancelled after child cleanup.

    """

    if not record.attempts:
        raise ValueError("`attempt` must be started before running configured commands.")
    attempt = record.attempts[-1]
    record.phase = "setup" if phase == "setup" else "verification"
    record.status = Status.SETTING_UP if phase == "setup" else Status.VERIFYING

    def spawned(pid: int) -> None:
        record.pid = pid
        record.pid_created_at = process_created_at(pid)
        record.pid_is_group = True
        paths.write_record(record)

    def updated(result: CommandResult) -> None:
        if not any(existing is result for existing in attempt.commands):
            attempt.commands.append(result)
        record.last_activity_at = result.ended_at or result.started_at
        if result.status != "running":
            record.pid = None
            record.pid_created_at = None
        paths.write_record(record)
        if on_change is not None:
            on_change()

    paths.write_record(record)
    if on_change is not None:
        on_change()

    for index, step in enumerate(steps, 1):
        result = await run_command(
            step,
            record.worktree,
            paths.attempt_dir(record.key, attempt.number) / f"{phase}-{index:03d}.log",
            phase,
            env=record.env,
            on_spawn=spawned,
            on_update=updated,
            stop_requested=lambda: paths.stop_file().exists() or paths.stop_file(record.key).exists(),
        )
        if result.status != "passed":
            record.status = (
                Status.TIMED_OUT
                if result.status == "timed_out"
                else Status.KILLED if result.status == "cancelled" else Status.FAILED
            )
            record.error = result.error or f"`{phase}={result.name}` did not succeed."
            record.exit_code = result.exit_code
            paths.write_record(record)
            return False

    return True


async def finalize_item(
    paths: RunPaths,
    record: SessionRecord,
    item: ResolvedItem,
    open_pr: bool,
    strip_token: bool = True,
    on_change: Callable[[], None] | None = None,
    verification_only: bool = False,
) -> None:
    """Commit, check, and deliver only an unchanged candidate revision.

    The caller must hold ownership and finish the attempt. Checks may create
    ignored build outputs, but any source edit or HEAD change invalidates them.
    A receipt records executed commands, not a guarantee that their assertions
    establish correctness.

    Args:
        paths: Run storage paths.
        record: Item record updated with candidate, evidence, and delivery.
        item: Resolved commands and delivery policy.
        open_pr: Whether to push and open or update a pull request.
        strip_token: Whether GitHub delivery removes ambient authentication tokens.
        on_change: Optional presentation refresh callback.
        verification_only: Re-execute checks and stop without delivering the candidate.

    Raises:
        git.GitError: Source revision or worktree state is unsafe to deliver.
        pr.PRError: Commit or GitHub delivery fails.
        OSError: Evidence or record persistence fails.
        asyncio.CancelledError: Finalization is interrupted and may have partial Git effects.

    """

    def changed() -> None:
        paths.write_record(record)
        if on_change is not None:
            on_change()

    title, body = pr.read_pr_draft(record.worktree)
    record.pr_title = title or record.pr_title or item.pr_title
    record.pr_body = body or record.pr_body or item.pr_body
    record.phase = "verification"
    record.status = Status.FINALIZING
    changed()

    branch = await run_sync(git.run_git, ["symbolic-ref", "--quiet", "--short", "HEAD"], record.worktree)
    if branch.stdout.strip() != record.branch:
        raise git.GitError(f"`{record.key}` worktree is no longer on branch `{record.branch}`.")

    await run_sync(
        pr.commit_all,
        record.worktree,
        f"{record.pr_title}\n\ncpmux item: {record.key}",
        pr.gh_env(strip_token),
    )
    candidate = await run_sync(git.head_commit, record.worktree)
    await run_sync(git.require_clean_revision, record.worktree, candidate)
    record.candidate_sha = candidate
    fingerprint = check_fingerprint(item, record)
    tree = await run_sync(git.commit_tree, record.worktree, candidate)
    if item.checks and (verification_only or not verified_candidate(item, record, candidate, tree)):
        record.verification = None
        if not await run_steps(paths, record, item.checks, "check", on_change):
            return
        await run_sync(git.require_clean_revision, record.worktree, candidate)
        receipt = VerificationReceipt(
            attempt=record.attempts[-1].number,
            commit_sha=candidate,
            tree_sha=tree,
            check_fingerprint=fingerprint,
        )
        record.verification = receipt
        write_model(paths.attempt_dir(record.key, receipt.attempt) / "verification.json", receipt)
        changed()
    elif not item.checks:
        record.verification = None

    await run_sync(git.require_clean_revision, record.worktree, candidate)
    if verification_only:
        record.status = Status.DONE
        record.phase = "complete"
        changed()
        return

    if not await run_sync(git.has_changes, record.worktree, record.base_sha):
        record.status = Status.NO_CHANGES
        record.phase = "complete"
        changed()
        return

    record.phase = "delivery"
    if open_pr:
        record.status = Status.OPENING_PR
        changed()
        record.pr_url = await run_sync(
            pr.publish_pull_request,
            record.worktree,
            item.remote,
            record.base,
            record.branch,
            record.pr_title,
            record.pr_body,
            item.labels,
            item.draft,
            candidate,
            strip_token,
            record.pr_url,
            len(item.checks) if record.verification is not None else None,
        )
        record.delivery_sha = candidate

    record.status = Status.DONE
    record.phase = "complete"
    changed()
