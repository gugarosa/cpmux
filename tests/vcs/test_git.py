# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import pytest

from cpmux.vcs.git import (
    GitError,
    add_worktree,
    branch_exists,
    has_changes,
    is_git_repo,
    remove_worktree,
    repo_root,
    require_paths_exist,
    resolve_base,
    run_git,
)


def _leave_worktree_clean(worktree):
    pass


def _add_untracked_file(worktree):
    (worktree / "new.txt").write_text("y")


def _existing_worktree(repo):
    _, sha = resolve_base(repo, "origin", "main")
    worktree = repo / "wt"
    add_worktree(repo, worktree, "feature/x", sha)
    return worktree, _assert_worktree_removed


def _absent_worktree(repo):
    return repo / "never-existed", _skip_path_assertion


def _assert_worktree_removed(worktree):
    assert not worktree.exists()


def _skip_path_assertion(worktree):
    pass


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


@pytest.mark.parametrize(
    ("mutate_worktree", "expected"),
    [
        pytest.param(_leave_worktree_clean, False, id="clean-worktree"),
        pytest.param(_add_untracked_file, True, id="untracked-file"),
    ],
)
def test_has_changes_reports_worktree_state(git_repo, mutate_worktree, expected):
    repo = git_repo
    _, sha = resolve_base(repo, "origin", "main")
    worktree = git_repo / "wt"
    add_worktree(repo, worktree, "feature/x", sha)
    mutate_worktree(worktree)
    assert has_changes(worktree, sha) is expected


@pytest.mark.parametrize(
    ("path_state", "expected"),
    [
        pytest.param(_existing_worktree, True, id="existing-worktree"),
        pytest.param(_absent_worktree, True, id="absent-worktree"),
    ],
)
def test_remove_worktree_handles_path_state(git_repo, path_state, expected):
    worktree, verify_path = path_state(git_repo)
    assert remove_worktree(git_repo, worktree) is expected
    verify_path(worktree)


def test_run_git_raises_on_bad_subcommand(git_repo):
    with pytest.raises(GitError):
        run_git(["not-a-real-subcommand"], cwd=git_repo)


def test_require_paths_exist_passes_for_present_paths(git_repo):
    require_paths_exist(git_repo, ["README.md"])


def test_require_paths_exist_raises_for_missing_path(git_repo):
    with pytest.raises(GitError):
        require_paths_exist(git_repo, ["nope-dir"])


def test_resolve_base_warns_when_base_unresolved(git_repo, monkeypatch):
    from cpmux.vcs import git

    warnings = []
    monkeypatch.setattr(git.logger, "warning", lambda message, *args: warnings.append(message))
    base, sha = resolve_base(git_repo, "origin", "definitely-missing")
    assert base == "definitely-missing"
    assert len(sha) == 40
    assert warnings and "not found" in warnings[0]
