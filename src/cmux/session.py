# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

"""Spawn a single headless ``copilot`` session and reduce its JSONL stream.

Each session is a one-shot ``copilot -p ... --output-format json`` subprocess in
its own process group. Its stdout is tee'd verbatim to the run's
``transcript.jsonl`` and folded into a live :class:`~cmux.events.SessionState`.
"""

import asyncio
import os
import signal
from collections.abc import Callable
from pathlib import Path

from cmux.events import SessionState, Status, apply_event, parse_line

OnUpdate = Callable[[str, SessionState, dict], None]
OnSpawn = Callable[[int], None]

_STREAM_LIMIT = 1 << 20


class SessionRunner:
    """Owns one ``copilot`` subprocess: spawns it, streams JSONL, tracks state."""

    def __init__(
        self,
        key: str,
        argv: list[str],
        transcript_path: str | Path,
        env: dict[str, str] | None = None,
    ) -> None:
        self.key = key
        self.argv = argv
        self.transcript_path = Path(transcript_path)
        self.env = env
        self.state = SessionState()
        self.proc: asyncio.subprocess.Process | None = None
        self._stderr = ""

    async def run(self, on_update: OnUpdate | None = None, on_spawn: OnSpawn | None = None) -> SessionState:
        """Spawn the session, stream its events to disk and ``on_update``, and return the final state."""
        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env=self.env,
            limit=_STREAM_LIMIT,
        )
        self.state.status = Status.STARTING
        if on_spawn is not None:
            on_spawn(self.proc.pid)
        stderr_task = asyncio.create_task(self._drain_stderr())

        with self.transcript_path.open("a", encoding="utf-8") as tf:
            try:
                async for raw in self.proc.stdout:  # type: ignore[union-attr]
                    line = raw.decode("utf-8", "replace")
                    tf.write(line)
                    tf.flush()
                    ev = parse_line(line)
                    if ev is None:
                        continue
                    apply_event(self.state, ev)
                    if on_update is not None:
                        on_update(self.key, self.state, ev)
            except (ValueError, asyncio.LimitOverrunError):
                pass

        rc = await self.proc.wait()
        self._stderr = await stderr_task
        if self.state.exit_code is None:
            self.state.exit_code = rc
            self.state.status = Status.DONE if rc == 0 else Status.FAILED
        if self.state.status == Status.FAILED and not self.state.error:
            self.state.error = self._stderr.strip()[-500:] or f"exit code {self.state.exit_code}"

        return self.state

    async def _drain_stderr(self) -> str:
        data = await self.proc.stderr.read()
        return data.decode("utf-8", "replace")

    def terminate(self) -> None:
        """Send SIGTERM to the session's process group if it is still running."""
        if self.proc is not None and self.proc.returncode is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
