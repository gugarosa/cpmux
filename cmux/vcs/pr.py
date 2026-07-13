# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import os
import subprocess
from pathlib import Path


class PRError(Exception):
    """Raised when a `git` or `gh` PR step fails."""


def gh_env(strip_token: bool = True) -> dict[str, str]:
    """Build a non-interactive environment for `gh` and `git`.

    Args:
        strip_token: Remove ambient GitHub tokens so `gh` uses the keyring.

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
    """Commit worktree changes, returning whether a commit was made."""

    _run(["git", "add", "-A"], worktree, env)
    if _run(["git", "diff", "--cached", "--quiet"], worktree, env).returncode == 0:
        return False

    proc = _run(["git", "commit", "-m", message], worktree, env)
    if proc.returncode != 0:
        raise PRError(f"`git commit` failed: {proc.stderr.strip()}.")

    return True


def push_branch(worktree: str | Path, remote: str, branch: str, env: dict[str, str]) -> None:
    """Push the worktree's HEAD to `branch` on `remote`."""

    proc = _run(["git", "push", "-u", remote, f"HEAD:refs/heads/{branch}"], worktree, env)
    if proc.returncode != 0:
        raise PRError(f"`git push` of `{branch}` to `{remote}` failed: {proc.stderr.strip()}.")


def existing_pr_url(worktree: str | Path, base: str, branch: str, env: dict[str, str]) -> str | None:
    """Return the open PR URL for `branch`, if any."""

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
    """Create a pull request for `branch` and return its URL."""

    cmd = ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", "-"]
    if draft:
        cmd.append("--draft")
    for label in labels:
        cmd += ["--label", label]

    proc = _run(cmd, worktree, env, stdin=body)
    if proc.returncode != 0:
        raise PRError(f"`gh pr create` for `{branch}` failed: {proc.stderr.strip() or proc.stdout.strip()}.")

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
    """Commit, push, and reuse or create a PR."""

    env = gh_env(strip_token)

    commit_all(worktree, commit_message, env)
    push_branch(worktree, remote, branch, env)

    url = existing_pr_url(worktree, base, branch, env)
    if url:
        return url

    return create_pr(worktree, base, branch, title, body, labels, draft, env)
