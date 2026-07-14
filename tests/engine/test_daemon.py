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


def _reconcile_scenario(tmp_path, run_id, status, owner_pid, **reconcile_kwargs):
    paths = RunPaths(tmp_path, run_id)
    record = _record("a", status)
    paths.write_record(record)
    write_owner(paths, owner_pid)

    return paths, record, reconcile_kwargs


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
    ("scenario_setup", "expected_outcome"),
    [
        pytest.param(
            lambda tmp_path: _reconcile_scenario(tmp_path, "run1", Status.RUNNING, _dead_pid()),
            (Status.FAILED, Status.FAILED),
            id="dead-owner-persists-failure",
        ),
        pytest.param(
            lambda tmp_path: _reconcile_scenario(
                tmp_path,
                "run4",
                Status.RUNNING,
                _dead_pid(),
                persist=False,
            ),
            (Status.FAILED, Status.RUNNING),
            id="dead-owner-memory-only",
        ),
    ],
)
def test_reconcile_orphaned_record_outcomes(tmp_path, scenario_setup, expected_outcome):
    paths, record, reconcile_kwargs = scenario_setup(tmp_path)
    reconcile(paths, [record], **reconcile_kwargs)

    assert record.status == expected_outcome[0]
    assert paths.read_record("a").status == expected_outcome[1]


@pytest.mark.parametrize(
    ("scenario_setup", "expected_outcome"),
    [
        pytest.param(
            lambda tmp_path: _reconcile_scenario(tmp_path, "run2", Status.RUNNING, os.getpid()),
            Status.RUNNING,
            id="live-owner",
        ),
        pytest.param(
            lambda tmp_path: _reconcile_scenario(tmp_path, "run3", Status.DONE, _dead_pid()),
            Status.DONE,
            id="terminal-record",
        ),
    ],
)
def test_reconcile_preserves_ignored_record_status(tmp_path, scenario_setup, expected_outcome):
    paths, record, reconcile_kwargs = scenario_setup(tmp_path)
    reconcile(paths, [record], **reconcile_kwargs)

    assert record.status == expected_outcome


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
