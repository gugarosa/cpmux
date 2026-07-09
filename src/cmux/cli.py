# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import os
import shutil
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.table import Table

from cmux import __version__, daemon
from cmux.config import ConfigError, Deps, Plan, ResolvedItem, load_plan
from cmux.events import TERMINAL, Status, parse_line
from cmux.gitutil import GitError, prune_worktrees, remove_worktree
from cmux.interact import followup_argv, resume_interactive_argv
from cmux.logging import get_logger
from cmux.search import search_transcripts
from cmux.session import SessionRunner
from cmux.state import RunPaths, SessionRecord, all_run_ids, latest_run_id, load_run
from cmux.supervisor import Options, Supervisor

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="cmux — a declarative, guided multiplexer for GitHub Copilot CLI agents.",
)
console = Console()
logger = get_logger(__name__)

_STATUS_COLOR = {"done": "green", "no_changes": "dim", "failed": "red"}


def _version(value: bool) -> None:
    if value:
        console.print(f"cmux {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version, is_eager=True, help="Show version and exit."
    ),
) -> None:
    pass


def _load(file: Path) -> Plan:
    try:
        return load_plan(file)
    except ConfigError as exc:
        logger.error(str(exc))
        raise typer.Exit(1)


def _resolve_record(run: str | None, key: str) -> tuple[RunPaths, SessionRecord]:
    run_id = run or latest_run_id(Path("."))
    if not run_id:
        logger.error("no cmux runs found here.")
        raise typer.Exit(1)

    paths = RunPaths(Path("."), run_id)
    if not paths.record_file(key).exists():
        logger.error(f"no session `{key}` in run `{run_id}`.")
        raise typer.Exit(1)

    return paths, paths.read_record(key)


def _display_argv(argv: list[str]) -> str:
    parts: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            parts.append(f"<prompt {len(token)} chars>")
            redact_next = False
            continue
        parts.append(token)
        if token == "-p":
            redact_next = True

    return " ".join(parts)


def _plan_table(resolved: list[ResolvedItem]) -> Table:
    table = Table(title="resolved plan", expand=True)
    table.add_column("item", style="bold")
    table.add_column("model")
    table.add_column("effort")
    table.add_column("branch")
    table.add_column("perms")
    table.add_column("deps on")

    for item in resolved:
        table.add_row(
            item.key,
            item.model,
            str(item.effort),
            item.branch,
            item.permissions.preset,
            ", ".join(item.depends_on) or "-",
        )

    return table


@app.command()
def up(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Path to the cmux YAML file."),
    dry_run: bool = typer.Option(False, "--dry-run", "--dry_run", help="Resolve and print the plan; spawn nothing."),
    detach: bool = typer.Option(False, "--detach", "-d", help="Run in the background and return immediately."),
    concurrency: int | None = typer.Option(None, "--concurrency", "-j", help="Max parallel sessions."),
    pr: bool = typer.Option(True, "--pr/--no-pr", help="Open one draft PR per item (default: on)."),
    deps: Deps | None = typer.Option(None, "--deps", help="Override the dependency strategy."),
    strip_github_token: bool = typer.Option(
        True,
        "--strip-github-token/--no-strip-github-token",
        "--strip_github_token/--no-strip_github_token",
        help="Unset ambient GITHUB_TOKEN/GH_TOKEN for gh + git push (keyring fallback).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Spawn one Copilot session per item, each in its own worktree, branch, and PR."""
    plan = _load(file)
    resolved = plan.resolve()

    if dry_run:
        console.print(_plan_table(resolved))
        console.print("\n[bold]spawn commands:[/bold]")
        for item in resolved:
            argv = item.spawn_argv(f"<worktree>/{item.key}", "<session-id>", "<log-dir>")
            console.print(f"  [cyan]{item.key}[/cyan]: {_display_argv(argv)}")
        return

    options = Options(
        concurrency=concurrency,
        open_pr=pr,
        strip_github_token=strip_github_token,
        deps_override=str(deps) if deps else None,
    )
    try:
        supervisor = Supervisor.create(plan, ".", options)
    except GitError as exc:
        logger.error(str(exc))
        raise typer.Exit(1)

    console.print(_plan_table(resolved))
    action = "open a draft PR" if pr else "commit locally (no PR)"
    if not yes and not typer.confirm(
        f"Spawn {len(resolved)} Copilot session(s), each in its own worktree, and {action}?"
    ):
        raise typer.Exit(1)

    supervisor.prepare()
    if detach:
        daemon.launch_detached(supervisor.run_id, str(supervisor.repo_root))
        console.print(f"[green]run {supervisor.run_id} started in the background.[/green]")
        console.print("[dim]monitor it with:[/dim] cmux attach")
        return

    daemon.write_owner(supervisor.paths, os.getpid())
    try:
        asyncio.run(supervisor.run())
    finally:
        daemon.clear_owner(supervisor.paths)

    _print_summary(Path("."), supervisor.run_id)


@app.command()
def ls(run: str | None = typer.Option(None, "--run", help="Run id (default: latest).")) -> None:
    """Show the status of a run."""
    _print_summary(Path("."), run)


@app.command()
def attach(run: str | None = typer.Option(None, "--run", help="Run id (default: latest).")) -> None:
    """Live-monitor a run's sessions read-only (Ctrl-C to exit)."""
    root = Path(".")
    run_id = run or latest_run_id(root)
    if not run_id:
        logger.error("no cmux runs found here.")
        raise typer.Exit(1)

    paths = RunPaths(root, run_id)
    try:
        with Live(console=console, refresh_per_second=4) as live:
            while True:
                _, records = load_run(root, run_id)
                records = daemon.reconcile(paths, records)
                live.update(_monitor_table(run_id, records, paths))
                if all(record.status in TERMINAL for record in records):
                    break
                time.sleep(0.5)
    except KeyboardInterrupt:
        pass


@app.command()
def dash(run: str | None = typer.Option(None, "--run", help="Run id (default: latest).")) -> None:
    """Open the interactive dashboard for a run."""
    run_id = run or latest_run_id(Path("."))
    if not run_id:
        logger.error("no cmux runs found here.")
        raise typer.Exit(1)

    from cmux.dashboard import CmuxApp

    CmuxApp(".", run_id).run()


@app.command()
def enter(
    key: str = typer.Argument(..., help="Item key to open interactively."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
) -> None:
    """Open an interactive Copilot session for an item, resuming it in place."""
    _, record = _resolve_record(run, key)
    if shutil.which("copilot") is None:
        logger.error("`copilot` is not on PATH.")
        raise typer.Exit(1)
    if not Path(record.worktree).exists():
        logger.error(f"`{record.worktree}` worktree is gone, the run may have been cleaned.")
        raise typer.Exit(1)

    os.execvp("copilot", resume_interactive_argv(record.session_id, record.worktree))


@app.command()
def send(
    key: str = typer.Argument(..., help="Item key to message."),
    message: str = typer.Argument(..., help="Follow-up prompt to append to the session."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
) -> None:
    """Append a follow-up turn to an item's session and print the reply."""
    paths, record = _resolve_record(run, key)
    if not Path(record.worktree).exists():
        logger.error(f"`{record.worktree}` worktree is gone, the run may have been cleaned.")
        raise typer.Exit(1)

    argv = followup_argv(record.session_id, record.worktree, record.model, record.permission_flags, message)
    state = asyncio.run(SessionRunner(key, argv, paths.transcript(key)).run())

    record.status = state.status
    record.exit_code = state.exit_code
    record.error = state.error
    if state.premium_requests is not None:
        record.premium_requests = state.premium_requests
    paths.write_record(record)

    if state.last_text:
        console.print(f"[bold green]🤖 assistant[/bold green] {escape(state.last_text)}")
    else:
        console.print(f"[dim]session {record.status}[/dim]")


@app.command()
def logs(
    key: str = typer.Argument(..., help="Item key to show the transcript for."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    raw: bool = typer.Option(False, "--raw", help="Print the raw JSONL transcript."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new events as they arrive."),
) -> None:
    """Print a session's transcript."""
    run_id = run or latest_run_id(Path("."))
    if not run_id:
        logger.error("no cmux runs found here.")
        raise typer.Exit(1)

    transcript = RunPaths(Path("."), run_id).transcript(key)
    if not transcript.exists():
        logger.error(f"no transcript for `{key}` in run `{run_id}`.")
        raise typer.Exit(1)

    consumed = _emit_transcript(transcript.read_text(encoding="utf-8"), raw)
    if follow:
        _follow_transcript(transcript, raw, consumed)


@app.command()
def search(
    query: str = typer.Argument(..., help="Text (or regex with --regex) to find."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    all_runs: bool = typer.Option(False, "--all", help="Search every run, not just the latest."),
    regex: bool = typer.Option(False, "--regex", help="Treat the query as a regular expression."),
) -> None:
    """Search across session transcripts for matching text."""
    root = Path(".")
    run_ids = all_run_ids(root) if all_runs else [run_id for run_id in [run or latest_run_id(root)] if run_id]
    if not run_ids:
        logger.error("no cmux runs found here.")
        raise typer.Exit(1)

    items: list[tuple[str, Path]] = []
    for run_id in run_ids:
        paths = RunPaths(root, run_id)
        _, records = load_run(root, run_id)
        for record in records:
            label = f"{run_id}/{record.key}" if all_runs else record.key
            items.append((label, paths.transcript(record.key)))

    hits = search_transcripts(items, query, regex)
    for hit in hits:
        console.print(f"[cyan]{hit.label}[/cyan] [dim]{hit.role}[/dim] {escape(hit.snippet)}")
    console.print(f"[dim]{len(hits)} hit(s)[/dim]")


@app.command()
def rm(
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation."),
) -> None:
    """Remove the git worktrees created for a run."""
    run_id = run or latest_run_id(Path("."))
    if not run_id:
        logger.error("no cmux runs found here.")
        raise typer.Exit(1)

    manifest, records = load_run(Path("."), run_id)
    if not force and not typer.confirm(f"Remove {len(records)} worktree(s) for run {run_id}?"):
        raise typer.Exit(1)

    for record in records:
        remove_worktree(manifest.repo_root, record.worktree, force=True)
    prune_worktrees(manifest.repo_root)
    console.print(f"[green]removed worktrees for run {run_id}.[/green]")


@app.command()
def down(
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Stop a run's background daemon and any live sessions."""
    root = Path(".")
    run_id = run or latest_run_id(root)
    if not run_id:
        logger.error("no cmux runs found here.")
        raise typer.Exit(1)

    _, records = load_run(root, run_id)
    if not yes and not typer.confirm(f"Stop run {run_id}?"):
        raise typer.Exit(1)

    signalled = daemon.stop(RunPaths(root, run_id), records)
    console.print(f"[green]stopped {signalled} process(es) for run {run_id}.[/green]")


@app.command()
def kill(
    key: str = typer.Argument(..., help="Item key to stop."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
) -> None:
    """Stop a single running session."""
    paths, record = _resolve_record(run, key)
    if daemon.kill_session(paths, record):
        console.print(f"[green]killed session {key}.[/green]")
    else:
        console.print(f"[dim]session {key} was not running.[/dim]")


@app.command(name="_daemon", hidden=True)
def _daemon_command(run_id: str = typer.Argument(...)) -> None:
    supervisor = Supervisor.from_run(".", run_id)
    try:
        asyncio.run(supervisor.run(headless=True))
    finally:
        daemon.clear_owner(supervisor.paths)


def _render_event(event: dict) -> None:
    event_type = event.get("type", "")
    data = event.get("data") if isinstance(event.get("data"), dict) else event

    if event_type == "user.message":
        console.print(f"[bold blue]🧑 user[/bold blue] {escape(str(data.get('content', '')).strip())}")
    elif event_type == "assistant.message":
        text = str(data.get("content", "")).strip()
        if text:
            console.print(f"[bold green]🤖 assistant[/bold green] {escape(text)}")
    elif event_type == "tool.execution_start":
        console.print(f"[cyan]🔧 tool[/cyan] {escape(str(data.get('toolName') or data.get('name') or ''))}")
    elif event_type == "result":
        console.print(f"[dim]— result exit={event.get('exitCode')}[/dim]")


def _emit_transcript(text: str, raw: bool) -> int:
    boundary = text.rfind("\n") + 1
    for line in text[:boundary].splitlines():
        if raw:
            typer.echo(line)
        else:
            event = parse_line(line)
            if event is not None:
                _render_event(event)

    return boundary


def _follow_transcript(transcript: Path, raw: bool, consumed: int) -> None:
    try:
        while True:
            text = transcript.read_text(encoding="utf-8")
            consumed += _emit_transcript(text[consumed:], raw)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass


def _tail_last_assistant(transcript: Path) -> str:
    if not transcript.exists():
        return ""

    last = ""
    for line in transcript.read_text(encoding="utf-8").splitlines():
        event = parse_line(line)
        if event is not None and event.get("type") == "assistant.message":
            data = event.get("data") if isinstance(event.get("data"), dict) else event
            text = str(data.get("content", ""))
            if text:
                last = text

    return " ".join(last.split())[:80]


def _monitor_table(run_id: str, records: list[SessionRecord], paths: RunPaths) -> Table:
    table = Table(title=f"cmux · run {run_id}", expand=True)
    table.add_column("item", style="bold", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("model", no_wrap=True)
    table.add_column("detail", overflow="ellipsis")
    table.add_column("branch / PR", no_wrap=True)

    for record in records:
        color = _STATUS_COLOR.get(record.status, "cyan")
        detail = record.error.splitlines()[0][:80] if record.status == Status.FAILED and record.error else ""
        if record.status not in TERMINAL:
            detail = _tail_last_assistant(paths.transcript(record.key))
        table.add_row(
            record.key,
            f"[{color}]{record.status}[/{color}]",
            record.model,
            detail,
            record.pr_url or record.branch,
        )

    return table


def _print_summary(root: Path, run_id: str | None) -> None:
    run_id = run_id or latest_run_id(root)
    if not run_id:
        logger.error("no cmux runs found here.")
        raise typer.Exit(1)

    _, records = load_run(root, run_id)
    records = daemon.reconcile(RunPaths(root, run_id), records)

    table = Table(title=f"cmux · run {run_id}", expand=True)
    table.add_column("item", style="bold")
    table.add_column("status")
    table.add_column("model")
    table.add_column("branch")
    table.add_column("PR")

    for record in records:
        color = _STATUS_COLOR.get(record.status, "cyan")
        table.add_row(
            record.key, f"[{color}]{record.status}[/{color}]", record.model, record.branch, record.pr_url or "-"
        )

    console.print(table)


def main() -> None:
    """Run the cmux command-line interface."""
    app()


if __name__ == "__main__":
    main()
