# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import pytest
from pydantic import ValidationError

from cpmux.config import ConfigError, Plan, Preset, interpolate_env, load_plan
from cpmux.vcs.pr import PR_DRAFT_FILENAME


@pytest.mark.parametrize(
    ("extract_value", "expected"),
    [
        pytest.param(lambda plan: plan.items[0].prompt, "fix the bug", id="prompt"),
        pytest.param(lambda plan: plan.resolve()[0].model, "gpt-5.5", id="default-model"),
        pytest.param(lambda plan: plan.resolve()[0].permissions.preset, Preset.edit, id="default-permissions"),
    ],
)
def test_item_accepts_string_shorthand(extract_value, expected):
    plan = Plan.model_validate({"items": ["fix the bug"]})
    assert extract_value(plan) == expected


@pytest.mark.parametrize(
    ("extract_value", "expected"),
    [
        pytest.param(lambda resolved: resolved[0].model, "gpt-5.5", id="default-model"),
        pytest.param(lambda resolved: resolved[1].model, "claude-opus-4.8", id="item-model"),
        pytest.param(lambda resolved: resolved[1].labels, ["batch", "refactor"], id="merged-labels"),
    ],
)
def test_resolve_applies_precedence_and_labels(extract_value, expected):
    plan = Plan.model_validate(
        {
            "system": "SYS",
            "defaults": {"pr": {"labels": ["batch"]}},
            "items": [
                "a simple task",
                {
                    "name": "Big refactor",
                    "prompt": "do it",
                    "model": "claude-opus-4.8",
                    "labels": ["refactor"],
                },
            ],
        }
    )

    assert extract_value(plan.resolve()) == expected


@pytest.mark.parametrize(
    ("matches_prompt", "expected"),
    [
        pytest.param(lambda prompt, expected: prompt.startswith(expected), "SYS", id="system-prefix"),
        pytest.param(
            lambda prompt, expected: prompt.rstrip().endswith(expected),
            "a simple task",
            id="item-prompt-suffix",
        ),
    ],
)
def test_resolve_applies_system_prompt_boundaries(matches_prompt, expected):
    plan = Plan.model_validate(
        {
            "system": "SYS",
            "defaults": {"pr": {"labels": ["batch"]}},
            "items": [
                "a simple task",
                {
                    "name": "Big refactor",
                    "prompt": "do it",
                    "model": "claude-opus-4.8",
                    "labels": ["refactor"],
                },
            ],
        }
    )
    assert matches_prompt(plan.resolve()[0].prompt, expected)


def test_resolve_omits_system_when_include_system_false():
    plan = Plan.model_validate({"system": "SYS", "items": [{"prompt": "solo", "include_system": False}]})
    assert plan.resolve()[0].prompt == "solo"


@pytest.mark.parametrize(
    ("preset", "expected_flags_present", "expected_flags_absent"),
    [
        pytest.param("full", ("--allow-all-tools",), (), id="full-allows-all-tools"),
        pytest.param(
            None,
            ("--allow-tool=shell", "--deny-tool=shell(git push)"),
            ("--no-ask-user",),
            id="default-edit-allows-shell-but-denies-push",
        ),
    ],
)
def test_to_flags_preset_permissions(preset, expected_flags_present, expected_flags_absent):
    items = ["x"] if preset is None else [{"prompt": "x", "permissions": preset}]
    flags = Plan.model_validate({"items": items}).resolve()[0].permissions.to_flags()

    for expected_flag in expected_flags_present:
        assert expected_flag in flags
    for expected_flag in expected_flags_absent:
        assert expected_flag not in flags


def test_resolve_feeds_item_paths_into_add_dir():
    plan = Plan.model_validate({"items": [{"prompt": "x", "paths": ["src/settings"]}]})
    flags = plan.resolve()[0].permissions.to_flags()

    assert "--add-dir" in flags and "src/settings" in flags


@pytest.mark.parametrize(
    "bad_plan_dict",
    [
        pytest.param(
            {"items": [{"prompt": "a", "id": "x"}, {"prompt": "b", "id": "x"}]},
            id="duplicate-item-keys",
        ),
        pytest.param(
            {"items": [{"prompt": "x", "depends_on": ["nope"]}]},
            id="unknown-dependency",
        ),
    ],
)
def test_plan_rejects_invalid_items(bad_plan_dict):
    with pytest.raises(ValidationError):
        Plan.model_validate(bad_plan_dict)


def test_plan_interpolates_env(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    plan = Plan.model_validate({"items": ["use ${FOO} now"]})
    assert plan.items[0].prompt == "use bar now"


def test_plan_uses_env_default_when_missing(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    plan = Plan.model_validate({"items": ["v=${MISSING_VAR:-def}"]})
    assert "v=def" in plan.items[0].prompt


@pytest.mark.parametrize(
    ("extract_value", "expected"),
    [
        pytest.param(lambda argv: argv[0], "copilot", id="copilot-command"),
        pytest.param(
            lambda argv: argv[argv.index("--session-id") + 1],
            "sid-123",
            id="session-id",
        ),
        pytest.param(lambda argv: argv[argv.index("-C") + 1], "/wt/fix-x", id="worktree"),
    ],
)
def test_spawn_argv_targets_session_worktree_and_model(extract_value, expected):
    resolved = Plan.model_validate({"items": [{"name": "Fix X", "prompt": "do"}]}).resolve()[0]
    argv = resolved.spawn_argv("/wt/fix-x", "sid-123", "/logs")

    assert extract_value(argv) == expected


@pytest.mark.parametrize(
    "expected_argument",
    [
        pytest.param("--output-format", id="output-format-option"),
        pytest.param("json", id="json-output-format"),
        pytest.param("--no-ask-user", id="no-ask-user"),
    ],
)
def test_spawn_argv_includes_required_arguments(expected_argument):
    resolved = Plan.model_validate({"items": [{"name": "Fix X", "prompt": "do"}]}).resolve()[0]
    argv = resolved.spawn_argv("/wt/fix-x", "sid-123", "/logs")

    assert expected_argument in argv


def test_spawn_argv_appends_pr_authoring_instructions():
    resolved = Plan.model_validate({"items": [{"name": "Fix X", "prompt": "do"}]}).resolve()[0]
    argv = resolved.spawn_argv("/wt/fix-x", "sid-123", "/logs")
    prompt = argv[argv.index("-p") + 1]

    assert prompt == resolved.effective_prompt()
    assert prompt.startswith("do")
    assert PR_DRAFT_FILENAME in prompt


def test_resolve_default_pr_body_is_structured():
    resolved = Plan.model_validate({"items": [{"prompt": "add a feature"}]}).resolve()[0]

    assert resolved.pr_body == "## Summary\n\nadd a feature\n"


@pytest.mark.parametrize(
    ("plan_dict", "extract_value", "expected"),
    [
        pytest.param(
            {"defaults": {"port_base": 3000}, "items": ["a", "b", "c"]},
            lambda resolved: [item.env["PORT"] for item in resolved],
            ["3000", "3001", "3002"],
            id="sequential-ports",
        ),
        pytest.param(
            {
                "defaults": {"port_base": 3000},
                "items": [{"name": "a", "prompt": "x", "env": {"PORT": "9999"}}],
            },
            lambda resolved: resolved[0].env["PORT"],
            "9999",
            id="explicit-port-wins",
        ),
    ],
)
def test_resolve_assigns_port_values(plan_dict, extract_value, expected):
    assert extract_value(Plan.model_validate(plan_dict).resolve()) == expected


@pytest.mark.parametrize(
    ("plan_dict", "expected_env"),
    [
        pytest.param(
            {"defaults": {"port_base": 4000, "port_env": "DEV_PORT"}, "items": ["a"]},
            {"DEV_PORT": "4000"},
            id="custom-port-variable",
        ),
        pytest.param(
            {"items": [{"name": "a", "prompt": "x", "env": {"FOO": "bar"}}]},
            {"FOO": "bar"},
            id="no-port-base",
        ),
    ],
)
def test_resolve_sets_expected_env_mapping(plan_dict, expected_env):
    resolved = Plan.model_validate(plan_dict).resolve()
    assert resolved[0].env == expected_env


def test_defaults_rejects_invalid_port_env_name():
    with pytest.raises(ValidationError):
        Plan.model_validate({"defaults": {"port_base": 4000, "port_env": "1bad"}, "items": ["a"]})


@pytest.mark.parametrize(
    ("env_name", "env_value", "value", "expected"),
    [
        pytest.param(
            "CPMUX_TEST_VAR",
            "hello",
            "say ${CPMUX_TEST_VAR}",
            "say hello",
            id="expands-set-variable",
        ),
        pytest.param(
            "CPMUX_TEST_MISSING",
            None,
            "${CPMUX_TEST_MISSING:-fallback}",
            "fallback",
            id="uses-fallback-for-missing-variable",
        ),
    ],
)
def test_interpolate_env_expands_and_falls_back(monkeypatch, env_name, env_value, value, expected):
    monkeypatch.delenv(env_name, raising=False)
    if env_value is not None:
        monkeypatch.setenv(env_name, env_value)

    assert interpolate_env(value) == expected


def test_interpolate_env_raises_on_unset_var_without_default(monkeypatch):
    monkeypatch.delenv("CPMUX_TEST_MISSING", raising=False)
    with pytest.raises(ValueError):
        interpolate_env("${CPMUX_TEST_MISSING}")


@pytest.mark.parametrize(
    ("filename", "contents"),
    [
        pytest.param("nope.yaml", None, id="missing-file"),
        pytest.param("bad.yaml", "- just\n- a\n- list\n", id="non-mapping-top-level"),
        pytest.param("bad.yaml", "items: []\n", id="invalid-plan"),
    ],
)
def test_load_plan_invalid_input_raises_config_error(tmp_path, filename, contents):
    path = tmp_path / filename
    if contents is not None:
        path.write_text(contents)

    with pytest.raises(ConfigError):
        load_plan(path)


@pytest.mark.parametrize(
    "expected_content",
    [
        pytest.param("not a valid cpmux plan", id="concise-plan-error"),
        pytest.param("items", id="field-name"),
    ],
)
def test_load_plan_invalid_includes_concise_field_errors(tmp_path, expected_content):
    path = tmp_path / "bad.yaml"
    path.write_text("items: []\n")

    with pytest.raises(ConfigError) as excinfo:
        load_plan(path)

    assert expected_content in str(excinfo.value)


@pytest.mark.parametrize(
    "unexpected_content",
    [
        pytest.param("for Plan", id="pydantic-model-boilerplate"),
    ],
)
def test_load_plan_invalid_omits_verbose_field_errors(tmp_path, unexpected_content):
    path = tmp_path / "bad.yaml"
    path.write_text("items: []\n")

    with pytest.raises(ConfigError) as excinfo:
        load_plan(path)

    assert unexpected_content not in str(excinfo.value)


@pytest.mark.parametrize(
    ("extract_value", "expected"),
    [
        pytest.param(lambda plan: len(plan.items), 1, id="one-item"),
        pytest.param(lambda plan: plan.resolve()[0].prompt, "fix a thing", id="resolved-prompt"),
    ],
)
def test_load_plan_reads_valid_file(tmp_path, extract_value, expected):
    path = tmp_path / "ok.yaml"
    path.write_text("version: 1\nitems:\n  - fix a thing\n")
    plan = load_plan(path)

    assert extract_value(plan) == expected
