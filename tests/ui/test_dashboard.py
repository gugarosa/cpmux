# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import json
import sys
import threading
from contextlib import nullcontext

import pytest
from textual.widgets import DataTable, Input, ListView, RichLog, Static

from cpmux.config import Plan, ResolvedItem
from cpmux.engine.ownership import BusyError, file_lease, matching_process
from cpmux.engine.review import DiffSnapshot
from cpmux.engine.store import RunManifest, RunPaths, SessionRecord
from cpmux.engine.supervisor import Options, Supervisor
from cpmux.events import TERMINAL, Status
from cpmux.ui.dashboard import CpmuxApp, _premium_text
from cpmux.vcs import pr
from cpmux.vcs.git import GitError
from cpmux.vcs.pr import PRError


def _build_run(tmp_path, keys, statuses=None, prs=None):
    statuses = statuses or {}
    prs = prs or {}
    paths = RunPaths(tmp_path, "run1")
    items = Plan.model_validate({"items": [{"id": key, "prompt": f"Task {key}"} for key in keys]}).resolve()
    paths.write_manifest(
        RunManifest(run_id="run1", repo_root=str(tmp_path), config_path="", item_keys=list(keys), resolved=items)
    )

    for key in keys:
        record = SessionRecord(
            key=key,
            name=key,
            slug=key,
            branch=f"cpmux/{key}",
            base="main",
            model="gpt-5.5",
            session_id=f"sid-{key}",
            worktree=str(tmp_path / key),
            status=statuses.get(key, Status.DONE),
            pr_url=prs.get(key),
        )
        record.begin_attempt("initial")
        record.status = statuses.get(key, Status.DONE)
        record.phase = "agent" if record.status not in TERMINAL else "complete"
        if record.status in TERMINAL:
            record.finish_attempt()
        paths.write_record(record)
        paths.transcript(key).write_text(
            json.dumps({"type": "assistant.message", "data": {"content": f"hello from {key}"}}) + "\n"
        )

    return paths


def _report(items, paused=False):
    return {
        "schema_version": 1,
        "run_id": "run1",
        "managed": True,
        "owner_error": None,
        "expected_items": len(items),
        "recorded_items": len(items),
        "paused": paused,
        "reported_premium_requests": 7,
        "items_with_unknown_usage": 1,
        "premium_budget": 20,
        "budget_reached": False,
        "items": items,
    }


def _report_item(key, attention, status):
    verified = attention == "ready_for_review"
    verification = {"status": "stale"} if attention == "needs_verification" else {"status": "missing"}
    commands = []
    if verified:
        verification = {
            "status": "passed",
            "attempt": 1,
            "commit_sha": "a81c4e2",
            "tree_sha": "tree-a81c4e2",
            "check_fingerprint": "checks-v1",
            "checked_at": "2026-09-06T15:31:00+00:00",
        }
        commands = [
            {
                "name": "pytest",
                "phase": "check",
                "status": "passed",
                "exit_code": 0,
                "ended_at": "2026-09-06T15:31:00+00:00",
            }
        ]

    return {
        "key": key,
        "status": status,
        "phase": "agent",
        "attention": attention,
        "base": "main",
        "base_sha": "7449fef5",
        "base_from": None,
        "depends_on": [],
        "source": {"url": f"https://github.com/acme/app/issues/{len(key)}"},
        "candidate_sha": "a81c4e2",
        "delivery_sha": None,
        "pr_url": None,
        "last_activity_at": "2026-09-06T15:30:00+00:00",
        "reported_premium_requests": None if key == "delta" else 2,
        "usage_incomplete": key == "delta",
        "process": None,
        "verification": verification,
        "attempts": [{"number": 1, "commands": commands}],
    }


def test_premium_text_preserves_fractional_and_incomplete_usage():
    assert _premium_text(1.5, False) == "1.5"
    assert _premium_text(2.25, True) == "2.25?"
    assert _premium_text(0, True) == "0?"
    assert _premium_text(None, True) == "unknown"


def test_dashboard_populates_the_session_table(tmp_path):
    _build_run(tmp_path, ["alpha", "beta"])

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test():
            await app.workers.wait_for_complete()
            assert app.query_one("#sessions", DataTable).row_count == 2
            assert app.query_one("#sessions", DataTable).cursor_row == 0

    asyncio.run(scenario())


def test_dashboard_cursor_navigation_changes_selection(tmp_path):
    _build_run(tmp_path, ["alpha", "beta"])

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("j")
            assert app.query_one("#sessions", DataTable).cursor_row == 1
            await pilot.press("k")
            assert app.query_one("#sessions", DataTable).cursor_row == 0

    asyncio.run(scenario())


def test_dashboard_ranks_and_filters_attention_views(tmp_path, monkeypatch):
    statuses = {
        "alpha": Status.RUNNING,
        "beta": Status.DONE,
        "gamma": Status.FAILED,
        "delta": Status.DONE,
        "epsilon": Status.NO_CHANGES,
    }
    _build_run(tmp_path, statuses, statuses=statuses)
    report = _report(
        [
            _report_item("alpha", "running", "running"),
            _report_item("beta", "ready_for_review", "done"),
            _report_item("gamma", "failed", "failed"),
            _report_item("delta", "needs_verification", "done"),
            _report_item("epsilon", "completed", "no_changes"),
        ]
    )
    monkeypatch.setattr("cpmux.ui.dashboard.run_report", lambda *args: report)
    monkeypatch.setattr("cpmux.ui.dashboard.daemon.reconcile", lambda paths, records: records)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test(size=(120, 36)) as pilot:
            await app.workers.wait_for_complete()
            table = app.query_one("#sessions", DataTable)
            assert [table.get_row_at(row)[0] for row in range(table.row_count)] == [
                "gamma",
                "delta",
                "beta",
                "alpha",
                "epsilon",
            ]

            await pilot.press("2")
            assert [table.get_row_at(row)[0] for row in range(table.row_count)] == ["gamma", "delta"]
            await pilot.press("3")
            assert [table.get_row_at(row)[0] for row in range(table.row_count)] == ["beta"]
            await pilot.press("4")
            assert [table.get_row_at(row)[0] for row in range(table.row_count)] == ["gamma"]

    asyncio.run(scenario())


def test_dashboard_preserves_selected_item_across_reload(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha", "beta", "gamma"])
    report = _report(
        [
            _report_item("alpha", "running", "running"),
            _report_item("beta", "running", "running"),
            _report_item("gamma", "running", "running"),
        ]
    )
    monkeypatch.setattr("cpmux.ui.dashboard.run_report", lambda *args: report)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("j")
            assert app._selected_record().key == "beta"
            app.reload(force_report=True)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app._selected_record().key == "beta"

    asyncio.run(scenario())


def test_dashboard_switches_read_only_detail_tabs(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha"])
    monkeypatch.setattr(
        "cpmux.ui.dashboard.run_report",
        lambda *args: _report([_report_item("alpha", "running", "running")]),
    )

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("c")
            assert app._tab == "checks"
            assert app.query_one("#checks-view").display
            await pilot.press("i")
            assert app._tab == "details"
            assert "Premium" in str(app.query_one("#details", Static).render())
            await pilot.press("t")
            assert app._tab == "transcript"
            assert app.query_one("#transcript", RichLog).display

    asyncio.run(scenario())


def test_dashboard_diff_tab_loads_immutable_snapshot(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha"])
    snapshot = DiffSnapshot(
        revision="diff@a81c4e2",
        base_sha="7449fef5",
        head_sha="a81c4e2",
        text="--- a/file.py\n+++ b/file.py\n+new line",
    )
    monkeypatch.setattr("cpmux.ui.dashboard.diff_snapshot", lambda paths, record: snapshot)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("d")
            await app.workers.wait_for_complete()
            assert app._tab == "diff"
            assert app._diff_snapshots["alpha"] is snapshot
            assert app.query_one("#diff", RichLog).display

    asyncio.run(scenario())


def test_dashboard_dispatches_verification_and_finalization_actions(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha"], prs={"alpha": "https://github.com/acme/app/pull/7"})
    verified = []
    finalized = []

    async def verify(paths, record):
        verified.append(record.key)

    async def finalize(paths, record):
        finalized.append(record.key)

    monkeypatch.setattr("cpmux.ui.dashboard.run_verification", verify)
    monkeypatch.setattr("cpmux.ui.dashboard.run_finalization", finalize)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("v")
            await app.workers.wait_for_complete()
            await pilot.press("f")
            await app.workers.wait_for_complete()
            await pilot.press("u")
            await app.workers.wait_for_complete()

    asyncio.run(scenario())
    assert verified == ["alpha"]
    assert finalized == ["alpha", "alpha"]


def test_dashboard_update_pr_requires_existing_pr(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha"])
    finalized = []
    notifications = []

    async def finalize(paths, record):
        finalized.append(record.key)

    monkeypatch.setattr("cpmux.ui.dashboard.run_finalization", finalize)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append((message, kwargs)))
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            assert app.check_action("update_pr", ()) is False
            await pilot.press("u")
            app.action_update_pr()

    asyncio.run(scenario())
    assert finalized == []
    assert any("no existing PR" in message and kwargs.get("severity") == "warning" for message, kwargs in notifications)


def test_dashboard_feedback_passes_revision_and_surfaces_stale_rejection(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha"])
    snapshot = DiffSnapshot(revision="diff@old", base_sha="base", head_sha="head", text="+change")
    calls = []
    notifications = []
    monkeypatch.setattr("cpmux.ui.dashboard.diff_snapshot", lambda paths, record: snapshot)

    async def reject(paths, record, message, revision):
        calls.append((record.key, message, revision))
        raise ValueError("`revision` is stale; reload the diff.")

    monkeypatch.setattr("cpmux.ui.dashboard.run_feedback", reject)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append((message, kwargs)))
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.press("s")
            app.screen.query_one(Input).value = "please revise"
            await pilot.press("enter")
            await app.workers.wait_for_complete()

    asyncio.run(scenario())
    assert calls == [("alpha", "please revise", "diff@old")]
    assert any("stale" in message and kwargs.get("severity") == "error" for message, kwargs in notifications)


def test_dashboard_action_failure_is_not_silent(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha"])
    notifications = []

    async def fail(paths, record):
        raise OSError("verification unavailable")

    monkeypatch.setattr("cpmux.ui.dashboard.run_verification", fail)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append((message, kwargs)))
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("v")
            await app.workers.wait_for_complete()

    asyncio.run(scenario())
    assert any(
        message == "verification unavailable" and kwargs.get("severity") == "error" for message, kwargs in notifications
    )


def test_dashboard_surfaces_review_git_and_pr_errors(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha"], prs={"alpha": "https://github.com/acme/app/pull/7"})
    notifications = []

    def fail_diff(paths, record):
        raise GitError("diff unavailable")

    async def fail_finalization(paths, record):
        raise PRError("PR unavailable")

    monkeypatch.setattr("cpmux.ui.dashboard.diff_snapshot", fail_diff)
    monkeypatch.setattr("cpmux.ui.dashboard.run_finalization", fail_finalization)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append((message, kwargs)))
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.press("f")
            await app.workers.wait_for_complete()

    asyncio.run(scenario())
    assert ("diff unavailable", {"severity": "error"}) in notifications
    assert ("PR unavailable", {"severity": "error"}) in notifications


def test_dashboard_narrow_routes_between_queue_and_detail(tmp_path):
    _build_run(tmp_path, ["alpha"])

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            assert app.query_one("#queue").display
            assert not app.query_one("#right").display
            await pilot.press("enter")
            await pilot.pause()
            assert not app.query_one("#queue").display
            assert app.query_one("#right").display
            assert "hello from alpha" in "".join(line.text for line in app.query_one("#transcript", RichLog).lines)
            await pilot.press("escape")
            assert app.query_one("#queue").display
            assert not app.query_one("#right").display

    asyncio.run(scenario())


def test_dashboard_throttles_reporting_refresh(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha"])
    calls = []

    def report(*args):
        calls.append(args)
        return _report([_report_item("alpha", "completed", "done")])

    monkeypatch.setattr("cpmux.ui.dashboard.run_report", report)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test():
            await app.workers.wait_for_complete()
            assert len(calls) == 1
            app.reload()
            app.reload()
            await app.workers.wait_for_complete()
            assert len(calls) == 1
            app._report_loaded_at -= 6
            app.reload()
            await app.workers.wait_for_complete()
            assert len(calls) == 2

    asyncio.run(scenario())


def test_dashboard_preserves_existing_keyboard_bindings():
    keys = {binding.key for binding in CpmuxApp.BINDINGS}
    assert {"q", "slash", "e", "s", "o", "x", "r", "j", "k"} <= keys


def test_dashboard_pause_toggles_queue_admission(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha"], statuses={"alpha": Status.RUNNING})
    state = {"paused": False}

    def report(*args):
        return _report([_report_item("alpha", "running", "running")], paused=state["paused"])

    def set_paused(paths, paused):
        state["paused"] = paused
        if paused:
            paths.pause_file.touch()
        else:
            paths.pause_file.unlink()

    monkeypatch.setattr("cpmux.ui.dashboard.run_report", report)
    monkeypatch.setattr("cpmux.ui.dashboard.daemon.reconcile", lambda paths, records: records)
    monkeypatch.setattr("cpmux.ui.dashboard.daemon.set_paused", set_paused)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("p")
            await app.workers.wait_for_complete()
            assert state["paused"]
            assert app.sub_title.startswith("paused")
            await pilot.press("p")
            await app.workers.wait_for_complete()
            assert not state["paused"]
            assert "queue running" in app.sub_title

    asyncio.run(scenario())


def test_dashboard_shows_selected_transcript(tmp_path):
    _build_run(tmp_path, ["alpha"])

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test():
            await app.workers.wait_for_complete()
            assert app._shown_key == "alpha"

    asyncio.run(scenario())


def _write_transcript(paths, key, count):
    lines = [json.dumps({"type": "assistant.message", "data": {"content": f"line {index}"}}) for index in range(count)]
    paths.transcript(key).write_text("\n".join(lines) + "\n")


def test_dashboard_keeps_scroll_position_across_reload(tmp_path):
    paths = _build_run(tmp_path, ["alpha"])
    _write_transcript(paths, "alpha", 60)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test(size=(100, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            log = app.query_one("#transcript", RichLog)
            log.scroll_to(y=0, animate=False)
            await pilot.pause()
            assert log.scroll_y == 0
            app.reload()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert log.scroll_y == 0

    asyncio.run(scenario())


def test_dashboard_follows_live_output_at_bottom(tmp_path):
    paths = _build_run(tmp_path, ["alpha"])
    _write_transcript(paths, "alpha", 60)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test(size=(100, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            log = app.query_one("#transcript", RichLog)
            assert log.is_vertical_scroll_end
            before = log.max_scroll_y
            with paths.transcript("alpha").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "assistant.message", "data": {"content": "tail"}}) + "\n")
            app.reload()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert log.max_scroll_y > before
            assert log.is_vertical_scroll_end

    asyncio.run(scenario())


def test_dashboard_surfaces_item_dependencies(tmp_path):
    resolved = Plan.model_validate(
        {"items": [{"name": "alpha", "prompt": "x"}, {"name": "beta", "prompt": "y", "depends_on": ["alpha"]}]}
    ).resolve()
    paths = RunPaths(tmp_path, "run1")
    paths.write_manifest(
        RunManifest(
            run_id="run1", repo_root=str(tmp_path), config_path="", item_keys=["alpha", "beta"], resolved=resolved
        )
    )

    for item in resolved:
        record = SessionRecord(
            key=item.key,
            name=item.name,
            slug=item.slug,
            branch=item.branch,
            base="main",
            model="gpt-5.5",
            session_id="sid",
            worktree=str(tmp_path / item.key),
            status=Status.RUNNING,
        )
        paths.write_record(record)
        paths.transcript(item.key).write_text("")

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test():
            await app.workers.wait_for_complete()
            assert app.deps_by_key == {"alpha": [], "beta": ["alpha"]}
            app.query_one("#sessions", DataTable).move_cursor(row=1)
            app.action_tab_details()
            assert "alpha" in str(app.query_one("#details", Static).render())

    asyncio.run(scenario())


def test_dashboard_header_shows_selected_session_context(tmp_path):
    _build_run(tmp_path, ["alpha"])

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test():
            await app.workers.wait_for_complete()
            header = app.query_one("#transcript-header", Static)
            assert "alpha" in str(header.render())

    asyncio.run(scenario())


def test_dashboard_open_pr_without_pr_does_not_open_browser(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha"])
    opened = []
    monkeypatch.setattr("cpmux.ui.dashboard.webbrowser.open", lambda url: opened.append(url))

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test():
            await app.workers.wait_for_complete()
            app.action_open_pr()

    asyncio.run(scenario())
    assert opened == []


def test_dashboard_followup_reports_startup_failure(tmp_path, monkeypatch):
    paths = _build_run(tmp_path, ["alpha"])
    (tmp_path / "alpha").mkdir()
    monkeypatch.setattr("cpmux.engine.interact.followup_argv", lambda *args: [str(tmp_path / "missing-copilot")])
    notifications = []

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append((message, kwargs)))
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("s")
            app.screen.query_one(Input).value = "retry"
            await pilot.press("enter")
            await app.workers.wait_for_complete()

    asyncio.run(scenario())

    assert paths.read_record("alpha").status == Status.FAILED
    assert any("missing-copilot" in message and kwargs.get("severity") == "error" for message, kwargs in notifications)


def test_dashboard_accepts_navigation_while_report_inspection_is_blocked(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha", "beta"])
    entered = threading.Event()
    release = threading.Event()
    blocked = False

    def report(*args):
        if blocked:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test report was not released")
        return _report([_report_item(key, "completed", "done") for key in ("alpha", "beta")])

    monkeypatch.setattr("cpmux.ui.dashboard.run_report", report)

    async def scenario():
        nonlocal blocked
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            blocked = True
            app.reload(True)
            try:
                async with asyncio.timeout(3):
                    while not entered.is_set():
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(pilot.press("j"), 1)
                assert app._selected_record().key == "beta"
                assert not release.is_set()
            finally:
                release.set()
                await app.workers.wait_for_complete()

    asyncio.run(scenario())


def test_dashboard_restores_transcript_after_a_filtered_view_becomes_empty(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha"])
    attention = "ready_for_review"
    monkeypatch.setattr(
        "cpmux.ui.dashboard.run_report",
        lambda *args: _report([_report_item("alpha", attention, "done")]),
    )

    async def scenario():
        nonlocal attention
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test(size=(120, 36)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("3")
            attention = "completed"
            app.reload(True)
            await app.workers.wait_for_complete()
            assert app.query_one("#sessions", DataTable).row_count == 0
            attention = "ready_for_review"
            app.reload(True)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "hello from alpha" in "".join(line.text for line in app.query_one("#transcript", RichLog).lines)

    asyncio.run(scenario())


def test_dashboard_keeps_diff_scroll_position_across_refreshes(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha"])
    snapshot = DiffSnapshot("revision", "base", "head", "\n".join(f"+line {index}" for index in range(100)))
    monkeypatch.setattr("cpmux.ui.dashboard.diff_snapshot", lambda *args: snapshot)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test(size=(120, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("d")
            await app.workers.wait_for_complete()
            await pilot.pause()
            log = app.query_one("#diff", RichLog)
            log.scroll_to(y=10, animate=False)
            await pilot.pause()
            app.reload(True)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert log.scroll_y == 10

    asyncio.run(scenario())


@pytest.mark.parametrize(("key", "service"), [("v", "run_verification"), ("f", "run_finalization")])
def test_dashboard_does_not_announce_failed_outcomes_as_success(tmp_path, monkeypatch, key, service):
    _build_run(tmp_path, ["alpha"])
    notifications = []

    async def fail(paths, record):
        record.status = Status.FAILED
        record.error = "acceptance failed"
        paths.write_record(record)

    monkeypatch.setattr(f"cpmux.ui.dashboard.{service}", fail)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        monkeypatch.setattr(app, "notify", lambda message, **kwargs: notifications.append((message, kwargs)))
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press(key)
            await app.workers.wait_for_complete()

    asyncio.run(scenario())
    assert ("acceptance failed", {"severity": "error"}) in notifications
    assert not any("completed" in message for message, _ in notifications)


def test_dashboard_quit_reaps_its_followup_and_persists_the_stopped_attempt(tmp_path, monkeypatch):
    paths = _build_run(tmp_path, ["alpha"])
    (tmp_path / "alpha").mkdir()
    monkeypatch.setattr(
        "cpmux.engine.interact.followup_argv",
        lambda *args: [sys.executable, "-c", "import time; time.sleep(60)"],
    )
    identity = []

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            app._send_worker(app._selected_record(), "continue")
            async with asyncio.timeout(5):
                while (record := paths.read_record("alpha")).pid is None:
                    await asyncio.sleep(0.01)
            identity.extend([record.pid, record.pid_created_at])
            await pilot.press("q")
        assert matching_process(*identity) is None
        assert paths.read_record("alpha").status == Status.KILLED
        assert not paths.session_owner("alpha").exists()

    asyncio.run(scenario())


def test_dashboard_quit_waits_for_git_work_before_releasing_ownership(git_repo, monkeypatch):
    supervisor = Supervisor.create(
        Plan.model_validate({"items": [{"id": "alpha", "prompt": "x"}]}),
        str(git_repo),
        Options(open_pr=False),
    )
    monkeypatch.setattr(ResolvedItem, "spawn_argv", lambda *args: ["true"])
    asyncio.run(supervisor.run(headless=True))
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def commit(*args):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test commit was not released")
        with pytest.raises(BusyError):
            with file_lease(supervisor.paths.session_lock("alpha")):
                pytest.fail("session ownership ended before Git work")
        finished.set()
        return False

    monkeypatch.setattr(pr, "commit_all", commit)

    async def scenario():
        app = CpmuxApp(str(git_repo), supervisor.run_id)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("v")
            async with asyncio.timeout(5):
                while not entered.is_set():
                    await asyncio.sleep(0.01)
            timer = threading.Timer(0.2, release.set)
            timer.start()
            try:
                await pilot.press("q")
            finally:
                release.set()
                timer.join()
        assert finished.is_set()
        assert not supervisor.paths.session_owner("alpha").exists()

    asyncio.run(scenario())


def test_dashboard_handles_multibyte_partial_lines_and_transcript_truncation(tmp_path, monkeypatch):
    paths = _build_run(tmp_path, ["alpha"], statuses={"alpha": Status.RUNNING})
    monkeypatch.setattr("cpmux.ui.dashboard.daemon.reconcile", lambda paths, records: records)
    event = (
        json.dumps(
            {"type": "assistant.message", "data": {"content": "caf\u00e9"}},
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )
    split = event.index(b"\xc3") + 1
    paths.transcript("alpha").write_bytes(event[:split])

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test(size=(120, 36)) as pilot:
            await app.workers.wait_for_complete()
            assert app._transcript_offset == 0
            with paths.transcript("alpha").open("ab") as handle:
                handle.write(event[split:])
            app.reload()
            await app.workers.wait_for_complete()
            await pilot.pause()
            log = app.query_one("#transcript", RichLog)
            assert "".join(line.text for line in log.lines).count("caf\u00e9") == 1
            assert app._transcript_offset == len(event)
            paths.transcript("alpha").write_text(
                json.dumps({"type": "assistant.message", "data": {"content": "new"}}) + "\n"
            )
            app.reload()
            await app.workers.wait_for_complete()
            await pilot.pause()
            rendered = "".join(line.text for line in log.lines)
            assert "new" in rendered and "caf\u00e9" not in rendered

    asyncio.run(scenario())


def test_dashboard_search_stays_usable_during_background_refresh(tmp_path):
    _build_run(tmp_path, ["alpha", "beta"])

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("slash")
            app.screen.query_one(Input).value = "beta"
            await pilot.pause()
            app.reload(True)
            await app.workers.wait_for_complete()
            await pilot.pause()
            results = app.screen.query_one("#results", ListView)
            assert len(results.children) == 1
            results.focus()
            await pilot.press("enter")
            await pilot.pause()
            assert app._selected_record().key == "beta"

    asyncio.run(scenario())


def test_dashboard_detail_panels_support_keyboard_scrolling_on_narrow_screens(tmp_path):
    paths = _build_run(tmp_path, ["alpha"], statuses={"alpha": Status.FAILED})
    record = paths.read_record("alpha")
    record.error = "\n".join(f"diagnostic line {index}" for index in range(100))
    paths.write_record(record)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test(size=(80, 24)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.press("enter", "i")
            await pilot.pause()
            assert app.focused.id == "details-view"
            await pilot.press("pagedown")
            await pilot.pause()
            assert app.query_one("#details-view").scroll_y > 0
            await pilot.press("escape")
            assert app.focused.id == "sessions"
            assert app.query_one("#queue").display

    asyncio.run(scenario())


def test_dashboard_disables_mutations_while_the_supervisor_owns_the_run(tmp_path, monkeypatch):
    _build_run(tmp_path, ["alpha"])
    monkeypatch.setattr(
        "cpmux.ui.dashboard.run_report",
        lambda *args: _report([_report_item("alpha", "completed", "done")]),
    )

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test():
            await app.workers.wait_for_complete()
            assert app.check_action("verify", ()) is False
            assert app.check_action("send", ()) is False
            app._report["managed"] = False
            assert app.check_action("verify", ()) is True
            assert app.check_action("send", ()) is True

    asyncio.run(scenario())


def test_dashboard_native_entry_does_not_block_an_independent_followup(tmp_path, monkeypatch):
    paths = _build_run(tmp_path, ["alpha", "beta"])
    for key in ("alpha", "beta"):
        (tmp_path / key).mkdir()
    native_ready = tmp_path / "native-ready"
    release = tmp_path / "release-native"
    followed_up = tmp_path / "followup-finished"
    monkeypatch.setattr("cpmux.ui.dashboard.shutil.which", lambda name: sys.executable)
    monkeypatch.setattr(
        "cpmux.engine.interact.resume_interactive_argv",
        lambda *args: [
            sys.executable,
            "-c",
            f"from pathlib import Path; import time\nPath({str(native_ready)!r}).touch()\n"
            f"while not Path({str(release)!r}).exists(): time.sleep(0.01)",
        ],
    )
    monkeypatch.setattr(
        "cpmux.engine.interact.followup_argv",
        lambda *args: [sys.executable, "-c", f"from pathlib import Path; Path({str(followed_up)!r}).touch()"],
    )

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        monkeypatch.setattr(app, "suspend", nullcontext)
        async with app.run_test():
            await app.workers.wait_for_complete()
            timer = threading.Timer(5, release.touch)
            timer.start()
            native = asyncio.create_task(app.action_enter())
            try:
                async with asyncio.timeout(4):
                    while not native_ready.exists():
                        await asyncio.sleep(0.01)
                app._send_worker(paths.read_record("beta"), "continue")
                async with asyncio.timeout(4):
                    while not followed_up.exists():
                        await asyncio.sleep(0.01)
                assert not native.done()
            finally:
                release.touch()
                await asyncio.wait_for(native, 5)
                timer.cancel()
                timer.join()
                await app.workers.wait_for_complete()
        assert paths.read_record("alpha").pid is None
        assert paths.read_record("beta").pid is None

    asyncio.run(scenario())
