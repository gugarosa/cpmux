# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import json
import re
import subprocess
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path
from urllib.parse import urlsplit

from cpmux.process import inherited_fds
from cpmux.vcs.pr import gh_env

_ISSUE_FIELDS = "number,title,body,url,updatedAt,labels"
_GH_TIMEOUT_SECONDS = 30
_OWNER_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
_NAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_HOST_RE = re.compile(
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*" r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)


@dataclass
class Issue:
    """A GitHub issue fetched for read-only intake.

    Attributes:
        repository: Repository containing the issue as owner/name.
        number: Positive repository-local issue number.
        title: Issue title.
        body: Issue body, or empty text when GitHub returns null.
        url: Canonical HTTPS issue URL.
        updated_at: GitHub's issue update timestamp.
        labels: Label names in GitHub's returned order.

    """

    repository: str
    number: int
    title: str
    body: str
    url: str
    updated_at: str
    labels: list[str]


class IssueError(Exception):
    """Raised when issue input is invalid or a GitHub lookup fails."""


def _validate_repository(repository: object, context: str = "repository") -> str:
    if not isinstance(repository, str) or repository.count("/") != 1:
        raise IssueError(f"`{context}` must have the `owner/name` shape.")
    owner, name = repository.split("/")
    if not _OWNER_RE.fullmatch(owner) or not _NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise IssueError(f"`{context}` must have the `owner/name` shape.")
    return repository


def _github_host(env: dict[str, str]) -> str:
    host = env.get("GH_HOST", "github.com").strip().lower()
    if not host or not _HOST_RE.fullmatch(host):
        raise IssueError("`GH_HOST` must be a hostname.")
    return host


def _positive_issue_number(value: str, context: str) -> int:
    if not re.fullmatch(r"[0-9]{1,20}", value) or int(value) < 1:
        raise IssueError(f"`{context}` must contain a positive issue number.")
    return int(value)


def _parse_issue_url(value: str, host: str, context: str) -> tuple[str, int]:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise IssueError(f"`{context}` is not a valid GitHub issue URL.") from exc

    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.hostname.lower() != host
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise IssueError(f"`{context}` must be an HTTPS issue URL on `{host}`.")

    parts = parsed.path.removesuffix("/").split("/")
    if len(parts) == 5 and parts[0] == "" and parts[3] in {"pull", "pulls"}:
        raise IssueError(f"`{context}` is a pull-request URL, not an issue URL.")
    if len(parts) != 5 or parts[0] != "" or parts[3] != "issues":
        raise IssueError(f"`{context}` is not a valid GitHub issue URL.")

    repository = _validate_repository(f"{parts[1]}/{parts[2]}", context)
    return repository, _positive_issue_number(parts[4], context)


def _parse_reference(reference: object, host: str) -> tuple[str | None, int]:
    if not isinstance(reference, str) or not reference.strip():
        raise IssueError("`reference` must be a positive issue number or HTTPS issue URL.")
    reference = reference.strip()
    if re.fullmatch(r"[0-9]+", reference):
        return None, _positive_issue_number(reference, "reference")
    if "://" in reference:
        return _parse_issue_url(reference, host, "reference")
    raise IssueError("`reference` must be a positive issue number or HTTPS issue URL.")


def _run_gh(args: list[str], cwd: str | Path, env: dict[str, str], operation: str) -> object:
    command = ["gh", *args]
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT_SECONDS,
            pass_fds=inherited_fds(),
        )
    except FileNotFoundError as exc:
        raise IssueError("`gh` was not found on PATH; install GitHub CLI.") from exc
    except subprocess.TimeoutExpired as exc:
        raise IssueError(f"`{operation}` timed out after {_GH_TIMEOUT_SECONDS} seconds.") from exc
    except OSError as exc:
        raise IssueError(f"`{operation}` could not start.") from exc

    if proc.returncode != 0:
        detail = proc.stderr.strip()
        if detail:
            for token_name in ("GH_TOKEN", "GITHUB_TOKEN"):
                if token := env.get(token_name):
                    detail = detail.replace(token, "[redacted]")
            raise IssueError(f"`{operation}` failed: {detail.removesuffix('.')}.")
        raise IssueError(f"`{operation}` failed with exit code {proc.returncode}.")

    try:
        return json.loads(proc.stdout)
    except JSONDecodeError as exc:
        raise IssueError(f"`{operation}` returned malformed JSON.") from exc


def _issue_from_response(data: object, repository: str, host: str, operation: str) -> Issue:
    if not isinstance(data, dict):
        raise IssueError(f"`{operation}` returned an invalid issue object.")

    number = data.get("number")
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise IssueError(f"`{operation}` returned an invalid `number`.")
    title = data.get("title")
    if not isinstance(title, str):
        raise IssueError(f"`{operation}` returned an invalid `title`.")
    body = data.get("body")
    if body is None:
        body = ""
    elif not isinstance(body, str):
        raise IssueError(f"`{operation}` returned an invalid `body`.")
    url = data.get("url")
    if not isinstance(url, str):
        raise IssueError(f"`{operation}` returned an invalid `url`.")
    url_repository, url_number = _parse_issue_url(url, host, f"{operation} response `url`")
    if url_repository.casefold() != repository.casefold():
        raise IssueError(f"`{operation}` returned an issue from `{url_repository}`, not `{repository}`.")
    if url_number != number:
        raise IssueError(f"`{operation}` returned mismatched issue numbers `{number}` and `{url_number}`.")
    updated_at = data.get("updatedAt")
    if not isinstance(updated_at, str):
        raise IssueError(f"`{operation}` returned an invalid `updatedAt`.")
    raw_labels = data.get("labels")
    if not isinstance(raw_labels, list):
        raise IssueError(f"`{operation}` returned invalid `labels`.")

    labels: list[str] = []
    for label in raw_labels:
        if not isinstance(label, dict) or not isinstance(label.get("name"), str):
            raise IssueError(f"`{operation}` returned an invalid label.")
        labels.append(label["name"])

    return Issue(repository, number, title, body, url, updated_at, labels)


def fetch_issues(
    repo_root: str | Path,
    references: list[str],
    repository: str | None = None,
    query: str | None = None,
    limit: int = 20,
) -> list[Issue]:
    """Fetch GitHub issues without modifying the repository or remote.

    Args:
        repo_root: Repository directory in which GitHub CLI runs.
        references: Explicit issue numbers or HTTPS issue URLs.
        repository: Target as owner/name or host/owner/name, inferred from repo_root when omitted.
        query: GitHub issue search query used instead of explicit references.
        limit: Maximum issue count from 1 through 100, without truncating explicit selections.

    Returns:
        Issues in stable explicit-reference order or GitHub's query order.

    Raises:
        IssueError: Input validation, GitHub CLI execution, or response validation failed.

    """

    if not isinstance(references, list):
        raise IssueError("`references` must be a list.")
    if references and query is not None:
        raise IssueError("`references` and `query` are mutually exclusive.")
    if not references and query is None:
        raise IssueError("`references` or `query` must be supplied.")
    if query is not None and (not isinstance(query, str) or not query.strip()):
        raise IssueError("`query` must be non-empty text.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise IssueError("`limit` must be an integer from 1 through 100.")

    env = gh_env(strip_token=False)
    env["GH_PROMPT_DISABLED"] = "1"
    env["GH_NO_UPDATE_NOTIFIER"] = "1"
    if isinstance(repository, str) and repository.count("/") == 2:
        explicit_host, repository = repository.split("/", 1)
        env["GH_HOST"] = explicit_host
    host = _github_host(env)
    if "GH_HOST" in env:
        env["GH_HOST"] = host

    if repository is None:
        operation = "gh repo view"
        data = _run_gh(["repo", "view", "--json", "nameWithOwner,url"], repo_root, env, operation)
        if not isinstance(data, dict):
            raise IssueError(f"`{operation}` returned an invalid response.")
        repository = _validate_repository(data.get("nameWithOwner"), "gh repo view response `nameWithOwner`")
        url = data.get("url")
        if not isinstance(url, str):
            raise IssueError(f"`{operation}` returned an invalid repository `url`.")
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError as exc:
            raise IssueError(f"`{operation}` returned an invalid repository `url`.") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
            or parsed.path.strip("/").casefold() != repository.casefold()
        ):
            raise IssueError(f"`{operation}` returned an invalid repository `url`.")
        host = _github_host({"GH_HOST": parsed.hostname})
        env["GH_HOST"] = host
    else:
        repository = _validate_repository(repository)

    parsed_references = [_parse_reference(reference, host) for reference in references]
    for reference_repository, _ in parsed_references:
        if reference_repository is not None and reference_repository.casefold() != repository.casefold():
            raise IssueError(
                f"`reference` targets `{reference_repository}`, not the requested repository `{repository}`."
            )
    if len({number for _, number in parsed_references}) > limit:
        raise IssueError(f"`references` exceeds `limit={limit}`. Increase the limit or split the selection.")

    if query is not None:
        operation = f"gh issue list for {repository}"
        data = _run_gh(
            [
                "issue",
                "list",
                "--search",
                query,
                "--state",
                "all",
                "--repo",
                repository,
                "--limit",
                str(limit),
                "--json",
                _ISSUE_FIELDS,
            ],
            repo_root,
            env,
            operation,
        )
        if not isinstance(data, list):
            raise IssueError(f"`{operation}` returned an invalid response.")
        unique: dict[int, Issue] = {}
        for entry in data:
            issue = _issue_from_response(entry, repository, host, operation)
            unique.setdefault(issue.number, issue)
        if len(unique) > limit:
            raise IssueError(f"`{operation}` returned more than `limit={limit}` issues.")
        return list(unique.values())

    issues: list[Issue] = []
    seen: set[int] = set()
    for _, number in parsed_references:
        if number in seen:
            continue
        seen.add(number)
        operation = f"gh issue view for {repository}#{number}"
        data = _run_gh(
            ["issue", "view", str(number), "--repo", repository, "--json", _ISSUE_FIELDS],
            repo_root,
            env,
            operation,
        )
        issue = _issue_from_response(data, repository, host, operation)
        if issue.number != number:
            raise IssueError(f"`{operation}` returned issue `{issue.number}`.")
        issues.append(issue)

    return issues
