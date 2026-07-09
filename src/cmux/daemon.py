# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

"""Detached-daemon lifecycle and crash reconciliation for cmux runs.

A run records its "owner" pid (the foreground ``up`` process, or the detached
daemon) in ``daemon.json``. While the owner is alive the run is managed; once it
exits cleanly the file is cleared. A stale owner (present but dead) marks a
crash, so :func:`reconcile` can flip abandoned sessions to a terminal state.
"""

import json
import os
import signal
import subprocess
import sys
import time

from cmux.events import TERMINAL, Status
from cmux.logging import get_logger
from cmux.state import RunPaths, SessionRecord

logger = get_logger(__name__)


def pid_alive(pid: int | None) -> bool:
    """Return whether ``pid`` names a live process."""
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
    """Record the pid that currently owns (manages) the run."""
    paths.daemon_file.write_text(json.dumps({"pid": pid}))


def read_owner(paths: RunPaths) -> int | None:
    """Return the run's recorded owner pid, or ``None`` if there is none."""
    if not paths.daemon_file.exists():
        return None

    try:
        return int(json.loads(paths.daemon_file.read_text()).get("pid"))
    except (ValueError, TypeError, OSError):
        return None


def clear_owner(paths: RunPaths) -> None:
    """Remove the owner pid file, marking the run as no longer managed."""
    paths.daemon_file.unlink(missing_ok=True)


def owner_alive(paths: RunPaths) -> bool:
    """Return whether the run's owner process is still alive."""
    return pid_alive(read_owner(paths))


def launch_detached(run_id: str, repo_root: str) -> int:
    """Start the run's supervisor as a detached background daemon and return its pid."""
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


def reconcile(paths: RunPaths, records: list[SessionRecord]) -> list[SessionRecord]:
    """Flip non-terminal sessions to failed when the run's owner has exited."""
    if owner_alive(paths):
        return records

    for record in records:
        if record.status not in TERMINAL:
            record.status = Status.FAILED
            record.error = record.error or "session interrupted, the run owner exited."
            record.mark_ended()
            paths.write_record(record)

    return records


def stop(paths: RunPaths, records: list[SessionRecord]) -> int:
    """Terminate the run's owner and any live sessions, returning how many were signalled."""
    signalled = 0
    if pid_alive(read_owner(paths)):
        _terminate(read_owner(paths))
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
    """Terminate a single session, returning whether it was still running."""
    alive = pid_alive(record.pid)
    if alive:
        _terminate(record.pid)
    if record.status not in TERMINAL:
        record.status = Status.KILLED
        record.mark_ended()
        paths.write_record(record)

    return alive
