# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import pytest
import yaml

from cpmux.config import ConfigError, Plan, parse_plan
from cpmux.engine.intake import issues_plan
from cpmux.vcs.issues import Issue


def _issue(number, title=None, body=None, labels=None):
    return Issue(
        repository="acme/widgets",
        number=number,
        title=title or f"Issue {number}",
        body=body if body is not None else f"Body {number}",
        url=f"https://github.com/acme/widgets/issues/{number}",
        updated_at="2026-09-06T12:00:00Z",
        labels=list(labels or []),
    )


def test_issues_plan_builds_ordered_editable_plan_with_source_and_closing_link():
    contents = issues_plan([_issue(7, labels=["bug"]), _issue(2)])
    raw = yaml.safe_load(contents)
    plan = parse_plan(contents)

    assert [item.id for item in plan.items] == ["issue-7", "issue-2"]
    assert [item["id"] for item in raw["items"]] == ["issue-7", "issue-2"]
    assert "slug" not in raw["items"][0]
    assert plan.items[0].name == "Issue 7"
    assert plan.items[0].prompt == (
        "Implement the following GitHub issue.\n\n"
        "Title: Issue 7\n"
        "URL: https://github.com/acme/widgets/issues/7\n\n"
        "--- BEGIN GITHUB ISSUE BODY ---\n"
        "Body 7\n"
        "--- END GITHUB ISSUE BODY ---\n\n"
        "Include this exact line in the pull-request description:\n\n"
        "Closes https://github.com/acme/widgets/issues/7"
    )
    assert plan.items[0].source.model_dump(mode="json") == {
        "repository": "acme/widgets",
        "number": 7,
        "url": "https://github.com/acme/widgets/issues/7",
        "updated_at": "2026-09-06T12:00:00Z",
        "title": "Issue 7",
        "labels": ["bug"],
    }
    assert "Closes https://github.com/acme/widgets/issues/7" in plan.resolve()[0].pr_body


def test_issues_plan_copies_only_template_settings_and_selects_profile():
    template = Plan.model_validate(
        {
            "system": "Follow repository conventions.",
            "defaults": {
                "model": "claude-sonnet-5",
                "premium_budget": 12,
                "pr": {"body_template": "Imported task\n\n{prompt}"},
            },
            "profiles": {"python": {"checks": ["uv run pytest"]}},
            "items": [
                {"id": "old-item", "prompt": "Do not copy me."},
                {"id": "old-dependent", "prompt": "Do not copy me either.", "base_from": "old-item"},
            ],
        }
    )

    plan = parse_plan(issues_plan([_issue(4)], template=template, profile="python"))

    assert plan.system == "Follow repository conventions."
    assert plan.defaults.model == "claude-sonnet-5"
    assert plan.defaults.premium_budget == 12
    assert set(plan.profiles) == {"python"}
    assert [item.id for item in plan.items] == ["issue-4"]
    assert plan.items[0].profile == "python"
    assert plan.resolve()[0].checks[0].command == "uv run pytest"
    assert "Closes https://github.com/acme/widgets/issues/4" in plan.resolve()[0].pr_body


def test_issues_plan_preserves_external_and_resolved_template_env_literals(monkeypatch):
    for name in ("SYSTEM", "BASE", "TITLE", "BODY", "ALREADY", "LABEL"):
        monkeypatch.delenv(name, raising=False)
    template = Plan.model_validate(
        {
            "system": "$${SYSTEM}",
            "defaults": {"base": "$${BASE}"},
            "items": ["template item"],
        }
    )
    issue = _issue(
        1,
        title="${TITLE}",
        body="Use ${BODY}, ${FALLBACK:-fallback}, and preserve $${ALREADY} plus {prompt}.",
        labels=["${LABEL}"],
    )

    contents = issues_plan([issue], template=template)
    plan = parse_plan(contents)
    item = plan.items[0]

    assert plan.system == "${SYSTEM}"
    assert plan.defaults.base == "${BASE}"
    assert item.name == "${TITLE}"
    assert "Use ${BODY}, ${FALLBACK:-fallback}, and preserve $${ALREADY} plus {prompt}." in item.prompt
    assert item.source.title == "${TITLE}"
    assert item.source.labels == ["${LABEL}"]


def test_issues_plan_rejects_unknown_profile():
    template = Plan.model_validate({"profiles": {"python": {}}, "items": ["template item"]})

    with pytest.raises(ConfigError, match="unknown profile"):
        issues_plan([_issue(1)], template=template, profile="missing")


def test_issues_plan_rejects_empty_issue_list():
    with pytest.raises(ConfigError, match="at least one"):
        issues_plan([])


def test_issues_plan_does_not_deduplicate_duplicate_inputs():
    with pytest.raises(ConfigError, match="duplicate identifiers"):
        issues_plan([_issue(1), _issue(1)])


def test_issues_plan_never_expands_environment_values_from_external_content(monkeypatch):
    monkeypatch.setenv("CPMUX_INTAKE_SECRET", "must-not-be-expanded")
    body = "Keep ${CPMUX_INTAKE_SECRET} and $${CPMUX_INTAKE_SECRET} literally."

    contents = issues_plan([_issue(42, body=body)])
    plan = parse_plan(contents)

    assert body in plan.items[0].prompt
    assert "must-not-be-expanded" not in contents
    assert "must-not-be-expanded" not in plan.model_dump_json()


def test_issues_plan_uses_stable_unique_branches_for_duplicate_titles():
    plan = parse_plan(issues_plan([_issue(1, title="Bug report"), _issue(2, title="Bug report")]))

    assert [item.name for item in plan.items] == ["Bug report", "Bug report"]
    assert [item.branch for item in plan.resolve()] == ["cpmux/issue-1", "cpmux/issue-2"]
