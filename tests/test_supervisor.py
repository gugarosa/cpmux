# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import subprocess
from pathlib import Path

from cmux import gitutil
from cmux.config import Plan
from cmux.supervisor import Options, Supervisor


def _repo(tmp_path):
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _plan():
    return Plan.model_validate({"system": "S", "defaults": {"concurrency": 3}, "items": ["fix a", "fix b"]})


def test_options_defaults_are_conservative():
    options = Options()
    assert options.concurrency is None
    assert options.open_pr is True
    assert options.strip_github_token is True
    assert options.deps_override is None


def test_create_builds_supervisor_from_plan(tmp_path):
    repo = _repo(tmp_path)
    sup = Supervisor.create(_plan(), str(repo), Options())
    assert isinstance(sup.run_id, str)
    assert sup.run_id
    assert len(sup.resolved) == 2


def test_create_uses_plan_concurrency_when_option_none(tmp_path):
    repo = _repo(tmp_path)
    sup = Supervisor.create(_plan(), str(repo), Options())
    assert sup.concurrency == 3


def test_prepare_creates_one_record_per_item(tmp_path):
    repo = _repo(tmp_path)
    sup = Supervisor.create(_plan(), str(repo), Options())
    sup.prepare()
    assert len(sup.records) == len(sup.resolved)
    for item in sup.resolved:
        assert item.key in sup.records


def test_prepare_records_have_session_and_worktree(tmp_path):
    repo = _repo(tmp_path)
    sup = Supervisor.create(_plan(), str(repo), Options())
    sup.prepare()
    for record in sup.records.values():
        assert record.session_id
        assert record.base_sha
        assert Path(record.worktree).exists()


def test_prepare_writes_manifest(tmp_path):
    repo = _repo(tmp_path)
    sup = Supervisor.create(_plan(), str(repo), Options())
    sup.prepare()
    assert sup.paths.manifest.exists()


def test_prepare_creates_branch_per_item(tmp_path):
    repo = _repo(tmp_path)
    sup = Supervisor.create(_plan(), str(repo), Options())
    sup.prepare()
    for record in sup.records.values():
        assert gitutil.branch_exists(repo, record.branch) is True


def test_from_run_reloads_resolved_and_records(tmp_path):
    repo = _repo(tmp_path)
    sup = Supervisor.create(_plan(), str(repo), Options())
    sup.prepare()

    loaded = Supervisor.from_run(str(repo), sup.run_id)
    assert [item.key for item in loaded.resolved] == [item.key for item in sup.resolved]
    assert sorted(loaded.records) == sorted(sup.records)
    for key, record in sup.records.items():
        assert loaded.records[key].branch == record.branch
