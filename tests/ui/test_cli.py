# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from importlib.metadata import version

import pytest
from typer.testing import CliRunner

from cpmux.engine.store import RunManifest, RunPaths, SessionRecord
from cpmux.events import Status
from cpmux.ui import cli
from cpmux.ui.cli import app

runner = CliRunner()


@pytest.mark.parametrize(
    ("argv", "expected_exit_code", "expected_substrings"),
    [
        pytest.param(["--version"], 0, ("cpmux",), id="version-reports-name"),
        pytest.param(["--help"], 0, ("Create & run", "Monitor"), id="help-groups-command-panels"),
    ],
)
def test_root_options_report_expected_output(argv, expected_exit_code, expected_substrings):
    result = runner.invoke(app, argv)
    assert result.exit_code == expected_exit_code
    for expected_substring in expected_substrings:
        assert expected_substring in result.output


def test_version_matches_installed_distribution():
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert result.output.strip() == f"cpmux {version('cpmux')}"


@pytest.mark.parametrize("concurrency", ["-1", "0", "65"])
def test_up_rejects_invalid_concurrency_before_launch(tmp_path, concurrency):
    path = tmp_path / "plan.yml"
    path.write_text("items: [x]\n")

    result = runner.invoke(app, ["up", str(path), "--dry-run", "--concurrency", concurrency])

    assert result.exit_code == 2
    assert "--concurrency" in result.output
    assert not (tmp_path / ".cpmux").exists()


@pytest.mark.parametrize(
    ("plan", "expected_substrings"),
    [
        pytest.param(
            "version: 1\nitems:\n  - fix the bug\n  - add a feature\n",
            ("spawn commands", "fix-the-bug"),
            id="resolved-plan-with-spawn-preview",
        ),
        pytest.param(
            "defaults:\n  port_base: 3000\nitems:\n  - fix a\n  - fix b\n",
            ("PORT=3000", "PORT=3001"),
            id="assigned-ports",
        ),
    ],
)
def test_up_dry_run_reports_plan_details(tmp_path, plan, expected_substrings):
    path = tmp_path / "p.yaml"
    path.write_text(plan)
    result = runner.invoke(app, ["up", str(path), "--dry-run"])
    assert result.exit_code == 0
    for expected_substring in expected_substrings:
        assert expected_substring in result.output


def test_ls_without_any_run_is_an_empty_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "no cpmux runs yet" in result.output


@pytest.mark.parametrize(
    ("argv", "expected_exit_code"),
    [
        pytest.param(["logs", "whatever"], 1, id="logs"),
        pytest.param(["enter", "whatever"], 1, id="enter"),
    ],
)
def test_commands_without_cpmux_dir_exit_one(tmp_path, monkeypatch, argv, expected_exit_code):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, argv)
    assert result.exit_code == expected_exit_code


def test_up_missing_config_path_exits_nonzero(tmp_path):
    result = runner.invoke(app, ["up", str(tmp_path / "missing.yaml"), "--dry-run"])
    assert result.exit_code != 0


def test_plan_text_writes_generated_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "synthesize_plan", lambda transcript, model: "items:\n  - fix the bug\n")
    out = tmp_path / "out.yml"
    result = runner.invoke(app, ["plan", str(out), "--text", "fix the bug"])
    assert result.exit_code == 0
    assert out.read_text() == "items:\n  - fix the bug\n"


def test_plan_text_up_launches_generated_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "synthesize_plan", lambda transcript, model: "items:\n  - fix the bug\n")
    launched = {}
    monkeypatch.setattr(
        cli, "_launch_run", lambda file, options, detach, yes: launched.update(file=file, open_pr=options.open_pr)
    )
    out = tmp_path / "out.yml"
    result = runner.invoke(app, ["plan", str(out), "--text", "fix the bug", "--up"])
    assert result.exit_code == 0
    assert launched["file"] == out
    assert launched["open_pr"] is True


def test_plan_up_no_pr_disables_pull_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "synthesize_plan", lambda transcript, model: "items:\n  - fix the bug\n")
    launched = {}
    monkeypatch.setattr(cli, "_launch_run", lambda file, options, detach, yes: launched.update(open_pr=options.open_pr))
    result = runner.invoke(app, ["plan", str(tmp_path / "out.yml"), "--text", "fix the bug", "--up", "--no-pr"])
    assert result.exit_code == 0
    assert launched["open_pr"] is False


def test_plan_synthesis_failure_exits_one(tmp_path, monkeypatch):
    def _boom(transcript, model):
        raise cli.VoiceError("copilot failed.")

    monkeypatch.setattr(cli, "synthesize_plan", _boom)
    result = runner.invoke(app, ["plan", str(tmp_path / "out.yml"), "--text", "fix the bug"])
    assert result.exit_code == 1


def test_plan_without_input_composes_in_editor(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.click, "edit", lambda **kwargs: "fix the bug")
    monkeypatch.setattr(cli, "synthesize_plan", lambda transcript, model: f"items:\n  - {transcript}\n")
    out = tmp_path / "out.yml"
    result = runner.invoke(app, ["plan", str(out)])
    assert result.exit_code == 0
    assert out.read_text() == "items:\n  - fix the bug\n"


def test_plan_empty_editor_exits_one(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.click, "edit", lambda **kwargs: None)
    result = runner.invoke(app, ["plan", str(tmp_path / "out.yml")])
    assert result.exit_code == 1


def test_plan_voice_records_instead_of_editor(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "record_and_transcribe", lambda *args, **kwargs: "spoken plan")
    monkeypatch.setattr(cli, "synthesize_plan", lambda transcript, model: f"items:\n  - {transcript}\n")
    out = tmp_path / "out.yml"
    result = runner.invoke(app, ["plan", str(out), "--voice"])
    assert result.exit_code == 0
    assert out.read_text() == "items:\n  - spoken plan\n"


@pytest.mark.parametrize(
    ("argv", "expected_exit_code"),
    [
        pytest.param(["search", "login", "--fts", "--regex"], 1, id="search-fts-with-regex"),
        pytest.param(["plan", "out.yml", "--text", "x", "--voice"], 1, id="plan-text-with-voice"),
    ],
)
def test_commands_with_conflicting_options_exit_one(tmp_path, monkeypatch, argv, expected_exit_code):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, argv)
    assert result.exit_code == expected_exit_code


def test_search_fts_reports_store_unavailable(tmp_path, monkeypatch):
    from cpmux.engine.store import RunManifest, RunPaths, SessionRecord

    paths = RunPaths(tmp_path, "run1")
    paths.write_manifest(RunManifest(run_id="run1", repo_root=str(tmp_path), config_path="", item_keys=["a"]))
    paths.write_record(
        SessionRecord(
            key="a",
            name="a",
            slug="a",
            branch="cpmux/a",
            base="main",
            model="m",
            session_id="sid-a",
            worktree=str(tmp_path / "a"),
        )
    )

    def _boom(session_ids, query):
        from cpmux.engine.copilot_store import CopilotStoreUnavailable

        raise CopilotStoreUnavailable("store gone.")

    monkeypatch.setattr(cli, "search_sessions", _boom)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["search", "login", "--fts"])
    assert result.exit_code == 1


def test_rm_exits_nonzero_when_a_worktree_cannot_be_removed(monkeypatch):
    from cpmux.engine.store import RunManifest, SessionRecord

    record = SessionRecord(
        key="alpha",
        name="alpha",
        slug="alpha",
        branch="cpmux/alpha",
        base="main",
        model="m",
        session_id="sid",
        worktree="/tmp/alpha",
    )
    manifest = RunManifest(run_id="run1", repo_root="/tmp", config_path="", item_keys=["alpha"])
    monkeypatch.setattr(cli, "_run_id_or_exit", lambda run, *a: "run1")
    monkeypatch.setattr(cli.daemon, "owner_alive", lambda paths: False)
    monkeypatch.setattr(cli, "load_run", lambda root, run_id: (manifest, [record]))
    monkeypatch.setattr(cli, "remove_worktree", lambda *a, **k: False)
    monkeypatch.setattr(cli, "prune_worktrees", lambda *a, **k: None)

    result = runner.invoke(app, ["rm", "--yes"])
    assert result.exit_code == 1


def test_rm_refuses_active_run(monkeypatch):
    monkeypatch.setattr(cli, "_run_id_or_exit", lambda run, *a: "run1")
    monkeypatch.setattr(cli.daemon, "owner_alive", lambda paths: True)

    result = runner.invoke(app, ["rm", "--yes"])
    assert result.exit_code == 1


def test_init_writes_a_valid_starter_plan(tmp_path, monkeypatch):
    from cpmux.config import load_plan

    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert (tmp_path / "cpmux.yml").exists()
    assert load_plan(tmp_path / "cpmux.yml").items


def test_init_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cpmux.yml").write_text("items: [do a thing]\n")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 1


def test_plan_refuses_to_overwrite_existing_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "out.yml").write_text("items: [x]\n")
    called = {}
    monkeypatch.setattr(cli, "synthesize_plan", lambda *a: called.setdefault("ran", True) or "items: [x]\n")
    result = runner.invoke(app, ["plan", "out.yml", "--text", "x"])
    assert result.exit_code == 1
    assert "ran" not in called


def test_search_rejects_invalid_regex(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["search", "[", "--regex"])
    assert result.exit_code == 1
    assert "regex" in result.output


def test_up_without_copilot_exits_cleanly(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "cpmux.yml").write_text("items: [do a thing]\n")
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    result = runner.invoke(app, ["up", "--yes"])
    assert result.exit_code == 1
    assert "copilot" in result.output


def test_search_groups_and_counts_matches(tmp_path, monkeypatch):
    import json

    from cpmux.engine.store import RunManifest, RunPaths, SessionRecord
    from cpmux.events import Status

    monkeypatch.chdir(tmp_path)
    paths = RunPaths(tmp_path, "run1")
    paths.write_manifest(RunManifest(run_id="run1", repo_root=str(tmp_path), config_path="", item_keys=["auth"]))
    record = SessionRecord(
        key="auth",
        name="auth",
        slug="auth",
        branch="cpmux/auth",
        base="main",
        model="m",
        session_id="s",
        worktree=str(tmp_path / "auth"),
        status=Status.DONE,
    )
    paths.write_record(record)
    paths.transcript("auth").write_text(
        json.dumps({"type": "user.message", "data": {"content": "fix the authorization retry"}}) + "\n"
    )

    result = runner.invoke(app, ["search", "authorization", "--run", "run1"])
    assert result.exit_code == 0
    assert "auth" in result.output
    assert "match(es) in 1 session(s)" in result.output


def test_rm_purge_deletes_run_history(monkeypatch):
    from cpmux.engine.store import RunManifest, SessionRecord

    record = SessionRecord(
        key="alpha",
        name="alpha",
        slug="alpha",
        branch="cpmux/alpha",
        base="main",
        model="m",
        session_id="sid",
        worktree="/tmp/alpha",
    )
    manifest = RunManifest(run_id="run1", repo_root="/tmp", config_path="", item_keys=["alpha"])
    purged = []
    monkeypatch.setattr(cli, "_run_id_or_exit", lambda run, *a: "run1")
    monkeypatch.setattr(cli.daemon, "owner_alive", lambda paths: False)
    monkeypatch.setattr(cli, "load_run", lambda root, run_id: (manifest, [record]))
    monkeypatch.setattr(cli, "remove_worktree", lambda *a, **k: True)
    monkeypatch.setattr(cli, "prune_worktrees", lambda *a, **k: None)
    monkeypatch.setattr(cli, "delete_run", lambda root, run_id: purged.append(run_id))

    result = runner.invoke(app, ["rm", "--purge", "--yes"])
    assert result.exit_code == 0
    assert purged == ["run1"]


def test_up_defaults_to_detached(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli, "_launch_run", lambda file, options, detach, yes: seen.update(detach=detach))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["up", "--yes"])
    assert result.exit_code == 0
    assert seen["detach"] is True


def test_up_foreground_flag_stays_attached(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(cli, "_launch_run", lambda file, options, detach, yes: seen.update(detach=detach))
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["up", "--foreground", "--yes"])
    assert result.exit_code == 0
    assert seen["detach"] is False


def test_send_reports_startup_failure_and_persists_it(tmp_path, monkeypatch):
    paths = RunPaths(tmp_path, "run1")
    paths.write_manifest(RunManifest(run_id="run1", repo_root=str(tmp_path), config_path="", item_keys=["a"]))
    paths.write_record(
        SessionRecord(
            key="a",
            name="a",
            slug="a",
            branch="cpmux/a",
            base="main",
            model="m",
            session_id="sid",
            worktree=str(tmp_path),
            status=Status.DONE,
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_require_tool", lambda *args: None)
    monkeypatch.setattr(cli, "followup_argv", lambda *args: [str(tmp_path / "missing-copilot")])

    result = runner.invoke(app, ["send", "a", "retry"])

    assert result.exit_code == 1
    assert "missing-copilot" in result.output
    assert "could not start" in result.output
    assert paths.read_record("a").status == Status.FAILED


@pytest.mark.parametrize("command", ["ls", "attach", "rm"])
def test_run_commands_reject_escaping_run_ids(tmp_path, monkeypatch, command):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [command, "--run", "../outside"])

    assert result.exit_code == 1
    assert "`run_id` must be a normalized relative identifier" in result.output


@pytest.mark.parametrize("command", ["logs", "enter", "kill"])
def test_session_commands_reject_escaping_keys(tmp_path, monkeypatch, command):
    paths = RunPaths(tmp_path, "run1")
    paths.write_manifest(RunManifest(run_id="run1", repo_root=str(tmp_path), config_path=""))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, [command, "../outside"])

    assert result.exit_code == 1
    assert "`key` must be a normalized relative identifier" in result.output
