# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import pytest

from cpmux.vcs import git
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
    _, sha = resolve_base(git_repo, "origin", "main")
    worktree = git_repo / "wt"
    add_worktree(git_repo, worktree, "feature/x", sha)

    assert has_changes(worktree, sha) is False


def test_has_changes_true_for_untracked_file(git_repo):
    _, sha = resolve_base(git_repo, "origin", "main")
    worktree = git_repo / "wt"
    add_worktree(git_repo, worktree, "feature/x", sha)
    (worktree / "new.txt").write_text("y")

    assert has_changes(worktree, sha) is True


def test_remove_worktree_removes_existing_worktree(git_repo):
    _, sha = resolve_base(git_repo, "origin", "main")
    worktree = git_repo / "wt"
    add_worktree(git_repo, worktree, "feature/x", sha)

    assert remove_worktree(git_repo, worktree) is True
    assert not worktree.exists()


def test_remove_worktree_true_for_absent_worktree(git_repo):
    assert remove_worktree(git_repo, git_repo / "never-existed") is True


def test_run_git_raises_on_bad_subcommand(git_repo):
    with pytest.raises(GitError):
        run_git(["not-a-real-subcommand"], cwd=git_repo)


def test_run_git_raises_when_git_missing(tmp_path, monkeypatch):
    def _missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("cpmux.vcs.git.subprocess.run", _missing)
    with pytest.raises(GitError):
        run_git(["status"], cwd=tmp_path)


def test_require_paths_exist_passes_for_present_paths(git_repo):
    require_paths_exist(git_repo, ["README.md"])


def test_require_paths_exist_raises_for_missing_path(git_repo):
    with pytest.raises(GitError):
        require_paths_exist(git_repo, ["nope-dir"])


def test_resolve_base_warns_when_base_unresolved(git_repo, monkeypatch):
    warnings = []
    monkeypatch.setattr(git.logger, "warning", lambda message, *args: warnings.append(message))
    base, sha = resolve_base(git_repo, "origin", "definitely-missing")
    assert base == "definitely-missing"
    assert len(sha) == 40
    assert warnings and "not found" in warnings[0]


def test_ignore_runtime_state_preserves_tracked_files_and_is_idempotent(git_repo):
    before = run_git(["status", "--porcelain"], git_repo).stdout
    git.ignore_runtime_state(git_repo)
    git.ignore_runtime_state(git_repo)
    runtime = git_repo / ".cpmux"
    runtime.mkdir()
    (runtime / "local-secret.json").write_text("{}")

    assert run_git(["status", "--porcelain"], git_repo).stdout == before
    assert not (git_repo / ".gitignore").exists()
    exclude = git_repo / ".git" / "info" / "exclude"
    assert exclude.read_text().splitlines().count("/.cpmux/") == 1


@pytest.mark.parametrize(
    ("lockfile", "command"),
    [
        ("pnpm-lock.yaml", ["pnpm", "install", "--frozen-lockfile"]),
        ("package-lock.json", ["npm", "ci"]),
        ("yarn.lock", ["yarn", "install", "--frozen-lockfile"]),
    ],
)
def test_dependency_install_command_preserves_lockfile_selection(tmp_path, lockfile, command):
    assert git.dependency_install_command(tmp_path) is None
    (tmp_path / lockfile).touch()
    assert git.dependency_install_command(tmp_path) == command
