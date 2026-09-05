# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import os
import signal
import sys
import threading
from pathlib import Path

import pytest

from cpmux.config import Plan, ResolvedItem
from cpmux.engine.store import RunManifest
from cpmux.engine.supervisor import Options, Supervisor
from cpmux.events import Status
from cpmux.vcs import git


def _plan():
    return Plan.model_validate({"system": "S", "defaults": {"concurrency": 3}, "items": ["fix a", "fix b"]})


def test_options_defaults_are_conservative():
    options = Options()

    assert options.concurrency is None
    assert options.open_pr is True
    assert options.strip_github_token is True
    assert options.deps_override is None


def test_create_builds_supervisor_from_plan(git_repo):
    repo = git_repo
    supervisor = Supervisor.create(_plan(), str(repo), Options())

    assert isinstance(supervisor.run_id, str)
    assert supervisor.run_id
    assert len(supervisor.resolved) == 2


def test_create_uses_plan_concurrency_when_option_none(git_repo):
    repo = git_repo
    supervisor = Supervisor.create(_plan(), str(repo), Options())
    assert supervisor.concurrency == 3


def test_prepare_creates_one_record_per_item(git_repo):
    repo = git_repo
    supervisor = Supervisor.create(_plan(), str(repo), Options())
    supervisor.prepare()

    assert len(supervisor.records) == len(supervisor.resolved)
    for item in supervisor.resolved:
        assert item.key in supervisor.records


def test_prepare_records_have_session_and_worktree(git_repo):
    repo = git_repo
    supervisor = Supervisor.create(_plan(), str(repo), Options())
    supervisor.prepare()

    for record in supervisor.records.values():
        assert record.session_id
        assert record.base_sha
        assert Path(record.worktree).exists()


def test_prepare_writes_manifest(git_repo):
    repo = git_repo
    supervisor = Supervisor.create(_plan(), str(repo), Options())
    supervisor.prepare()
    assert supervisor.paths.manifest.exists()


def test_prepare_records_config_path_for_provenance(git_repo):
    supervisor = Supervisor.create(_plan(), str(git_repo), Options(), "issues.yaml")
    supervisor.prepare()
    manifest = RunManifest.model_validate_json(supervisor.paths.manifest.read_text())
    assert manifest.config_path == "issues.yaml"


def test_prepare_creates_branch_per_item(git_repo):
    repo = git_repo
    supervisor = Supervisor.create(_plan(), str(repo), Options())
    supervisor.prepare()

    for record in supervisor.records.values():
        assert git.branch_exists(repo, record.branch) is True


def test_from_run_reloads_resolved_and_records(git_repo):
    repo = git_repo
    supervisor = Supervisor.create(_plan(), str(repo), Options())
    supervisor.prepare()

    loaded = Supervisor.from_run(str(repo), supervisor.run_id)

    assert [item.key for item in loaded.resolved] == [item.key for item in supervisor.resolved]
    assert sorted(loaded.records) == sorted(supervisor.records)
    for key, record in supervisor.records.items():
        assert loaded.records[key].branch == record.branch


def test_prepare_marks_failed_when_add_dir_missing(git_repo):
    plan = Plan.model_validate({"items": [{"prompt": "fix x", "paths": ["nope-dir"]}]})
    supervisor = Supervisor.create(plan, str(git_repo), Options())
    supervisor.prepare()
    record = next(iter(supervisor.records.values()))

    assert record.status == Status.FAILED
    assert "nope-dir" in record.error


def test_prepare_accepts_existing_add_dir(git_repo):
    plan = Plan.model_validate({"items": [{"prompt": "fix x", "paths": ["README.md"]}]})
    supervisor = Supervisor.create(plan, str(git_repo), Options())
    supervisor.prepare()
    record = next(iter(supervisor.records.values()))

    assert record.status != Status.FAILED


def test_prepare_keeps_namespaced_items_in_disjoint_worktrees(git_repo):
    plan = Plan.model_validate(
        {"items": [{"id": "frontend/login", "prompt": "x"}, {"id": "frontend/profile", "prompt": "y"}]}
    )
    supervisor = Supervisor.create(plan, str(git_repo), Options(open_pr=False))
    supervisor.prepare()

    for key in ("frontend/login", "frontend/profile"):
        record = supervisor.paths.read_record(key)
        assert record.status == Status.PENDING
        assert Path(record.worktree).is_dir()
        assert Path(record.worktree).is_relative_to(supervisor.paths.worktrees_dir)


def test_run_spawn_failure_does_not_abort_independent_items(git_repo, monkeypatch):
    plan = Plan.model_validate(
        {
            "items": [
                {"id": "bad", "prompt": "x"},
                {"id": "good", "prompt": "y"},
                {"id": "dependent", "prompt": "z", "depends_on": ["bad"]},
            ]
        }
    )
    supervisor = Supervisor.create(plan, str(git_repo), Options(open_pr=False))
    supervisor.prepare()

    def argv(item, *args):
        if item.key == "bad":
            return [str(git_repo / "missing-copilot")]
        return [sys.executable, "-c", "pass"]

    monkeypatch.setattr(ResolvedItem, "spawn_argv", argv)
    records = {record.key: record for record in asyncio.run(supervisor.run(headless=True))}

    assert records["bad"].status == Status.FAILED
    assert "missing-copilot" in records["bad"].error
    assert records["good"].status == Status.NO_CHANGES
    assert records["dependent"].status == Status.FAILED
    assert "dependency `bad`" in records["dependent"].error
    for key, record in records.items():
        assert record.ended_at is not None
        assert record.pid is None
        assert supervisor.paths.read_record(key).status == record.status


@pytest.mark.parametrize("ignore_term", [False, True])
def test_run_cancellation_persists_terminal_records(git_repo, monkeypatch, ignore_term):
    plan = Plan.model_validate({"items": [{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y", "depends_on": ["a"]}]})
    supervisor = Supervisor.create(plan, str(git_repo), Options(open_pr=False))
    supervisor.prepare()
    script = (
        "import json, signal, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + "print(json.dumps({'type': 'assistant.message', 'data': {'content': 'ready'}}), flush=True)\n"
        "time.sleep(60)"
    )
    monkeypatch.setattr(ResolvedItem, "spawn_argv", lambda *args: [sys.executable, "-c", script])

    async def scenario():
        task = asyncio.create_task(supervisor.run(headless=True))

        async def started():
            transcript = supervisor.paths.transcript("a")
            while not transcript.exists() or "ready" not in transcript.read_text():
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(started(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 8)

            for key in ("a", "b"):
                record = supervisor.paths.read_record(key)
                assert record.status == Status.KILLED
                assert record.ended_at is not None
                assert record.pid is None
            assert supervisor.runners["a"].proc.returncode is not None
        finally:
            task.cancel()
            for runner in supervisor.runners.values():
                if runner.proc is not None:
                    if runner.proc.returncode is None:
                        os.killpg(runner.proc.pid, signal.SIGKILL)
                    await runner.proc.stdout.read()
                    await runner.proc.wait()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_run_cancellation_during_finalization_reports_partial_failure(git_repo, monkeypatch):
    supervisor = Supervisor.create(Plan.model_validate({"items": ["x"]}), str(git_repo), Options(open_pr=False))
    supervisor.prepare()
    monkeypatch.setattr(ResolvedItem, "spawn_argv", lambda *args: [sys.executable, "-c", "pass"])
    started = threading.Event()
    release = threading.Event()

    def has_changes(*args):
        started.set()
        release.wait(10)
        return False

    monkeypatch.setattr(git, "has_changes", has_changes)

    async def scenario():
        task = asyncio.create_task(supervisor.run(headless=True))

        async def finalizing():
            while not started.is_set():
                await asyncio.sleep(0.01)

        try:
            await asyncio.wait_for(finalizing(), 5)
            assert supervisor.paths.read_record("x").pid is None
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

            record = supervisor.paths.read_record("x")
            assert record.status == Status.FAILED
            assert "cancelled during finalization" in record.error
            assert record.pid is None
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
