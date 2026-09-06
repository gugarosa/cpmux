# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import shlex
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cpmux.config import Plan
from cpmux.engine import interact
from cpmux.engine.daemon import stop
from cpmux.engine.review import (
    StaleRevisionError,
    diff_snapshot,
    run_feedback,
    run_finalization,
    run_verification,
)
from cpmux.engine.store import RunManifest, RunPaths, SessionRecord, VerificationReceipt
from cpmux.events import SessionState, Status
from cpmux.vcs import git, pr


def _run(git_repo, command):
    return subprocess.run(command, cwd=git_repo, check=True, capture_output=True, text=True).stdout.strip()


def _prepared(git_repo, checks=None, open_pr=False):
    git.ignore_runtime_state(git_repo)
    branch = _run(git_repo, ["git", "symbolic-ref", "--short", "HEAD"])
    base_sha = git.head_commit(git_repo)
    item = Plan.model_validate(
        {
            "items": [
                {
                    "id": "a",
                    "prompt": "x",
                    "branch": branch,
                    "checks": checks or [],
                }
            ]
        }
    ).resolve()[0]
    paths = RunPaths(git_repo, "run1")
    paths.write_manifest(
        RunManifest(
            run_id="run1",
            repo_root=str(git_repo),
            config_path="",
            item_keys=["a"],
            resolved=[item],
            open_pr=open_pr,
            strip_github_token=False,
        )
    )
    record = SessionRecord(
        key="a",
        name="a",
        slug="a",
        branch=branch,
        base="main",
        model="model",
        session_id="sid",
        worktree=str(git_repo),
        base_sha=base_sha,
        status=Status.DONE,
        agent_complete=True,
    )
    paths.write_record(record)
    return paths, record, item


def test_diff_snapshot_includes_all_non_ignored_source_without_mutation(git_repo):
    paths, record, _ = _prepared(git_repo)
    (git_repo / "README.md").write_text("staged")
    _run(git_repo, ["git", "add", "README.md"])
    (git_repo / "README.md").write_text("worktree")
    (git_repo / "new.py").write_text("print('new')\n")
    exclude = git_repo / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "\n*.artifact\n")
    (git_repo / "build.artifact").write_text("ignored")
    before = _run(git_repo, ["git", "status", "--porcelain=v1", "--untracked-files=all"])
    index_mtime = (git_repo / ".git" / "index").stat().st_mtime_ns

    snapshot = diff_snapshot(paths, record)

    assert snapshot.base_sha == record.base_sha
    assert snapshot.head_sha == record.base_sha
    assert "README.md" in snapshot.text
    assert "+worktree" in snapshot.text
    assert "new.py" in snapshot.text
    assert "+print('new')" in snapshot.text
    assert "build.artifact" not in snapshot.text
    assert (git_repo / ".git" / "index").stat().st_mtime_ns == index_mtime
    assert _run(git_repo, ["git", "status", "--porcelain=v1", "--untracked-files=all"]) == before
    with pytest.raises(FrozenInstanceError):
        snapshot.revision = "changed"


def test_diff_snapshot_revision_detects_index_and_untracked_content(git_repo):
    paths, record, _ = _prepared(git_repo)
    (git_repo / "README.md").write_text("changed")
    (git_repo / "new.py").write_text("one")
    unstaged = diff_snapshot(paths, record)

    _run(git_repo, ["git", "add", "README.md"])
    staged = diff_snapshot(paths, record)
    (git_repo / "new.py").write_text("two")
    untracked_changed = diff_snapshot(paths, record)

    assert staged.text == unstaged.text
    assert staged.revision != unstaged.revision
    assert untracked_changed.revision != staged.revision


def test_diff_snapshot_reports_missing_worktree_and_base(git_repo):
    paths, record, _ = _prepared(git_repo)
    record.worktree = str(git_repo / "missing")
    with pytest.raises(FileNotFoundError, match="worktree"):
        diff_snapshot(paths, record)

    record.worktree = str(git_repo)
    record.base_sha = "f" * 40
    with pytest.raises(git.GitError, match="rev-parse"):
        diff_snapshot(paths, record)


def test_run_feedback_rejects_stale_revision_before_native_execution(git_repo, monkeypatch):
    paths, record, _ = _prepared(git_repo)
    (git_repo / "README.md").write_text("reviewed")
    snapshot = diff_snapshot(paths, record)
    record.begin_attempt("initial")
    record.status = Status.DONE
    record.finish_attempt()
    record.verification = VerificationReceipt(
        attempt=1,
        commit_sha=record.base_sha,
        tree_sha=git.commit_tree(git_repo, record.base_sha),
        check_fingerprint="configured",
    )
    paths.write_record(record)
    previous = record.model_copy(deep=True)
    (git_repo / "README.md").write_text("changed after review")
    calls = []

    async def run(self, *args, **kwargs):
        calls.append(self)
        return SessionState(status=Status.DONE, exit_code=0)

    monkeypatch.setattr(interact.SessionRunner, "run", run)

    with pytest.raises(StaleRevisionError, match="stale"):
        asyncio.run(run_feedback(paths, record, "repair it", snapshot.revision))

    assert calls == []
    assert record == previous
    assert paths.read_record("a") == previous


def test_run_feedback_uses_shared_followup_with_optional_target(git_repo, monkeypatch):
    paths, record, _ = _prepared(git_repo)
    (git_repo / "README.md").write_text("reviewed")
    snapshot = diff_snapshot(paths, record)
    prompts = []

    def argv(session_id, worktree, model, flags, message):
        prompts.append(message)
        return ["copilot"]

    async def run(self, *args, **kwargs):
        return SessionState(status=Status.DONE, exit_code=0)

    monkeypatch.setattr(interact, "followup_argv", argv)
    monkeypatch.setattr(interact.SessionRunner, "run", run)

    state = asyncio.run(run_feedback(paths, record, "repair it", snapshot.revision, file_path="README.md", line=1))

    assert state.status == Status.DONE
    assert snapshot.revision in prompts[0]
    assert "README.md" in prompts[0]
    assert "line 1" in prompts[0]
    assert record.agent_complete is True


def test_run_verification_commits_and_checks_without_delivery(git_repo, monkeypatch):
    paths, record, _ = _prepared(
        git_repo, checks=[shlex.join([sys.executable, "-c", "print('checked')"])], open_pr=True
    )
    (git_repo / "change.py").write_text("value = 1\n")
    delivered = []
    monkeypatch.setattr(pr, "publish_pull_request", lambda *args, **kwargs: delivered.append(args))

    asyncio.run(run_verification(paths, record))

    assert record.status == Status.DONE
    assert record.verification is not None
    assert record.verification.commit_sha == git.head_commit(git_repo)
    assert record.attempts[-1].mode == "verify"
    assert record.attempts[-1].commands[0].status == "passed"
    assert delivered == []


def test_run_finalization_reuses_receipt_and_uses_recorded_stacked_base(git_repo, monkeypatch):
    paths, record, _ = _prepared(
        git_repo, checks=[shlex.join([sys.executable, "-c", "print('checked')"])], open_pr=True
    )
    (git_repo / "change.py").write_text("value = 1\n")
    asyncio.run(run_verification(paths, record))
    receipt = record.verification
    record.base = "stacked-parent"
    paths.write_record(record)
    delivered = []

    def publish(*args, **kwargs):
        delivered.append(args)
        return "https://github.com/example/repo/pull/1"

    monkeypatch.setattr(pr, "publish_pull_request", publish)

    asyncio.run(run_finalization(paths, record))

    assert record.status == Status.DONE
    assert record.verification == receipt
    assert record.attempts[-1].mode == "finalize"
    assert record.attempts[-1].commands == []
    assert delivered[0][2] == "stacked-parent"
    assert delivered[0][8] == receipt.commit_sha


def test_run_verification_without_checks_does_not_claim_verification(git_repo):
    paths, record, _ = _prepared(git_repo)
    (git_repo / "change.py").write_text("value = 1\n")

    asyncio.run(run_verification(paths, record))

    assert record.status == Status.DONE
    assert record.verification is None
    assert record.candidate_sha == git.head_commit(git_repo)


def test_run_verification_explicitly_reruns_checks_for_an_unchanged_candidate(git_repo):
    paths, record, _ = _prepared(git_repo, checks=[shlex.join([sys.executable, "-c", "print('checked')"])])
    (git_repo / "change.py").write_text("value = 1\n")
    asyncio.run(run_verification(paths, record))
    first = record.verification

    asyncio.run(run_verification(paths, record))

    assert record.verification.commit_sha == first.commit_sha
    assert record.verification.attempt == 2
    assert record.attempts[-1].commands[0].status == "passed"


def test_diff_snapshot_handles_binary_and_non_utf8_content_without_color(git_repo):
    paths, record, _ = _prepared(git_repo)
    _run(git_repo, ["git", "config", "color.ui", "always"])
    (git_repo / "README.md").write_bytes(b"caf\xe9\n")
    (git_repo / "binary.dat").write_bytes(b"\x00\xff\xfe")

    snapshot = diff_snapshot(paths, record)

    assert "\x1b[" not in snapshot.text
    assert b"+caf\xe9" in snapshot.text.encode("utf-8", errors="surrogateescape")
    assert "GIT binary patch" in snapshot.text
    assert "binary.dat" in snapshot.text


def test_run_verification_honors_whole_run_stop_without_abandoning_its_child(git_repo):
    paths, record, _ = _prepared(git_repo, checks=[f"'{sys.executable}' -c 'import time; time.sleep(60)'"])
    (git_repo / "change.py").write_text("value = 1\n")

    async def scenario():
        task = asyncio.create_task(run_verification(paths, record))
        try:
            async with asyncio.timeout(5):
                while (current := paths.read_record("a")).pid is None:
                    await asyncio.sleep(0.01)
            assert await asyncio.to_thread(stop, paths, [current]) == 1
            await asyncio.wait_for(task, 5)
            assert not task.cancelled()
            assert task.cancelling() == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert record.status == Status.KILLED
    assert record.pid is None
    assert record.verification is None
    assert not paths.session_owner("a").exists()
