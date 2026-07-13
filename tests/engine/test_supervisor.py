# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
from pathlib import Path

from cmux.config import Plan
from cmux.engine.store import RunManifest
from cmux.engine.supervisor import Options, Supervisor
from cmux.events import SessionState, Status
from cmux.vcs import git


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
    sup = Supervisor.create(_plan(), str(repo), Options())

    assert isinstance(sup.run_id, str)
    assert sup.run_id
    assert len(sup.resolved) == 2


def test_create_uses_plan_concurrency_when_option_none(git_repo):
    repo = git_repo
    sup = Supervisor.create(_plan(), str(repo), Options())
    assert sup.concurrency == 3


def test_prepare_creates_one_record_per_item(git_repo):
    repo = git_repo
    sup = Supervisor.create(_plan(), str(repo), Options())
    sup.prepare()

    assert len(sup.records) == len(sup.resolved)
    for item in sup.resolved:
        assert item.key in sup.records


def test_prepare_records_have_session_and_worktree(git_repo):
    repo = git_repo
    sup = Supervisor.create(_plan(), str(repo), Options())
    sup.prepare()

    for record in sup.records.values():
        assert record.session_id
        assert record.base_sha
        assert Path(record.worktree).exists()


def test_prepare_writes_manifest(git_repo):
    repo = git_repo
    sup = Supervisor.create(_plan(), str(repo), Options())
    sup.prepare()
    assert sup.paths.manifest.exists()


def test_prepare_records_config_path_for_provenance(git_repo):
    sup = Supervisor.create(_plan(), str(git_repo), Options(), "issues.yaml")
    sup.prepare()
    manifest = RunManifest.model_validate_json(sup.paths.manifest.read_text())
    assert manifest.config_path == "issues.yaml"


def test_prepare_creates_branch_per_item(git_repo):
    repo = git_repo
    sup = Supervisor.create(_plan(), str(repo), Options())
    sup.prepare()

    for record in sup.records.values():
        assert git.branch_exists(repo, record.branch) is True


def test_from_run_reloads_resolved_and_records(git_repo):
    repo = git_repo
    sup = Supervisor.create(_plan(), str(repo), Options())
    sup.prepare()

    loaded = Supervisor.from_run(str(repo), sup.run_id)

    assert [item.key for item in loaded.resolved] == [item.key for item in sup.resolved]
    assert sorted(loaded.records) == sorted(sup.records)
    for key, record in sup.records.items():
        assert loaded.records[key].branch == record.branch


def test_commit_local_commits_worktree_and_marks_done(git_repo):
    sup = Supervisor.create(_plan(), str(git_repo), Options(open_pr=False))
    sup.prepare()
    item = sup.resolved[0]
    record = sup.records[item.key]
    (Path(record.worktree) / "new.txt").write_text("hi")

    asyncio.run(sup._commit_local(item, record))

    assert record.status == Status.DONE
    assert git.run_git(["status", "--porcelain"], cwd=record.worktree).stdout.strip() == ""
    assert git.run_git(["rev-parse", "HEAD"], cwd=record.worktree).stdout.strip() != record.base_sha


def test_commit_local_marks_no_changes_when_clean(git_repo):
    sup = Supervisor.create(_plan(), str(git_repo), Options(open_pr=False))
    sup.prepare()
    item = sup.resolved[0]
    record = sup.records[item.key]

    asyncio.run(sup._commit_local(item, record))

    assert record.status == Status.NO_CHANGES


def test_on_update_persists_live_status_to_disk(git_repo):
    sup = Supervisor.create(_plan(), str(git_repo), Options())
    sup.prepare()
    key = sup.resolved[0].key
    assert sup.paths.read_record(key).status == Status.PENDING

    sup._on_update(key, SessionState(status=Status.RUNNING), {})

    assert sup.paths.read_record(key).status == Status.RUNNING


def test_on_update_maps_session_done_to_finalizing(git_repo):
    sup = Supervisor.create(_plan(), str(git_repo), Options())
    sup.prepare()
    key = sup.resolved[0].key

    sup._on_update(key, SessionState(status=Status.DONE), {})

    assert sup.paths.read_record(key).status == Status.FINALIZING


def test_finalize_marks_failed_on_unexpected_error(git_repo):
    sup = Supervisor.create(_plan(), str(git_repo), Options())
    sup.prepare()
    item = sup.resolved[0]
    record = sup.records[item.key]

    async def _boom(_item, _record):
        raise OSError("gh not found")

    sup._open_pr = _boom
    asyncio.run(sup._finalize(item, record))

    assert record.status == Status.FAILED
    assert record.error
