# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import os
import subprocess
import sys

import pytest

from cpmux.config import Plan
from cpmux.engine.daemon import pid_alive, reconcile, write_owner
from cpmux.engine.store import RunManifest, RunPaths, SessionRecord
from cpmux.events import Status


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


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


@pytest.mark.parametrize(
    ("pid", "expected"),
    [
        pytest.param(os.getpid, True, id="current-process"),
        pytest.param(_dead_pid, False, id="dead-process"),
    ],
)
def test_pid_alive_reports_process_state(pid, expected):
    assert pid_alive(pid()) == expected


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
    write_owner(paths, _dead_pid())

    reconcile(paths, [record], persist=persist)

    assert record.status == Status.FAILED
    assert paths.read_record("a").status == stored_status


@pytest.mark.parametrize(
    ("status", "owner_pid"),
    [
        pytest.param(Status.RUNNING, os.getpid, id="live-owner"),
        pytest.param(Status.DONE, _dead_pid, id="terminal-record"),
    ],
)
def test_reconcile_preserves_ignored_record_status(tmp_path, status, owner_pid):
    paths = RunPaths(tmp_path, "run1")
    record = _record("a", status)
    paths.write_record(record)
    write_owner(paths, owner_pid())

    reconcile(paths, [record])

    assert record.status == status
    assert paths.read_record("a").status == status


def test_reconcile_reaps_orphaned_child(tmp_path):
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True)
    try:
        paths = RunPaths(tmp_path, "run5")
        record = _record("a", Status.RUNNING)
        record.pid = child.pid
        paths.write_record(record)
        write_owner(paths, _dead_pid())

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
