# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from cpmux.config import PR_DRAFT_FILENAME
from cpmux.process import inherited_fds
from cpmux.vcs import git

_EVIDENCE_START = "<!-- cpmux:verification -->"
_EVIDENCE_END = "<!-- /cpmux:verification -->"


class PRError(Exception):
    """Raised when a `git` or `gh` PR step fails."""


def gh_env(strip_token: bool = True) -> dict[str, str]:
    """Build a non-interactive environment for `gh` and `git`.

    Args:
        strip_token: Remove ambient GitHub tokens so `gh` uses the keyring.

    Returns:
        The configured subprocess environment.

    """

    env = os.environ.copy()
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"

    if strip_token:
        env.pop("GITHUB_TOKEN", None)
        env.pop("GH_TOKEN", None)

    return env


def _run(
    cmd: list[str], cwd: str | Path, env: dict[str, str], stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            input=stdin,
            stdin=subprocess.DEVNULL if stdin is None else None,
            capture_output=True,
            text=True,
            check=False,
            timeout=60 if cmd[0] == "gh" else None,
            pass_fds=inherited_fds(),
        )
    except subprocess.TimeoutExpired as exc:
        raise PRError(f"`{' '.join(cmd[:2])}` timed out. Inspect the remote before retrying delivery.") from exc


def _repository_identity(value: str, require_pr: bool = False) -> tuple[str, str] | None:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.netloc.casefold() != parsed.hostname.casefold()
    ):
        return None

    parts = parsed.path.strip("/").split("/")
    if len(parts) != (4 if require_pr else 2) or not all(parts):
        return None
    if require_pr and (parts[2] != "pull" or not parts[3].isdigit() or int(parts[3]) < 1):
        return None
    return parsed.hostname.casefold(), f"{parts[0]}/{parts[1]}".casefold()


def _remote_repository(worktree: str | Path, remote: str, env: dict[str, str]) -> tuple[str, str]:
    remote_url = _run(["git", "remote", "get-url", "--push", "--all", "--", remote], worktree, env)
    if remote_url.returncode != 0:
        detail = remote_url.stderr.strip() or remote_url.stdout.strip()
        raise PRError(f"`git remote get-url {remote}` failed: {detail.removesuffix('.')}.")
    destinations = remote_url.stdout.strip().splitlines()
    if len(destinations) != 1:
        raise PRError(f"`remote={remote}` must have exactly one push destination.")
    response = _run(["gh", "repo", "view", destinations[0], "--json", "url"], worktree, env)
    if response.returncode != 0:
        detail = response.stderr.strip() or response.stdout.strip()
        raise PRError(f"`remote={remote}` could not be resolved by GitHub CLI: {detail.removesuffix('.')}.")
    try:
        data = json.loads(response.stdout)
    except json.JSONDecodeError as exc:
        raise PRError(f"`remote={remote}` returned invalid repository metadata.") from exc
    identity = (
        _repository_identity(data["url"]) if isinstance(data, dict) and isinstance(data.get("url"), str) else None
    )
    if identity is None:
        raise PRError(f"`remote={remote}` returned an invalid repository URL.")
    return identity


def _is_commit(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(character in "0123456789abcdef" for character in value)
    )


def _validate_existing_pr(
    worktree: str | Path,
    repository: tuple[str, str],
    base: str,
    branch: str,
    url: str,
    env: dict[str, str],
    commit_sha: str | None = None,
) -> str:
    if _repository_identity(url, require_pr=True) != repository:
        raise PRError(f"`{url}` does not belong to the configured remote repository.")

    current = _run(
        ["gh", "pr", "view", url, "--json", "state,url,headRefName,baseRefName,headRefOid,isCrossRepository,body"],
        worktree,
        env,
    )
    if current.returncode != 0:
        detail = current.stderr.strip() or current.stdout.strip()
        raise PRError(f"`{url}` could not be inspected: {detail.removesuffix('.')}.")
    try:
        data = json.loads(current.stdout)
    except json.JSONDecodeError as exc:
        raise PRError(f"`{url}` returned invalid pull-request metadata.") from exc
    if not isinstance(data, dict):
        raise PRError(f"`{url}` returned invalid pull-request metadata.")

    if data.get("state") != "OPEN":
        raise PRError(f"`{url}` is not an open pull request. Start a new run instead.")
    if str(data.get("url", "")).rstrip("/").casefold() != url.rstrip("/").casefold():
        raise PRError(f"`{url}` resolved to a different pull request.")
    if data.get("headRefName") != branch:
        raise PRError(f"`{url}` has head branch `{data.get('headRefName')}`, expected `{branch}`.")
    if data.get("baseRefName") != base:
        raise PRError(f"`{url}` has base branch `{data.get('baseRefName')}`, expected `{base}`.")
    if data.get("isCrossRepository") is not False:
        raise PRError(f"`{url}` does not use a head branch in the configured remote repository.")
    if not _is_commit(data.get("headRefOid")) or (commit_sha is not None and data["headRefOid"] != commit_sha):
        raise PRError(f"`{url}` does not contain the delivered candidate `{commit_sha}`.")
    body = data.get("body")
    if not isinstance(body, str):
        raise PRError(f"`{url}` returned an invalid pull-request body.")
    return body


def _reconcile_delivery_pr(
    worktree: str | Path,
    repository: tuple[str, str],
    base: str,
    branch: str,
    commit_sha: str,
    env: dict[str, str],
) -> str | None:
    current = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            "/".join(repository),
            "--head",
            branch,
            "--base",
            base,
            "--state",
            "all",
            "--limit",
            "100",
            "--json",
            "url,state,headRefName,baseRefName,headRefOid,isCrossRepository",
        ],
        worktree,
        env,
    )
    if current.returncode != 0:
        detail = current.stderr.strip() or current.stdout.strip()
        raise PRError(f"`gh pr list` for `{branch}` failed: {detail.removesuffix('.')}.")
    try:
        entries = json.loads(current.stdout)
    except json.JSONDecodeError as exc:
        raise PRError(f"`gh pr list` for `{branch}` returned invalid metadata.") from exc
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise PRError(f"`gh pr list` for `{branch}` returned invalid metadata.")
    if any(
        entry.get("state") not in {"OPEN", "CLOSED", "MERGED"}
        or not isinstance(entry.get("isCrossRepository"), bool)
        or (entry["state"] == "OPEN" and not _is_commit(entry.get("headRefOid")))
        or not isinstance(entry.get("url"), str)
        or not isinstance(entry.get("headRefName"), str)
        or not isinstance(entry.get("baseRefName"), str)
        for entry in entries
    ):
        raise PRError(f"`gh pr list` for `{branch}` returned incomplete pull-request identities.")

    matching = [
        entry
        for entry in entries
        if _repository_identity(entry["url"], require_pr=True) == repository
        and entry["isCrossRepository"] is False
        and entry.get("headRefName") == branch
        and entry.get("baseRefName") == base
    ]
    completed = next(
        (
            entry
            for entry in matching
            if entry.get("state") in {"CLOSED", "MERGED"} and entry.get("headRefOid") == commit_sha
        ),
        None,
    )
    if completed is not None:
        raise PRError(
            f"`{completed.get('url')}` already closed or merged candidate `{commit_sha}`. Start a new run instead."
        )
    opened = next((entry for entry in matching if entry.get("state") == "OPEN"), None)

    return str(opened["url"]) if opened is not None else None


def _verification_body(body: str, commit_sha: str, checks_passed: int | None) -> str:
    before, marker, remainder = body.partition(_EVIDENCE_START)
    if marker:
        _, end, after = remainder.partition(_EVIDENCE_END)
        if not end or _EVIDENCE_START in after:
            raise PRError("`pull-request body` contains malformed cpmux verification markers.")
        body = before.rstrip() + ("\n\n" + after.lstrip() if after.strip() else "")
    outcome = (
        f"- Configured checks passed: {checks_passed}\n"
        "- Results are recorded from executed commands, not inferred from the agent response.\n"
        if checks_passed is not None
        else "- Acceptance checks: not configured; this candidate is unverified.\n"
    )
    return (
        f"{body.rstrip()}\n\n{_EVIDENCE_START}\n## cpmux verification\n\n"
        f"- Candidate: `{commit_sha}`\n{outcome}{_EVIDENCE_END}\n"
    )


def commit_all(worktree: str | Path, message: str, env: dict[str, str]) -> bool:
    """Commit all worktree changes.

    Args:
        worktree: Worktree containing the changes.
        message: Commit message.
        env: Subprocess environment.

    Returns:
        Whether a commit was created.

    Raises:
        PRError: Staging, inspecting the index, or committing failed.

    """

    proc = _run(["git", "add", "-A"], worktree, env)
    if proc.returncode != 0:
        raise PRError(f"`git add` failed: {proc.stderr.strip().removesuffix('.')}.")

    proc = _run(["git", "diff", "--cached", "--quiet"], worktree, env)
    if proc.returncode == 0:
        return False
    if proc.returncode != 1:
        raise PRError(f"`git diff --cached` failed: {proc.stderr.strip().removesuffix('.')}.")

    proc = _run(["git", "commit", "-m", message], worktree, env)
    if proc.returncode != 0:
        raise PRError(f"`git commit` failed: {proc.stderr.strip().removesuffix('.')}.")

    return True


def push_branch(
    worktree: str | Path,
    remote: str,
    branch: str,
    env: dict[str, str],
    source: str,
) -> None:
    """Push the worktree branch to a remote.

    Args:
        worktree: Worktree containing the branch.
        remote: Remote to push to.
        branch: Remote branch name.
        env: Subprocess environment.
        source: Full immutable commit object identifier to push.

    Raises:
        PRError: The push failed.

    """

    if not _is_commit(source):
        raise PRError("`source` must be a full Git object identifier, not a movable reference.")
    proc = _run(["git", "push", "-u", "--", remote, f"{source}:refs/heads/{branch}"], worktree, env)
    if proc.returncode != 0:
        raise PRError(f"`git push` of `{branch}` to `{remote}` failed: {proc.stderr.strip().removesuffix('.')}.")


def read_pr_draft(worktree: str | Path) -> tuple[str | None, str | None]:
    """Read and remove an agent-authored pull-request draft.

    Args:
        worktree: Worktree that may contain the draft file.

    Returns:
        The parsed title and body, or `(None, None)` when absent or unusable.

    Raises:
        OSError: The draft cannot be read or removed.

    """

    path = Path(worktree) / PR_DRAFT_FILENAME
    text = ""
    if path.is_file() and not path.is_symlink():
        text = path.read_text(encoding="utf-8", errors="replace").strip()

    path.unlink(missing_ok=True)
    if not text:
        return None, None

    lines = text.splitlines()
    title = lines[0].lstrip("#").strip()
    body = "\n".join(lines[1:]).strip()
    if len(title) > 256:
        return None, body or None

    return title or None, body or None


def create_pr(
    worktree: str | Path,
    base: str,
    branch: str,
    title: str,
    body: str,
    labels: list[str],
    draft: bool,
    env: dict[str, str],
    repository: str,
) -> str:
    """Create a pull request.

    Args:
        worktree: Worktree in which to run GitHub CLI.
        base: Pull request base branch.
        branch: Pull request head branch.
        title: Pull request title.
        body: Pull request body.
        labels: Labels to apply.
        draft: Whether to create a draft pull request.
        env: Subprocess environment.
        repository: Explicit host/owner/repository target resolved from the selected remote.

    Returns:
        The created pull request URL.

    Raises:
        PRError: Pull request creation failed.

    """

    cmd = [
        "gh",
        "pr",
        "create",
        "--repo",
        repository,
        "--base",
        base,
        "--head",
        branch,
        "--title",
        title,
        "--body-file",
        "-",
    ]
    if draft:
        cmd.append("--draft")
    for label in labels:
        cmd += ["--label", label]

    proc = _run(cmd, worktree, env, stdin=body)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise PRError(f"`gh pr create` for `{branch}` failed: {detail.removesuffix('.')}.")

    if not proc.stdout.strip():
        raise PRError("`gh pr create` returned no pull-request URL. Inspect the remote before retrying.")
    return proc.stdout.strip().splitlines()[-1]


def publish_pull_request(
    worktree: str | Path,
    remote: str,
    base: str,
    branch: str,
    title: str,
    body: str,
    labels: list[str],
    draft: bool,
    commit_sha: str,
    strip_token: bool = True,
    existing_url: str | None = None,
    checks_passed: int | None = None,
) -> str:
    """Deliver an already committed candidate without committing additional edits.

    A recorded or discoverable pull request is reconciled before pushing.
    Closed or merged pull requests for the exact candidate are rejected, while
    an open matching head and base may be updated without force-pushing.

    Args:
        worktree: Worktree containing the candidate.
        remote: Git remote to push to.
        base: Pull-request base branch.
        branch: Pull-request head branch.
        title: Pull-request title.
        body: Pull-request description.
        labels: Labels for a newly created pull request.
        draft: Whether a newly created pull request is a draft.
        commit_sha: Exact candidate commit to deliver.
        strip_token: Whether to remove ambient authentication tokens.
        existing_url: Previously recorded pull request that must still be open.
        checks_passed: Configured checks covered by the candidate receipt, or None when not configured.

    Returns:
        The open pull-request URL.

    Raises:
        PRError: Candidate identity, push, lookup, or creation fails.
        git.GitError: Source changed before the candidate could be pushed.
        OSError: A required executable or local resource cannot be accessed.

    """

    env = gh_env(strip_token)
    if not _is_commit(commit_sha):
        raise PRError("`commit_sha` must be a full Git object identifier.")
    if checks_passed is not None and (
        not isinstance(checks_passed, int) or isinstance(checks_passed, bool) or checks_passed < 1
    ):
        raise PRError("`checks_passed` must be positive when acceptance checks are configured.")

    repository = _remote_repository(worktree, remote, env)
    if existing_url is None:
        existing_url = _reconcile_delivery_pr(worktree, repository, base, branch, commit_sha, env)
    if existing_url is not None:
        body = _validate_existing_pr(worktree, repository, base, branch, existing_url, env)
    delivery_body = _verification_body(body, commit_sha, checks_passed)

    git.require_clean_revision(worktree, commit_sha)
    push_branch(worktree, remote, branch, env, source=commit_sha)
    if existing_url is None:
        existing_url = _reconcile_delivery_pr(worktree, repository, base, branch, commit_sha, env)
    if existing_url is None:
        created = create_pr(worktree, base, branch, title, delivery_body, labels, draft, env, "/".join(repository))
        _validate_existing_pr(worktree, repository, base, branch, created, env, commit_sha)
        return created

    current_body = _validate_existing_pr(worktree, repository, base, branch, existing_url, env, commit_sha)
    updated_body = _verification_body(current_body, commit_sha, checks_passed)
    if updated_body != current_body:
        edited = _run(["gh", "pr", "edit", existing_url, "--body-file", "-"], worktree, env, stdin=updated_body)
        if edited.returncode != 0:
            detail = edited.stderr.strip() or edited.stdout.strip()
            raise PRError(f"`{existing_url}` verification summary could not be updated: {detail.removesuffix('.')}.")
    return existing_url
