# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import os
import signal
from collections.abc import Callable
from pathlib import Path

from cpmux.events import SessionState, Status, apply_event, parse_line

OnUpdate = Callable[[str, SessionState, dict], None]
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

    async def run(self, on_update: OnUpdate | None = None, on_spawn: OnSpawn | None = None) -> SessionState:
        """Stream session events to the transcript.

        Args:
            on_update: Applied-event callback.
            on_spawn: Subprocess-start callback.

        Returns:
            Terminal session state.

        """

        self.transcript_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env={**os.environ, **self.env} if self.env else None,
                limit=_STREAM_LIMIT,
            )
        except (OSError, ValueError) as exc:
            self.state.status = Status.FAILED
            self.state.error = f"`{self.argv[0]}` could not start: {exc}."
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
                self._signal(signal.SIGTERM)
                cleanup = asyncio.gather(_drain(stdout), stderr_task, self.proc.wait())
                try:
                    await asyncio.wait_for(asyncio.shield(cleanup), 3.0)
                except TimeoutError:
                    self._signal(signal.SIGKILL)
                    await cleanup
                self.state.exit_code = self.proc.returncode

        if return_code != 0 or self.state.exit_code is None:
            self.state.exit_code = return_code
        self.state.status = (
            Status.FAILED if self.state.exit_code != 0 or self.state.status == Status.FAILED else Status.DONE
        )
        if self.state.status == Status.FAILED and not self.state.error:
            self.state.error = self._stderr.strip()[-500:] or f"exit code {self.state.exit_code}."

        return self.state

    def _signal(self, signum: int) -> None:
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signum)
            except ProcessLookupError:
                pass

    def terminate(self) -> None:
        """Send SIGTERM to a running session process group."""

        if self.proc is not None and self.proc.returncode is None:
            self._signal(signal.SIGTERM)
