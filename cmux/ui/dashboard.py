# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import shutil
import subprocess
from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
)

from cmux.engine import daemon
from cmux.engine.interact import followup_argv, resume_interactive_argv
from cmux.engine.session import SessionRunner
from cmux.engine.store import RunPaths, SessionRecord, load_run
from cmux.events import parse_line
from cmux.ui.render import STATUS_COLOR, event_text
from cmux.ui.search import search_transcripts


class SearchScreen(ModalScreen[str | None]):
    """Full-text search overlay that returns the key of the chosen session."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, items: list[tuple[str, Path]]) -> None:
        """Initialize the search overlay.

        Args:
            items: `(label, transcript_path)` pairs to search.

        """

        super().__init__()
        self.items = items

    def compose(self) -> ComposeResult:
        yield Input(placeholder="search transcripts…", id="query")
        yield ListView(id="results")

    def on_input_changed(self, event: Input.Changed) -> None:
        results = self.query_one("#results", ListView)
        results.clear()

        query = event.value.strip()
        if not query:
            return

        for hit in search_transcripts(self.items, query)[:50]:
            results.append(ListItem(Label(f"{hit.label}  ·  {hit.role}  ·  {hit.snippet}"), name=hit.label))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.name)

    def action_close(self) -> None:
        """Close the search overlay."""

        self.dismiss(None)


class SendScreen(ModalScreen[str | None]):
    """Prompt overlay that returns a follow-up message to send to a session."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def compose(self) -> ComposeResult:
        yield Input(placeholder="follow-up message…", id="message")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_close(self) -> None:
        """Close the message overlay."""

        self.dismiss(None)


class CmuxApp(App):
    """Interactive cmux dashboard for a single run."""

    CSS = """
    #sessions { width: 45%; border-right: solid $panel; }
    #transcript { width: 1fr; padding: 0 1; }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "search", "Search"),
        Binding("e", "enter", "Enter"),
        Binding("s", "send", "Send"),
        Binding("r", "refresh", "Refresh"),
        Binding("j", "cursor_down", "Down"),
        Binding("k", "cursor_up", "Up"),
    ]

    def __init__(self, start_path: str, run_id: str) -> None:
        """Initialize the dashboard.

        Args:
            start_path: Path inside the target git repository.
            run_id: Identifier of the run to display.

        """

        super().__init__()

        self.start_path = Path(start_path)
        self.run_id = run_id
        self.paths = RunPaths(start_path, run_id)
        self.records: list[SessionRecord] = []
        self._shown_key: str | None = None
        self._transcript_len = 0

    def compose(self) -> ComposeResult:
        yield Header()

        with Horizontal():
            yield DataTable(id="sessions")
            yield RichLog(id="transcript", wrap=True)

        yield Footer()

    def on_mount(self) -> None:
        self.title = f"cmux · {self.run_id}"
        table = self.query_one("#sessions", DataTable)
        table.cursor_type = "row"
        table.add_columns("item", "status", "model", "branch / PR")

        self.reload()
        self.set_interval(1.0, self.reload)

    def reload(self) -> None:
        """Reload records from disk, reconcile crashes, and refresh both panes."""

        try:
            _, records = load_run(self.start_path, self.run_id)
        except FileNotFoundError:
            return

        self.records = daemon.reconcile(self.paths, records)
        self._refresh_table()
        self._refresh_transcript()

    def _refresh_table(self) -> None:
        table = self.query_one("#sessions", DataTable)
        cursor = table.cursor_row
        table.clear()

        for record in self.records:
            style = STATUS_COLOR.get(record.status, "cyan")
            table.add_row(record.key, Text(record.status, style=style), record.model, record.pr_url or record.branch)

        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1))

    def _selected_record(self) -> SessionRecord | None:
        table = self.query_one("#sessions", DataTable)
        if not self.records or table.row_count == 0:
            return None

        return self.records[table.cursor_row]

    def _refresh_transcript(self, force: bool = False) -> None:
        record = self._selected_record()
        if record is None:
            return

        transcript = self.paths.transcript(record.key)
        text = transcript.read_text(encoding="utf-8") if transcript.exists() else ""
        log = self.query_one("#transcript", RichLog)

        if force or record.key != self._shown_key:
            log.clear()
            self._write_events(log, text)
            self._shown_key = record.key
            self._transcript_len = len(text)
        elif len(text) > self._transcript_len:
            boundary = text.rfind("\n", self._transcript_len) + 1
            if boundary > self._transcript_len:
                self._write_events(log, text[self._transcript_len : boundary])
                self._transcript_len = boundary

    def _write_events(self, log: RichLog, text: str) -> None:
        for line in text.splitlines():
            event = parse_line(line)
            if event is None:
                continue
            renderable = event_text(event)
            if renderable is not None:
                log.write(renderable)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._refresh_transcript(force=True)

    def action_cursor_down(self) -> None:
        """Move the session cursor down one row."""

        table = self.query_one("#sessions", DataTable)
        table.move_cursor(row=min(table.cursor_row + 1, table.row_count - 1))

    def action_cursor_up(self) -> None:
        """Move the session cursor up one row."""

        table = self.query_one("#sessions", DataTable)
        table.move_cursor(row=max(table.cursor_row - 1, 0))

    def action_refresh(self) -> None:
        """Reload the run immediately."""

        self.reload()

    def action_search(self) -> None:
        """Open the cross-session search overlay."""

        items = [(record.key, self.paths.transcript(record.key)) for record in self.records]

        self.push_screen(SearchScreen(items), self._jump_to_key)

    def _jump_to_key(self, key: str | None) -> None:
        if not key:
            return

        for index, record in enumerate(self.records):
            if record.key == key:
                self.query_one("#sessions", DataTable).move_cursor(row=index)
                return

    def action_enter(self) -> None:
        """Suspend the dashboard and drop into an interactive copilot session."""

        record = self._selected_record()
        if record is None:
            return
        if shutil.which("copilot") is None:
            self.notify("`copilot` is not on PATH.", severity="error")
            return
        if not Path(record.worktree).exists():
            self.notify("worktree is gone, the run may have been cleaned.", severity="error")
            return

        with self.suspend():
            subprocess.run(resume_interactive_argv(record.session_id, record.worktree))

        self.reload()

    def action_send(self) -> None:
        """Prompt for a follow-up message and append a turn to the selected session."""

        record = self._selected_record()
        if record is None:
            return

        self.push_screen(SendScreen(), lambda message: self._send(record, message))

    def _send(self, record: SessionRecord, message: str | None) -> None:
        if not message:
            return
        if not Path(record.worktree).exists():
            self.notify("worktree is gone, the run may have been cleaned.", severity="error")
            return

        self.notify(f"sending to {record.key}…")
        self._send_worker(record, message)

    @work(thread=True)
    def _send_worker(self, record: SessionRecord, message: str) -> None:
        argv = followup_argv(record.session_id, record.worktree, record.model, record.permission_flags, message)
        state = asyncio.run(SessionRunner(record.key, argv, self.paths.transcript(record.key)).run())

        record.status = state.status
        record.exit_code = state.exit_code
        record.error = state.error
        if state.premium_requests is not None:
            record.premium_requests = state.premium_requests

        self.paths.write_record(record)
