# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from cpmux.config import (
    CommandSpec,
    ConfigError,
    Plan,
    Preset,
    interpolate_env,
    load_plan,
    parse_plan,
    slugify,
)
from cpmux.vcs.pr import PR_DRAFT_FILENAME


def test_item_accepts_string_shorthand():
    plan = Plan.model_validate({"items": ["fix the bug"]})
    resolved = plan.resolve()[0]

    assert plan.items[0].prompt == "fix the bug"
    assert resolved.model == "gpt-5.5"
    assert resolved.permissions.preset == Preset.edit


def test_resolve_applies_precedence_and_labels():
    plan = Plan.model_validate(
        {
            "system": "SYS",
            "defaults": {"pr": {"labels": ["batch"]}},
            "items": [
                "a simple task",
                {"name": "Big refactor", "prompt": "do it", "model": "claude-opus-4.8", "labels": ["refactor"]},
            ],
        }
    )
    resolved = plan.resolve()

    assert resolved[0].model == "gpt-5.5"
    assert resolved[1].model == "claude-opus-4.8"
    assert resolved[1].labels == ["batch", "refactor"]


def test_resolve_applies_system_prompt_boundaries():
    plan = Plan.model_validate({"system": "SYS", "items": ["a simple task"]})
    prompt = plan.resolve()[0].prompt

    assert prompt.startswith("SYS")
    assert prompt.rstrip().endswith("a simple task")


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
        pytest.param({"items": [""]}, id="empty-string-item"),
        pytest.param(
            {"items": [{"id": "a", "prompt": "x", "depends_on": ["a"]}]},
            id="self-dependency",
        ),
        pytest.param(
            {
                "items": [
                    {"id": "a", "prompt": "x", "depends_on": ["b"]},
                    {"id": "b", "prompt": "y", "depends_on": ["a"]},
                ]
            },
            id="dependency-cycle",
        ),
        pytest.param(
            {"defaults": {"branch_template": "x/{foo}"}, "items": ["t"]},
            id="unknown-branch-placeholder",
        ),
        pytest.param(
            {"defaults": {"pr": {"title_template": "{bogus}"}}, "items": ["t"]},
            id="unknown-title-placeholder",
        ),
        pytest.param({"defaults": {"model": ""}, "items": ["t"]}, id="empty-default-model"),
        pytest.param({"defaults": {"base": "  "}, "items": ["t"]}, id="empty-default-base"),
        pytest.param({"defaults": {"port_base": 65535}, "items": ["a", "b"]}, id="port-base-overflow"),
        pytest.param({"items": ["   "]}, id="blank-prompt"),
        pytest.param({"items": [{"prompt": "\n\n"}]}, id="whitespace-prompt"),
    ],
)
def test_plan_rejects_invalid_items(bad_plan_dict):
    with pytest.raises(ValidationError):
        Plan.model_validate(bad_plan_dict)


@pytest.mark.parametrize(
    "identifier", ["", " ", ".", "..", "../outside", "/outside", "nested/../item", "nested//item", "nul\0id"]
)
def test_plan_rejects_unsafe_or_aliased_ids(identifier):
    with pytest.raises(ValidationError, match="id"):
        Plan.model_validate({"items": [{"id": identifier, "prompt": "x"}]})


def test_resolve_preserves_an_explicit_id_with_spaces():
    plan = Plan.model_validate({"items": [{"id": "release candidate", "prompt": "x"}]})
    assert plan.resolve()[0].key == "release candidate"


def test_resolve_preserves_disjoint_namespaced_ids():
    plan = Plan.model_validate(
        {"items": [{"id": "frontend/login", "prompt": "x"}, {"id": "frontend/profile", "prompt": "y"}]}
    )

    assert [item.key for item in plan.resolve()] == ["frontend/login", "frontend/profile"]


def test_plan_rejects_overlapping_worktree_ids():
    with pytest.raises(ValidationError, match="overlapping identifiers"):
        Plan.model_validate(
            {
                "items": [
                    {"id": "a", "prompt": "x"},
                    {"id": "a-b", "prompt": "y"},
                    {"id": "a/nested", "prompt": "z"},
                ]
            }
        )


@pytest.mark.parametrize(
    "defaults",
    [
        {"branch_template": "feature/{}"},
        {"branch_template": "feature/{slug!z}"},
        {"pr": {"title_template": "{name:invalid}"}},
        {"pr": {"body_template": "{prompt:{unknown}}"}},
        {"pr": {"title_template": "{name:{prompt}}"}},
    ],
)
def test_plan_rejects_templates_that_cannot_resolve(defaults):
    with pytest.raises(ValidationError):
        Plan.model_validate({"defaults": defaults, "items": [{"name": "Release", "prompt": "do the work"}]})


def test_resolve_preserves_valid_string_formatting():
    plan = Plan.model_validate(
        {
            "defaults": {
                "branch_template": "feature/{slug:.3}",
                "pr": {"title_template": "{name:{prompt}}", "body_template": "{{literal}} {slug!r}"},
            },
            "items": [{"name": "Release", "prompt": ">12"}],
        }
    )
    item = plan.resolve()[0]

    assert item.branch == "feature/rel"
    assert item.pr_title == "     Release"
    assert item.pr_body == "{literal} 'release'"


def test_permissions_drop_blank_specs():
    plan = Plan.model_validate(
        {"items": [{"prompt": "t", "permissions": {"preset": "edit", "allow": ["", "shell(x)"]}}]}
    )
    assert plan.items[0].permissions.allow == ["shell(x)"]


def test_resolve_drops_blank_labels():
    plan = Plan.model_validate(
        {"defaults": {"pr": {"labels": ["keep", ""]}}, "items": [{"prompt": "t", "labels": ["", "x"]}]}
    )
    assert plan.resolve()[0].labels == ["keep", "x"]


def test_resolve_title_template_supports_prompt():
    plan = Plan.model_validate({"defaults": {"pr": {"title_template": "{prompt}"}}, "items": ["do the thing"]})
    assert plan.resolve()[0].pr_title == "do the thing"


def test_slugify_transliterates_accents():
    assert slugify("Fix the café crash") == "fix-the-cafe-crash"


def test_plan_interpolates_env(monkeypatch):
    monkeypatch.setenv("FOO", "bar")
    plan = Plan.model_validate({"items": ["use ${FOO} now"]})
    assert plan.items[0].prompt == "use bar now"


def test_plan_uses_env_default_when_missing(monkeypatch):
    monkeypatch.delenv("MISSING_VAR", raising=False)
    plan = Plan.model_validate({"items": ["v=${MISSING_VAR:-def}"]})
    assert "v=def" in plan.items[0].prompt


def test_spawn_argv_targets_session_worktree_and_model():
    resolved = Plan.model_validate(
        {"items": [{"name": "Fix X", "prompt": "do", "model": "custom-model", "effort": "high"}]}
    ).resolve()[0]
    argv = resolved.spawn_argv("/wt/fix-x", "sid-123", "/logs")

    assert argv[0] == "copilot"
    assert argv[argv.index("--session-id") + 1] == "sid-123"
    assert argv[argv.index("-C") + 1] == "/wt/fix-x"
    assert argv[argv.index("--model") + 1] == "custom-model"
    assert argv[argv.index("--effort") + 1] == "high"
    assert argv[argv.index("--name") + 1] == "Fix X"
    assert argv[argv.index("--log-dir") + 1] == "/logs"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--no-ask-user" in argv


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


def test_load_plan_reads_valid_file(tmp_path):
    path = tmp_path / "ok.yaml"
    path.write_text("version: 1\nitems:\n  - fix a thing\n")
    plan = load_plan(path)

    assert len(plan.items) == 1
    assert plan.resolve()[0].prompt == "fix a thing"


def test_config_import_keeps_vcs_unloaded():
    script = (
        "import sys\n"
        "from cpmux.config import Plan\n"
        "Plan.model_validate({'items': ['x']})\n"
        "print(any(name.startswith('cpmux.vcs') for name in sys.modules))"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)

    assert result.returncode == 0
    assert result.stdout.strip() == "False"


def test_load_plan_reports_directory_as_config_error(tmp_path):
    with pytest.raises(ConfigError) as error:
        load_plan(tmp_path)

    assert str(tmp_path) in str(error.value)
    assert isinstance(error.value.__cause__, IsADirectoryError)


def test_load_plan_preserves_unreadable_file_diagnostics(tmp_path, monkeypatch):
    path = tmp_path / "plan.yaml"
    path.write_text("items: [x]\n")
    original_open = Path.open

    def open_path(self, *args, **kwargs):
        if self == path:
            raise PermissionError("permission denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)
    with pytest.raises(ConfigError, match="permission denied") as error:
        load_plan(path)

    assert isinstance(error.value.__cause__, PermissionError)
    assert str(path) in str(error.value)


def test_load_plan_reports_invalid_encoding_as_config_error(tmp_path):
    path = tmp_path / "plan.yaml"
    path.write_bytes(b"\xff")

    with pytest.raises(ConfigError, match="not valid YAML") as error:
        load_plan(path)

    assert error.value.__cause__ is not None
    assert str(path) in str(error.value)


@pytest.mark.parametrize("contents", ["[]", "false", "0", '""'])
def test_load_plan_rejects_falsey_non_mapping_roots(tmp_path, contents):
    path = tmp_path / "plan.yaml"
    path.write_text(contents)

    with pytest.raises(ConfigError, match="top-level YAML must be a mapping"):
        load_plan(path)


@pytest.mark.parametrize(
    ("encoding", "prefix"),
    [("utf-8", b""), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")],
)
def test_load_plan_reads_supported_yaml_encodings(tmp_path, encoding, prefix):
    path = tmp_path / "plan.yaml"
    path.write_bytes(prefix + "items: [fix the caf\u00e9]\n".encode(encoding))

    assert load_plan(path).items[0].prompt == "fix the caf\u00e9"


@pytest.mark.parametrize("contents", ["items: [x]", b"items: [x]"])
def test_parse_plan_accepts_text_and_bytes(contents):
    assert parse_plan(contents).resolve()[0].prompt == "x"


@pytest.mark.parametrize("contents", ["items: []", "defaults:\n  model: ''\nitems: [x]"])
def test_load_plan_formats_failures_with_one_final_period(tmp_path, contents):
    path = tmp_path / "plan.yaml"
    path.write_text(contents)

    with pytest.raises(ConfigError) as error:
        load_plan(path)

    message = str(error.value)
    assert message.startswith(f"`{path}`")
    assert message.endswith(".")
    assert not message.endswith("..")


def test_resolve_applies_profile_and_item_command_precedence():
    plan = Plan.model_validate(
        {
            "profiles": {"python": {"setup": ["uv sync"], "checks": ["uv run pytest"]}},
            "defaults": {"profile": "python", "setup": ["default setup"], "timeout_seconds": 60},
            "items": [
                {"id": "a", "prompt": "x"},
                {"id": "b", "prompt": "y", "checks": [], "timeout_seconds": 10},
            ],
        }
    )
    first, second = plan.resolve()

    assert first.profile == "python"
    assert first.setup[0].command == "uv sync"
    assert first.checks[0].command == "uv run pytest"
    assert first.timeout_seconds == 60
    assert second.setup == first.setup
    assert second.checks == []
    assert second.timeout_seconds == 10


def test_resolve_profile_omissions_inherit_run_defaults():
    plan = Plan.model_validate(
        {
            "profiles": {"python": {"setup": ["uv sync"]}},
            "defaults": {"profile": "python", "checks": ["uv run pytest"]},
            "items": ["x"],
        }
    )
    assert plan.resolve()[0].checks[0].command == "uv run pytest"


def test_plan_rejects_unknown_execution_profiles():
    with pytest.raises(ValidationError, match="unknown profile"):
        Plan.model_validate({"defaults": {"profile": "missing"}, "items": ["x"]})


@pytest.mark.parametrize(
    "data",
    [
        {"command": " "},
        {"command": "x\0y"},
        {"command": "pytest", "timeout_seconds": 0},
        {"command": "pytest", "timeout_seconds": float("inf")},
    ],
)
def test_command_spec_rejects_invalid_execution_contracts(data):
    with pytest.raises(ValidationError):
        CommandSpec.model_validate(data)


def test_interpolate_env_preserves_escaped_references_without_recursive_expansion(monkeypatch):
    monkeypatch.setenv("CPMUX_SECRET", "secret")
    monkeypatch.setenv("CPMUX_INDIRECT", "${CPMUX_SECRET}")

    assert interpolate_env("$${CPMUX_SECRET} $${MISSING:-fallback}") == "${CPMUX_SECRET} ${MISSING:-fallback}"
    assert interpolate_env("${CPMUX_INDIRECT}") == "${CPMUX_SECRET}"


def test_plan_base_from_is_an_explicit_ordering_edge_without_changing_depends_on():
    resolved = Plan.model_validate(
        {"items": [{"id": "a", "prompt": "foundation"}, {"id": "b", "prompt": "extension", "base_from": "a"}]}
    ).resolve()

    assert resolved[1].base_from == "a"
    assert resolved[1].depends_on == []


@pytest.mark.parametrize(
    "items",
    [
        [{"id": "a", "prompt": "x", "base_from": "missing"}],
        [{"id": "a", "prompt": "x", "base_from": "a"}],
        [{"id": "a", "prompt": "x", "base_from": "b"}, {"id": "b", "prompt": "y", "depends_on": ["a"]}],
        [{"id": "a", "prompt": "x"}, {"id": "b", "prompt": "y", "base_from": "a", "base": "main"}],
    ],
)
def test_plan_rejects_ambiguous_or_cyclic_base_inheritance(items):
    with pytest.raises(ValidationError):
        Plan.model_validate({"items": items})


def test_plan_rejects_branch_collisions_before_worktree_creation():
    with pytest.raises(ValidationError, match="shared by"):
        Plan.model_validate(
            {"items": [{"id": "a", "name": "same", "prompt": "x"}, {"id": "b", "name": "same", "prompt": "y"}]}
        )
