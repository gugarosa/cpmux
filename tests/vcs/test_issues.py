# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import json
import subprocess

import pytest

from cpmux.vcs import issues
from cpmux.vcs.issues import Issue, IssueError, fetch_issues


def _issue(number, repository="acme/widgets", body="Body", labels=None, host="github.com"):
    return {
        "number": number,
        "title": f"Issue {number}",
        "body": body,
        "url": f"https://{host}/{repository}/issues/{number}",
        "updatedAt": "2026-09-06T12:00:00Z",
        "labels": [{"name": name} for name in (labels or [])],
    }


def _completed(command, payload, returncode=0, stderr=""):
    return subprocess.CompletedProcess(command, returncode, json.dumps(payload), stderr)


def test_fetch_issues_preserves_explicit_order_and_deduplicates(tmp_path, monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return _completed(command, _issue(int(command[3]), body=None, labels=["bug", "urgent"]))

    monkeypatch.setattr(issues.subprocess, "run", run)

    result = fetch_issues(
        tmp_path,
        ["2", "https://github.com/acme/widgets/issues/1", "2"],
        repository="acme/widgets",
    )

    assert result == [
        Issue(
            repository="acme/widgets",
            number=2,
            title="Issue 2",
            body="",
            url="https://github.com/acme/widgets/issues/2",
            updated_at="2026-09-06T12:00:00Z",
            labels=["bug", "urgent"],
        ),
        Issue(
            repository="acme/widgets",
            number=1,
            title="Issue 1",
            body="",
            url="https://github.com/acme/widgets/issues/1",
            updated_at="2026-09-06T12:00:00Z",
            labels=["bug", "urgent"],
        ),
    ]
    assert [call[0][3] for call in calls] == ["2", "1"]
    assert all(call[0][-1] == "number,title,body,url,updatedAt,labels" for call in calls)
    assert all(call[1]["cwd"] == str(tmp_path) for call in calls)
    assert all(call[1]["capture_output"] is True for call in calls)
    assert all(call[1]["text"] is True and call[1]["check"] is False for call in calls)
    assert all(call[1]["timeout"] == 30 for call in calls)


def test_fetch_issues_resolves_default_repository(tmp_path, monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["gh", "repo", "view"]:
            return _completed(command, {"nameWithOwner": "acme/widgets", "url": "https://github.com/acme/widgets"})
        return _completed(command, _issue(7))

    monkeypatch.setattr(issues.subprocess, "run", run)

    result = fetch_issues(tmp_path, ["7"])

    assert result[0].repository == "acme/widgets"
    assert commands == [
        ["gh", "repo", "view", "--json", "nameWithOwner,url"],
        [
            "gh",
            "issue",
            "view",
            "7",
            "--repo",
            "acme/widgets",
            "--json",
            "number,title,body,url,updatedAt,labels",
        ],
    ]


def test_fetch_issues_queries_with_limit_and_preserves_order(tmp_path, monkeypatch):
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        return _completed(command, [_issue(9), _issue(3)])

    monkeypatch.setattr(issues.subprocess, "run", run)

    result = fetch_issues(tmp_path, [], repository="acme/widgets", query="label:bug is:open", limit=7)

    assert [issue.number for issue in result] == [9, 3]
    assert commands == [
        [
            "gh",
            "issue",
            "list",
            "--search",
            "label:bug is:open",
            "--state",
            "all",
            "--repo",
            "acme/widgets",
            "--limit",
            "7",
            "--json",
            "number,title,body,url,updatedAt,labels",
        ]
    ]


def test_fetch_issues_uses_configured_host_for_url_targeting(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_HOST", "git.example.com")
    monkeypatch.setenv("GH_PROMPT_DISABLED", "0")
    monkeypatch.setenv("GH_NO_UPDATE_NOTIFIER", "0")

    def run(command, **kwargs):
        assert kwargs["env"]["GH_HOST"] == "git.example.com"
        assert kwargs["env"]["GH_PROMPT_DISABLED"] == "1"
        assert kwargs["env"]["GH_NO_UPDATE_NOTIFIER"] == "1"
        return _completed(command, _issue(4, host="git.example.com"))

    monkeypatch.setattr(issues.subprocess, "run", run)

    result = fetch_issues(
        tmp_path,
        ["https://git.example.com/acme/widgets/issues/4"],
        repository="acme/widgets",
    )

    assert result[0].url == "https://git.example.com/acme/widgets/issues/4"


@pytest.mark.parametrize(
    ("references", "repository", "query", "limit"),
    [
        pytest.param([], "acme/widgets", None, 20, id="missing-source"),
        pytest.param(["1"], "acme/widgets", "is:open", 20, id="both-sources"),
        pytest.param([], "acme/widgets", " ", 20, id="empty-query"),
        pytest.param([], "acme/widgets", "is:open", 0, id="zero-limit"),
        pytest.param([], "acme/widgets", "is:open", 101, id="large-limit"),
        pytest.param([], "acme/widgets", "is:open", True, id="boolean-limit"),
        pytest.param(["1"], "invalid", None, 20, id="invalid-repository"),
        pytest.param(["0"], "acme/widgets", None, 20, id="zero-reference"),
        pytest.param(["#1"], "acme/widgets", None, 20, id="malformed-reference"),
        pytest.param(
            ["https://github.com/acme/widgets/pull/1"],
            "acme/widgets",
            None,
            20,
            id="pull-request-url",
        ),
        pytest.param(
            ["http://github.com/acme/widgets/issues/1"],
            "acme/widgets",
            None,
            20,
            id="non-https-url",
        ),
        pytest.param(
            ["https://example.com/acme/widgets/issues/1"],
            "acme/widgets",
            None,
            20,
            id="unsupported-host",
        ),
        pytest.param(
            ["https://github.com/other/widgets/issues/1"],
            "acme/widgets",
            None,
            20,
            id="cross-repository-url",
        ),
    ],
)
def test_fetch_issues_rejects_invalid_input(tmp_path, monkeypatch, references, repository, query, limit):
    def run(*args, **kwargs):
        raise AssertionError("subprocess should not run")

    monkeypatch.setattr(issues.subprocess, "run", run)

    with pytest.raises(IssueError):
        fetch_issues(tmp_path, references, repository=repository, query=query, limit=limit)


def test_fetch_issues_reports_api_failure_without_exposing_stdout(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "top-secret-token")

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, "private issue body", "API unavailable: top-secret-token")

    monkeypatch.setattr(issues.subprocess, "run", run)

    with pytest.raises(IssueError, match="API unavailable") as error:
        fetch_issues(tmp_path, ["1"], repository="acme/widgets")

    assert "private issue body" not in str(error.value)
    assert "top-secret-token" not in str(error.value)
    assert "[redacted]" in str(error.value)


def test_fetch_issues_reports_missing_cli(tmp_path, monkeypatch):
    def run(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(issues.subprocess, "run", run)

    with pytest.raises(IssueError, match="not found") as error:
        fetch_issues(tmp_path, ["1"], repository="acme/widgets")

    assert isinstance(error.value.__cause__, FileNotFoundError)


def test_fetch_issues_reports_timeout(tmp_path, monkeypatch):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(issues.subprocess, "run", run)

    with pytest.raises(IssueError, match="timed out") as error:
        fetch_issues(tmp_path, ["1"], repository="acme/widgets")

    assert isinstance(error.value.__cause__, subprocess.TimeoutExpired)


def test_fetch_issues_reports_malformed_json(tmp_path, monkeypatch):
    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "{", "")

    monkeypatch.setattr(issues.subprocess, "run", run)

    with pytest.raises(IssueError, match="malformed JSON") as error:
        fetch_issues(tmp_path, ["1"], repository="acme/widgets")

    assert isinstance(error.value.__cause__, json.JSONDecodeError)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([], id="issue-not-object"),
        pytest.param({**_issue(1), "number": True}, id="invalid-number"),
        pytest.param({**_issue(1), "title": None}, id="invalid-title"),
        pytest.param({**_issue(1), "url": "https://github.com/other/repo/issues/1"}, id="wrong-repository"),
        pytest.param({**_issue(1), "url": "https://github.com/acme/widgets/issues/2"}, id="wrong-number"),
        pytest.param({**_issue(1), "labels": [{"color": "red"}]}, id="invalid-label"),
    ],
)
def test_fetch_issues_rejects_malformed_issue_responses(tmp_path, monkeypatch, payload):
    monkeypatch.setattr(
        issues.subprocess,
        "run",
        lambda command, **kwargs: _completed(command, payload),
    )

    with pytest.raises(IssueError):
        fetch_issues(tmp_path, ["1"], repository="acme/widgets")


def test_fetch_issues_rejects_malformed_default_repository_response(tmp_path, monkeypatch):
    monkeypatch.setattr(
        issues.subprocess,
        "run",
        lambda command, **kwargs: _completed(command, {"nameWithOwner": "invalid"}),
    )

    with pytest.raises(IssueError, match="nameWithOwner"):
        fetch_issues(tmp_path, ["1"])


def test_fetch_issues_infers_enterprise_host_from_the_current_repository(tmp_path, monkeypatch):
    monkeypatch.delenv("GH_HOST", raising=False)

    def run(command, **kwargs):
        if command[:3] == ["gh", "repo", "view"]:
            return _completed(command, {"nameWithOwner": "acme/widgets", "url": "https://git.example.com/acme/widgets"})
        assert kwargs["env"]["GH_HOST"] == "git.example.com"
        return _completed(command, _issue(4, host="git.example.com"))

    monkeypatch.setattr(issues.subprocess, "run", run)

    assert fetch_issues(tmp_path, ["4"])[0].url == "https://git.example.com/acme/widgets/issues/4"


def test_fetch_issues_accepts_an_explicit_repository_host(tmp_path, monkeypatch):
    def run(command, **kwargs):
        assert kwargs["env"]["GH_HOST"] == "git.example.com"
        assert command[command.index("--repo") + 1] == "acme/widgets"
        return _completed(command, _issue(4, host="git.example.com"))

    monkeypatch.setattr(issues.subprocess, "run", run)

    assert fetch_issues(tmp_path, ["4"], repository="git.example.com/acme/widgets")[0].number == 4


def test_fetch_issues_refuses_to_silently_truncate_explicit_selections(tmp_path, monkeypatch):
    monkeypatch.setattr(issues.subprocess, "run", lambda *args, **kwargs: pytest.fail("selection exceeded its limit"))

    with pytest.raises(IssueError, match="exceeds"):
        fetch_issues(tmp_path, ["1", "2"], repository="acme/widgets", limit=1)


def test_fetch_issues_honors_closed_issue_search_and_deduplicates_query_results(tmp_path, monkeypatch):
    def run(command, **kwargs):
        assert command[command.index("--state") + 1] == "all"
        assert command[command.index("--search") + 1] == "is:closed label:bug"
        return _completed(command, [_issue(4), _issue(4), _issue(7)])

    monkeypatch.setattr(issues.subprocess, "run", run)

    assert [
        issue.number for issue in fetch_issues(tmp_path, [], repository="acme/widgets", query="is:closed label:bug")
    ] == [4, 7]
