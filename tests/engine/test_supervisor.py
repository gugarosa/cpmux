# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import json
import os
import signal
import sys
import threading
from pathlib import Path

import pytest

from cpmux.config import Plan, ResolvedItem
from cpmux.engine.daemon import set_paused
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


def test_from_run_rejects_incomplete_records_without_recreating_source(git_repo):
    supervisor = Supervisor.create(_plan(), str(git_repo), Options(open_pr=False))
    supervisor.prepare()
    key = supervisor.resolved[0].key
    supervisor.paths.record_file(key).unlink()

    with pytest.raises(ValueError, match="no record"):
        Supervisor.from_run(str(git_repo), supervisor.run_id)
    assert supervisor.paths.worktree(key).is_dir()


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
    assert records["dependent"].status == Status.BLOCKED
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


def test_run_setup_failure_prevents_agent_without_aborting_independent_items(git_repo, monkeypatch):
    plan = Plan.model_validate(
        {"items": [{"id": "bad", "prompt": "x", "setup": ["exit 7"]}, {"id": "good", "prompt": "y"}]}
    )
    supervisor = Supervisor.create(plan, str(git_repo), Options(open_pr=False))
    launched = []

    def argv(item, *args):
        launched.append(item.key)
        return [sys.executable, "-c", "pass"]

    monkeypatch.setattr(ResolvedItem, "spawn_argv", argv)
    records = {record.key: record for record in asyncio.run(supervisor.run(headless=True))}

    assert launched == ["good"]
    assert records["bad"].status == Status.FAILED
    assert records["bad"].attempts[-1].commands[0].exit_code == 7
    assert records["good"].status == Status.NO_CHANGES


def test_run_dependency_install_uses_owned_setup_and_prevents_agent_on_failure(git_repo, monkeypatch):
    plan = Plan.model_validate({"defaults": {"deps": "install"}, "items": [{"id": "a", "prompt": "x"}]})
    supervisor = Supervisor.create(plan, str(git_repo), Options(open_pr=False))
    monkeypatch.setattr(
        git, "dependency_install_command", lambda worktree: [sys.executable, "-c", "raise SystemExit(9)"]
    )
    monkeypatch.setattr(ResolvedItem, "spawn_argv", lambda *args: pytest.fail("agent ran after dependency failure"))

    asyncio.run(supervisor.run(headless=True))

    record = supervisor.paths.read_record("a")
    assert record.status == Status.FAILED
    assert record.attempts[-1].commands[0].name == "dependencies"
    assert record.attempts[-1].commands[0].exit_code == 9
    assert record.pid is None


def test_prepare_retry_rechecks_failed_delivery_without_replaying_agent_or_successful_items(git_repo, monkeypatch):
    gate = git_repo / ".cpmux" / "accept"
    plan = Plan.model_validate(
        {
            "items": [
                {"id": "a", "prompt": "x", "checks": [f"test -f '{gate}'"]},
                {"id": "b", "prompt": "y"},
            ]
        }
    )
    supervisor = Supervisor.create(plan, str(git_repo), Options(open_pr=False))
    launched = []

    def argv(item, worktree, *args):
        launched.append(item.key)
        return [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(worktree / 'new.txt')!r}).write_text('code'); "
            f"print({json.dumps({'type': 'result', 'exitCode': 0, 'usage': {'premiumRequests': 2}})!r})",
        ]

    monkeypatch.setattr(ResolvedItem, "spawn_argv", argv)
    asyncio.run(supervisor.run(headless=True))
    failed = supervisor.paths.read_record("a")
    succeeded = supervisor.paths.read_record("b")
    gate.touch()
    assert supervisor.prepare_retry() == ["a"]

    asyncio.run(supervisor.run(headless=True))
    recovered = supervisor.paths.read_record("a")

    assert failed.status == Status.FAILED
    assert failed.phase == "verification"
    assert failed.agent_complete
    assert recovered.status == Status.DONE
    assert recovered.candidate_sha == failed.candidate_sha
    assert recovered.verification.commit_sha == recovered.candidate_sha
    assert recovered.premium_requests == 2
    assert [attempt.status for attempt in recovered.attempts] == [Status.FAILED, Status.DONE]
    assert launched == ["a", "b"]
    assert supervisor.paths.read_record("b") == succeeded


@pytest.mark.parametrize("mode", ["retry", "resume", "fresh"])
def test_prepare_retry_distinguishes_native_resume_and_new_conversations(git_repo, monkeypatch, mode):
    supervisor = Supervisor.create(
        Plan.model_validate({"items": [{"id": "a", "prompt": "x"}]}), str(git_repo), Options(open_pr=False)
    )
    failed = json.dumps({"type": "result", "exitCode": 1, "usage": {"premiumRequests": 0.5}})
    monkeypatch.setattr(ResolvedItem, "spawn_argv", lambda *args: [sys.executable, "-c", f"print({failed!r})"])
    asyncio.run(supervisor.run(headless=True))
    initial_id = supervisor.records["a"].session_id
    calls = []

    def resumed(session_id, *args):
        calls.append(("resume", session_id))
        return [sys.executable, "-c", "pass"]

    def fresh(item, worktree, session_id, *args):
        calls.append(("fresh", session_id))
        return [sys.executable, "-c", "pass"]

    monkeypatch.setattr("cpmux.engine.supervisor.followup_argv", resumed)
    monkeypatch.setattr(ResolvedItem, "spawn_argv", fresh)
    supervisor.prepare_retry(["a"], mode)
    asyncio.run(supervisor.run(headless=True))
    record = supervisor.paths.read_record("a")

    assert record.status == Status.NO_CHANGES
    assert record.attempts[-1].mode == mode
    assert record.premium_requests == 0.5
    assert record.attempts[-1].agent_started
    assert record.attempts[-1].premium_requests is None
    if mode == "resume":
        assert record.session_id == initial_id
        assert calls[-1] == ("resume", initial_id)
    else:
        assert record.session_id != initial_id
        assert calls == [("fresh", record.session_id)]


def test_prepare_retry_validates_all_selected_items_before_mutation(git_repo):
    supervisor = Supervisor.create(_plan(), str(git_repo), Options(open_pr=False))
    supervisor.prepare()
    active = supervisor.records[supervisor.resolved[0].key]
    active.status = Status.RUNNING
    supervisor.paths.write_record(active)
    before = [supervisor.paths.record_file(item.key).read_bytes() for item in supervisor.resolved]

    with pytest.raises(ValueError, match="not terminal"):
        supervisor.prepare_retry([supervisor.resolved[0].key])
    with pytest.raises(ValueError, match="unknown"):
        supervisor.prepare_retry(["missing"])

    assert [supervisor.paths.record_file(item.key).read_bytes() for item in supervisor.resolved] == before


def test_run_inherits_only_explicit_predecessor_code_from_its_recorded_commit(git_repo, monkeypatch):
    plan = Plan.model_validate(
        {
            "items": [
                {"id": "parent", "prompt": "foundation"},
                {"id": "child", "prompt": "extend", "base_from": "parent"},
                {"id": "ordered", "prompt": "independent", "depends_on": ["parent"]},
            ]
        }
    )
    supervisor = Supervisor.create(plan, str(git_repo), Options(open_pr=False))
    supervisor.prepare()
    assert not supervisor.paths.worktree("child").exists()
    inherited = {}

    def argv(item, worktree, *args):
        inherited[item.key] = (worktree / "parent.txt").exists()
        return [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(worktree / (item.key + '.txt'))!r}).write_text('code')",
        ]

    monkeypatch.setattr(ResolvedItem, "spawn_argv", argv)
    asyncio.run(supervisor.run(headless=True))
    parent = supervisor.paths.read_record("parent")
    child = supervisor.paths.read_record("child")

    assert inherited == {"parent": False, "child": True, "ordered": False}
    assert child.base_sha == parent.candidate_sha
    assert child.base == parent.branch
    assert child.base_from == parent.key
    assert child.status == Status.DONE
    assert (
        git.run_git(["merge-base", parent.candidate_sha, child.candidate_sha], git_repo).stdout.strip()
        == parent.candidate_sha
    )


def test_run_failed_predecessor_does_not_create_child_worktree(git_repo, monkeypatch):
    supervisor = Supervisor.create(
        Plan.model_validate({"items": [{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y", "base_from": "a"}]}),
        str(git_repo),
        Options(open_pr=False),
    )
    monkeypatch.setattr(ResolvedItem, "spawn_argv", lambda *args: [sys.executable, "-c", "raise SystemExit(1)"])
    asyncio.run(supervisor.run(headless=True))

    assert supervisor.paths.read_record("b").status == Status.BLOCKED
    assert not supervisor.paths.worktree("b").exists()


async def _until(predicate):
    while not predicate():
        await asyncio.sleep(0.02)


def test_run_one_item_cancellation_does_not_cancel_its_siblings(git_repo, monkeypatch):
    supervisor = Supervisor.create(
        Plan.model_validate(
            {
                "items": [
                    {"id": "a", "prompt": "x"},
                    {"id": "b", "prompt": "y"},
                    {"id": "c", "prompt": "z", "depends_on": ["a"]},
                ]
            }
        ),
        str(git_repo),
        Options(open_pr=False),
    )

    def argv(item, *args):
        return [sys.executable, "-c", f"import time; time.sleep({60 if item.key == 'a' else 0.3})"]

    monkeypatch.setattr(ResolvedItem, "spawn_argv", argv)

    async def scenario():
        task = asyncio.create_task(supervisor.run(headless=True))
        try:
            await asyncio.wait_for(_until(lambda: "a" in supervisor.runners and supervisor.records["a"].pid), 5)
            supervisor.paths.stop_file("a").touch()
            await asyncio.wait_for(task, 8)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert supervisor.paths.read_record("a").status == Status.KILLED
    assert supervisor.paths.read_record("b").status == Status.NO_CHANGES
    assert supervisor.paths.read_record("c").status == Status.BLOCKED
    assert all(runner.proc.returncode is not None for runner in supervisor.runners.values())


def test_run_pause_stops_admission_but_allows_active_work_to_finish(git_repo, monkeypatch):
    supervisor = Supervisor.create(
        Plan.model_validate(
            {"defaults": {"concurrency": 1}, "items": [{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y"}]}
        ),
        str(git_repo),
        Options(open_pr=False),
    )
    gate = supervisor.paths.run_dir / "continue-agent"

    def argv(item, *args):
        script = (
            f"from pathlib import Path; import time\nwhile not Path({str(gate)!r}).exists(): time.sleep(0.02)"
            if item.key == "a"
            else "pass"
        )
        return [sys.executable, "-c", script]

    monkeypatch.setattr(ResolvedItem, "spawn_argv", argv)

    async def scenario():
        task = asyncio.create_task(supervisor.run(headless=True))
        try:
            await asyncio.wait_for(_until(lambda: "a" in supervisor.runners and supervisor.records["a"].pid), 5)
            set_paused(supervisor.paths, True)
            gate.touch()
            await asyncio.wait_for(_until(lambda: supervisor.records["a"].status == Status.NO_CHANGES), 5)
            await asyncio.sleep(0.2)
            assert "b" not in supervisor.runners
            assert not task.done()
            set_paused(supervisor.paths, False)
            await asyncio.wait_for(task, 5)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert supervisor.paths.read_record("b").status == Status.NO_CHANGES


def test_run_soft_budget_blocks_new_work_and_retry_can_raise_it(git_repo, monkeypatch):
    supervisor = Supervisor.create(
        Plan.model_validate(
            {
                "defaults": {"concurrency": 1, "premium_budget": 1},
                "items": [{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y"}],
            }
        ),
        str(git_repo),
        Options(open_pr=False),
    )
    event = json.dumps({"type": "result", "exitCode": 0, "usage": {"premiumRequests": 1}})
    launched = []

    def argv(item, *args):
        launched.append(item.key)
        return [sys.executable, "-c", f"print({event!r})"]

    monkeypatch.setattr(ResolvedItem, "spawn_argv", argv)
    asyncio.run(supervisor.run(headless=True))

    assert launched == ["a"]
    assert supervisor.paths.read_record("b").status == Status.BLOCKED
    assert "premium_budget" in supervisor.paths.read_record("b").error
    supervisor.prepare_retry(premium_budget=2)
    asyncio.run(supervisor.run(headless=True))
    assert launched == ["a", "b"]
    assert supervisor.paths.read_record("b").status == Status.NO_CHANGES
