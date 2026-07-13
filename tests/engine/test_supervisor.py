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


def test_commit_local_commits_worktree_and_marks_done(git_repo):
    supervisor = Supervisor.create(_plan(), str(git_repo), Options(open_pr=False))
    supervisor.prepare()
    item = supervisor.resolved[0]
    record = supervisor.records[item.key]
    (Path(record.worktree) / "new.txt").write_text("hi")

    asyncio.run(supervisor._commit_local(item, record))

    assert record.status == Status.DONE
    assert git.run_git(["status", "--porcelain"], cwd=record.worktree).stdout.strip() == ""
    assert git.run_git(["rev-parse", "HEAD"], cwd=record.worktree).stdout.strip() != record.base_sha


def test_commit_local_marks_no_changes_when_clean(git_repo):
    supervisor = Supervisor.create(_plan(), str(git_repo), Options(open_pr=False))
    supervisor.prepare()
    item = supervisor.resolved[0]
    record = supervisor.records[item.key]

    asyncio.run(supervisor._commit_local(item, record))

    assert record.status == Status.NO_CHANGES


def test_on_update_persists_live_status_to_disk(git_repo):
    supervisor = Supervisor.create(_plan(), str(git_repo), Options())
    supervisor.prepare()
    key = supervisor.resolved[0].key
    assert supervisor.paths.read_record(key).status == Status.PENDING

    supervisor._on_update(key, SessionState(status=Status.RUNNING), {})

    assert supervisor.paths.read_record(key).status == Status.RUNNING


def test_on_update_maps_session_done_to_finalizing(git_repo):
    supervisor = Supervisor.create(_plan(), str(git_repo), Options())
    supervisor.prepare()
    key = supervisor.resolved[0].key

    supervisor._on_update(key, SessionState(status=Status.DONE), {})

    assert supervisor.paths.read_record(key).status == Status.FINALIZING


def test_finalize_marks_failed_on_unexpected_error(git_repo):
    supervisor = Supervisor.create(_plan(), str(git_repo), Options())
    supervisor.prepare()
    item = supervisor.resolved[0]
    record = supervisor.records[item.key]

    async def _boom(_item, _record):
        raise OSError("gh not found")

    supervisor._open_pr = _boom
    asyncio.run(supervisor._finalize(item, record))

    assert record.status == Status.FAILED
    assert record.error


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
