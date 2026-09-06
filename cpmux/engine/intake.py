# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import yaml

from cpmux.config import ConfigError, Plan, escape_env, parse_plan
from cpmux.vcs.issues import Issue


def _escape_env_literals(value: object) -> object:
    if isinstance(value, str):
        return escape_env(value)
    if isinstance(value, list):
        return [_escape_env_literals(item) for item in value]
    if isinstance(value, dict):
        return {key: _escape_env_literals(item) for key, item in value.items()}
    return value


def _issue_prompt(issue: Issue) -> str:
    return (
        "Implement the following GitHub issue.\n\n"
        f"Title: {issue.title}\n"
        f"URL: {issue.url}\n\n"
        "--- BEGIN GITHUB ISSUE BODY ---\n"
        f"{issue.body}\n"
        "--- END GITHUB ISSUE BODY ---\n\n"
        "Include this exact line in the pull-request description:\n\n"
        f"Closes {issue.url}"
    )


def issues_plan(issues: list[Issue], template: Plan | None = None, profile: str | None = None) -> str:
    """Build an editable, validated YAML plan from read-only GitHub issue snapshots.

    Args:
        issues: Issues to convert in their existing order.
        template: Plan contributing only its system, defaults, and profiles.
        profile: Optional template profile selected for every generated item.

    Returns:
        YAML that preserves issue and template strings literally when parsed once.

    Raises:
        ConfigError: No issues were supplied or the generated plan is invalid.

    """

    if not issues:
        raise ConfigError("`issues` must contain at least one issue.")
    if profile is not None and (template is None or profile not in template.profiles):
        raise ConfigError(f"`profile` references unknown profile `{profile}`.")

    if template is None:
        raw: dict[str, object] = {"version": 1, "defaults": {"branch_template": "cpmux/{id}"}}
    else:
        raw = template.model_dump(mode="json", round_trip=True, exclude={"items", "defaults"})
        defaults = template.defaults.model_dump(mode="json", round_trip=True)
        if "branch_template" not in template.defaults.model_fields_set:
            defaults["branch_template"] = "cpmux/{id}"
        raw["defaults"] = defaults

    raw["items"] = [
        {
            "id": f"issue-{issue.number}",
            "name": issue.title,
            "prompt": _issue_prompt(issue),
            "source": {
                "repository": issue.repository,
                "number": issue.number,
                "url": issue.url,
                "updated_at": issue.updated_at,
                "title": issue.title,
                "labels": list(issue.labels),
            },
            **({"profile": profile} if profile is not None else {}),
        }
        for issue in issues
    ]
    escaped = _escape_env_literals(raw)
    contents = yaml.safe_dump(escaped, sort_keys=False, allow_unicode=True, width=120)
    parse_plan(contents, source="generated issue plan")

    return contents
