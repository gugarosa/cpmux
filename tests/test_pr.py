# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import subprocess

import pytest

from cmux.pr import PRError, commit_all, gh_env, push_branch


def _repo(tmp_path):
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_gh_env_strips_tokens_when_requested(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("GH_TOKEN", "y")
    env = gh_env(strip_token=True)
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert env["GH_PROMPT_DISABLED"] == "1"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"


def test_gh_env_keeps_token_when_not_stripping(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    env = gh_env(strip_token=False)
    assert env["GITHUB_TOKEN"] == "x"


def test_commit_all_returns_false_without_changes(tmp_path):
    repo = _repo(tmp_path)
    assert commit_all(repo, "noop", gh_env(strip_token=False)) is False


def test_commit_all_commits_new_file(tmp_path):
    repo = _repo(tmp_path)
    (repo / "new.txt").write_text("hello")
    assert commit_all(repo, "add new file", gh_env(strip_token=False)) is True
    log = subprocess.run(["git", "log", "--pretty=%s"], cwd=repo, check=True, capture_output=True, text=True)
    assert "add new file" in log.stdout


def test_push_branch_creates_branch_on_remote(tmp_path):
    repo = _repo(tmp_path)
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)

    push_branch(repo, "origin", "feature/x", gh_env(strip_token=False))

    subprocess.run(
        ["git", f"--git-dir={bare}", "rev-parse", "--verify", "refs/heads/feature/x"],
        check=True,
        capture_output=True,
    )


def test_push_branch_raises_on_bogus_remote(tmp_path):
    repo = _repo(tmp_path)
    with pytest.raises(PRError):
        push_branch(repo, "nope", "feature/x", gh_env(strip_token=False))
