# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import subprocess

import pytest

from cmux.gitutil import (
    GitError,
    add_worktree,
    branch_exists,
    has_changes,
    is_git_repo,
    remove_worktree,
    repo_root,
    resolve_base,
    run_git,
)


def _repo(tmp_path):
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)
    return tmp_path


def test_is_git_repo_true_for_repo(tmp_path):
    assert is_git_repo(_repo(tmp_path)) is True


def test_is_git_repo_false_for_non_repo(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert is_git_repo(empty) is False


def test_repo_root_matches_repo_path(tmp_path):
    repo = _repo(tmp_path)
    assert repo_root(repo).resolve() == repo.resolve()


def test_repo_root_raises_for_non_repo(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(GitError):
        repo_root(empty)


def test_resolve_base_falls_back_to_head(tmp_path):
    repo = _repo(tmp_path)
    base, sha = resolve_base(repo, "origin", "main")
    assert base == "main"
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_branch_exists_false_for_missing_branch(tmp_path):
    assert branch_exists(_repo(tmp_path), "nope") is False


def test_add_worktree_creates_dir_and_branch(tmp_path):
    repo = _repo(tmp_path)
    _, sha = resolve_base(repo, "origin", "main")
    add_worktree(repo, tmp_path / "wt", "feature/x", sha)
    assert branch_exists(repo, "feature/x") is True
    assert (tmp_path / "wt").is_dir()


def test_has_changes_false_for_clean_worktree(tmp_path):
    repo = _repo(tmp_path)
    _, sha = resolve_base(repo, "origin", "main")
    worktree = tmp_path / "wt"
    add_worktree(repo, worktree, "feature/x", sha)
    assert has_changes(worktree, sha) is False


def test_has_changes_true_after_new_file(tmp_path):
    repo = _repo(tmp_path)
    _, sha = resolve_base(repo, "origin", "main")
    worktree = tmp_path / "wt"
    add_worktree(repo, worktree, "feature/x", sha)
    (worktree / "new.txt").write_text("y")
    assert has_changes(worktree, sha) is True


def test_remove_worktree_removes_dir(tmp_path):
    repo = _repo(tmp_path)
    _, sha = resolve_base(repo, "origin", "main")
    worktree = tmp_path / "wt"
    add_worktree(repo, worktree, "feature/x", sha)
    remove_worktree(repo, worktree)
    assert not worktree.exists()


def test_run_git_raises_on_bad_subcommand(tmp_path):
    with pytest.raises(GitError):
        run_git(["not-a-real-subcommand"], cwd=_repo(tmp_path))
