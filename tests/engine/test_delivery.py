# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import shlex
import sys
import threading
from pathlib import Path

import pytest

from cpmux.config import Plan
from cpmux.engine.delivery import finalize_item
from cpmux.engine.ownership import run_owner
from cpmux.engine.supervisor import Options, Supervisor
from cpmux.events import Status
from cpmux.vcs import git, pr


def _prepared(git_repo, script):
    command = shlex.join([sys.executable, "-c", script])
    plan = Plan.model_validate(
        {"items": [{"id": "a", "prompt": "x", "checks": [{"name": "tests", "command": command}]}]}
    )
    supervisor = Supervisor.create(plan, str(git_repo), Options(open_pr=False))
    supervisor.prepare()
    record = supervisor.records["a"]
    record.begin_attempt("initial")
    record.agent_complete = True
    (Path(record.worktree) / "change.txt").write_text("candidate")
    supervisor.paths.write_record(record)
    return supervisor, record, plan.resolve()[0]


def test_finalize_item_records_real_checks_against_the_candidate(git_repo):
    supervisor, record, item = _prepared(git_repo, "print('checks actually ran')")

    with run_owner(supervisor.paths):
        asyncio.run(finalize_item(supervisor.paths, record, item, open_pr=False))

    assert record.status == Status.DONE
    assert record.verification.commit_sha == git.head_commit(record.worktree)
    assert record.verification.tree_sha == git.commit_tree(record.worktree, record.candidate_sha)
    assert record.verification.attempt == 1
    result = record.attempts[0].commands[0]
    assert result.status == "passed"
    assert result.exit_code == 0
    assert "checks actually ran" in Path(result.log_path).read_text()
    assert supervisor.paths.read_record("a").verification == record.verification


def test_finalize_item_check_failure_prevents_delivery(git_repo, monkeypatch):
    supervisor, record, item = _prepared(git_repo, "import sys; print('test failed'); sys.exit(3)")
    delivered = []
    monkeypatch.setattr(pr, "publish_pull_request", lambda *args: delivered.append(args))

    with run_owner(supervisor.paths):
        asyncio.run(finalize_item(supervisor.paths, record, item, open_pr=True))

    assert record.status == Status.FAILED
    assert record.verification is None
    assert record.phase == "verification"
    assert record.attempts[0].commands[0].exit_code == 3
    assert record.candidate_sha == git.head_commit(record.worktree)
    assert delivered == []


def test_finalize_item_rejects_source_changes_made_by_checks(git_repo, monkeypatch):
    supervisor, record, item = _prepared(git_repo, "from pathlib import Path; Path('change.txt').write_text('changed')")
    delivered = []
    monkeypatch.setattr(pr, "publish_pull_request", lambda *args: delivered.append(args))

    with run_owner(supervisor.paths), pytest.raises(git.GitError, match="changes after verification"):
        asyncio.run(finalize_item(supervisor.paths, record, item, open_pr=True))

    assert record.verification is None
    assert delivered == []
    assert (Path(record.worktree) / "change.txt").read_text() == "changed"


def test_finalize_item_delivers_only_the_checked_commit_and_summary(git_repo, monkeypatch):
    supervisor, record, item = _prepared(git_repo, "print('passed')")
    delivered = []

    def publish(*args):
        delivered.append(args)
        return "https://example.test/pr/1"

    monkeypatch.setattr(pr, "publish_pull_request", publish)

    with run_owner(supervisor.paths):
        asyncio.run(finalize_item(supervisor.paths, record, item, open_pr=True))

    assert record.status == Status.DONE
    assert record.delivery_sha == record.verification.commit_sha
    assert delivered[0][8] == record.verification.commit_sha
    assert delivered[0][5] == record.pr_body
    assert delivered[0][11] == 1
    assert record.pr_url == "https://example.test/pr/1"


def test_finalize_item_rechecks_after_candidate_changes(git_repo):
    supervisor, record, item = _prepared(git_repo, "print('passed')")
    with run_owner(supervisor.paths):
        asyncio.run(finalize_item(supervisor.paths, record, item, open_pr=False))
        first = record.verification.commit_sha
        (Path(record.worktree) / "change.txt").write_text("second candidate")
        record.begin_attempt("finalize")
        asyncio.run(finalize_item(supervisor.paths, record, item, open_pr=False))

    assert record.verification.commit_sha != first
    assert record.verification.attempt == 2
    assert record.attempts[1].commands[0].status == "passed"


def test_finalize_item_rechecks_when_receipt_tree_does_not_match(git_repo):
    supervisor, record, item = _prepared(git_repo, "print('passed')")
    with run_owner(supervisor.paths):
        asyncio.run(finalize_item(supervisor.paths, record, item, open_pr=False))
        record.verification.tree_sha = "f" * 40
        record.begin_attempt("finalize")
        asyncio.run(finalize_item(supervisor.paths, record, item, open_pr=False))

    assert record.verification.tree_sha == git.commit_tree(record.worktree, record.candidate_sha)
    assert record.verification.attempt == 2
    assert record.attempts[1].commands[0].status == "passed"


@pytest.mark.parametrize("thread_fails", [False, True])
def test_finalize_item_cancellation_waits_for_vcs_thread(git_repo, monkeypatch, thread_fails):
    supervisor, record, item = _prepared(git_repo, "print('passed')")
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def has_changes(*args):
        started.set()
        release.wait(timeout=2)
        finished.set()
        if thread_fails:
            raise git.GitError("Git operation failed during cancellation.")
        return True

    monkeypatch.setattr(git, "has_changes", has_changes)

    async def cancel_finalization():
        with run_owner(supervisor.paths):
            task = asyncio.create_task(finalize_item(supervisor.paths, record, item, open_pr=False))
            assert await asyncio.to_thread(started.wait, 2)
            timer = threading.Timer(0.1, release.set)
            timer.start()
            task.cancel()
            try:
                with pytest.raises(git.GitError if thread_fails else asyncio.CancelledError):
                    await task
            finally:
                release.set()
                timer.join()
            assert finished.is_set()

    asyncio.run(cancel_finalization())
