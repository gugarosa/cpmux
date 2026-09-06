# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import os
import signal
import subprocess
import sys
import time

from cpmux.engine.ownership import (
    BusyError,
    OwnershipError,
    file_lease,
    process_created_at,
    process_owner_alive,
    read_process_owner,
    terminate_process,
    write_process_owner,
)
from cpmux.engine.store import RunPaths, SessionRecord
from cpmux.events import TERMINAL, Status
from cpmux.logging import get_logger

logger = get_logger(__name__)


def owner_alive(paths: RunPaths) -> bool:
    """Check whether the recorded run owner is still the same process.

    Args:
        paths: Run paths.

    Returns:
        Whether the recorded owner is alive.

    Raises:
        OwnershipError: Existing ownership cannot be safely established.

    """

    return process_owner_alive(paths.owner_file)


def _clear_spawn_owner(paths: RunPaths, pid: int, created_at: float) -> None:
    owner = read_process_owner(paths.owner_file)
    if owner is not None and owner.pid == pid and owner.process_created_at == created_at:
        paths.owner_file.unlink(missing_ok=True)


def _stop_spawned_daemon(proc: subprocess.Popen[bytes], created_at: float | None) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=4)
        except subprocess.TimeoutExpired:
            if created_at is None:
                proc.kill()
            else:
                terminate_process(proc.pid, created_at, grace=1.0)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired as exc:
        proc.kill()
        proc.wait()
        raise OwnershipError(f"`pid={proc.pid}` daemon required direct killing during startup cleanup.") from exc


def launch_detached(run_id: str, repo_root: str) -> int:
    """Launch a daemon and wait for its explicit startup acknowledgement.

    Args:
        run_id: Prepared run identifier.
        repo_root: Repository root.

    Returns:
        Started daemon PID.

    Raises:
        OwnershipError: The daemon exits or does not acknowledge startup.
        OSError: The daemon log or subprocess cannot be created.

    """

    paths = RunPaths(repo_root, run_id)
    with file_lease(paths.owner_lock):
        if owner_alive(paths):
            raise BusyError(f"`run={run_id}` already has an active owner.")
        paths.ready_file.unlink(missing_ok=True)
        with (paths.run_dir / "daemon.log").open("ab") as log:
            proc = subprocess.Popen(
                [sys.executable, "-m", "cpmux", "_daemon", run_id],
                cwd=repo_root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )

        identity = None
        try:
            identity = process_created_at(proc.pid)
            if identity is None:
                proc.wait()
                raise OwnershipError(f"`run={run_id}` daemon exited before its identity could be recorded.")
            initial_owner = write_process_owner(paths.owner_file, proc.pid)
        except (OSError, OwnershipError):
            _stop_spawned_daemon(proc, identity)
            if identity is not None:
                _clear_spawn_owner(paths, proc.pid, identity)
            raise

    deadline = time.monotonic() + 15.0
    acknowledged = False
    try:
        while time.monotonic() < deadline:
            if paths.ready_file.exists():
                current = read_process_owner(paths.owner_file)
                if current is not None and (
                    current.pid != proc.pid
                    or current.process_created_at != identity
                    or current.token == initial_owner.token
                ):
                    raise OwnershipError(f"`run={run_id}` daemon did not complete its ownership handoff.")
                acknowledged = True
                return proc.pid
            if proc.poll() is not None:
                raise OwnershipError(
                    f"`run={run_id}` daemon exited before startup. Inspect `{paths.run_dir / 'daemon.log'}`."
                )
            time.sleep(0.05)
        raise OwnershipError(f"`run={run_id}` daemon did not acknowledge startup.")
    finally:
        if not acknowledged:
            _stop_spawned_daemon(proc, identity)
            _clear_spawn_owner(paths, proc.pid, identity)


def reconcile(paths: RunPaths, records: list[SessionRecord], persist: bool = True) -> list[SessionRecord]:
    """Reconcile abandoned records without racing a live run or session writer.

    Args:
        paths: Run paths.
        records: Records to refresh and update in place.
        persist: Whether to write reconciled records.

    Returns:
        The supplied records with safely identified orphaned work marked failed.

    Raises:
        OwnershipError: Run ownership cannot be safely established.
        OSError: Run state cannot be read or updated.

    """

    if owner_alive(paths):
        return records

    try:
        with file_lease(paths.owner_lock):
            if owner_alive(paths):
                return records
            abandoned_owner = paths.owner_file.exists()
            for record in records:
                paths.refresh_record(record)
                if record.status in TERMINAL:
                    continue
                if (
                    record.status == Status.PENDING
                    and (record.started_at is None or record.recovery_mode is not None)
                    and not abandoned_owner
                    and not paths.ready_file.exists()
                ):
                    continue
                try:
                    terminate_process(record.pid, record.pid_created_at, group=record.pid_is_group)
                    detail = "run owner exited."
                except OwnershipError as exc:
                    detail = f"run owner exited. {exc}"
                    logger.warning(
                        f"`{record.key}` orphan identity could not be verified: {str(exc).removesuffix('.')}."
                    )
                    record.error = detail
                    if persist:
                        paths.write_record(record)
                    continue
                record.status = Status.FAILED
                record.error = record.error or detail
                record.finish_attempt()
                if persist:
                    paths.write_record(record)
            paths.owner_file.unlink(missing_ok=True)
    except BusyError:
        return records

    return records


def stop(paths: RunPaths, records: list[SessionRecord]) -> int:
    """Request run cancellation and stop only identity-verified children.

    Args:
        paths: Run paths.
        records: Records to refresh after the owner stops.

    Returns:
        Number of live owner/child processes targeted.

    Raises:
        OwnershipError: A process cannot be identified or an operation remains busy.

    """

    paths.stop_file().touch()
    targeted: set[int] = set()
    owner = read_process_owner(paths.owner_file)
    if owner is not None and owner_alive(paths):
        if owner.pid == os.getpid():
            raise OwnershipError("`run` is owned by this controller. Cancel its supervisor task instead.")
        deadline = time.monotonic() + 4.0
        while owner_alive(paths) and time.monotonic() < deadline:
            time.sleep(0.05)
        if owner_alive(paths):
            for record in records:
                paths.refresh_record(record)
            if any(
                record.status not in TERMINAL and record.phase in {"verification", "delivery"} and record.pid is None
                for record in records
            ):
                raise BusyError(
                    f"`run={paths.run_id}` is finishing a Git operation. Cancellation is queued; wait before retrying."
                )
            terminate_process(owner.pid, owner.process_created_at, group=False)
        targeted.add(owner.pid)

    for record in records:
        paths.refresh_record(record)
        if record.status not in TERMINAL:
            paths.stop_file(record.key).touch()
            if record.pid is not None and process_owner_alive(paths.session_owner(record.key)):
                targeted.add(record.pid)

    deadline = time.monotonic() + 4.0
    while any(process_owner_alive(paths.session_owner(record.key)) for record in records):
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)

    for record in records:
        paths.refresh_record(record)
        if record.status not in TERMINAL:
            pid = record.pid
            if terminate_process(record.pid, record.pid_created_at, group=record.pid_is_group):
                if pid is not None:
                    targeted.add(pid)

    with file_lease(paths.owner_lock, wait_seconds=4.0):
        for record in records:
            paths.refresh_record(record)
            if record.status not in TERMINAL:
                record.status = Status.KILLED
                record.finish_attempt()
                paths.write_record(record)
        paths.owner_file.unlink(missing_ok=True)
        paths.stop_file().unlink(missing_ok=True)

    return len(targeted)


def kill_session(paths: RunPaths, record: SessionRecord) -> bool:
    """Request cancellation without overwriting a live owner's session record.

    Args:
        paths: Run paths.
        record: Session to stop.

    Returns:
        Whether an active or queued session was targeted.

    Raises:
        OwnershipError: The active process cannot be safely identified.

    """

    paths.refresh_record(record)
    if record.status in TERMINAL:
        return False

    paths.stop_file(record.key).touch()
    if owner_alive(paths) or process_owner_alive(paths.session_owner(record.key)):
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            paths.refresh_record(record)
            if record.status in TERMINAL or record.pid is None:
                return True
            time.sleep(0.05)
        terminate_process(record.pid, record.pid_created_at, group=record.pid_is_group)
        return True

    with file_lease(paths.owner_lock, shared=True), file_lease(paths.session_lock(record.key)):
        paths.refresh_record(record)
        if record.status in TERMINAL:
            return False
        terminate_process(record.pid, record.pid_created_at, group=record.pid_is_group)
        record.status = Status.KILLED
        record.finish_attempt()
        paths.write_record(record)
    return True


def set_paused(paths: RunPaths, paused: bool) -> None:
    """Change queue admission without interrupting already admitted work.

    This only writes a control marker. Unpausing never starts an idle run or
    overrides its premium budget.

    Args:
        paths: Existing run storage paths.
        paused: Whether to stop admitting queued items.

    Raises:
        OSError: The run does not exist or its control marker cannot be updated.

    """

    if not paths.manifest.is_file():
        raise FileNotFoundError(f"`run={paths.run_id}` has no manifest.")
    if paused:
        paths.pause_file.touch()
    else:
        paths.pause_file.unlink(missing_ok=True)
