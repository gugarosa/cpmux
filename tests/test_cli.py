# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from typer.testing import CliRunner

from cmux.cli import app

runner = CliRunner()


def test_version_reports_name_and_exits_zero():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "cmux" in result.output
    assert "cmux" in result.stdout


def test_up_dry_run_resolves_plan_without_spawning(tmp_path):
    path = tmp_path / "p.yaml"
    path.write_text("version: 1\nitems:\n  - fix the bug\n  - add a feature\n")
    result = runner.invoke(app, ["up", str(path), "--dry-run"])
    assert result.exit_code == 0


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
