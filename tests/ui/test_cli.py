# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from typer.testing import CliRunner

from cmux.ui import cli
from cmux.ui.cli import app

runner = CliRunner()


def test_version_reports_name_and_exits_zero():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "cmux" in result.output


def test_up_dry_run_resolves_plan_without_spawning(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text("version: 1\nitems:\n  - fix the bug\n  - add a feature\n")
    result = runner.invoke(app, ["up", str(path), "--dry-run"])
    assert result.exit_code == 0
    assert "spawn commands" in result.output
    assert "fix-the-bug" in result.output


def test_ls_without_cmux_dir_exits_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 1


def test_logs_without_cmux_dir_exits_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["logs", "whatever"])
    assert result.exit_code == 1


def test_enter_without_cmux_dir_exits_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["enter", "whatever"])
    assert result.exit_code == 1


def test_up_missing_config_path_exits_nonzero(tmp_path):
    result = runner.invoke(app, ["up", str(tmp_path / "missing.yaml"), "--dry-run"])
    assert result.exit_code != 0


def test_voice_text_writes_generated_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "synthesize_plan", lambda transcript, model: "items:\n  - fix the bug\n")
    out = tmp_path / "out.yml"
    result = runner.invoke(app, ["voice", str(out), "--text", "fix the bug"])
    assert result.exit_code == 0
    assert out.read_text() == "items:\n  - fix the bug\n"


def test_voice_text_up_launches_generated_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "synthesize_plan", lambda transcript, model: "items:\n  - fix the bug\n")
    launched = {}
    monkeypatch.setattr(
        cli, "_launch_run", lambda file, options, detach, yes: launched.update(file=file, open_pr=options.open_pr)
    )
    out = tmp_path / "out.yml"
    result = runner.invoke(app, ["voice", str(out), "--text", "fix the bug", "--up"])
    assert result.exit_code == 0
    assert launched["file"] == out
    assert launched["open_pr"] is True


def test_voice_up_no_pr_disables_pull_requests(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "synthesize_plan", lambda transcript, model: "items:\n  - fix the bug\n")
    launched = {}
    monkeypatch.setattr(cli, "_launch_run", lambda file, options, detach, yes: launched.update(open_pr=options.open_pr))
    result = runner.invoke(app, ["voice", str(tmp_path / "out.yml"), "--text", "fix the bug", "--up", "--no-pr"])
    assert result.exit_code == 0
    assert launched["open_pr"] is False


def test_voice_synthesis_failure_exits_one(tmp_path, monkeypatch):
    def _boom(transcript, model):
        raise cli.VoiceError("copilot failed.")

    monkeypatch.setattr(cli, "synthesize_plan", _boom)
    result = runner.invoke(app, ["voice", str(tmp_path / "out.yml"), "--text", "fix the bug"])
    assert result.exit_code == 1


def test_up_dry_run_shows_assigned_ports(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text("defaults:\n  port_base: 3000\nitems:\n  - fix a\n  - fix b\n")
    result = runner.invoke(app, ["up", str(path), "--dry-run"])
    assert result.exit_code == 0
    assert "PORT=3000" in result.output
    assert "PORT=3001" in result.output


def test_search_fts_rejects_regex_combination(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["search", "login", "--fts", "--regex"])
    assert result.exit_code == 1


def test_search_fts_reports_store_unavailable(tmp_path, monkeypatch):
    from cmux.engine.store import RunManifest, RunPaths, SessionRecord

    paths = RunPaths(tmp_path, "run1")
    paths.write_manifest(RunManifest(run_id="run1", repo_root=str(tmp_path), config_path="", item_keys=["a"]))
    paths.write_record(
        SessionRecord(
            key="a",
            name="a",
            slug="a",
            branch="cmux/a",
            base="main",
            model="m",
            session_id="sid-a",
            worktree=str(tmp_path / "a"),
        )
    )

    def _boom(session_ids, query):
        from cmux.engine.copilot_store import CopilotStoreUnavailable

        raise CopilotStoreUnavailable("store gone.")

    monkeypatch.setattr(cli, "search_sessions", _boom)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["search", "login", "--fts"])
    assert result.exit_code == 1
