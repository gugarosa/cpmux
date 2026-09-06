# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import json
import subprocess

import pytest

from cpmux.vcs import git, pr
from cpmux.vcs.pr import (
    PR_DRAFT_FILENAME,
    PRError,
    commit_all,
    gh_env,
    publish_pull_request,
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


def test_push_branch_creates_branch_on_remote(git_repo):
    repo = git_repo
    bare = git_repo / "bare.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(bare)], cwd=repo, check=True)

    push_branch(repo, "origin", "feature/x", gh_env(strip_token=False), git.head_commit(repo))

    subprocess.run(
        ["git", f"--git-dir={bare}", "rev-parse", "--verify", "refs/heads/feature/x"],
        check=True,
        capture_output=True,
    )


def test_push_branch_raises_on_bogus_remote(git_repo):
    repo = git_repo
    with pytest.raises(PRError):
        push_branch(repo, "nope", "feature/x", gh_env(strip_token=False), git.head_commit(repo))


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


def test_publish_pull_request_rejects_recorded_url_for_different_repository_before_push(tmp_path, monkeypatch):
    pushed = []

    def run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(cmd, 0, "git@github.com:owner/repo.git\n", "")
        assert cmd[:3] == ["gh", "repo", "view"]
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"url": "https://github.com/owner/repo"}), "")

    monkeypatch.setattr(pr, "_run", run)
    monkeypatch.setattr(pr, "push_branch", lambda *args, **kwargs: pushed.append(args))

    with pytest.raises(PRError, match="does not belong"):
        publish_pull_request(
            tmp_path,
            "origin",
            "main",
            "feature",
            "title",
            "body",
            [],
            False,
            "a" * 40,
            existing_url="https://github.com/other/repo/pull/1",
        )

    assert pushed == []


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        ({"state": "CLOSED", "headRefName": "feature", "baseRefName": "main"}, "not an open"),
        ({"state": "MERGED", "headRefName": "feature", "baseRefName": "main"}, "not an open"),
        ({"state": "OPEN", "headRefName": "other", "baseRefName": "main"}, "head branch"),
        ({"state": "OPEN", "headRefName": "feature", "baseRefName": "other"}, "base branch"),
        ({"state": "OPEN", "headRefName": "feature", "baseRefName": "main", "isCrossRepository": True}, "head branch"),
    ],
)
def test_publish_pull_request_rejects_wrong_existing_pr_identity_before_push(tmp_path, monkeypatch, metadata, message):
    url = "https://github.com/owner/repo/pull/1"
    pushed = []

    def run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/owner/repo.git\n", "")
        if cmd[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"url": "https://github.com/owner/repo"}), "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"url": url, **metadata}), "")

    monkeypatch.setattr(pr, "_run", run)
    monkeypatch.setattr(pr, "push_branch", lambda *args, **kwargs: pushed.append(args))

    with pytest.raises(PRError, match=message):
        publish_pull_request(
            tmp_path,
            "origin",
            "main",
            "feature",
            "title",
            "body",
            [],
            False,
            "a" * 40,
            existing_url=url,
        )

    assert pushed == []


def test_publish_pull_request_updates_matching_open_pr_with_exact_candidate(tmp_path, monkeypatch):
    url = "https://github.com/owner/repo/pull/1"
    pushed = []
    edited = []
    monkeypatch.setattr(git, "require_clean_revision", lambda *args: None)

    def run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(cmd, 0, "ssh://git@github.com/owner/repo.git\n", "")
        if cmd[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"url": "https://github.com/owner/repo"}), "")
        if cmd[:3] == ["gh", "pr", "edit"]:
            edited.append(kwargs["stdin"])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        metadata = {
            "state": "OPEN",
            "url": url,
            "headRefName": "feature",
            "baseRefName": "stacked-parent",
            "headRefOid": "a" * 40,
            "isCrossRepository": False,
            "body": "Maintainer-written description",
        }
        return subprocess.CompletedProcess(cmd, 0, json.dumps(metadata), "")

    monkeypatch.setattr(pr, "_run", run)
    monkeypatch.setattr(pr, "push_branch", lambda *args, **kwargs: pushed.append((args, kwargs)))

    result = publish_pull_request(
        tmp_path,
        "origin",
        "stacked-parent",
        "feature",
        "title",
        "body",
        [],
        False,
        "a" * 40,
        existing_url=url,
        checks_passed=2,
    )

    assert result == url
    assert pushed[0][0][2] == "feature"
    assert pushed[0][1]["source"] == "a" * 40
    assert "Maintainer-written description" in edited[0]
    assert "Candidate: `" + "a" * 40 in edited[0]
    assert "Configured checks passed: 2" in edited[0]


def test_publish_pull_request_reconciles_unrecorded_open_pr_before_push(tmp_path, monkeypatch):
    url = "https://github.com/owner/repo/pull/1"
    metadata = {
        "state": "OPEN",
        "url": url,
        "headRefName": "feature",
        "baseRefName": "main",
        "headRefOid": "a" * 40,
        "isCrossRepository": False,
        "body": "body",
    }
    commands = []
    pushed = []
    monkeypatch.setattr(git, "require_clean_revision", lambda *args: None)

    def run(cmd, *args, **kwargs):
        commands.append(cmd)
        if cmd[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(cmd, 0, "git@github.com:owner/repo.git\n", "")
        if cmd[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"url": "https://github.com/owner/repo"}), "")
        if cmd[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps([metadata]), "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps(metadata), "")

    monkeypatch.setattr(pr, "_run", run)
    monkeypatch.setattr(pr, "push_branch", lambda *args, **kwargs: pushed.append((args, kwargs)))

    result = publish_pull_request(
        tmp_path,
        "origin",
        "main",
        "feature",
        "title",
        "body",
        [],
        False,
        "a" * 40,
    )

    assert result == url
    assert next(command for command in commands if command[:3] == ["gh", "pr", "list"])
    assert pushed[0][1]["source"] == "a" * 40


@pytest.mark.parametrize("state", ["CLOSED", "MERGED"])
def test_publish_pull_request_rejects_unrecorded_completed_exact_candidate_before_push(tmp_path, monkeypatch, state):
    metadata = {
        "state": state,
        "url": "https://github.com/owner/repo/pull/1",
        "headRefName": "feature",
        "baseRefName": "main",
        "headRefOid": "a" * 40,
        "isCrossRepository": False,
    }
    pushed = []

    def run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/owner/repo.git\n", "")
        if cmd[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"url": "https://github.com/owner/repo"}), "")
        return subprocess.CompletedProcess(cmd, 0, json.dumps([metadata]), "")

    monkeypatch.setattr(pr, "_run", run)
    monkeypatch.setattr(pr, "push_branch", lambda *args, **kwargs: pushed.append(args))

    with pytest.raises(PRError, match="already closed or merged"):
        publish_pull_request(
            tmp_path,
            "origin",
            "main",
            "feature",
            "title",
            "body",
            [],
            False,
            "a" * 40,
        )

    assert pushed == []


def test_publish_pull_request_ignores_completed_pr_for_different_candidate(tmp_path, monkeypatch):
    metadata = {
        "state": "MERGED",
        "url": "https://github.com/owner/repo/pull/1",
        "headRefName": "feature",
        "baseRefName": "main",
        "headRefOid": "b" * 40,
        "isCrossRepository": False,
    }
    pushed = []
    created = []
    monkeypatch.setattr(git, "require_clean_revision", lambda *args: None)

    def run(cmd, *args, **kwargs):
        if cmd[:3] == ["git", "remote", "get-url"]:
            return subprocess.CompletedProcess(cmd, 0, "https://github.com/owner/repo.git\n", "")
        if cmd[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"url": "https://github.com/owner/repo"}), "")
        if cmd[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                json.dumps(
                    {
                        **metadata,
                        "state": "OPEN",
                        "url": "https://github.com/owner/repo/pull/2",
                        "headRefOid": "a" * 40,
                        "body": "body",
                    }
                ),
                "",
            )
        return subprocess.CompletedProcess(cmd, 0, json.dumps([metadata]), "")

    monkeypatch.setattr(pr, "_run", run)
    monkeypatch.setattr(pr, "push_branch", lambda *args, **kwargs: pushed.append((args, kwargs)))
    monkeypatch.setattr(
        pr, "create_pr", lambda *args, **kwargs: created.append(args) or "https://github.com/owner/repo/pull/2"
    )

    result = publish_pull_request(
        tmp_path,
        "origin",
        "main",
        "feature",
        "title",
        "body",
        [],
        False,
        "a" * 40,
    )

    assert result == "https://github.com/owner/repo/pull/2"
    assert pushed[0][1]["source"] == "a" * 40
    assert len(created) == 1
    assert created[0][-1] == "github.com/owner/repo"


def test_push_branch_requires_an_immutable_commit(git_repo):
    with pytest.raises(PRError, match="full Git object identifier"):
        push_branch(git_repo, "origin", "feature", {}, "HEAD")


def test_create_pr_targets_the_explicit_repository(tmp_path, monkeypatch):
    calls = []

    def run(cmd, *args, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "https://git.example.com/owner/repo/pull/1\n", "")

    monkeypatch.setattr(pr, "_run", run)

    assert pr.create_pr(tmp_path, "main", "feature", "title", "body", [], False, {}, "git.example.com/owner/repo") == (
        "https://git.example.com/owner/repo/pull/1"
    )
    assert calls[0][calls[0].index("--repo") + 1] == "git.example.com/owner/repo"


@pytest.mark.parametrize("change", ["merged", "different-head"])
def test_publish_pull_request_checks_remote_state_and_commit_after_push(tmp_path, monkeypatch, change):
    url = "https://github.com/owner/repo/pull/1"
    pushed = []
    monkeypatch.setattr(pr, "_remote_repository", lambda *args: ("github.com", "owner/repo"))
    monkeypatch.setattr(git, "require_clean_revision", lambda *args: None)
    monkeypatch.setattr(pr, "push_branch", lambda *args, **kwargs: pushed.append(args))

    def run(cmd, *args, **kwargs):
        assert cmd[:3] == ["gh", "pr", "view"]
        return subprocess.CompletedProcess(
            cmd,
            0,
            json.dumps(
                {
                    "url": url,
                    "state": "MERGED" if pushed and change == "merged" else "OPEN",
                    "headRefName": "feature",
                    "baseRefName": "main",
                    "isCrossRepository": False,
                    "headRefOid": "b" * 40 if pushed and change == "different-head" else "a" * 40,
                    "body": "human text",
                }
            ),
            "",
        )

    monkeypatch.setattr(pr, "_run", run)

    with pytest.raises(PRError, match="not an open|does not contain"):
        publish_pull_request(
            tmp_path, "origin", "main", "feature", "title", "body", [], False, "a" * 40, existing_url=url
        )
    assert len(pushed) == 1


def test_publish_pull_request_does_not_create_a_duplicate_when_the_pr_merges_during_push(tmp_path, monkeypatch):
    pushed = []
    monkeypatch.setattr(pr, "_remote_repository", lambda *args: ("github.com", "owner/repo"))
    monkeypatch.setattr(git, "require_clean_revision", lambda *args: None)
    monkeypatch.setattr(pr, "push_branch", lambda *args, **kwargs: pushed.append(args))
    monkeypatch.setattr(pr, "create_pr", lambda *args: pytest.fail("duplicate PR creation"))

    def run(cmd, *args, **kwargs):
        assert cmd[:3] == ["gh", "pr", "list"]
        metadata = {
            "url": "https://github.com/owner/repo/pull/1",
            "state": "MERGED",
            "headRefName": "feature",
            "baseRefName": "main",
            "headRefOid": "a" * 40,
            "isCrossRepository": False,
        }
        return subprocess.CompletedProcess(cmd, 0, json.dumps([metadata] if pushed else []), "")

    monkeypatch.setattr(pr, "_run", run)

    with pytest.raises(PRError, match="already closed or merged"):
        publish_pull_request(tmp_path, "origin", "main", "feature", "title", "body", [], False, "a" * 40)


def test_publish_pull_request_replaces_stale_evidence_without_erasing_human_content(tmp_path, monkeypatch):
    url = "https://github.com/owner/repo/pull/1"
    old_body = (
        "Human summary\n\n<!-- cpmux:verification -->\n"
        "Old candidate and passed checks\n<!-- /cpmux:verification -->\n\nMaintainer follow-up"
    )
    edited = []
    monkeypatch.setattr(pr, "_remote_repository", lambda *args: ("github.com", "owner/repo"))
    monkeypatch.setattr(git, "require_clean_revision", lambda *args: None)
    monkeypatch.setattr(pr, "push_branch", lambda *args, **kwargs: None)

    def run(cmd, *args, **kwargs):
        if cmd[:3] == ["gh", "pr", "edit"]:
            edited.append(kwargs["stdin"])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(
            cmd,
            0,
            json.dumps(
                {
                    "url": url,
                    "state": "OPEN",
                    "headRefName": "feature",
                    "baseRefName": "main",
                    "headRefOid": "a" * 40,
                    "isCrossRepository": False,
                    "body": old_body,
                }
            ),
            "",
        )

    monkeypatch.setattr(pr, "_run", run)

    publish_pull_request(
        tmp_path, "origin", "main", "feature", "title", "agent body", [], False, "a" * 40, existing_url=url
    )

    assert len(edited) == 1
    assert "Human summary" in edited[0] and "Maintainer follow-up" in edited[0]
    assert "Old candidate" not in edited[0] and "agent body" not in edited[0]
    assert "not configured" in edited[0]
    assert "a" * 40 in edited[0]
    assert edited[0].count("<!-- cpmux:verification -->") == 1


def test_publish_pull_request_checks_source_again_after_github_lookup(git_repo, monkeypatch):
    candidate = git.head_commit(git_repo)
    monkeypatch.setattr(pr, "_remote_repository", lambda *args: ("github.com", "owner/repo"))
    monkeypatch.setattr(pr, "push_branch", lambda *args, **kwargs: pytest.fail("changed source was pushed"))

    def lookup(*args):
        (git_repo / "README.md").write_text("changed during GitHub lookup")
        return None

    monkeypatch.setattr(pr, "_reconcile_delivery_pr", lookup)

    with pytest.raises(git.GitError, match="changes after verification"):
        publish_pull_request(git_repo, "origin", "main", "feature", "title", "body", [], False, candidate)


def test_remote_repository_uses_the_effective_push_url(git_repo, monkeypatch):
    git.run_git(["remote", "add", "origin", "https://github.com/source/repo.git"], git_repo)
    git.run_git(["remote", "set-url", "--push", "origin", "https://github.com/destination/repo.git"], git_repo)
    original_run = pr._run
    looked_up = []

    def run(command, *args, **kwargs):
        if command[0] == "git":
            return original_run(command, *args, **kwargs)
        looked_up.append(command[3])
        return subprocess.CompletedProcess(command, 0, json.dumps({"url": "https://github.com/destination/repo"}), "")

    monkeypatch.setattr(pr, "_run", run)

    assert pr._remote_repository(git_repo, "origin", gh_env(False)) == ("github.com", "destination/repo")
    assert looked_up == ["https://github.com/destination/repo.git"]


def test_remote_repository_rejects_multiple_push_destinations(git_repo):
    git.run_git(["remote", "add", "origin", "https://github.com/source/repo.git"], git_repo)
    for destination in ("one", "two"):
        git.run_git(
            ["remote", "set-url", "--add", "--push", "origin", f"https://github.com/{destination}/repo.git"], git_repo
        )

    with pytest.raises(PRError, match="exactly one push destination"):
        pr._remote_repository(git_repo, "origin", gh_env(False))
