# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path

import click
import typer
from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.syntax import Syntax
from rich.table import Table

from cmux import __version__
from cmux.config import ConfigError, Deps, Plan, ResolvedItem, load_plan
from cmux.engine import daemon
from cmux.engine.copilot_store import (
    CopilotStoreUnavailable,
    InvalidFtsQuery,
    search_sessions,
)
from cmux.engine.interact import followup_argv, resume_interactive_argv
from cmux.engine.session import SessionRunner
from cmux.engine.store import (
    RunPaths,
    SessionRecord,
    all_run_ids,
    latest_run_id,
    load_run,
)
from cmux.engine.supervisor import Options, Supervisor
from cmux.events import TERMINAL, Status, event_data, parse_line
from cmux.logging import get_logger
from cmux.ui.render import STATUS_COLOR, event_text
from cmux.ui.search import search_transcripts
from cmux.vcs.git import GitError, prune_worktrees, remove_worktree
from cmux.voice.recorder import record_to_file
from cmux.voice.synthesizer import synthesize_plan
from cmux.voice.transcriber import DEFAULT_TRANSCRIBE_MODEL, VoiceError, transcribe

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Run GitHub Copilot CLI agents from YAML.",
)
console = Console()
logger = get_logger(__name__)


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


def _run_id_or_exit(run: str | None, root: Path = Path(".")) -> str:
    run_id = run or latest_run_id(root)
    if not run_id:
        logger.error("no cmux runs found here.")
        raise typer.Exit(1)

    return run_id


def _resolve_record(run: str | None, key: str) -> tuple[RunPaths, SessionRecord]:
    run_id = _run_id_or_exit(run)
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
    show_env = any(item.env for item in resolved)
    table = Table(title="resolved plan", expand=True)
    table.add_column("item", style="bold")
    table.add_column("model")
    table.add_column("effort")
    table.add_column("branch")
    table.add_column("perms")
    table.add_column("deps on")
    if show_env:
        table.add_column("env")

    for item in resolved:
        row = [
            item.key,
            item.model,
            str(item.effort),
            item.branch,
            item.permissions.preset,
            ", ".join(item.depends_on) or "-",
        ]
        if show_env:
            row.append(", ".join(f"{name}={value}" for name, value in item.env.items()) or "-")
        table.add_row(*row)

    return table


@app.command()
def up(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="cmux YAML file."),
    dry_run: bool = typer.Option(False, "--dry-run", "--dry_run", help="Resolve and print the plan; spawn nothing."),
    detach: bool = typer.Option(False, "--detach", "-d", help="Run in background and return."),
    concurrency: int | None = typer.Option(None, "--concurrency", "-j", help="Max parallel sessions."),
    pr: bool = typer.Option(True, "--pr/--no-pr", help="Open one draft PR per item (default: on)."),
    deps: Deps | None = typer.Option(None, "--deps", help="Override dependency strategy."),
    strip_github_token: bool = typer.Option(
        True,
        "--strip-github-token/--no-strip-github-token",
        "--strip_github_token/--no-strip_github_token",
        help="Unset ambient GITHUB_TOKEN/GH_TOKEN for gh + git push (keyring fallback).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Spawn one Copilot session per item."""

    if dry_run:
        resolved = _load(file).resolve()
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
    _launch_run(file, options, detach, yes)


def _launch_run(file: Path, options: Options, detach: bool, yes: bool) -> None:
    plan = _load(file)
    resolved = plan.resolve()

    try:
        supervisor = Supervisor.create(plan, ".", options, str(file))
    except GitError as exc:
        logger.error(str(exc))
        raise typer.Exit(1)

    console.print(_plan_table(resolved))
    action = "open a draft PR" if options.open_pr else "commit locally (no PR)"
    if not yes and not typer.confirm(f"Spawn {len(resolved)} Copilot session(s) in separate worktrees and {action}?"):
        raise typer.Exit(1)

    supervisor.prepare()
    if detach:
        daemon.launch_detached(supervisor.run_id, str(supervisor.repo_root))
        console.print(f"[green]run {supervisor.run_id} started in background.[/green]")
        console.print("[dim]monitor with:[/dim] cmux attach")
        return

    daemon.write_owner(supervisor.paths, os.getpid())
    try:
        asyncio.run(supervisor.run())
    finally:
        daemon.clear_owner(supervisor.paths)

    _print_summary(Path("."), supervisor.run_id)


@app.command()
def plan(
    output: Path = typer.Argument(Path("cmux.yml"), dir_okay=False, help="Output cmux file."),
    text: str | None = typer.Option(None, "--text", help="Plan text (skips the editor)."),
    voice: bool = typer.Option(False, "--voice", help="Record a spoken plan from the mic (Enter to stop)."),
    audio: Path | None = typer.Option(None, "--audio", exists=True, dir_okay=False, help="Audio file to transcribe."),
    transcribe_model: str = typer.Option(
        DEFAULT_TRANSCRIBE_MODEL, "--transcribe-model", help="faster-whisper model size (tiny…large-v3)."
    ),
    model: str = typer.Option("gpt-5.5", "--model", help="Copilot model for plan synthesis."),
    up: bool = typer.Option(False, "--up", help="Launch the generated plan."),
    pr: bool = typer.Option(True, "--pr/--no-pr", help="With --up, open one draft PR per item (default: on)."),
    detach: bool = typer.Option(False, "--detach", "-d", help="With --up, run in background."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip launch confirmation."),
) -> None:
    """Compose a cmux plan in your editor, or from text, speech, or audio."""

    try:
        transcript = _resolve_transcript(text, audio, voice, transcribe_model)
        console.print(f"[dim]transcript:[/dim] {escape(transcript)}")
        yaml_text = synthesize_plan(transcript, model)
    except VoiceError as exc:
        logger.error(str(exc))
        raise typer.Exit(1)

    output.write_text(yaml_text, encoding="utf-8")
    console.print(f"[green]wrote {output}[/green]")
    console.print(Syntax(yaml_text, "yaml", theme="ansi_dark", background_color="default"))

    if up:
        _launch_run(output, Options(open_pr=pr), detach, yes)


def _resolve_transcript(text: str | None, audio: Path | None, voice: bool, transcribe_model: str) -> str:
    if voice:
        return _record_and_transcribe(transcribe_model)
    if audio is not None:
        return transcribe(audio, transcribe_model)
    if text:
        return text
    return _compose_in_editor()


def _compose_in_editor() -> str:
    composed = click.edit(extension=".md")
    if composed is None or not composed.strip():
        raise VoiceError("no plan text provided.")
    return composed.strip()


def _record_and_transcribe(transcribe_model: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as handle:
        wav = Path(handle.name)
    try:
        record_to_file(wav)
        return transcribe(wav, transcribe_model)
    finally:
        wav.unlink(missing_ok=True)


@app.command()
def ls(run: str | None = typer.Option(None, "--run", help="Run id (default: latest).")) -> None:
    """Show run status."""

    _print_summary(Path("."), run)


@app.command()
def attach(run: str | None = typer.Option(None, "--run", help="Run id (default: latest).")) -> None:
    """Monitor a run read-only (Ctrl-C to exit)."""

    root = Path(".")
    run_id = _run_id_or_exit(run, root)
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
    """Open a run dashboard."""

    run_id = _run_id_or_exit(run)

    from cmux.ui.dashboard import CmuxApp

    CmuxApp(".", run_id).run()


@app.command()
def enter(
    key: str = typer.Argument(..., help="Item key to open."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
) -> None:
    """Open an item's Copilot session."""

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
    message: str = typer.Argument(..., help="Follow-up prompt."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
) -> None:
    """Send a follow-up prompt to an item."""

    paths, record = _resolve_record(run, key)
    if not Path(record.worktree).exists():
        logger.error(f"`{record.worktree}` worktree is gone, the run may have been cleaned.")
        raise typer.Exit(1)

    argv = followup_argv(record.session_id, record.worktree, record.model, record.permission_flags, message)
    state = asyncio.run(SessionRunner(key, argv, paths.transcript(key), env=record.env).run())

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
    key: str = typer.Argument(..., help="Item key to show."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    raw: bool = typer.Option(False, "--raw", help="Print raw JSONL."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new events."),
) -> None:
    """Print a session transcript."""

    run_id = _run_id_or_exit(run)

    transcript = RunPaths(Path("."), run_id).transcript(key)
    if not transcript.exists():
        logger.error(f"no transcript for `{key}` in run `{run_id}`.")
        raise typer.Exit(1)

    consumed = _emit_transcript(transcript.read_text(encoding="utf-8"), raw)
    if follow:
        _follow_transcript(transcript, raw, consumed)


@app.command()
def search(
    query: str = typer.Argument(..., help="Text to find; regex with --regex."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    all_runs: bool = typer.Option(False, "--all", help="Search every run."),
    regex: bool = typer.Option(False, "--regex", help="Treat query as regex."),
    fts: bool = typer.Option(False, "--fts", help="Rank matches via copilot's full-text index."),
) -> None:
    """Search session transcripts."""

    if fts and regex:
        logger.error("`--regex` cannot be combined with `--fts`.")
        raise typer.Exit(1)

    root = Path(".")
    if all_runs:
        run_ids = all_run_ids(root)
    else:
        latest = run or latest_run_id(root)
        run_ids = [latest] if latest else []
    if not run_ids:
        logger.error("no cmux runs found here.")
        raise typer.Exit(1)

    items: list[tuple[str, Path]] = []
    label_by_session: dict[str, str] = {}
    for run_id in run_ids:
        paths = RunPaths(root, run_id)
        _, records = load_run(root, run_id)
        for record in records:
            label = f"{run_id}/{record.key}" if all_runs else record.key
            items.append((label, paths.transcript(record.key)))
            label_by_session[record.session_id] = label

    if fts:
        _search_fts(query, label_by_session)
        return

    hits = search_transcripts(items, query, regex)
    for hit in hits:
        console.print(f"[cyan]{hit.label}[/cyan] [dim]{hit.role}[/dim] {escape(hit.snippet)}")
    console.print(f"[dim]{len(hits)} hit(s)[/dim]")


def _search_fts(query: str, label_by_session: dict[str, str]) -> None:
    try:
        hits = search_sessions(list(label_by_session), query)
    except (InvalidFtsQuery, CopilotStoreUnavailable) as exc:
        logger.error(str(exc))
        raise typer.Exit(1)

    for hit in hits:
        label = label_by_session.get(hit.session_id, hit.session_id)
        console.print(f"[cyan]{label}[/cyan] {escape(hit.snippet)}")
    console.print(f"[dim]{len(hits)} hit(s)[/dim]")


@app.command()
def rm(
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    force: bool = typer.Option(False, "--force", "-f", help="Skip confirmation."),
) -> None:
    """Remove a run's git worktrees."""

    run_id = _run_id_or_exit(run)

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
    """Stop a run and its sessions."""

    root = Path(".")
    run_id = _run_id_or_exit(run, root)

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
    """Stop a running session."""

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
    text = event_text(event)
    if text is not None:
        # Apply the repr highlighter console.print skips for Text inputs
        console.print(console.highlighter(text))


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
            data = event_data(event)
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
        color = STATUS_COLOR.get(record.status, "cyan")
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
    run_id = _run_id_or_exit(run_id, root)

    _, records = load_run(root, run_id)
    records = daemon.reconcile(RunPaths(root, run_id), records)

    table = Table(title=f"cmux · run {run_id}", expand=True)
    table.add_column("item", style="bold")
    table.add_column("status")
    table.add_column("model")
    table.add_column("branch")
    table.add_column("PR")

    for record in records:
        color = STATUS_COLOR.get(record.status, "cyan")
        table.add_row(
            record.key, f"[{color}]{record.status}[/{color}]", record.model, record.branch, record.pr_url or "-"
        )

    console.print(table)


def main() -> None:
    """Run the cmux CLI."""

    app()


if __name__ == "__main__":
    main()
