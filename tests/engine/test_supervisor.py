# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from pathlib import Path

from cpmux.config import Plan
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
