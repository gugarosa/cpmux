# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import fcntl
import os
import signal
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import psutil
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cpmux.engine.store import RunPaths, write_model
from cpmux.process import inherit_lease


class OwnershipError(Exception):
    """An operation cannot safely acquire or identify its process owner."""


class BusyError(OwnershipError):
    """Another operation owns the requested run or session."""


class ProcessOwner(BaseModel):
    """Identity of a process holding an orchestration lease.

    Attributes:
        pid: Process identifier.
        process_created_at: Operating-system process creation time.
        operation: Human-readable operation holding the lease.
        token: Unique lease identity used when releasing metadata.

    """

    model_config = ConfigDict(extra="forbid")

    pid: int = Field(gt=0, strict=True)
    process_created_at: float = Field(gt=0, allow_inf_nan=False)
    operation: str
    token: str = Field(min_length=1)


def process_created_at(pid: int) -> float | None:
    """Read a process identity without mistaking a reused PID for an owner.

    Args:
        pid: Process identifier.

    Returns:
        Creation time, or None if the process has already exited.

    Raises:
        OwnershipError: Process identity cannot be inspected.

    """

    if pid <= 0:
        raise OwnershipError(f"`pid={pid}` is not a positive process identifier.")
    try:
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as exc:
        raise OwnershipError(f"`pid={pid}` identity cannot be inspected.") from exc


def matching_process(pid: int | None, created_at: float | None) -> psutil.Process | None:
    """Find a live process only when its recorded identity still matches.

    Args:
        pid: Recorded process identifier.
        created_at: Recorded process creation time.

    Returns:
        Matching live process, or None when it exited or the PID was reused.

    Raises:
        OwnershipError: A live PID has no verifiable identity.

    """

    if pid is None:
        return None
    if pid <= 0:
        raise OwnershipError(f"`pid={pid}` is not a positive process identifier.")
    if created_at is None:
        if psutil.pid_exists(pid):
            raise OwnershipError(f"`pid={pid}` has no recorded creation time. Refusing to signal an unknown process.")
        return None

    try:
        process = psutil.Process(pid)
        if process.create_time() != created_at or not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
            return None
        return process
    except psutil.NoSuchProcess:
        return None
    except psutil.AccessDenied as exc:
        raise OwnershipError(f"`pid={pid}` identity cannot be inspected.") from exc


def read_process_owner(path: Path) -> ProcessOwner | None:
    """Read owner metadata without treating corrupt state as an unlocked run.

    Args:
        path: Owner metadata path.

    Returns:
        Owner metadata, or None when the file is absent.

    Raises:
        OwnershipError: Existing owner metadata cannot be read or validated.

    """

    try:
        return ProcessOwner.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValidationError) as exc:
        raise OwnershipError(f"`{path}` owner metadata is unreadable or invalid.") from exc


def write_process_owner(path: Path, pid: int, operation: str = "run") -> ProcessOwner:
    """Persist a process identity for the current orchestration operation.

    Args:
        path: Owner metadata path.
        pid: Owner process identifier.
        operation: Operation name for diagnostics.

    Returns:
        Persisted owner record.

    Raises:
        OwnershipError: A live process identity cannot be established.
        OSError: Owner metadata cannot be persisted.

    """

    created_at = process_created_at(pid)
    if created_at is None:
        raise OwnershipError(f"`pid={pid}` exited before its identity could be recorded.")
    owner = ProcessOwner(pid=pid, process_created_at=created_at, operation=operation, token=uuid4().hex)
    write_model(path, owner)
    return owner


def process_owner_alive(path: Path) -> bool:
    """Check a recorded owner using both PID and process creation time.

    Args:
        path: Owner metadata path.

    Returns:
        Whether the recorded process is still the same live owner.

    Raises:
        OwnershipError: The owner cannot be safely identified.

    """

    owner = read_process_owner(path)
    return owner is not None and matching_process(owner.pid, owner.process_created_at) is not None


def group_processes(group_id: int) -> list[psutil.Process]:
    """Inspect live group members without interpreting signal permission as liveness.

    Args:
        group_id: Positive process-group identifier to inspect.

    Returns:
        Live non-zombie members with operating-system process identities.

    Raises:
        OwnershipError: A matching process cannot be inspected.

    """

    if group_id <= 0:
        raise OwnershipError("`group_id` must be positive.")
    members = []
    for pid in psutil.pids():
        if pid <= 0:
            continue
        try:
            if os.getpgid(pid) != group_id:
                continue
            process = psutil.Process(pid)
            if process.is_running() and process.status() != psutil.STATUS_ZOMBIE:
                members.append(process)
        except (ProcessLookupError, psutil.NoSuchProcess):
            continue
        except (PermissionError, psutil.AccessDenied) as exc:
            raise OwnershipError(f"`pid={pid}` process group cannot be inspected.") from exc
    return members


def _live_identities(identities: list[tuple[int, float]]) -> list[psutil.Process]:
    live = []
    for member_pid, member_created_at in identities:
        process = matching_process(member_pid, member_created_at)
        if process is None:
            continue
        live.append(process)
    return live


def _signal_processes(processes: list[psutil.Process], signum: signal.Signals) -> None:
    for process in processes:
        try:
            process.send_signal(signum)
        except psutil.NoSuchProcess:
            continue
        except (PermissionError, psutil.AccessDenied) as exc:
            raise OwnershipError(f"`pid={process.pid}` cannot be signalled.") from exc


@contextmanager
def file_lease(path: Path, shared: bool = False, wait_seconds: float = 0.0) -> Iterator[None]:
    """Hold an operating-system lease without deleting its lock-file inode.

    Args:
        path: Stable lock-file path.
        shared: Allow other shared holders, excluding run-wide writers.
        wait_seconds: Bounded acquisition wait for detached-owner startup.

    Raises:
        BusyError: Another operation still owns the lease.
        OSError: The lock file cannot be accessed.

    """

    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_seconds
    with path.open("a+b") as lock:
        operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
        while True:
            try:
                fcntl.flock(lock, operation | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    raise BusyError(f"`{path}` is owned by another operation. Wait for it to finish.") from exc
                time.sleep(0.05)

        # Closing our descriptor keeps the lock alive if a surviving child inherited it
        with inherit_lease(lock.fileno()):
            yield


@contextmanager
def run_owner(paths: RunPaths, operation: str = "run", wait_seconds: float = 0.0) -> Iterator[ProcessOwner]:
    """Own run-wide mutations while excluding competing session operations.

    Args:
        paths: Run storage paths.
        operation: Operation name for diagnostics.
        wait_seconds: Bounded acquisition wait during daemon startup.

    Raises:
        OwnershipError: Ownership cannot be acquired or safely identified.

    """

    with file_lease(paths.owner_lock, wait_seconds=wait_seconds):
        previous = read_process_owner(paths.owner_file)
        if previous is not None and previous.pid != os.getpid() and process_owner_alive(paths.owner_file):
            raise BusyError(f"`run={paths.run_id}` is still owned by `{previous.operation}`.")
        owner = write_process_owner(paths.owner_file, os.getpid(), operation)
        try:
            yield owner
        finally:
            current = read_process_owner(paths.owner_file)
            if current is not None and current.token == owner.token:
                paths.owner_file.unlink(missing_ok=True)


@contextmanager
def session_owner(paths: RunPaths, key: str, operation: str) -> Iterator[ProcessOwner]:
    """Own one session mutation while allowing independent idle-run sessions.

    Args:
        paths: Run storage paths.
        key: Session identifier.
        operation: Operation name for diagnostics.

    Raises:
        OwnershipError: A run or session operation already owns the work.

    """

    with file_lease(paths.owner_lock, shared=True):
        if paths.stop_file().exists():
            raise BusyError(f"`run={paths.run_id}` is stopping. Wait for cancellation to finish.")
        if process_owner_alive(paths.owner_file):
            raise BusyError(f"`run={paths.run_id}` is active. Wait for the run before starting `{operation}`.")
        with file_lease(paths.session_lock(key)):
            owner_path = paths.session_owner(key)
            if process_owner_alive(owner_path):
                raise BusyError(f"`session={key}` is already active.")
            owner = write_process_owner(owner_path, os.getpid(), operation)
            try:
                yield owner
            finally:
                current = read_process_owner(owner_path)
                if current is not None and current.token == owner.token:
                    owner_path.unlink(missing_ok=True)


def terminate_process(pid: int | None, created_at: float | None, group: bool = True, grace: float = 3.0) -> bool:
    """Stop only a process whose recorded identity still matches.

    Args:
        pid: Recorded process identifier.
        created_at: Recorded operating-system creation time.
        group: Signal an owned session group instead of just its leader.
        grace: Wait before escalating from termination to killing.

    Returns:
        Whether a matching live process was targeted.

    Raises:
        OwnershipError: The process cannot be identified or safely signalled.

    """

    process = matching_process(pid, created_at)
    if process is None:
        return False
    if process.pid == os.getpid():
        raise OwnershipError("`process` is the current controller. Cancel its task instead of signalling it.")

    if group:
        try:
            if os.getpgid(process.pid) != process.pid:
                raise OwnershipError(f"`pid={process.pid}` does not lead an isolated process group.")
        except ProcessLookupError:
            return False
        except PermissionError as exc:
            raise OwnershipError(f"`pid={pid}` cannot be signalled.") from exc
        identities = [(member.pid, member.create_time()) for member in group_processes(process.pid)]
    else:
        identities = [(process.pid, process.create_time())]

    try:
        current = matching_process(pid, created_at)
        if current is None:
            _signal_processes(_live_identities(identities), signal.SIGTERM)
        elif group:
            if os.getpgid(current.pid) != current.pid:
                raise OwnershipError(f"`pid={pid}` no longer leads its recorded group.")
            os.killpg(process.pid, signal.SIGTERM)
        else:
            current.send_signal(signal.SIGTERM)
    except (ProcessLookupError, psutil.NoSuchProcess):
        return True
    except (PermissionError, psutil.AccessDenied) as exc:
        raise OwnershipError(f"`pid={pid}` cannot be signalled.") from exc

    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        if not _live_identities(identities):
            return True
        time.sleep(0.05)

    current = matching_process(pid, created_at)
    if group and current is not None:
        try:
            if os.getpgid(current.pid) == current.pid:
                os.killpg(current.pid, signal.SIGKILL)
            else:
                _signal_processes(_live_identities(identities), signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise OwnershipError(f"`pid={pid}` cannot be signalled.") from exc
    else:
        _signal_processes(_live_identities(identities), signal.SIGKILL)

    kill_deadline = time.monotonic() + min(max(grace, 0.1), 1.0)
    while time.monotonic() < kill_deadline and _live_identities(identities):
        time.sleep(0.05)
    if _live_identities(identities):
        raise OwnershipError(f"`pid={pid}` still has live owned processes after escalation. Inspect before retrying.")
    return True
