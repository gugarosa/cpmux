# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import os
import subprocess
import sys

from cmux.config import Plan
from cmux.engine.daemon import pid_alive, reconcile, write_owner
from cmux.engine.store import RunManifest, RunPaths, SessionRecord
from cmux.events import Status


def _dead_pid():
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _record(key, status):
    return SessionRecord(
        key=key,
        name=key,
        slug=key,
        branch=f"cmux/{key}",
        base="main",
        model="m",
        session_id="sid",
        worktree="/tmp/x",
        status=status,
    )


def test_pid_alive_true_for_current_process():
    assert pid_alive(os.getpid())


def test_pid_alive_false_for_dead_pid():
    assert not pid_alive(_dead_pid())


def test_reconcile_marks_orphaned_when_owner_dead(tmp_path):
    paths = RunPaths(tmp_path, "run1")
    record = _record("a", Status.RUNNING)
    paths.write_record(record)
    write_owner(paths, _dead_pid())

    reconcile(paths, [record])

    assert record.status == Status.FAILED
    assert paths.read_record("a").status == Status.FAILED


def test_reconcile_skips_when_owner_alive(tmp_path):
    paths = RunPaths(tmp_path, "run2")
    record = _record("a", Status.RUNNING)
    paths.write_record(record)
    write_owner(paths, os.getpid())

    reconcile(paths, [record])

    assert record.status == Status.RUNNING


def test_reconcile_leaves_terminal_records_untouched(tmp_path):
    paths = RunPaths(tmp_path, "run3")
    record = _record("a", Status.DONE)
    paths.write_record(record)
    write_owner(paths, _dead_pid())

    reconcile(paths, [record])

    assert record.status == Status.DONE


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
