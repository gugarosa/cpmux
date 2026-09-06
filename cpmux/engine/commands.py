# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import os
import signal
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from cpmux.config import CommandSpec
from cpmux.engine.store import CommandResult
from cpmux.process import complete, inherited_fds

_FAILURE_TAIL_BYTES = 8192
_POLL_SECONDS = 0.1
_TERMINATION_GRACE_SECONDS = 0.5


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return

    deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
    while _process_group_exists(process.pid) and time.monotonic() < deadline:
        await asyncio.sleep(_POLL_SECONDS)

    if _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        kill_deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
        while _process_group_exists(process.pid) and time.monotonic() < kill_deadline:
            await asyncio.sleep(_POLL_SECONDS)

    await process.wait()


async def _wait_for_process(
    process: asyncio.subprocess.Process,
    timeout_seconds: float,
    stop_requested: Callable[[], bool] | None,
) -> Literal["passed", "failed", "timed_out", "cancelled"]:
    deadline = time.monotonic() + timeout_seconds
    while process.returncode is None:
        if stop_requested is not None and stop_requested():
            await _terminate_process_group(process)
            return "cancelled"

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            await _terminate_process_group(process)
            return "timed_out"

        try:
            await asyncio.wait_for(process.wait(), timeout=min(_POLL_SECONDS, remaining))
        except TimeoutError:
            continue

    return "passed" if process.returncode == 0 else "failed"


def _failure_with_tail(message: str, log_path: Path) -> str:
    with log_path.open("rb") as log:
        log.seek(0, os.SEEK_END)
        size = log.tell()
        log.seek(max(0, size - _FAILURE_TAIL_BYTES))
        tail = log.read(_FAILURE_TAIL_BYTES).decode(errors="replace").strip()
    return f"{message}\n{tail}" if tail else message


async def run_command(
    spec: CommandSpec,
    worktree: str | Path,
    log_path: str | Path,
    phase: Literal["setup", "check"],
    env: dict[str, str] | None = None,
    on_spawn: Callable[[int], None] | None = None,
    on_update: Callable[[CommandResult], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> CommandResult:
    """Run an approved POSIX shell command while streaming combined output to disk.

    The returned result is also passed, by identity, to update callbacks at start
    and completion. Startup failures are returned as failed results. Callback and
    filesystem errors propagate after cleanup. Commands must finish their work
    in the foreground rather than leave an unowned background process group.

    Args:
        spec: Validated command and timeout configuration.
        worktree: Existing working directory for the child process.
        log_path: Local file that receives combined standard output and error.
        phase: Setup or acceptance-check phase recorded in the result.
        env: Environment values merged over the parent environment.
        on_spawn: Callback receiving the owned process-group leader PID.
        on_update: Callback receiving the mutable command result.
        stop_requested: Callback polled while the command is running.

    Returns:
        The completed command result with monotonic duration and diagnostics.

    Raises:
        OSError: The log directory or file cannot be prepared or read.
        asyncio.CancelledError: The caller cancels execution after the child is cleaned up.

    """

    worktree_path = Path(worktree)
    output_path = Path(log_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    child_env = os.environ.copy()
    if env is not None:
        child_env.update(env)

    result = CommandResult(
        name=spec.name or phase,
        command=spec.command,
        phase=phase,
        log_path=str(output_path),
    )
    started = time.monotonic()
    process: asyncio.subprocess.Process | None = None
    startup_error: str | None = None

    with output_path.open("wb") as output:
        try:
            process = await asyncio.create_subprocess_exec(
                "/bin/sh",
                "-c",
                spec.command,
                cwd=worktree_path,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                pass_fds=inherited_fds(),
            )
        except OSError as exc:
            startup_error = f"`command` failed to start: {type(exc).__name__}: {exc}."
            output.write(f"{startup_error}\n".encode())
            output.flush()
        else:
            completed = False
            notified = False
            try:
                if on_spawn is not None:
                    on_spawn(process.pid)
                if on_update is not None:
                    on_update(result)
                    notified = True
                result.status = await _wait_for_process(process, spec.timeout_seconds, stop_requested)
                if result.status in {"passed", "failed"} and _process_group_exists(process.pid):
                    result.status = "failed"
                    result.error = "`command` exited with background children still running. Use a foreground command."
                    await _terminate_process_group(process)
                completed = True
            except asyncio.CancelledError:
                result.status = "cancelled"
                result.error = "`command` was cancelled by its owner."
                raise
            finally:
                try:
                    if not completed:
                        await complete(asyncio.create_task(_terminate_process_group(process)))
                finally:
                    if not completed and result.status == "running":
                        result.status = "failed"
                        result.error = "`command` execution or its callback did not complete."
                    result.exit_code = process.returncode
                    result.ended_at = _utc_now()
                    result.duration_seconds = time.monotonic() - started
                    if not completed and notified and on_update is not None:
                        on_update(result)

    result.ended_at = _utc_now()
    result.duration_seconds = time.monotonic() - started
    if startup_error is not None:
        result.status = "failed"
        result.error = startup_error
    elif result.status == "failed" and result.error is None:
        result.error = _failure_with_tail(f"`command` exited with status {result.exit_code}.", output_path)
    elif result.status == "timed_out":
        result.error = _failure_with_tail(
            f"`command` timed out after {spec.timeout_seconds:g} seconds.",
            output_path,
        )
    elif result.status == "cancelled":
        result.error = _failure_with_tail("`command` was cancelled.", output_path)

    if on_update is not None:
        on_update(result)
    return result
