# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import subprocess

import pytest

from cpmux.vcs import pr
from cpmux.vcs.pr import (
    PR_DRAFT_FILENAME,
    PRError,
    commit_all,
    existing_pr_url,
    gh_env,
    open_pull_request,
    push_branch,
    read_pr_draft,
)


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


def test_commit_all_returns_false_when_nothing_changed(git_repo):
    assert commit_all(git_repo, "noop", gh_env(strip_token=False)) is False


def test_commit_all_commits_and_returns_true_on_change(git_repo):
    (git_repo / "new.txt").write_text("hello")

    assert commit_all(git_repo, "add new file", gh_env(strip_token=False)) is True

    log = subprocess.run(["git", "log", "--pretty=%s"], cwd=git_repo, check=True, capture_output=True, text=True)
    assert "add new file" in log.stdout


def test_commit_all_reports_staging_failure_instead_of_no_changes(git_repo):
    (git_repo / "new.txt").write_text("uncommitted work")
    (git_repo / ".git" / "index.lock").touch()

    with pytest.raises(PRError, match="git add"):
        commit_all(git_repo, "must not report success", gh_env(strip_token=False))

    assert (git_repo / "new.txt").read_text() == "uncommitted work"


def test_commit_all_does_not_commit_after_diff_failure(tmp_path, monkeypatch):
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 128 if cmd[1] == "diff" else 0, "", "index unreadable")

    monkeypatch.setattr(pr.subprocess, "run", run)
    with pytest.raises(PRError, match="git diff"):
        commit_all(tmp_path, "must not commit", {})

    assert [cmd[1] for cmd in commands] == ["add", "diff"]


def test_existing_pr_url_reports_lookup_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        pr.subprocess, "run", lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 1, "", "API unavailable")
    )

    with pytest.raises(PRError, match="API unavailable"):
        existing_pr_url(tmp_path, "main", "feature", {})


def test_open_pull_request_does_not_create_after_lookup_failure(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(pr, "commit_all", lambda *args: False)
    monkeypatch.setattr(pr, "push_branch", lambda *args: None)

    def run(cmd, **kwargs):
        commands.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "API unavailable")

    monkeypatch.setattr(pr.subprocess, "run", run)
    with pytest.raises(PRError, match="API unavailable"):
        open_pull_request(tmp_path, "origin", "main", "feature", "title", "body", [], True, "message")

    assert [cmd[2] for cmd in commands] == ["list"]


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
