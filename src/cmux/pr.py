# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import os
import subprocess
from pathlib import Path


class PRError(Exception):
    """Raised when a git or gh step in the pull-request pipeline fails."""


def gh_env(strip_token: bool = True) -> dict[str, str]:
    """Build a subprocess environment for `gh`/`git`.

    Args:
        strip_token: Drop ambient `GITHUB_TOKEN`/`GH_TOKEN` so `gh` uses the keyring account.

    Returns:
        The environment mapping for the subprocess.

    """
    env = os.environ.copy()
    env.setdefault("GH_PROMPT_DISABLED", "1")
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")

    if strip_token:
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_TOKEN", None)

    return env


def _run(
    cmd: list[str], cwd: str | Path, env: dict[str, str], stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=str(cwd), env=env, input=stdin, capture_output=True, text=True)


def commit_all(worktree: str | Path, message: str, env: dict[str, str]) -> bool:
    """Stage and commit everything in the worktree.

    Args:
        worktree: Worktree to commit.
        message: Commit message.
        env: Subprocess environment.

    Returns:
        `True` if a commit was made, `False` if there was nothing to commit.

    Raises:
        PRError: If `git commit` fails.

    """
    _run(["git", "add", "-A"], worktree, env)
    if _run(["git", "diff", "--cached", "--quiet"], worktree, env).returncode == 0:
        return False

    proc = _run(["git", "commit", "-m", message], worktree, env)
    if proc.returncode != 0:
        raise PRError(f"`git commit` failed: {proc.stderr.strip()}.")

    return True


def push_branch(worktree: str | Path, remote: str, branch: str, env: dict[str, str]) -> None:
    """Push the worktree's HEAD to `branch` on `remote`.

    Args:
        worktree: Worktree to push from.
        remote: Remote name.
        branch: Target branch name.
        env: Subprocess environment.

    Raises:
        PRError: If `git push` fails.

    """
    proc = _run(["git", "push", "-u", remote, f"HEAD:refs/heads/{branch}"], worktree, env)
    if proc.returncode != 0:
        raise PRError(f"`git push` failed: {proc.stderr.strip()}.")


def existing_pr_url(worktree: str | Path, base: str, branch: str, env: dict[str, str]) -> str | None:
    """Return the URL of an open PR for `branch`, or `None` if there is none.

    Args:
        worktree: Worktree used to run `gh`.
        base: Base branch of the PR.
        branch: Head branch of the PR.
        env: Subprocess environment.

    Returns:
        The open PR URL, or `None` if none exists.

    """
    proc = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--base",
            base,
            "--state",
            "open",
            "--json",
            "url",
            "--jq",
            ".[0].url // empty",
        ],
        worktree,
        env,
    )
    if proc.returncode != 0:
        return None

    return proc.stdout.strip() or None


def create_pr(
    worktree: str | Path,
    base: str,
    branch: str,
    title: str,
    body: str,
    labels: list[str],
    draft: bool,
    env: dict[str, str],
) -> str:
    """Create a pull request for `branch` and return its URL.

    Args:
        worktree: Worktree used to run `gh`.
        base: Base branch of the PR.
        branch: Head branch of the PR.
        title: PR title.
        body: PR body, passed on stdin.
        labels: Labels to apply.
        draft: Whether to open the PR as a draft.
        env: Subprocess environment.

    Returns:
        The created PR URL, or an empty string if `gh` printed nothing.

    Raises:
        PRError: If `gh pr create` fails.

    """
    cmd = ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", "-"]
    if draft:
        cmd.append("--draft")
    for label in labels:
        cmd += ["--label", label]

    proc = _run(cmd, worktree, env, stdin=body)
    if proc.returncode != 0:
        raise PRError(f"`gh pr create` failed: {proc.stderr.strip() or proc.stdout.strip()}.")

    return proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""


def open_pull_request(
    worktree: str | Path,
    remote: str,
    base: str,
    branch: str,
    title: str,
    body: str,
    labels: list[str],
    draft: bool,
    commit_message: str,
    strip_token: bool = True,
) -> str:
    """Commit, push, and reuse-or-create a PR, returning its URL.

    Args:
        worktree: Worktree to publish.
        remote: Remote to push to.
        base: Base branch of the PR.
        branch: Head branch of the PR.
        title: PR title.
        body: PR body.
        labels: Labels to apply.
        draft: Whether to open the PR as a draft.
        commit_message: Message for the worktree commit.
        strip_token: Whether to strip ambient GitHub tokens from the environment.

    Returns:
        The existing or newly created PR URL, empty only if `gh` printed nothing.

    Raises:
        PRError: If a commit, push, or create step fails.

    """
    env = gh_env(strip_token)

    commit_all(worktree, commit_message, env)
    push_branch(worktree, remote, branch, env)

    url = existing_pr_url(worktree, base, branch, env)
    if url:
        return url

    return create_pr(worktree, base, branch, title, body, labels, draft, env)
