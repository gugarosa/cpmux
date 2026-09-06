# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import json
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import pytest
from click import unstyle
from typer.testing import CliRunner

from cpmux.config import Plan, ResolvedItem, load_plan
from cpmux.engine.copilot_store import CopilotStoreUnavailable
from cpmux.engine.store import RunManifest, RunPaths, SessionRecord
from cpmux.engine.supervisor import Options, Supervisor
from cpmux.events import SessionState, Status
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
    output = unstyle(result.output)

    assert result.exit_code == 2
    assert "--concurrency" in output
    assert "range" in output
    assert not (tmp_path / ".cpmux").exists()


@pytest.mark.parametrize("concurrency", ["1", "64"])
def test_up_accepts_concurrency_bounds(tmp_path, concurrency):
    path = tmp_path / "plan.yml"
    path.write_text("items: [x]\n")

    result = runner.invoke(app, ["up", str(path), "--dry-run", "--concurrency", concurrency])

    assert result.exit_code == 0
    assert f"max {concurrency} concurrent" in unstyle(result.output)


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
    assert "create one with" in result.output


def test_up_reports_unreadable_config_without_a_create_hint(tmp_path, monkeypatch):
    path = tmp_path / "plan.yaml"
    path.write_text("items: [x]\n")
    original_open = Path.open

    def open_path(self, *args, **kwargs):
        if self == path:
            raise PermissionError("permission denied")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_path)

    result = runner.invoke(app, ["up", str(path), "--dry-run"])

    assert result.exit_code == 1
    assert "permission denied" in result.output
    assert "create one with" not in result.output


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
    assert "`plan` text is None or blank." in result.output


def test_plan_editor_failure_is_reported_without_a_framework_traceback(tmp_path, monkeypatch):
    def fail_editor(**kwargs):
        raise cli.click.ClickException("editor could not start")

    monkeypatch.setattr(cli.click, "edit", fail_editor)
    output = tmp_path / "plan.yml"

    result = runner.invoke(app, ["plan", str(output)])

    assert result.exit_code == 1
    assert "editor could not start" in unstyle(result.output)
    assert not output.exists()


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
        raise CopilotStoreUnavailable("store gone.")

    monkeypatch.setattr(cli, "search_sessions", _boom)
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["search", "login", "--fts"])
    assert result.exit_code == 1


def test_rm_exits_nonzero_when_a_worktree_cannot_be_removed(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
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


def test_rm_refuses_active_run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_run_id_or_exit", lambda run, *a: "run1")
    monkeypatch.setattr(cli.daemon, "owner_alive", lambda paths: True)

    result = runner.invoke(app, ["rm", "--yes"])
    assert result.exit_code == 1


def test_init_writes_a_valid_starter_plan(tmp_path, monkeypatch):
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


def test_rm_purge_deletes_run_history(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
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
    monkeypatch.setattr("cpmux.engine.interact.followup_argv", lambda *args: [str(tmp_path / "missing-copilot")])

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


def _terminal_run(tmp_path, status=Status.DONE):
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
            status=status,
        )
    )
    return paths


@pytest.mark.parametrize(("status", "code"), [(Status.DONE, 0), (Status.FAILED, 1), (Status.PENDING, 2)])
def test_wait_reports_terminal_or_unowned_outcomes(tmp_path, monkeypatch, status, code):
    _terminal_run(tmp_path, status)
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["wait", "--json"])

    assert result.exit_code == code
    assert json.loads(result.stdout)["items"][0]["status"] == status.value


def test_wait_times_out_without_stopping_active_work(tmp_path, monkeypatch):
    paths = _terminal_run(tmp_path, Status.RUNNING)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.daemon, "reconcile", lambda paths, records: records)
    monkeypatch.setattr(cli.daemon, "owner_alive", lambda paths: True)

    result = runner.invoke(app, ["wait", "--timeout", "0"])

    assert result.exit_code == 124
    assert "work continues" in unstyle(result.output)
    assert not paths.stop_file().exists()
    assert paths.read_record("a").status == Status.RUNNING


def test_pause_and_unpause_only_change_queue_admission(tmp_path, monkeypatch):
    paths = _terminal_run(tmp_path)
    monkeypatch.chdir(tmp_path)
    before = paths.record_file("a").read_bytes()

    assert runner.invoke(app, ["pause"]).exit_code == 0
    assert paths.pause_file.exists()
    assert runner.invoke(app, ["unpause"]).exit_code == 0
    assert not paths.pause_file.exists()
    assert paths.record_file("a").read_bytes() == before
    assert not paths.stop_file().exists()


def test_retry_recovers_prepared_work_with_no_paid_calls(git_repo, monkeypatch):
    supervisor = Supervisor.create(
        Plan.model_validate({"items": [{"id": "a", "prompt": "x"}]}), str(git_repo), Options(open_pr=False)
    )
    supervisor.prepare()
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(cli, "_require_tool", lambda *args: None)
    monkeypatch.setattr(ResolvedItem, "spawn_argv", lambda *args: ["true"])

    result = runner.invoke(app, ["retry", "--yes"])

    assert result.exit_code == 0
    assert supervisor.paths.read_record("a").status == Status.NO_CHANGES
    assert supervisor.paths.read_record("a").attempts[-1].mode == "retry"


def test_retry_rejects_conflicting_modes_without_writing_state(tmp_path, monkeypatch):
    paths = _terminal_run(tmp_path)
    monkeypatch.chdir(tmp_path)
    before = paths.record_file("a").read_bytes()

    result = runner.invoke(app, ["retry", "a", "--resume", "--fresh", "--yes"])

    assert result.exit_code == 1
    assert "mutually exclusive" in unstyle(result.output)
    assert paths.record_file("a").read_bytes() == before


def test_issues_writes_an_editable_plan_without_starting_an_agent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_require_tool", lambda *args: None)
    fetched = []
    imported = [object()]

    def fetch(root, references, **kwargs):
        fetched.append((root, references, kwargs))
        return imported

    def render(issues, template, profile):
        assert issues is imported
        assert template is None
        assert profile is None
        return "items: [review imported issue]\n"

    monkeypatch.setattr(cli, "fetch_issues", fetch)
    monkeypatch.setattr(cli, "issues_plan", render)
    monkeypatch.setattr(cli, "_launch_run", lambda *args: pytest.fail("issue intake launched an agent"))

    result = runner.invoke(app, ["issues", "42", "--repo", "owner/repo", "--output", "issues.yml"])

    assert result.exit_code == 0
    assert fetched == [(".", ["42"], {"repository": "owner/repo", "query": None, "limit": 20})]
    assert load_plan(tmp_path / "issues.yml").items[0].prompt == "review imported issue"
    assert "no agents were started" in unstyle(result.output)


def test_issues_does_not_overwrite_an_existing_plan_by_default(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "cpmux.yml"
    path.write_text("existing plan")
    monkeypatch.setattr(cli, "fetch_issues", lambda *args, **kwargs: pytest.fail("unnecessary GitHub request"))

    result = runner.invoke(app, ["issues", "42"])

    assert result.exit_code == 1
    assert path.read_text() == "existing plan"


def test_diff_json_exposes_review_identity_without_mutation(tmp_path, monkeypatch):
    paths = _terminal_run(tmp_path)
    monkeypatch.chdir(tmp_path)
    before = paths.record_file("a").read_bytes()
    snapshot = SimpleNamespace(revision="token", base_sha="base", head_sha="head", text="diff --git a/x b/x\n")
    monkeypatch.setattr(cli, "diff_snapshot", lambda paths, record: snapshot)

    result = runner.invoke(app, ["diff", "a", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == vars(snapshot)
    assert paths.record_file("a").read_bytes() == before


def test_feedback_forwards_the_required_review_revision(tmp_path, monkeypatch):
    _terminal_run(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_require_tool", lambda *args: None)
    calls = []

    async def repair(paths, record, message, revision, *, file_path=None, line=None):
        calls.append((record.key, message, revision, file_path, line))
        return SessionState(status=Status.DONE, exit_code=0, last_text="repaired")

    monkeypatch.setattr(cli, "run_feedback", repair)
    result = runner.invoke(
        app,
        [
            "feedback",
            "a",
            "handle the boundary",
            "--revision",
            "review-token",
            "--file",
            "README.md",
            "--line",
            "1",
        ],
    )

    assert result.exit_code == 0
    assert calls == [("a", "handle the boundary", "review-token", "README.md", 1)]
    assert "repaired" in result.output


@pytest.mark.parametrize(("command", "operation"), [("verify", "run_verification"), ("finalize", "run_finalization")])
def test_review_commands_surface_failed_outcomes_without_requiring_an_agent(tmp_path, monkeypatch, command, operation):
    _terminal_run(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_require_tool", lambda *args: pytest.fail("verification requested an agent"))

    async def failed(paths, record):
        record.status = Status.FAILED
        record.error = "acceptance check failed."

    monkeypatch.setattr(cli, operation, failed)

    result = runner.invoke(app, [command, "a"])

    assert result.exit_code == 1
    assert "acceptance check failed" in unstyle(result.output)
