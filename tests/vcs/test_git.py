# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import pytest

from cmux.vcs.git import (
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


def test_is_git_repo_true_for_repo(git_repo):
    assert is_git_repo(git_repo) is True


def test_is_git_repo_false_for_non_repo(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert is_git_repo(empty) is False


def test_repo_root_matches_repo_path(git_repo):
    repo = git_repo
    assert repo_root(repo).resolve() == repo.resolve()


def test_repo_root_raises_for_non_repo(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(GitError):
        repo_root(empty)


def test_resolve_base_falls_back_to_head(git_repo):
    repo = git_repo
    base, sha = resolve_base(repo, "origin", "main")
    assert base == "main"
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_branch_exists_false_for_missing_branch(git_repo):
    assert branch_exists(git_repo, "nope") is False


def test_add_worktree_creates_dir_and_branch(git_repo):
    repo = git_repo
    _, sha = resolve_base(repo, "origin", "main")
    add_worktree(repo, git_repo / "wt", "feature/x", sha)
    assert branch_exists(repo, "feature/x") is True
    assert (git_repo / "wt").is_dir()


def test_has_changes_false_for_clean_worktree(git_repo):
    repo = git_repo
    _, sha = resolve_base(repo, "origin", "main")
    worktree = git_repo / "wt"
    add_worktree(repo, worktree, "feature/x", sha)
    assert has_changes(worktree, sha) is False


def test_has_changes_true_after_new_file(git_repo):
    repo = git_repo
    _, sha = resolve_base(repo, "origin", "main")
    worktree = git_repo / "wt"
    add_worktree(repo, worktree, "feature/x", sha)
    (worktree / "new.txt").write_text("y")
    assert has_changes(worktree, sha) is True


def test_remove_worktree_removes_dir(git_repo):
    repo = git_repo
    _, sha = resolve_base(repo, "origin", "main")
    worktree = git_repo / "wt"
    add_worktree(repo, worktree, "feature/x", sha)
    remove_worktree(repo, worktree)
    assert not worktree.exists()


def test_run_git_raises_on_bad_subcommand(git_repo):
    with pytest.raises(GitError):
        run_git(["not-a-real-subcommand"], cwd=git_repo)
