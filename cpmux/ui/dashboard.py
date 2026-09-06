# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import shutil
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rich.markup import escape
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
)
from textual.worker import WorkerCancelled, WorkerState

from cpmux import theme
from cpmux.engine import daemon
from cpmux.engine.interact import run_followup, run_interactive
from cpmux.engine.ownership import OwnershipError
from cpmux.engine.reporting import run_report
from cpmux.engine.review import (
    DiffSnapshot,
    diff_snapshot,
    run_feedback,
    run_finalization,
    run_verification,
)
from cpmux.engine.store import RunManifest, RunPaths, SessionRecord, load_run
from cpmux.events import ACTIVE, TERMINAL, TERMINAL_FAILURE, Status, parse_line
from cpmux.ui.render import event_text
from cpmux.ui.search import search_transcripts
from cpmux.vcs.git import GitError
from cpmux.vcs.pr import PRError

_FILTERS = ("all", "attention", "review", "failed")
_ATTENTION_RANK = {
    "failed": 0,
    "needs_verification": 1,
    "delivered_unverified": 2,
    "ready_for_review": 3,
    "running": 4,
    "pending": 5,
    "completed": 6,
    "unavailable": 0,
}
_ATTENTION_FILTER = frozenset({"failed", "needs_verification", "delivered_unverified", "unavailable"})
_REPORT_INTERVAL_SECONDS = 5.0


class SearchScreen(ModalScreen[str | None]):
    """Search overlay returning the selected session key."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, items: list[tuple[str, Path]]) -> None:
        """Initialize transcript search.

        Args:
            items: Session labels paired with their transcript paths.

        """

        super().__init__()
        self.items = items
        self._result_query: str | None = None

    def compose(self) -> ComposeResult:
        yield Input(placeholder="search transcripts…", id="query")
        yield ListView(id="results")

    def on_input_changed(self, event: Input.Changed) -> None:
        self._result_query = None
        self._search(event.value.strip())

    @work(group="transcript-search", exclusive=True)
    async def _search(self, query: str) -> None:
        results = self.query_one("#results", ListView)
        await results.clear()
        if not query:
            self._result_query = query
            return
        await asyncio.sleep(0.15)
        try:
            hits = await asyncio.to_thread(search_transcripts, self.items, query)
        except (OSError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return
        await results.extend(
            [
                ListItem(Label(Text(f"{hit.label}  ·  {hit.role}  ·  {hit.snippet}")), name=hit.label)
                for hit in hits[:50]
            ]
        )
        self._result_query = query
        if hits:
            results.index = 0

    def on_input_submitted(self, event: Input.Submitted) -> None:
        results = self.query_one("#results", ListView)
        if self._result_query != event.value.strip():
            self.notify("Search is still running.")
        elif results.index is not None and results.children:
            self.dismiss(results.children[results.index].name)
        else:
            self.notify("No matching transcript.")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(event.item.name)

    def action_close(self) -> None:
        self.dismiss(None)


class SendScreen(ModalScreen[str | None]):
    """Message overlay for follow-ups and revision-bound feedback."""

    BINDINGS = [Binding("escape", "close", "Close")]

    def __init__(self, placeholder: str = "follow-up message…") -> None:
        """Initialize message input.

        Args:
            placeholder: Prompt shown in the empty input.

        """

        super().__init__()
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        yield Input(placeholder=self.placeholder, id="message")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_close(self) -> None:
        self.dismiss(None)


def _pr_cell(record: SessionRecord) -> str:
    if record.pr_url:
        return f"#{record.pr_url.rstrip('/').rsplit('/', 1)[-1]}"

    return "-"


def _short_sha(value: str | None) -> str:
    return value[:8] if value else "-"


def _premium_text(value: int | float | None, incomplete: bool) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float) and value.is_integer():
        value = int(value)

    return f"{value}?" if incomplete else str(value)


def _relative_time(value: str | None) -> str:
    if not value:
        return "-"
    try:
        then = datetime.fromisoformat(value)
        if then.tzinfo is None:
            return "no timezone"
        seconds = max(0, int((datetime.now(timezone.utc) - then).total_seconds()))
    except ValueError:
        return "invalid time"
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"

    return f"{seconds // 86400}d"


def _attempt_number(record: SessionRecord) -> int:
    return record.attempts[-1].number if record.attempts else 0


class CpmuxApp(App):
    """Attention-first dashboard for one run."""

    CSS = """
    #queue {
        width: 40%;
        border-right: solid $panel;
    }
    #queue-header, #transcript-header, #tabs {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text;
    }
    #transcript-header {
        height: 2;
    }
    #sessions {
        height: 1fr;
    }
    #right {
        width: 1fr;
    }
    #transcript, #diff {
        padding: 0 1;
    }
    #checks-view, #details-view {
        height: 1fr;
    }
    #diff, #checks-view, #details-view {
        display: none;
    }
    #checks, #details {
        padding: 1 2;
    }
    """
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("slash", "search", "Search"),
        Binding("p", "pause", "Queue"),
        Binding("x", "stop", "Stop"),
        Binding("v", "verify", "Verify"),
        Binding("f", "finalize", "Finalize"),
        Binding("u", "update_pr", "Update PR", show=False),
        Binding("e", "enter", "Attach"),
        Binding("s", "send", "Send"),
        Binding("o", "open_pr", "Open PR"),
        Binding("r", "refresh", "Refresh", show=False),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("1", "filter_all", "All", show=False),
        Binding("2", "filter_attention", "Attention", show=False),
        Binding("3", "filter_review", "Review", show=False),
        Binding("4", "filter_failed", "Failed", show=False),
        Binding("t", "tab_transcript", "Transcript", show=False),
        Binding("d", "tab_diff", "Diff", show=False),
        Binding("c", "tab_checks", "Checks", show=False),
        Binding("i", "tab_details", "Details", show=False),
        Binding("enter", "open_detail", "Detail", show=False),
        Binding("escape", "back", "Back", show=False),
    ]

    def __init__(self, start_path: str, run_id: str) -> None:
        """Create the dashboard.

        Args:
            start_path: Repository root containing the run history.
            run_id: Run identifier to display.

        Raises:
            ValueError: The run identifier is invalid.

        """

        super().__init__()

        self.start_path = Path(start_path)
        self.run_id = run_id
        self.paths = RunPaths(start_path, run_id)
        self.records: list[SessionRecord] = []
        self.deps_by_key: dict[str, list[str]] = {}
        self._visible_records: list[SessionRecord] = []
        self._report: dict[str, Any] | None = None
        self._report_items: dict[str, dict[str, Any]] = {}
        self._report_loaded_at = 0.0
        self._report_signature: tuple[object, ...] = ()
        self._reloading = False
        self._force_report = False
        self._quit_requested = False
        self._shown_key: str | None = None
        self._selected_key: str | None = None
        self._transcript_offset = 0
        self._transcript_identity: tuple[int, int] | None = None
        self._transcript_error: str | None = None
        self._load_error: str | None = None
        self._filter = "all"
        self._tab = "transcript"
        self._narrow_detail = False
        self._refreshing_table = False
        self._diff_snapshots: dict[str, DiffSnapshot] = {}
        self._rendered_diff: tuple[str, str | None] | None = None

    @property
    def _view(self) -> Screen:
        return self.screen_stack[0]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading run...", id="queue-header")
        with Horizontal(id="body"):
            with Vertical(id="queue"):
                yield DataTable(id="sessions")
            with Vertical(id="right"):
                yield Static(id="transcript-header")
                yield Static(id="tabs")
                yield RichLog(id="transcript", wrap=True, auto_scroll=False)
                yield RichLog(id="diff", wrap=False, auto_scroll=False)
                with VerticalScroll(id="checks-view"):
                    yield Static(id="checks")
                with VerticalScroll(id="details-view"):
                    yield Static(id="details")

        yield Footer()

    def on_mount(self) -> None:
        self.title = f"cpmux · {self.run_id}"
        table = self._view.query_one("#sessions", DataTable)
        table.cursor_type = "row"
        table.add_columns("item", "state", "try", "premium")

        self.reload(force_report=True)
        self._apply_layout()
        self.set_interval(1.0, self.reload)

    def on_resize(self, event: events.Resize) -> None:
        self._apply_layout()

    def reload(self, force_report: bool = False) -> None:
        """Schedule a nonblocking refresh, coalescing requests while one is running.

        Reconciliation may stop identified orphans and persist failed records.
        Source inspections are throttled unless lifecycle or receipt data changed.
        Missing or unreadable state is surfaced without presenting cached verification
        as current. All widget updates occur on the app's event loop.

        Args:
            force_report: Whether to bypass the summary refresh throttle.

        """

        if self._quit_requested:
            return
        self._force_report |= force_report
        if self._reloading:
            return
        self._reloading = True
        self._reload_worker()

    def _load_records(self) -> tuple[RunManifest, list[SessionRecord]]:
        manifest, records = load_run(self.start_path, self.run_id)
        return manifest, daemon.reconcile(self.paths, records)

    @work(group="refresh")
    async def _reload_worker(self) -> None:
        force = self._force_report
        self._force_report = False
        try:
            manifest, records = await asyncio.to_thread(self._load_records)
            signature = tuple(
                (
                    record.key,
                    record.status,
                    record.phase,
                    record.candidate_sha,
                    record.delivery_sha,
                    record.verification,
                    record.premium_requests,
                    record.pr_url,
                )
                for record in records
            )
            self.deps_by_key = {item.key: list(item.depends_on) for item in manifest.resolved}
            self.records = records
            now = time.monotonic()
            if (
                force
                or self._report is None
                or signature != self._report_signature
                or now - self._report_loaded_at >= _REPORT_INTERVAL_SECONDS
            ):
                report = await asyncio.to_thread(run_report, self.start_path, self.run_id)
                self._report = report
                self._report_items = {item["key"]: item for item in report["items"]}
                self._report_loaded_at = time.monotonic()
                self._report_signature = signature
            self._load_error = None
            self._refresh_view()
        except (OwnershipError, OSError, ValueError) as exc:
            if str(exc) != self._load_error:
                self.notify(str(exc), severity="error")
                self._load_error = str(exc)
            self._report = None
            self._report_items.clear()
            self.sub_title = "run data unavailable; press r to retry"
            self._refresh_table()
            self._refresh_detail()
        finally:
            self._reloading = False
        if self._force_report:
            self.reload()

    def _refresh_view(self) -> None:
        report = self._report
        if report is None:
            return
        active = sum(record.status in ACTIVE for record in self.records)
        premium = report["reported_premium_requests"]
        unknown = report["items_with_unknown_usage"]
        budget = report["premium_budget"]
        usage = _premium_text(premium, unknown > 0)
        usage = f"{usage}/{budget} premium" if budget is not None else f"{usage} premium"
        if report["budget_reached"]:
            usage += " · soft budget reached"
        queue_state = "paused" if report["paused"] else "queue running" if report["managed"] else "idle"
        if report["owner_error"]:
            queue_state = "ownership unavailable"
        self.sub_title = (
            f"{queue_state} · {active}/{report['expected_items']} active · usage {usage} · {time.strftime('%H:%M:%S')}"
        )

        self._refresh_table()
        self._refresh_detail()

    def _report_item(self, record: SessionRecord) -> dict[str, Any] | None:
        return self._report_items.get(record.key)

    def _attention(self, record: SessionRecord) -> str:
        item = self._report_item(record)
        return item["attention"] if item is not None else "unavailable"

    def _verification(self, record: SessionRecord) -> dict[str, Any]:
        item = self._report_item(record)
        return item["verification"] if item is not None else {"status": "unavailable"}

    def _matches_filter(self, record: SessionRecord) -> bool:
        attention = self._attention(record)
        if self._filter == "attention":
            return attention in _ATTENTION_FILTER
        if self._filter == "review":
            return attention == "ready_for_review"
        if self._filter == "failed":
            return attention == "failed"

        return True

    def _attention_text(self, record: SessionRecord) -> Text:
        attention = self._attention(record)
        if attention == "failed":
            return theme.status_text(record.status)
        if attention == "unavailable":
            return Text("? unavailable", style=theme.STYLE_WARNING)
        if attention == "needs_verification":
            return Text("! needs verify", style=theme.STYLE_WARNING)
        if attention == "delivered_unverified":
            return Text("! PR unverified", style=theme.STYLE_WARNING)
        if attention == "ready_for_review":
            return Text("◆ review ready", style=theme.STYLE_ACCENT)
        return theme.status_text(record.status)

    def _refresh_table(self) -> None:
        table = self._view.query_one("#sessions", DataTable)
        current = self._selected_record()
        selected_key = current.key if current is not None else self._selected_key
        order = {record.key: index for index, record in enumerate(self.records)}
        visible = [record for record in self.records if self._matches_filter(record)]
        visible.sort(key=lambda record: (_ATTENTION_RANK[self._attention(record)], order[record.key]))
        self._visible_records = visible

        counts = {
            "all": len(self.records),
            "attention": sum(self._attention(record) in _ATTENTION_FILTER for record in self.records),
            "review": sum(self._attention(record) == "ready_for_review" for record in self.records),
            "failed": sum(self._attention(record) == "failed" for record in self.records),
        }
        labels = []
        for index, name in enumerate(_FILTERS, start=1):
            label = f"{index} {name} {counts[name]}"
            labels.append(f"[reverse]{escape(label)}[/reverse]" if name == self._filter else escape(label))
        self._view.query_one("#queue-header", Static).update("  ".join(labels))

        self._refreshing_table = True
        try:
            table.clear()
            for record in visible:
                item = self._report_item(record)
                incomplete = item["usage_incomplete"] if item is not None else True
                table.add_row(
                    record.key,
                    self._attention_text(record),
                    str(_attempt_number(record) or "-"),
                    _premium_text(record.premium_requests, incomplete),
                    key=record.key,
                )

            if visible:
                keys = [record.key for record in visible]
                row = keys.index(selected_key) if selected_key in keys else 0
                table.move_cursor(row=row)
                self._selected_key = visible[row].key
            else:
                self._selected_key = None
        finally:
            self._refreshing_table = False

    def _selected_record(self) -> SessionRecord | None:
        if not self._visible_records:
            return None
        table = self._view.query_one("#sessions", DataTable)
        if table.row_count == 0:
            return None

        return self._visible_records[min(table.cursor_row, len(self._visible_records) - 1)]

    def _refresh_detail(self, force_transcript: bool = False) -> None:
        record = self._selected_record()
        if record is None:
            self._shown_key = None
            self._transcript_offset = 0
            self._transcript_identity = None
            self._rendered_diff = None
            self._view.query_one("#transcript-header", Static).update("[dim]No tasks in this view.[/dim]")
            self._view.query_one("#tabs", Static).update("")
            for selector in ("#transcript", "#diff"):
                self._view.query_one(selector, RichLog).clear()
            for selector in ("#checks", "#details"):
                self._view.query_one(selector, Static).update("")
            self.refresh_bindings()
            return

        self._selected_key = record.key
        self._update_header(record)
        self._update_tabs()
        if self._tab == "transcript":
            self._refresh_transcript(force=force_transcript)
        elif self._tab == "diff":
            self._render_diff(record)
        elif self._tab == "checks":
            self._render_checks(record)
        else:
            self._render_details(record)
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in {"verify", "finalize", "update_pr", "send", "enter", "stop", "open_pr"}:
            if not self.is_mounted or self._quit_requested:
                return False
            record = self._selected_record()
            if record is None:
                return False
            if action == "stop":
                return record.status not in TERMINAL
            if action == "open_pr":
                return bool(record.pr_url)
            if self._report is None or self._report["managed"] is not False:
                return False
            if record.status not in TERMINAL:
                return False
            if action == "update_pr":
                return bool(record.pr_url)
            if action == "send" and self._tab == "diff":
                return record.key in self._diff_snapshots
        return True

    def _update_header(self, record: SessionRecord, suffix: str = "") -> None:
        item = self._report_item(record)
        pr = _pr_cell(record) if record.pr_url else "no PR"
        verification = self._verification(record)
        warning = "  ·  [bold yellow]! STALE[/bold yellow]" if verification["status"] == "stale" else ""
        usage = _premium_text(record.premium_requests, item["usage_incomplete"] if item is not None else True)
        header = self._view.query_one("#transcript-header", Static)
        header.update(
            f"[bold]{escape(record.key)}[/bold] · try {_attempt_number(record) or '-'} · "
            f"last {_relative_time(record.last_activity_at)} · premium {usage}\n"
            f"{_short_sha(record.base_sha)}→{_short_sha(record.candidate_sha)} · {escape(pr)}{warning}{suffix}"
        )

    def _update_tabs(self) -> None:
        labels = []
        for key, label in (
            ("transcript", "T Transcript"),
            ("diff", "D Diff"),
            ("checks", "C Checks"),
            ("details", "I Details"),
        ):
            labels.append(f"[bold cyan]{label}[/bold cyan]" if key == self._tab else f"[dim]{label}[/dim]")
        self._view.query_one("#tabs", Static).update("   ".join(labels))
        self._view.query_one("#transcript", RichLog).display = self._tab == "transcript"
        self._view.query_one("#diff", RichLog).display = self._tab == "diff"
        self._view.query_one("#checks-view", VerticalScroll).display = self._tab == "checks"
        self._view.query_one("#details-view", VerticalScroll).display = self._tab == "details"

    def _refresh_transcript(self, force: bool = False) -> None:
        record = self._selected_record()
        if record is None:
            return

        transcript = self.paths.transcript(record.key)
        log = self._view.query_one("#transcript", RichLog)
        new_selection = force or record.key != self._shown_key
        if new_selection:
            log.clear()
            self._shown_key = record.key
            self._transcript_offset = 0
            self._transcript_identity = None
        try:
            if not transcript.exists():
                self._update_header(record, " · waiting for output" if record.status in ACTIVE else " · no transcript")
                return
            metadata = transcript.stat()
            identity = (metadata.st_dev, metadata.st_ino)
            if metadata.st_size < self._transcript_offset or (
                self._transcript_identity is not None and identity != self._transcript_identity
            ):
                log.clear()
                self._transcript_offset = 0
                new_selection = True
            self._transcript_identity = identity
            with transcript.open("rb") as handle:
                handle.seek(self._transcript_offset)
                pending = handle.read()
            boundary = pending.rfind(b"\n") + 1 if record.status in ACTIVE else len(pending)
            text = pending[:boundary].decode("utf-8")
        except (OSError, UnicodeError) as exc:
            error = f"`{transcript}` cannot be displayed: {exc}"
            if error != self._transcript_error:
                self.notify(error, severity="error")
            self._transcript_error = error
            self._update_header(record, " · transcript unavailable")
            return

        self._transcript_error = None
        following = log.is_vertical_scroll_end and not log.is_vertical_scrollbar_grabbed
        self._write_events(log, text)
        self._transcript_offset += boundary
        if new_selection or (text and following):
            log.scroll_end(animate=False)
        if not self._transcript_offset:
            self._update_header(record, " · waiting for output" if record.status in ACTIVE else " · no transcript")

    def _render_diff(self, record: SessionRecord) -> None:
        log = self._view.query_one("#diff", RichLog)
        snapshot = self._diff_snapshots.get(record.key)
        render_key = (record.key, snapshot.revision if snapshot is not None else None)
        if render_key == self._rendered_diff:
            return
        self._rendered_diff = render_key
        log.clear()
        if snapshot is None:
            log.write(Text("Press d to load the immutable diff snapshot.", style=theme.STYLE_MUTED))
            return

        revision = snapshot.revision
        base_sha = snapshot.base_sha
        head_sha = snapshot.head_sha
        text = snapshot.text
        log.write(
            Text.assemble(
                ("revision ", theme.STYLE_MUTED),
                (revision[:12], theme.STYLE_ACCENT),
                ("  base ", theme.STYLE_MUTED),
                (_short_sha(base_sha), theme.STYLE_INFO),
                ("  head ", theme.STYLE_MUTED),
                (_short_sha(head_sha), theme.STYLE_INFO),
            )
        )
        for line in text.splitlines():
            style = theme.STYLE_SUCCESS if line.startswith("+") else theme.STYLE_DANGER if line.startswith("-") else ""
            if line.startswith("@@"):
                style = theme.STYLE_INFO
            log.write(Text(line, style=style))
        log.scroll_home(animate=False)

    def _render_checks(self, record: SessionRecord) -> None:
        verification = self._verification(record)
        status = verification["status"]
        color = {
            "passed": "green",
            "stale": "yellow",
            "failed": "red",
            "running": "cyan",
            "not_configured": "dim",
            "missing": "yellow",
            "unavailable": "yellow",
        }.get(status, "dim")
        lines = [f"[bold]Verification[/bold]  [{color}]{escape(status.replace('_', ' '))}[/{color}]"]
        if verification.get("reason"):
            lines.append(f"\n[yellow]{escape(str(verification['reason']))}[/yellow]")
        lines.append(
            f"\nsource {_short_sha(record.base_sha)}"
            f"  ·  candidate {_short_sha(record.candidate_sha)}"
            f"  ·  checked {_short_sha(verification.get('commit_sha'))}"
        )
        if record.error:
            lines.append(f"\n\n[red]{escape(record.error)}[/red]")
        for attempt in reversed(record.attempts):
            lines.append(f"\n\n[bold]Attempt {attempt.number} · {attempt.mode}[/bold]")
            if not attempt.commands:
                lines.append("\nNo setup or checks executed in this attempt.")
            for command in attempt.commands:
                lines.append(
                    f"\n{escape(command.name)} ({command.phase}) · {command.status} · "
                    f"exit {command.exit_code if command.exit_code is not None else '-'} · {command.duration_seconds:.1f}s"
                    f"\n[dim]{escape(command.log_path)}[/dim]"
                )
                if command.error:
                    lines.append(f"\n[red]{escape(command.error)}[/red]")
        self._view.query_one("#checks", Static).update("".join(lines))

    def _render_details(self, record: SessionRecord) -> None:
        item = self._report_item(record)
        usage = _premium_text(record.premium_requests, item["usage_incomplete"] if item is not None else True)
        source_text = record.source.url if record.source is not None else "not an imported issue"
        dependencies = self.deps_by_key.get(record.key, [])
        stack = f"{record.base_from} → {record.key}" if record.base_from else record.key
        verification = self._verification(record)
        process = item["process"] if item is not None else None
        memory = (
            f"{process['rss_bytes'] / 1048576:.1f} MiB"
            if process is not None and "rss_bytes" in process
            else "unavailable"
        )
        details = (
            f"[bold]Status[/bold]       {record.status.value}\n"
            f"[bold]Phase[/bold]        {record.phase}\n"
            f"[bold]Attention[/bold]    {escape(self._attention(record).replace('_', ' '))}\n"
            f"[bold]Attempts[/bold]     {_attempt_number(record) or '-'}\n"
            f"[bold]Last activity[/bold] {_relative_time(record.last_activity_at)}\n"
            f"[bold]Premium[/bold]      {escape(usage)}\n"
            f"[bold]Child RSS[/bold]    {memory} (excludes descendants)\n"
            f"[bold]Verification[/bold] {verification['status'].replace('_', ' ')}\n\n"
            f"[bold]Source[/bold]       {escape(source_text)}\n"
            f"[bold]Base[/bold]         {escape(record.base)} @ {_short_sha(record.base_sha)}\n"
            f"[bold]Stack[/bold]        {escape(stack)}\n"
            f"[bold]Dependencies[/bold] {escape(', '.join(dependencies) if dependencies else '-')}\n"
            f"[bold]Candidate[/bold]    {_short_sha(record.candidate_sha)}\n"
            f"[bold]Checked[/bold]      {_short_sha(verification.get('commit_sha'))}\n"
            f"[bold]Delivered[/bold]    {_short_sha(record.delivery_sha)}\n"
            f"[bold]Model[/bold]        {escape(record.model)}\n"
            f"[bold]Branch[/bold]       {escape(record.branch)}\n"
            f"[bold]PR[/bold]           {escape(record.pr_url or 'none')}\n\n"
            "[dim]Writes use engine ownership; conflicting session edits are rejected.[/dim]"
        )
        if record.error:
            details += f"\n\n[red]{escape(record.error)}[/red]"
        self._view.query_one("#details", Static).update(details)

    def _write_events(self, log: RichLog, text: str) -> None:
        for line in text.splitlines():
            event = parse_line(line)
            if event is None:
                continue
            renderable = event_text(event)
            if renderable is not None:
                log.write(renderable)

    def _apply_layout(self) -> None:
        if not self.is_mounted:
            return
        narrow = self.size.width < 84
        queue = self._view.query_one("#queue", Vertical)
        right = self._view.query_one("#right", Vertical)
        queue.styles.width = "1fr" if narrow else "40%"
        right.styles.width = "1fr"
        queue.display = not narrow or not self._narrow_detail
        right.display = not narrow or self._narrow_detail

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._refreshing_table or self._quit_requested or not event.control.is_attached:
            return
        record = self._selected_record()
        if record is not None:
            changed = record.key != self._selected_key
            self._selected_key = record.key
            self._refresh_detail(force_transcript=changed)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if not self._quit_requested and event.control.is_attached:
            self.action_open_detail()

    def action_cursor_down(self) -> None:
        table = self._view.query_one("#sessions", DataTable)
        if not table.row_count:
            return
        table.move_cursor(row=min(table.cursor_row + 1, table.row_count - 1))

    def action_cursor_up(self) -> None:
        table = self._view.query_one("#sessions", DataTable)
        if not table.row_count:
            return
        table.move_cursor(row=max(table.cursor_row - 1, 0))

    def action_refresh(self) -> None:
        self.reload(force_report=True)

    async def action_quit(self) -> None:
        self._quit_requested = True
        self.sub_title = "stopping dashboard-owned operations..."
        workers = list(self.workers)
        for worker in workers:
            worker.cancel()
        results = await asyncio.gather(
            *(worker.wait() for worker in workers if worker.state != WorkerState.PENDING),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, (WorkerCancelled, asyncio.CancelledError)):
                raise result
        self.exit()

    def _set_filter(self, name: str) -> None:
        self._filter = name
        self._narrow_detail = False
        self._refresh_table()
        self._refresh_detail(force_transcript=True)
        self._apply_layout()

    def action_filter_all(self) -> None:
        self._set_filter("all")

    def action_filter_attention(self) -> None:
        self._set_filter("attention")

    def action_filter_review(self) -> None:
        self._set_filter("review")

    def action_filter_failed(self) -> None:
        self._set_filter("failed")

    def _set_tab(self, name: str) -> None:
        focused_detail = self._narrow_detail or (
            self.focused is not None and self.focused.id in {"transcript", "diff", "checks-view", "details-view"}
        )
        self._tab = name
        self._refresh_detail()
        if focused_detail:
            self._focus_detail()

    def _focus_detail(self) -> None:
        suffix = "-view" if self._tab in {"checks", "details"} else ""
        self._view.query_one(f"#{self._tab}{suffix}").focus()

    def action_tab_transcript(self) -> None:
        self._set_tab("transcript")

    def action_tab_diff(self) -> None:
        self._set_tab("diff")
        record = self._selected_record()
        if record is None:
            return

        log = self._view.query_one("#diff", RichLog)
        self._rendered_diff = None
        log.clear()
        log.write(Text("Loading immutable diff snapshot…", style=theme.STYLE_MUTED))
        self._diff_worker(record)

    def action_tab_checks(self) -> None:
        self._set_tab("checks")

    def action_tab_details(self) -> None:
        self._set_tab("details")

    def action_open_detail(self) -> None:
        if self._selected_record() is None:
            return
        self._narrow_detail = True
        self._apply_layout()
        self._focus_detail()

    def action_back(self) -> None:
        if self.size.width < 84 and self._narrow_detail:
            self._narrow_detail = False
            self._apply_layout()
        self._view.query_one("#sessions", DataTable).focus()

    def action_open_pr(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        if not record.pr_url:
            self.notify(f"no PR for `{record.key}` yet.")
            return

        webbrowser.open(record.pr_url)
        self.notify(f"opening PR for `{record.key}`.")

    def action_stop(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        self._stop_worker(record)

    @work(group="control")
    async def _stop_worker(self, record: SessionRecord) -> None:
        try:
            if await asyncio.to_thread(daemon.kill_session, self.paths, record):
                self.notify(f"requested stop for `{record.key}`.")
            else:
                self.notify(f"`{record.key}` was not running.")
        except (OwnershipError, OSError, ValueError) as exc:
            self.notify(str(exc), severity="error")

        self.reload(force_report=True)

    def action_pause(self) -> None:
        paused = self.paths.pause_file.exists()
        try:
            daemon.set_paused(self.paths, not paused)
        except (OSError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return

        message = "queue paused; active children continue." if not paused else "queue unpaused."
        self.notify(message)
        self.reload(force_report=True)

    def action_verify(self) -> None:
        record = self._selected_record()
        if record is None:
            return

        self.notify(f"verifying `{record.key}`.")
        self._verification_worker(record)

    def action_finalize(self) -> None:
        record = self._selected_record()
        if record is None:
            return

        self.notify(f"finalizing `{record.key}`.")
        self._finalization_worker(record)

    def action_update_pr(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        if not record.pr_url:
            self.notify(f"`{record.key}` has no existing PR to update.", severity="warning")
            return

        self.notify(f"updating PR for `{record.key}`.")
        self._finalization_worker(record)

    def action_search(self) -> None:
        items = [(record.key, self.paths.transcript(record.key)) for record in self.records]

        self.push_screen(SearchScreen(items), self._jump_to_key)

    def _jump_to_key(self, key: str | None) -> None:
        if not key:
            return
        if key not in {record.key for record in self._visible_records}:
            self._filter = "all"
            self._refresh_table()

        for index, record in enumerate(self._visible_records):
            if record.key == key:
                self._view.query_one("#sessions", DataTable).move_cursor(row=index)
                return

    async def action_enter(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        if shutil.which("copilot") is None:
            self.notify("could not find `copilot` on `PATH`.", severity="error")
            return
        if not Path(record.worktree).exists():
            self.notify("worktree is gone; run may be cleaned.", severity="error")
            return

        with self.suspend():
            try:
                await run_interactive(self.paths, record)
            except (OwnershipError, OSError, ValueError) as exc:
                self.notify(str(exc), severity="error")

        self._notify_outcome(record, "interactive session")

    def action_send(self) -> None:
        record = self._selected_record()
        if record is None:
            return
        if self._tab == "diff":
            snapshot = self._diff_snapshots.get(record.key)
            revision = snapshot.revision if snapshot is not None else None
            if revision is None:
                self.notify("load the diff with `d` before sending feedback.", severity="warning")
                return
            self.push_screen(
                SendScreen(f"feedback for {revision[:12]}…"),
                lambda message: self._feedback(record, str(revision), message),
            )
            return

        self.push_screen(SendScreen(), lambda message: self._send(record, message))

    def _send(self, record: SessionRecord, message: str | None) -> None:
        if not message:
            return
        if not Path(record.worktree).exists():
            self.notify("worktree is gone; run may be cleaned.", severity="error")
            return

        self.notify(f"sending to `{record.key}`.")
        self._send_worker(record, message)

    @work(group="mutation")
    async def _send_worker(self, record: SessionRecord, message: str) -> None:
        try:
            await run_followup(self.paths, record, message)
        except (OwnershipError, GitError, PRError, OSError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            self.reload(True)
            return
        self._notify_outcome(record, "follow-up")

    def _feedback(self, record: SessionRecord, revision: str, message: str | None) -> None:
        if not message:
            return

        self.notify(f"sending feedback for `{record.key}` at `{revision}`.")
        self._feedback_worker(record, message, revision)

    @work(group="diff", exclusive=True)
    async def _diff_worker(self, record: SessionRecord) -> None:
        try:
            snapshot = await asyncio.to_thread(diff_snapshot, self.paths, record)
        except (OwnershipError, GitError, PRError, OSError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            return

        self._accept_diff(record.key, snapshot)

    def _accept_diff(self, key: str, snapshot: DiffSnapshot) -> None:
        self._diff_snapshots[key] = snapshot
        self.refresh_bindings()
        record = self._selected_record()
        if record is not None and record.key == key and self._tab == "diff":
            self._render_diff(record)

    @work(group="mutation")
    async def _feedback_worker(self, record: SessionRecord, message: str, revision: str) -> None:
        try:
            await run_feedback(self.paths, record, message, revision)
        except (OwnershipError, GitError, PRError, OSError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            self.reload(True)
            return

        self._notify_outcome(record, "feedback")

    @work(group="mutation")
    async def _verification_worker(self, record: SessionRecord) -> None:
        try:
            await run_verification(self.paths, record)
        except (OwnershipError, GitError, PRError, OSError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            self.reload(True)
            return

        self._notify_outcome(record, "verification", require_verification=True)

    @work(group="mutation")
    async def _finalization_worker(self, record: SessionRecord) -> None:
        try:
            await run_finalization(self.paths, record)
        except (OwnershipError, GitError, PRError, OSError, ValueError) as exc:
            self.notify(str(exc), severity="error")
            self.reload(True)
            return

        self._notify_outcome(record, "finalization")

    def _notify_outcome(self, record: SessionRecord, action: str, require_verification: bool = False) -> None:
        if record.status == Status.KILLED:
            self.notify(f"{action} stopped for `{record.key}`.", severity="warning")
        elif record.status in TERMINAL_FAILURE:
            self.notify(record.error or f"{action} failed for `{record.key}`.", severity="error")
        elif require_verification and record.verification is None:
            self.notify(f"`{record.key}` has no acceptance checks configured; it is not verified.", severity="warning")
        else:
            self.notify(f"{action} completed for `{record.key}`.")
        self.reload(True)
