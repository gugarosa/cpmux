# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import pytest
from pydantic import ValidationError

from cmux.config import ConfigError, Plan, Preset, interpolate_env, load_plan


def test_string_item_coercion():
    plan = Plan.model_validate({"items": ["fix the bug"]})
    assert plan.items[0].prompt == "fix the bug"
    resolved = plan.resolve()[0]
    assert resolved.model == "gpt-5.5"
    assert resolved.permissions.preset == Preset.edit


def test_precedence_and_labels_and_system_prompt():
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

    resolved = plan.resolve()

    assert resolved[0].model == "gpt-5.5"
    assert resolved[1].model == "claude-opus-4.8"
    assert resolved[1].labels == ["batch", "refactor"]
    assert resolved[0].prompt.startswith("SYS")
    assert resolved[0].prompt.rstrip().endswith("a simple task")


def test_include_system_false_opts_out():
    plan = Plan.model_validate({"system": "SYS", "items": [{"prompt": "solo", "include_system": False}]})
    assert plan.resolve()[0].prompt == "solo"


def test_full_preset_flags():
    plan = Plan.model_validate({"items": [{"prompt": "x", "permissions": "full"}]})
    flags = plan.resolve()[0].permissions.to_flags()

    assert "--allow-all-tools" in flags


def test_edit_preset_gates_push_but_allows_shell():
    flags = Plan.model_validate({"items": ["x"]}).resolve()[0].permissions.to_flags()
    assert "--allow-tool=shell" in flags
    assert "--deny-tool=shell(git push)" in flags
    assert "--no-ask-user" not in flags


def test_paths_feed_add_dir():
    plan = Plan.model_validate({"items": [{"prompt": "x", "paths": ["src/settings"]}]})
    flags = plan.resolve()[0].permissions.to_flags()

    assert "--add-dir" in flags and "src/settings" in flags


def test_duplicate_keys_rejected():
    with pytest.raises(ValidationError):
        Plan.model_validate({"items": [{"prompt": "a", "id": "x"}, {"prompt": "b", "id": "x"}]})


def test_depends_on_unknown_rejected():
    with pytest.raises(ValidationError):
        Plan.model_validate({"items": [{"prompt": "x", "depends_on": ["nope"]}]})


def test_env_interpolation(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    plan = Plan.model_validate({"items": ["use ${FOO} now"]})
    assert plan.items[0].prompt == "use bar now"


def test_env_default_used_when_missing(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    plan = Plan.model_validate({"items": ["v=${MISSING_VAR:-def}"]})
    assert "v=def" in plan.items[0].prompt


def test_spawn_argv_targets_session_worktree_and_model():
    resolved = Plan.model_validate({"items": [{"name": "Fix X", "prompt": "do"}]}).resolve()[0]
    argv = resolved.spawn_argv("/wt/fix-x", "sid-123", "/logs")

    assert argv[0] == "copilot"
    assert argv[argv.index("--session-id") + 1] == "sid-123"
    assert argv[argv.index("-C") + 1] == "/wt/fix-x"
    assert "--output-format" in argv and "json" in argv
    assert "--no-ask-user" in argv


def test_port_base_assigns_a_port_per_item():
    resolved = Plan.model_validate({"defaults": {"port_base": 3000}, "items": ["a", "b", "c"]}).resolve()
    assert [item.env["PORT"] for item in resolved] == ["3000", "3001", "3002"]


def test_explicit_env_port_wins_over_assignment():
    resolved = Plan.model_validate(
        {"defaults": {"port_base": 3000}, "items": [{"name": "a", "prompt": "x", "env": {"PORT": "9999"}}]}
    ).resolve()
    assert resolved[0].env["PORT"] == "9999"


def test_port_env_names_the_injected_variable():
    resolved = Plan.model_validate({"defaults": {"port_base": 4000, "port_env": "DEV_PORT"}, "items": ["a"]}).resolve()
    assert resolved[0].env == {"DEV_PORT": "4000"}


def test_no_port_base_leaves_env_untouched():
    resolved = Plan.model_validate({"items": [{"name": "a", "prompt": "x", "env": {"FOO": "bar"}}]}).resolve()
    assert resolved[0].env == {"FOO": "bar"}


def test_invalid_port_env_name_is_rejected():
    with pytest.raises(ValidationError):
        Plan.model_validate({"defaults": {"port_base": 4000, "port_env": "1bad"}, "items": ["a"]})


def test_interpolate_env_expands_and_falls_back(monkeypatch):
    monkeypatch.setenv("CMUX_TEST_VAR", "hello")
    assert interpolate_env("say ${CMUX_TEST_VAR}") == "say hello"
    monkeypatch.delenv("CMUX_TEST_MISSING", raising=False)
    assert interpolate_env("${CMUX_TEST_MISSING:-fallback}") == "fallback"


def test_interpolate_env_raises_on_unset_var_without_default(monkeypatch):
    monkeypatch.delenv("CMUX_TEST_MISSING", raising=False)
    with pytest.raises(ValueError):
        interpolate_env("${CMUX_TEST_MISSING}")


def test_load_plan_missing_file_raises_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_plan(tmp_path / "nope.yaml")


def test_load_plan_non_mapping_top_level_raises_config_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a\n- list\n")
    with pytest.raises(ConfigError):
        load_plan(path)


def test_load_plan_reads_valid_file(tmp_path):
    path = tmp_path / "ok.yaml"
    path.write_text("version: 1\nitems:\n  - fix a thing\n")
    plan = load_plan(path)
    assert len(plan.items) == 1
    assert plan.resolve()[0].prompt == "fix a thing"


def test_load_plan_invalid_reports_concise_field_errors(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("items: []\n")
    with pytest.raises(ConfigError) as excinfo:
        load_plan(path)
    message = str(excinfo.value)
    assert "not a valid cmux plan" in message
    assert "items" in message
    assert "for Plan" not in message
