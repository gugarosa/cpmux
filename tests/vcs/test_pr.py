# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import subprocess

import pytest

from cpmux.vcs.pr import (
    PR_DRAFT_FILENAME,
    PRError,
    commit_all,
    gh_env,
    push_branch,
    read_pr_draft,
)


def _leave_repo_unchanged(repo):
    return "noop", _skip_commit_log_assertion


def _add_new_file(repo):
    (repo / "new.txt").write_text("hello")
    return "add new file", _assert_commit_logged


def _assert_commit_logged(repo):
    log = subprocess.run(["git", "log", "--pretty=%s"], cwd=repo, check=True, capture_output=True, text=True)
    assert "add new file" in log.stdout


def _skip_commit_log_assertion(repo):
    pass


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


@pytest.mark.parametrize(
    ("mutate", "expected_bool"),
    [
        pytest.param(_leave_repo_unchanged, False, id="no-changes"),
        pytest.param(_add_new_file, True, id="new-file"),
    ],
)
def test_commit_all_reports_change_state(git_repo, mutate, expected_bool):
    repo = git_repo
    message, verify_commit = mutate(repo)
    assert commit_all(repo, message, gh_env(strip_token=False)) is expected_bool
    verify_commit(repo)


def test_push_branch_creates_branch_on_remote(git_repo):
    repo = git_repo
    bare = git_repo / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)

    push_branch(repo, "origin", "feature/x", gh_env(strip_token=False))

    subprocess.run(
        ["git", f"--git-dir={bare}", "rev-parse", "--verify", "refs/heads/feature/x"],
        check=True,
        capture_output=True,
    )


def test_push_branch_raises_on_bogus_remote(git_repo):
    repo = git_repo
    with pytest.raises(PRError):
        push_branch(repo, "nope", "feature/x", gh_env(strip_token=False))


def test_read_pr_draft_parses_title_and_body_and_consumes_file(tmp_path):
    (tmp_path / PR_DRAFT_FILENAME).write_text("# Add pagination\n\nAdds pagination to the feed.\n")

    assert read_pr_draft(tmp_path) == ("Add pagination", "Adds pagination to the feed.")
    assert not (tmp_path / PR_DRAFT_FILENAME).exists()


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        pytest.param(None, (None, None), id="missing-file"),
        pytest.param("   \n", (None, None), id="empty-file"),
        pytest.param("plain title\n\nbody", ("plain title", "body"), id="heading-less-first-line"),
        pytest.param("# Only a title\n", ("Only a title", None), id="title-without-body"),
        pytest.param(f"# {'x' * 300}\n\nbody", (None, "body"), id="oversized-title-falls-back"),
    ],
)
def test_read_pr_draft_handles_edge_cases(tmp_path, contents, expected):
    if contents is not None:
        (tmp_path / PR_DRAFT_FILENAME).write_text(contents)

    assert read_pr_draft(tmp_path) == expected


def test_read_pr_draft_ignores_symlinks(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("# Secret\n\nleaked")
    (tmp_path / PR_DRAFT_FILENAME).symlink_to(secret)

    assert read_pr_draft(tmp_path) == (None, None)
    assert secret.exists()
    assert not (tmp_path / PR_DRAFT_FILENAME).exists()
