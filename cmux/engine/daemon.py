# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import json
import os
import signal
import subprocess
import sys
import time

from cmux.engine.store import RunPaths, SessionRecord
from cmux.events import TERMINAL, Status
from cmux.logging import get_logger

logger = get_logger(__name__)


def pid_alive(pid: int | None) -> bool:
    """Check whether a process id exists."""

    if not pid:
        return False

    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False

    return True


def _terminate(pid: int | None, grace: float = 3.0) -> None:
    if not pid:
        return

    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        return

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def write_owner(paths: RunPaths, pid: int) -> None:
    """Record the run owner pid."""

    paths.owner_file.write_text(json.dumps({"pid": pid}))


def read_owner(paths: RunPaths) -> int | None:
    """Read the owner pid, or `None` when unavailable."""

    if not paths.owner_file.exists():
        return None

    try:
        return int(json.loads(paths.owner_file.read_text()).get("pid"))
    except (ValueError, TypeError, OSError):
        return None


def clear_owner(paths: RunPaths) -> None:
    """Delete the owner file."""

    paths.owner_file.unlink(missing_ok=True)


def owner_alive(paths: RunPaths) -> bool:
    """Check whether the owner process is alive."""

    return pid_alive(read_owner(paths))


def launch_detached(run_id: str, repo_root: str) -> int:
    """Launch the supervisor daemon and return its pid."""

    paths = RunPaths(repo_root, run_id)

    with (paths.run_dir / "daemon.log").open("ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "cmux", "_daemon", run_id],
            cwd=repo_root,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )

    write_owner(paths, proc.pid)

    return proc.pid


def reconcile(paths: RunPaths, records: list[SessionRecord], persist: bool = True) -> list[SessionRecord]:
    """Mark orphaned non-terminal sessions failed."""

    if owner_alive(paths):
        return records

    for record in records:
        if record.status not in TERMINAL:
            record.status = Status.FAILED
            record.error = record.error or "run owner exited."
            record.mark_ended()
            if persist:
                paths.write_record(record)

    return records


def stop(paths: RunPaths, records: list[SessionRecord]) -> int:
    """Terminate the owner and live sessions."""

    signalled = 0

    owner = read_owner(paths)
    if pid_alive(owner):
        _terminate(owner)
        signalled += 1

    for record in records:
        if pid_alive(record.pid):
            _terminate(record.pid)
            signalled += 1

        if record.status not in TERMINAL:
            record.status = Status.KILLED
            record.mark_ended()
            paths.write_record(record)

    clear_owner(paths)

    return signalled


def kill_session(paths: RunPaths, record: SessionRecord) -> bool:
    """Terminate a session and report whether it was running."""

    alive = pid_alive(record.pid)

    if alive:
        _terminate(record.pid)

    if record.status not in TERMINAL:
        record.status = Status.KILLED
        record.mark_ended()
        paths.write_record(record)

    return alive
