# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import json

from textual.widgets import DataTable

from cmux.engine.store import RunManifest, RunPaths, SessionRecord
from cmux.events import Status
from cmux.ui.dashboard import CmuxApp


def _build_run(tmp_path, keys):
    paths = RunPaths(tmp_path, "run1")
    paths.write_manifest(RunManifest(run_id="run1", repo_root=str(tmp_path), config_path="", item_keys=list(keys)))
    for key in keys:
        record = SessionRecord(
            key=key,
            name=key,
            slug=key,
            branch=f"cmux/{key}",
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
        app = CmuxApp(str(tmp_path), "run1")
        async with app.run_test():
            assert app.query_one("#sessions", DataTable).row_count == 2
            assert app._selected_record().key == "alpha"

    asyncio.run(scenario())


def test_dashboard_cursor_navigation_changes_selection(tmp_path):
    _build_run(tmp_path, ["alpha", "beta"])

    async def scenario():
        app = CmuxApp(str(tmp_path), "run1")
        async with app.run_test() as pilot:
            await pilot.press("j")
            assert app._selected_record().key == "beta"
            await pilot.press("k")
            assert app._selected_record().key == "alpha"

    asyncio.run(scenario())


def test_dashboard_shows_selected_transcript(tmp_path):
    _build_run(tmp_path, ["alpha"])

    async def scenario():
        app = CmuxApp(str(tmp_path), "run1")
        async with app.run_test():
            assert app._shown_key == "alpha"

    asyncio.run(scenario())
