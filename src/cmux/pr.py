"""Commit -> push -> pull-request pipeline, run by the orchestrator (agents are edit-only).

The agent sessions never push; the orchestrator deterministically commits the
worktree diff, pushes the branch, and opens exactly one draft PR per item. On
this user's machine the ambient ``GITHUB_TOKEN``/``GH_TOKEN`` is a fine-grained
PAT that lacks repo permissions, so ``strip_token`` (default) removes them to
fall back to the keyring account for ``gh`` and ``git push``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


class PRError(Exception):
    pass


def gh_env(strip_token: bool = True) -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GH_PROMPT_DISABLED", "1")
    env.setdefault("GH_NO_UPDATE_NOTIFIER", "1")
    if strip_token:
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_TOKEN", None)
    return env


def _run(cmd: list[str], cwd: str | Path, env: dict[str, str], stdin: str | None = None):
    return subprocess.run(
        cmd, cwd=str(cwd), env=env, input=stdin, capture_output=True, text=True
    )


def commit_all(worktree: str | Path, message: str, env: dict[str, str]) -> bool:
    """Stage and commit everything in the worktree. Returns False if nothing to commit."""
    _run(["git", "add", "-A"], worktree, env)
    staged = _run(["git", "diff", "--cached", "--quiet"], worktree, env)
    if staged.returncode == 0:
        return False
    proc = _run(["git", "commit", "-m", message], worktree, env)
    if proc.returncode != 0:
        raise PRError(f"git commit failed: {proc.stderr.strip()}")
    return True


def push_branch(worktree: str | Path, remote: str, branch: str, env: dict[str, str]) -> None:
    proc = _run(
        ["git", "push", "-u", remote, f"HEAD:refs/heads/{branch}"], worktree, env
    )
    if proc.returncode != 0:
        raise PRError(f"git push failed: {proc.stderr.strip()}")


def existing_pr_url(
    worktree: str | Path, base: str, branch: str, env: dict[str, str]
) -> str | None:
    proc = _run(
        [
            "gh", "pr", "list",
            "--head", branch, "--base", base, "--state", "open",
            "--json", "url", "--jq", ".[0].url // empty",
        ],
        worktree,
        env,
    )
    if proc.returncode != 0:
        return None
    url = proc.stdout.strip()
    return url or None


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
    cmd = ["gh", "pr", "create", "--base", base, "--head", branch,
           "--title", title, "--body-file", "-"]
    if draft:
        cmd.append("--draft")
    for label in labels:
        cmd += ["--label", label]
    proc = _run(cmd, worktree, env, stdin=body)
    if proc.returncode != 0:
        raise PRError(f"gh pr create failed: {proc.stderr.strip() or proc.stdout.strip()}")
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
    """Full idempotent pipeline: commit -> push -> reuse-or-create the PR. Returns the URL."""
    env = gh_env(strip_token)
    commit_all(worktree, commit_message, env)
    push_branch(worktree, remote, branch, env)
    url = existing_pr_url(worktree, base, branch, env)
    if url:
        return url
    return create_pr(worktree, base, branch, title, body, labels, draft, env)
