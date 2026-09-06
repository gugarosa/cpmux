# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from cpmux.engine.ownership import (
    BusyError,
    OwnershipError,
    process_created_at,
    session_owner,
)
from cpmux.engine.session import SessionRunner
from cpmux.engine.store import RunPaths, SessionRecord
from cpmux.events import TERMINAL, SessionState, Status
from cpmux.process import cancel, complete, inherited_fds, run_sync
from cpmux.vcs import git, pr


def resume_interactive_argv(session_id: str, worktree: str | Path) -> list[str]:
    """Build arguments for an interactive resume.

    Args:
        session_id: Copilot session identifier.
        worktree: Session worktree.

    Returns:
        Copilot command arguments.

    """

    return ["copilot", f"--resume={session_id}", "-C", str(worktree)]


def followup_argv(
    session_id: str,
    worktree: str | Path,
    model: str,
    permission_flags: list[str],
    message: str,
) -> list[str]:
    """Build arguments for a headless follow-up turn.

    Args:
        session_id: Copilot session identifier.
        worktree: Session worktree.
        model: Copilot model.
        permission_flags: Permission arguments.
        message: Follow-up prompt.

    Returns:
        Copilot command arguments.

    """

    argv = [
        "copilot",
        "-C",
        str(worktree),
        "-p",
        message,
        f"--resume={session_id}",
        "--model",
        model,
        "--output-format",
        "json",
        *permission_flags,
    ]
    if "--no-ask-user" not in argv:
        argv.append("--no-ask-user")

    return argv


@asynccontextmanager
async def _operation(
    paths: RunPaths,
    record: SessionRecord,
    mode: Literal["followup", "verify", "finalize", "interactive"],
    before_begin: Callable[[], None] | None = None,
) -> AsyncIterator[None]:
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("`operation` requires an asyncio task.")
    watcher: asyncio.Task[None] | None = None
    requested = False

    async def watch_stop() -> None:
        nonlocal requested
        while True:
            if paths.stop_file().exists() or paths.stop_file(record.key).exists():
                if not task.cancelling():
                    requested = True
                    task.cancel()
                return
            await asyncio.sleep(0.1)

    with session_owner(paths, record.key, mode):
        paths.refresh_record(record)
        if record.status not in TERMINAL:
            raise BusyError(f"`session={record.key}` has no terminal outcome. Reconcile or stop the run first.")
        if before_begin is not None:
            await run_sync(before_begin)
        paths.stop_file(record.key).unlink(missing_ok=True)
        record.begin_attempt(mode)
        if mode in {"followup", "interactive"}:
            record.phase = "agent"
            record.status = Status.RUNNING
            record.agent_complete = False
            record.verification = None
        else:
            record.phase = "verification"
            record.status = Status.FINALIZING

        try:
            paths.write_record(record)
            if mode in {"verify", "finalize"}:
                watcher = asyncio.create_task(watch_stop())
            yield
        except (asyncio.CancelledError, KeyboardInterrupt) as exc:
            record.status = Status.FAILED if record.phase == "delivery" else Status.KILLED
            record.error = f"`session={record.key}` {mode} was cancelled."
            if record.phase == "delivery":
                record.error += " Inspect the worktree and remote for partial delivery."
            # Only consume the cancellation requested by this operation's stop watcher
            if isinstance(exc, asyncio.CancelledError) and requested and task.cancelling() == 1:
                task.uncancel()
            else:
                raise
        except (OSError, ValueError, OwnershipError, git.GitError, pr.PRError) as exc:
            record.status = Status.FAILED
            record.error = str(exc)
            raise
        finally:
            try:
                if watcher is not None:
                    await cancel(watcher)
            finally:
                if record.status not in TERMINAL:
                    record.status = Status.FAILED
                    record.error = record.error or f"`session={record.key}` {mode} ended unexpectedly."
                record.finish_attempt()
                paths.write_record(record)


async def _run_followup(
    paths: RunPaths,
    record: SessionRecord,
    message: str,
    before_start: Callable[[], None] | None = None,
) -> SessionState:
    if not message.strip():
        raise ValueError("`message` must not be blank.")
    async with _operation(paths, record, "followup", before_start):
        last_write = time.monotonic()

        def spawned(pid: int) -> None:
            record.pid = pid
            record.pid_created_at = process_created_at(pid)
            record.pid_is_group = True
            record.attempts[-1].agent_started = True
            paths.write_record(record)

        def updated(key: str, state: SessionState, event: dict[str, Any]) -> None:
            nonlocal last_write
            status = state.status if state.status not in TERMINAL else Status.RUNNING
            usage_changed = (
                state.premium_requests is not None and state.premium_requests != record.attempts[-1].premium_requests
            )
            changed = status != record.status or not record.native_started or usage_changed
            record.status = status
            record.native_started = True
            if state.premium_requests is not None:
                record.record_usage(state.premium_requests)
            record.last_activity_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if changed or time.monotonic() - last_write >= 1.0:
                paths.write_record(record)
                last_write = time.monotonic()

        argv = followup_argv(record.session_id, record.worktree, record.model, record.permission_flags, message)
        state = await SessionRunner(record.key, argv, paths.transcript(record.key), env=record.env).run(
            updated,
            spawned,
            timeout_seconds=record.timeout_seconds,
            stop_requested=lambda: paths.stop_file().exists() or paths.stop_file(record.key).exists(),
        )
        record.status = state.status
        record.exit_code = state.exit_code
        record.error = state.error
        record.files_modified = state.files_modified or record.files_modified
        if state.premium_requests is not None:
            record.record_usage(state.premium_requests)
        record.agent_complete = state.status == Status.DONE
        record.phase = "complete"
        return state


async def run_followup(paths: RunPaths, record: SessionRecord, message: str) -> SessionState:
    """Run a follow-up turn, update its record in place, and persist the outcome.

    Reported premium usage is accumulated. An absent modified-file list leaves
    the previous list intact. A session lease rejects competing mutations and a
    running supervisor. No automatic Git finalization is performed. Every attempt,
    including cancellation, is recorded, and prior verification is invalidated.

    Args:
        paths: Storage paths for the existing run.
        record: Session to resume and update.
        message: Follow-up prompt.

    Returns:
        Terminal session state, including execution failures recorded in its error field.

    Raises:
        ValueError: The follow-up message is blank.
        OSError: Transcript or record persistence fails.
        OwnershipError: Another operation owns the run or session.
        asyncio.CancelledError: The turn is cancelled after subprocess cleanup.

    """

    return await _run_followup(paths, record, message)


async def run_interactive(paths: RunPaths, record: SessionRecord) -> int:
    """Resume interactively while retaining ownership in the cpmux parent.

    Native terminal output is not copied into the JSONL transcript. Exiting or
    cancelling the operation releases ownership only after the child is reaped.
    The parent event loop remains available to independent session operations.

    Args:
        paths: Existing run storage paths.
        record: Session to resume and update.

    Returns:
        Native Copilot process exit code.

    Raises:
        OwnershipError: A conflicting run or session operation is active.
        OSError: Process startup or state persistence fails.
        KeyboardInterrupt: The interactive operation is interrupted after cleanup.
        asyncio.CancelledError: The operation is cancelled after child cleanup.

    """

    async with _operation(paths, record, "interactive"):
        proc = await asyncio.create_subprocess_exec(
            *resume_interactive_argv(record.session_id, record.worktree),
            env={**os.environ, **record.env},
            pass_fds=inherited_fds(),
        )
        stopped = False
        try:
            record.pid = proc.pid
            record.pid_created_at = process_created_at(proc.pid)
            record.pid_is_group = False
            record.attempts[-1].agent_started = True
            paths.write_record(record)
            while proc.returncode is None:
                if paths.stop_file().exists() or paths.stop_file(record.key).exists():
                    stopped = True
                    break
                await asyncio.sleep(0.1)
        finally:
            await complete(asyncio.create_task(_reap_interactive(proc)))
        code = await proc.wait()
        record.exit_code = code
        record.status = Status.KILLED if stopped else Status.DONE if code == 0 else Status.FAILED
        record.error = None if code == 0 and not stopped else f"`session={record.key}` interactive exit was {code}."
        record.agent_complete = record.status == Status.DONE
        record.native_started |= record.agent_complete
        record.phase = "complete"
        return code


async def _reap_interactive(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except TimeoutError:
            process.kill()
    await process.wait()
