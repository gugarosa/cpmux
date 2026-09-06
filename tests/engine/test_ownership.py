# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import os
import signal
import subprocess
import sys
import time
from contextlib import suppress

import psutil
import pytest

from cpmux.engine.ownership import (
    BusyError,
    OwnershipError,
    file_lease,
    matching_process,
    process_created_at,
    process_owner_alive,
    read_process_owner,
    run_owner,
    session_owner,
    terminate_process,
    write_process_owner,
)
from cpmux.engine.store import RunPaths


def _process_active(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def test_matching_process_rejects_a_reused_pid_identity():
    created = process_created_at(os.getpid())

    assert matching_process(os.getpid(), created).pid == os.getpid()
    assert matching_process(os.getpid(), created + 1) is None


def test_matching_process_refuses_an_unidentified_live_pid():
    with pytest.raises(OwnershipError, match="no recorded creation time"):
        matching_process(os.getpid(), None)


def test_terminate_process_never_signals_the_current_controller():
    with pytest.raises(OwnershipError, match="current controller"):
        terminate_process(os.getpid(), process_created_at(os.getpid()), group=False)


def test_terminate_process_preserves_mismatched_processes_and_stops_owned_groups():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        created = process_created_at(child.pid)
        assert terminate_process(child.pid, created + 1) is False
        assert child.poll() is None

        assert terminate_process(child.pid, created) is True
        assert child.wait(timeout=5) is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_terminate_process_kills_sigterm_ignoring_group_children(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    child_source = (
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
        f"open({str(child_pid_file)!r}, 'w').write(str(os.getpid())); "
        "time.sleep(60)"
    )
    leader_source = (
        "import subprocess, time; " f"subprocess.Popen([{sys.executable!r}, '-c', {child_source!r}]); " "time.sleep(60)"
    )
    leader = subprocess.Popen([sys.executable, "-c", leader_source], start_new_session=True)
    child_pid = None
    try:
        deadline = time.monotonic() + 3
        while not child_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        child_pid = int(child_pid_file.read_text())

        assert terminate_process(leader.pid, process_created_at(leader.pid), grace=0.2) is True
        assert leader.wait(timeout=3) is not None
        assert not _process_active(child_pid)
    finally:
        if leader.poll() is None:
            os.killpg(leader.pid, signal.SIGKILL)
        leader.wait()
        if child_pid is not None and psutil.pid_exists(child_pid):
            with suppress(psutil.NoSuchProcess):
                psutil.Process(child_pid).kill()


def test_terminate_process_refuses_group_signal_for_foreground_child():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        created = process_created_at(child.pid)

        with pytest.raises(OwnershipError, match="isolated process group"):
            terminate_process(child.pid, created, group=True)

        assert child.poll() is None
        assert terminate_process(child.pid, created, group=False, grace=0.2) is True
        assert child.wait(timeout=3) is not None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_file_lease_excludes_other_handles_and_releases_after_failure(tmp_path):
    lock = tmp_path / "owner.lock"
    with pytest.raises(ValueError, match="operation failed"):
        with file_lease(lock):
            with pytest.raises(BusyError):
                with file_lease(lock, shared=True):
                    pytest.fail("conflicting lease was acquired")
            raise ValueError("operation failed")

    with file_lease(lock):
        assert lock.exists()


def test_session_owner_allows_disjoint_sessions_but_excludes_a_run(tmp_path):
    paths = RunPaths(tmp_path, "run")
    with session_owner(paths, "a", "followup"):
        assert process_owner_alive(paths.session_owner("a"))
        with session_owner(paths, "b", "followup"):
            assert process_owner_alive(paths.session_owner("b"))
        with pytest.raises(BusyError):
            with session_owner(paths, "a", "followup"):
                pytest.fail("same-session writer was admitted")
        with pytest.raises(BusyError):
            with run_owner(paths):
                pytest.fail("run writer was admitted")

    assert read_process_owner(paths.session_owner("a")) is None
    assert read_process_owner(paths.session_owner("b")) is None


def test_run_owner_excludes_session_writers_and_cleans_its_metadata(tmp_path):
    paths = RunPaths(tmp_path, "run")
    with run_owner(paths):
        assert process_owner_alive(paths.owner_file)
        with pytest.raises(BusyError):
            with session_owner(paths, "a", "followup"):
                pytest.fail("session writer was admitted")

    assert read_process_owner(paths.owner_file) is None


def test_run_owner_does_not_erase_transferred_owner_metadata(tmp_path):
    paths = RunPaths(tmp_path, "run")
    with run_owner(paths):
        transferred = write_process_owner(paths.owner_file, os.getpid(), "daemon handoff")

    assert read_process_owner(paths.owner_file) == transferred


def test_read_process_owner_reports_invalid_state(tmp_path):
    owner_file = tmp_path / "owner.json"
    owner_file.write_text("not JSON")

    with pytest.raises(OwnershipError, match="invalid"):
        read_process_owner(owner_file)


@pytest.mark.parametrize("contents", [b"\xff\xfe", b'{"pid": 0}', b'{"pid": -1}'])
def test_read_process_owner_rejects_corrupt_utf8_and_nonpositive_pids(tmp_path, contents):
    owner_file = tmp_path / "owner.json"
    owner_file.write_bytes(contents)

    with pytest.raises(OwnershipError, match="invalid"):
        read_process_owner(owner_file)


@pytest.mark.parametrize("pid", [0, -1])
def test_process_created_at_rejects_nonpositive_pids(pid):
    with pytest.raises(OwnershipError, match="positive"):
        process_created_at(pid)


def test_terminate_process_reports_unfinished_escalation(monkeypatch):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        created = process_created_at(child.pid)
        monkeypatch.setattr(psutil.Process, "send_signal", lambda *args: None)

        with pytest.raises(OwnershipError, match="still has live"):
            terminate_process(child.pid, created, group=False, grace=0.1)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()
