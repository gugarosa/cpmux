# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import json

from textual.widgets import DataTable, RichLog

from cpmux.config import Plan
from cpmux.engine.store import RunManifest, RunPaths, SessionRecord
from cpmux.events import Status
from cpmux.ui.dashboard import CpmuxApp


def _build_run(tmp_path, keys):
    paths = RunPaths(tmp_path, "run1")
    paths.write_manifest(RunManifest(run_id="run1", repo_root=str(tmp_path), config_path="", item_keys=list(keys)))

    for key in keys:
        record = SessionRecord(
            key=key,
            name=key,
            slug=key,
            branch=f"cpmux/{key}",
            base="main",
            model="gpt-5.5",
            session_id="sid",
            worktree=str(tmp_path / key),
            status=Status.DONE,
        )
        paths.write_record(record)
        paths.transcript(key).write_text(
            json.dumps({"type": "assistant.message", "data": {"content": f"hello from {key}"}}) + "\n"
        )

    return paths


def test_dashboard_populates_the_session_table(tmp_path):
    _build_run(tmp_path, ["alpha", "beta"])

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test():
            assert app.query_one("#sessions", DataTable).row_count == 2
            assert app.query_one("#sessions", DataTable).cursor_row == 0

    asyncio.run(scenario())


def test_dashboard_cursor_navigation_changes_selection(tmp_path):
    _build_run(tmp_path, ["alpha", "beta"])

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test() as pilot:
            await pilot.press("j")
            assert app.query_one("#sessions", DataTable).cursor_row == 1
            await pilot.press("k")
            assert app.query_one("#sessions", DataTable).cursor_row == 0

    asyncio.run(scenario())


def test_dashboard_shows_selected_transcript(tmp_path):
    _build_run(tmp_path, ["alpha"])

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test():
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
            await pilot.pause()
            log = app.query_one("#transcript", RichLog)
            log.scroll_to(y=0, animate=False)
            await pilot.pause()
            assert log.scroll_y == 0
            app.reload()
            await pilot.pause()
            assert log.scroll_y == 0

    asyncio.run(scenario())


def test_dashboard_follows_live_output_at_bottom(tmp_path):
    paths = _build_run(tmp_path, ["alpha"])
    _write_transcript(paths, "alpha", 60)

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            log = app.query_one("#transcript", RichLog)
            assert log.is_vertical_scroll_end
            before = log.max_scroll_y
            with paths.transcript("alpha").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"type": "assistant.message", "data": {"content": "tail"}}) + "\n")
            app.reload()
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
            assert app.deps_by_key == {"alpha": [], "beta": ["alpha"]}
            table = app.query_one("#sessions", DataTable)
            deps_col = [table.get_row_at(row)[3].plain for row in range(table.row_count)]
            assert deps_col == ["-", "alpha"]

    asyncio.run(scenario())


def test_dashboard_header_shows_selected_session_context(tmp_path):
    _build_run(tmp_path, ["alpha"])

    async def scenario():
        app = CpmuxApp(str(tmp_path), "run1")
        async with app.run_test():
            from textual.widgets import Static

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
            app.action_open_pr()

    asyncio.run(scenario())
    assert opened == []
