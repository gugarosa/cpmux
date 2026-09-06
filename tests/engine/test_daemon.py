# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import os
import subprocess
import sys
import time
from contextlib import suppress
from types import SimpleNamespace

import psutil
import pytest

import cpmux.engine.daemon as daemon
from cpmux.config import Plan
from cpmux.engine.daemon import kill_session, launch_detached, reconcile, stop
from cpmux.engine.interact import run_followup
from cpmux.engine.ownership import (
    OwnershipError,
    ProcessOwner,
    matching_process,
    process_created_at,
    read_process_owner,
    session_owner,
    terminate_process,
    write_process_owner,
)
from cpmux.engine.store import RunManifest, RunPaths, SessionRecord
from cpmux.engine.supervisor import Options, Supervisor
from cpmux.events import TERMINAL, Status


def _write_dead_owner(paths):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        write_process_owner(paths.owner_file, proc.pid)
    finally:
        proc.terminate()
        proc.wait()


def _record(key, status):
    return SessionRecord(
        key=key,
        name=key,
        slug=key,
        branch=f"cpmux/{key}",
        base="main",
        model="m",
        session_id="sid",
        worktree="/tmp/x",
        status=status,
    )


def _wait_for(get_value, timeout=10):
    deadline = time.monotonic() + timeout
    value = get_value()
    while not value and time.monotonic() < deadline:
        time.sleep(0.05)
        value = get_value()
    return value


def _install_fake_copilot(root, monkeypatch, sleep_seconds):
    bin_dir = root / "fake-bin"
    bin_dir.mkdir()
    marker = root / "copilot-started"
    executable = bin_dir / "copilot"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "Path(os.environ['CPMUX_FAKE_STARTED']).write_text('started')\n"
        "time.sleep(float(os.environ['CPMUX_FAKE_SLEEP']))\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("CPMUX_FAKE_STARTED", str(marker))
    monkeypatch.setenv("CPMUX_FAKE_SLEEP", str(sleep_seconds))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return marker


def _prepared_run(git_repo):
    plan = Plan.model_validate({"items": [{"id": "daemon-item", "prompt": "make no changes"}]})
    supervisor = Supervisor.create(plan, str(git_repo), Options(open_pr=False))
    supervisor.prepare()
    return supervisor


def _cleanup_known_process(pid, created_at, group):
    if pid is None or created_at is None:
        return
    terminate_process(pid, created_at, group=group, grace=0.2)
    with suppress(psutil.NoSuchProcess):
        psutil.Process(pid).wait(timeout=3)


@pytest.mark.parametrize(
    ("persist", "stored_status"),
    [
        pytest.param(True, Status.FAILED, id="dead-owner-persists-failure"),
        pytest.param(False, Status.RUNNING, id="dead-owner-memory-only"),
    ],
)
def test_reconcile_orphaned_record_outcomes(tmp_path, persist, stored_status):
    paths = RunPaths(tmp_path, "run1")
    record = _record("a", Status.RUNNING)
    paths.write_record(record)
    _write_dead_owner(paths)

    reconcile(paths, [record], persist=persist)

    assert record.status == Status.FAILED
    assert paths.read_record("a").status == stored_status


@pytest.mark.parametrize(
    ("status", "live_owner"),
    [
        pytest.param(Status.RUNNING, True, id="live-owner"),
        pytest.param(Status.DONE, False, id="terminal-record"),
    ],
)
def test_reconcile_preserves_ignored_record_status(tmp_path, status, live_owner):
    paths = RunPaths(tmp_path, "run1")
    record = _record("a", status)
    paths.write_record(record)
    if live_owner:
        write_process_owner(paths.owner_file, os.getpid())
    else:
        _write_dead_owner(paths)

    reconcile(paths, [record])

    assert record.status == status
    assert paths.read_record("a").status == status


def test_reconcile_reaps_orphaned_child(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        paths = RunPaths(tmp_path, "run5")
        record = _record("a", Status.RUNNING)
        record.pid = child.pid
        record.pid_created_at = process_created_at(child.pid)
        paths.write_record(record)
        _write_dead_owner(paths)

        reconcile(paths, [record])

        assert child.wait(timeout=6) is not None
        assert record.status == Status.FAILED
    finally:
        if child.poll() is None:
            child.kill()


def test_manifest_roundtrips_resolved_items(tmp_path):
    resolved = Plan.model_validate({"system": "S", "items": ["do a thing"]}).resolve()
    paths = RunPaths(tmp_path, "r")
    paths.write_manifest(
        RunManifest(
            run_id="r",
            repo_root=str(tmp_path),
            config_path="",
            system="S",
            item_keys=[resolved[0].key],
            resolved=resolved,
            concurrency=4,
        )
    )

    loaded = RunManifest.model_validate_json(paths.manifest.read_text())

    assert loaded.resolved[0].key == resolved[0].key
    assert loaded.resolved[0].prompt == resolved[0].prompt


def test_reconcile_keeps_prepared_but_unstarted_items_pending(tmp_path):
    paths = RunPaths(tmp_path, "prepared")
    record = _record("a", Status.PENDING)
    paths.write_record(record)

    reconcile(paths, [record])

    assert record.status == Status.PENDING


def test_reconcile_does_not_race_an_active_followup(tmp_path):
    paths = RunPaths(tmp_path, "run")
    record = _record("a", Status.RUNNING)
    paths.write_record(record)

    with session_owner(paths, "a", "followup"):
        reconcile(paths, [record])

    assert record.status == Status.RUNNING


def test_reconcile_never_signals_a_reused_pid(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        paths = RunPaths(tmp_path, "run")
        record = _record("a", Status.RUNNING)
        record.pid = child.pid
        record.pid_created_at = process_created_at(child.pid) + 1
        paths.write_record(record)
        _write_dead_owner(paths)

        reconcile(paths, [record])

        assert child.poll() is None
        assert record.status == Status.FAILED
        assert record.pid is None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait()


def test_reconcile_keeps_unidentified_live_children_unresolved(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    try:
        paths = RunPaths(tmp_path, "run")
        record = _record("a", Status.RUNNING)
        record.pid = child.pid
        paths.write_record(record)
        _write_dead_owner(paths)

        reconcile(paths, [record])

        assert child.poll() is None
        assert record.status == Status.RUNNING
        assert record.pid == child.pid
        assert "no recorded creation time" in record.error
        assert paths.read_record("a") == record
    finally:
        child.kill()
        child.wait()


def test_launch_detached_hands_off_owner_and_completes_with_fake_copilot(git_repo, monkeypatch):
    supervisor = _prepared_run(git_repo)
    paths = supervisor.paths
    key = supervisor.resolved[0].key
    marker = _install_fake_copilot(git_repo, monkeypatch, 1.0)
    initial_tokens = []
    original_write_owner = daemon.write_process_owner
    daemon_pid = None
    daemon_created_at = None

    def capture_initial_owner(owner_path, pid):
        owner = original_write_owner(owner_path, pid)
        initial_tokens.append(owner.token)
        return owner

    monkeypatch.setattr(daemon, "write_process_owner", capture_initial_owner)
    try:
        daemon_pid = launch_detached(supervisor.run_id, str(git_repo))
        owner = read_process_owner(paths.owner_file)
        daemon_created_at = owner.process_created_at

        assert paths.ready_file.exists()
        assert owner.pid == daemon_pid
        assert owner.token != initial_tokens[0]
        assert matching_process(owner.pid, owner.process_created_at) is not None
        assert _wait_for(marker.exists, timeout=3)

        record = _wait_for(
            lambda: (current if (current := paths.read_record(key)).status in TERMINAL else None),
            timeout=10,
        )
        assert record.status == Status.NO_CHANGES
        assert record.pid is None
        assert _wait_for(lambda: read_process_owner(paths.owner_file) is None, timeout=5)
    finally:
        record = paths.read_record(key)
        _cleanup_known_process(record.pid, record.pid_created_at, record.pid_is_group)
        _cleanup_known_process(daemon_pid, daemon_created_at, False)


@pytest.mark.parametrize("operation", ["run", "item"])
def test_detached_stop_operations_clean_up_fake_copilot(git_repo, monkeypatch, operation):
    supervisor = _prepared_run(git_repo)
    paths = supervisor.paths
    key = supervisor.resolved[0].key
    marker = _install_fake_copilot(git_repo, monkeypatch, 60)
    daemon_pid = None
    daemon_created_at = None
    child_pid = None
    child_created_at = None

    try:
        daemon_pid = launch_detached(supervisor.run_id, str(git_repo))
        daemon_owner = read_process_owner(paths.owner_file)
        daemon_created_at = daemon_owner.process_created_at
        assert _wait_for(marker.exists, timeout=3)
        running = _wait_for(
            lambda: (current if (current := paths.read_record(key)).pid is not None else None),
            timeout=5,
        )
        child_pid = running.pid
        child_created_at = running.pid_created_at

        if operation == "run":
            assert stop(paths, [running]) >= 1
        else:
            assert kill_session(paths, running) is True

        record = _wait_for(
            lambda: (current if (current := paths.read_record(key)).status in TERMINAL else None),
            timeout=10,
        )
        assert record.status == Status.KILLED
        assert record.pid is None
        assert _wait_for(lambda: read_process_owner(paths.owner_file) is None, timeout=5)
        assert matching_process(child_pid, child_created_at) is None
    finally:
        _cleanup_known_process(child_pid, child_created_at, True)
        _cleanup_known_process(daemon_pid, daemon_created_at, False)


def test_launch_detached_reaps_daemon_that_fails_before_ready(git_repo, monkeypatch):
    supervisor = _prepared_run(git_repo)
    paths = supervisor.paths
    paths.manifest.write_text("not JSON")
    spawned = []
    original_popen = subprocess.Popen

    def capture_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(daemon.subprocess, "Popen", capture_process)

    with pytest.raises(OwnershipError, match="exited before startup"):
        launch_detached(supervisor.run_id, str(git_repo))

    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    assert read_process_owner(paths.owner_file) is None
    assert not paths.ready_file.exists()


def test_launch_detached_reaps_daemon_when_initial_owner_write_fails(git_repo, monkeypatch):
    supervisor = _prepared_run(git_repo)
    paths = supervisor.paths
    spawned = []
    original_popen = subprocess.Popen

    def capture_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        return process

    def fail_owner_write(owner_paths, pid):
        raise OSError("owner write failed")

    monkeypatch.setattr(daemon.subprocess, "Popen", capture_process)
    monkeypatch.setattr(daemon, "write_process_owner", fail_owner_write)

    with pytest.raises(OSError, match="owner write failed"):
        launch_detached(supervisor.run_id, str(git_repo))

    assert len(spawned) == 1
    assert spawned[0].poll() is not None
    assert read_process_owner(paths.owner_file) is None
    assert not paths.ready_file.exists()


@pytest.mark.parametrize("whole_run", [False, True])
def test_kill_session_gives_an_idle_run_followup_time_to_record_cancellation(git_repo, monkeypatch, whole_run):
    supervisor = _prepared_run(git_repo)
    paths = supervisor.paths
    record = paths.read_record("daemon-item")
    record.status = Status.DONE
    paths.write_record(record)
    marker = _install_fake_copilot(git_repo, monkeypatch, 60)

    async def scenario():
        task = asyncio.create_task(run_followup(paths, record, "continue"))
        try:
            async with asyncio.timeout(5):
                while not marker.exists():
                    await asyncio.sleep(0.01)
            if whole_run:
                assert await asyncio.to_thread(stop, paths, [paths.read_record(record.key)]) == 1
            else:
                assert await asyncio.to_thread(kill_session, paths, paths.read_record(record.key))
            await asyncio.wait_for(task, 8)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert paths.read_record(record.key).status == Status.KILLED
    assert paths.read_record(record.key).pid is None
    assert not paths.session_owner(record.key).exists()


def test_launch_detached_cleans_up_after_acknowledged_owner_read_failure(git_repo, monkeypatch):
    supervisor = _prepared_run(git_repo)
    paths = supervisor.paths
    _install_fake_copilot(git_repo, monkeypatch, 60)
    spawned = []
    original_popen = subprocess.Popen
    original_read = daemon.read_process_owner
    reads = 0

    def capture_process(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        spawned.append(process)
        return process

    def fail_ack_read(path):
        nonlocal reads
        reads += 1
        if reads == 1:
            raise OwnershipError("owner metadata became unreadable")
        return original_read(path)

    monkeypatch.setattr(daemon.subprocess, "Popen", capture_process)
    monkeypatch.setattr(daemon, "read_process_owner", fail_ack_read)
    try:
        with pytest.raises(OwnershipError, match="unreadable"):
            launch_detached(supervisor.run_id, str(git_repo))
        assert spawned[0].poll() is not None
        record = paths.read_record("daemon-item")
        assert record.pid is None
        assert not paths.owner_file.exists()
    finally:
        record = paths.read_record("daemon-item")
        _cleanup_known_process(record.pid, record.pid_created_at, record.pid_is_group)
        for process in spawned:
            if process.poll() is None:
                process.kill()
            process.wait()


def test_stop_keeps_the_owner_of_an_inflight_git_operation(tmp_path, monkeypatch):
    paths = RunPaths(tmp_path, "run")
    record = _record("a", Status.FINALIZING)
    record.phase = "verification"
    paths.write_record(record)
    owner = ProcessOwner(pid=os.getpid() + 100000, process_created_at=1, operation="run", token="test")
    ticks = iter(range(100))
    monkeypatch.setattr(daemon, "read_process_owner", lambda path: owner)
    monkeypatch.setattr(daemon, "owner_alive", lambda paths: True)
    monkeypatch.setattr(daemon, "time", SimpleNamespace(monotonic=lambda: next(ticks), sleep=lambda seconds: None))
    monkeypatch.setattr(daemon, "terminate_process", lambda *args, **kwargs: pytest.fail("Git owner was abandoned"))

    with pytest.raises(OwnershipError, match="finishing a Git operation"):
        stop(paths, [record])

    assert paths.stop_file().exists()
    assert paths.read_record("a").status == Status.FINALIZING
