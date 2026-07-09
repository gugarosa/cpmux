# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from pathlib import Path

from cmux.config import Plan
from cmux.engine.supervisor import Options, Supervisor
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
