# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

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
    """Check whether a process id is running.

    Args:
        pid: Process id to probe, or None.

    Returns:
        `True` when a process with that id exists.

    """
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
    """Record the pid that owns the run.

    Args:
        paths: Run paths locating the owner file.
        pid: Owner process id to record.

    """
    paths.owner_file.write_text(json.dumps({"pid": pid}))


def read_owner(paths: RunPaths) -> int | None:
    """Read the run's recorded owner pid.

    Args:
        paths: Run paths locating the owner file.

    Returns:
        The recorded owner pid, or `None` when absent or unreadable.

    """
    if not paths.owner_file.exists():
        return None

    try:
        return int(json.loads(paths.owner_file.read_text()).get("pid"))
    except (ValueError, TypeError, OSError):
        return None


def clear_owner(paths: RunPaths) -> None:
    """Delete the owner file, marking the run as no longer managed.

    Args:
        paths: Run paths locating the owner file.

    """
    paths.owner_file.unlink(missing_ok=True)


def owner_alive(paths: RunPaths) -> bool:
    """Check whether the run's owner process is still alive.

    Args:
        paths: Run paths locating the owner file.

    Returns:
        `True` when a recorded owner pid is running.

    """
    return pid_alive(read_owner(paths))


def launch_detached(run_id: str, repo_root: str) -> int:
    """Start the run's supervisor as a detached background daemon.

    Args:
        run_id: Identifier of the run to supervise.
        repo_root: Repository root the daemon runs from.

    Returns:
        The pid of the spawned daemon.

    """
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
    """Flip non-terminal sessions to failed when the run's owner has exited.

    Args:
        paths: Run paths for the run being reconciled.
        records: Session records to inspect and update.

    Returns:
        The same records, with abandoned sessions marked failed.

    """
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
    """Terminate the run's owner and any live sessions.

    Args:
        paths: Run paths for the run to stop.
        records: Session records whose processes may be running.

    Returns:
        The number of processes signalled.

    """
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
    """Terminate a single session.

    Args:
        paths: Run paths used to persist the updated record.
        record: Session record identifying the process to kill.

    Returns:
        `True` when the session was still running.

    """
    alive = pid_alive(record.pid)

    if alive:
        _terminate(record.pid)
    if record.status not in TERMINAL:
        record.status = Status.KILLED
        record.mark_ended()
        paths.write_record(record)

    return alive
