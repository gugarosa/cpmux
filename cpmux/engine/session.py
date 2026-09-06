# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import math
import os
import signal
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from cpmux.events import SessionState, Status, apply_event, parse_line
from cpmux.process import cancel, complete, inherited_fds

OnUpdate = Callable[[str, SessionState, dict[str, Any]], None]
OnSpawn = Callable[[int], None]

_STREAM_LIMIT = 1 << 20


async def _drain(stream: asyncio.StreamReader) -> str:
    # Only the diagnostic tail is needed, not an unbounded copy of the output
    tail = b""
    while chunk := await stream.read(_STREAM_LIMIT):
        tail = (tail + chunk)[-4096:]

    return tail.decode("utf-8", "replace")


class SessionRunner:
    """Run a `copilot` subprocess and track JSONL events."""

    def __init__(
        self,
        key: str,
        argv: list[str],
        transcript_path: str | Path,
        env: dict[str, str] | None = None,
    ) -> None:
        """Initialize a session runner.

        Args:
            key: Session key.
            argv: Subprocess arguments.
            transcript_path: Transcript file path.
            env: Environment overrides.

        """

        self.key = key
        self.argv = argv
        self.transcript_path = Path(transcript_path)
        self.env = env

        self.state = SessionState()
        self.proc: asyncio.subprocess.Process | None = None
        self._stderr = ""

    async def run(
        self,
        on_update: OnUpdate | None = None,
        on_spawn: OnSpawn | None = None,
        *,
        timeout_seconds: float | None = None,
        stop_requested: Callable[[], bool] | None = None,
    ) -> SessionState:
        """Append subprocess output to the transcript and update the live state.

        Own the spawned process group until it exits or is reaped on cancellation
        or failure. Startup and process failures are returned as failed states.
        Callback and filesystem errors propagate after cleanup.

        Args:
            on_update: Synchronous callback after each decoded event updates the state.
            on_spawn: Synchronous callback receiving the child PID before output is read.
            timeout_seconds: Optional wall-time limit for this child execution.
            stop_requested: Optional owner-controlled cancellation predicate.

        Returns:
            The mutated state after subprocess exit, including its diagnostic error.

        Raises:
            OSError: Transcript creation or writing fails.
            asyncio.CancelledError: Execution is cancelled after subprocess cleanup.

        """

        if timeout_seconds is None and stop_requested is None:
            return await self._stream(on_update, on_spawn)
        if timeout_seconds is not None and (not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError("`timeout_seconds` must be positive.")
        if stop_requested is not None and stop_requested():
            self.state.status = Status.KILLED
            self.state.error = f"`session={self.key}` was stopped before execution."
            return self.state

        deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
        task = asyncio.create_task(self._stream(on_update, on_spawn))
        stopped: Status | None = None
        try:
            while not task.done():
                if stop_requested is not None and stop_requested():
                    stopped = Status.KILLED
                    break
                if deadline is not None and time.monotonic() >= deadline:
                    stopped = Status.TIMED_OUT
                    break
                await asyncio.wait({task}, timeout=0.1)
            if stopped is None:
                return await task
        finally:
            await cancel(task)

        self.state.status = stopped
        self.state.error = (
            f"`session={self.key}` exceeded its execution timeout."
            if stopped == Status.TIMED_OUT
            else f"`session={self.key}` was stopped by its owner."
        )
        return self.state

    async def _stream(self, on_update: OnUpdate | None, on_spawn: OnSpawn | None) -> SessionState:
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env={**os.environ, **self.env} if self.env else None,
                limit=_STREAM_LIMIT,
                pass_fds=inherited_fds(),
            )
        except (OSError, ValueError) as exc:
            self.state.status = Status.FAILED
            self.state.error = f"`{self.argv[0]}` could not start: {str(exc).removesuffix('.')}."
            return self.state

        stdout, stderr = self.proc.stdout, self.proc.stderr
        if stdout is None or stderr is None:
            raise RuntimeError("`copilot` output pipes were not created.")

        self.state.status = Status.STARTING
        stderr_task = asyncio.create_task(_drain(stderr))
        completed = False
        try:
            if on_spawn is not None:
                on_spawn(self.proc.pid)

            with self.transcript_path.open("a", encoding="utf-8") as transcript_file:
                chunks: list[bytes] = []
                while True:
                    try:
                        raw = await stdout.readuntil()
                    except asyncio.LimitOverrunError as exc:
                        chunks.append(await stdout.readexactly(exc.consumed))
                        continue
                    except asyncio.IncompleteReadError as exc:
                        raw = exc.partial
                        if not raw and not chunks:
                            break

                    if chunks:
                        raw = b"".join([*chunks, raw])
                        chunks.clear()

                    line = raw.decode("utf-8", "replace")
                    transcript_file.write(line)
                    transcript_file.flush()

                    event = parse_line(line)
                    if event is None:
                        continue

                    apply_event(self.state, event)
                    if on_update is not None:
                        on_update(self.key, self.state, event)

            return_code = await self.proc.wait()
            self._stderr = await asyncio.shield(stderr_task)
            completed = True
        except asyncio.CancelledError:
            self.state.status = Status.KILLED
            raise
        finally:
            if not completed:
                try:
                    await complete(asyncio.create_task(self._reap(stdout, stderr_task)))
                finally:
                    self.state.exit_code = self.proc.returncode

        if return_code != 0 or self.state.exit_code is None:
            self.state.exit_code = return_code
        self.state.status = (
            Status.FAILED if self.state.exit_code != 0 or self.state.status == Status.FAILED else Status.DONE
        )
        if self.state.status == Status.FAILED and not self.state.error:
            self.state.error = self._stderr.strip()[-500:] or f"exit code {self.state.exit_code}."

        return self.state

    async def _reap(self, stdout: asyncio.StreamReader, stderr_task: asyncio.Task[str]) -> None:
        process = self.proc
        if process is None:
            raise RuntimeError("`session` has no process to clean up.")
        self._signal(signal.SIGTERM)
        drained = asyncio.gather(_drain(stdout), stderr_task, process.wait())
        try:
            await asyncio.wait_for(asyncio.shield(drained), 3.0)
        except TimeoutError:
            self._signal(signal.SIGKILL)
            await drained

    def _signal(self, signum: int) -> None:
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signum)
            except ProcessLookupError:
                pass

    def terminate(self) -> None:
        """Request SIGTERM without waiting for a running session process group.

        The active run call remains responsible for draining output and reaping
        the child. Calling this before spawn or after exit has no effect.

        Raises:
            PermissionError: The process group cannot be signalled.

        """

        if self.proc is not None and self.proc.returncode is None:
            self._signal(signal.SIGTERM)
