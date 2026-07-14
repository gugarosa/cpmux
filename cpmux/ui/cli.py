# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import os
import re
import shlex
import shutil
import sys
import termios
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click
import typer
from rich.live import Live
from rich.markup import escape
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from cpmux import __version__, theme
from cpmux.config import ConfigError, Deps, Plan, ResolvedItem, load_plan
from cpmux.engine import daemon
from cpmux.engine.copilot_store import (
    CopilotStoreUnavailable,
    InvalidFtsQuery,
    search_sessions,
)
from cpmux.engine.interact import followup_argv, resume_interactive_argv
from cpmux.engine.session import SessionRunner
from cpmux.engine.store import (
    RunPaths,
    SessionRecord,
    all_run_ids,
    delete_run,
    latest_run_id,
    load_run,
)
from cpmux.engine.supervisor import Options, Supervisor
from cpmux.events import (
    ACTIVE,
    SUCCESS,
    TERMINAL,
    TERMINAL_FAILURE,
    event_data,
    parse_line,
)
from cpmux.ui.render import event_text
from cpmux.ui.search import search_transcripts
from cpmux.vcs.git import GitError, prune_worktrees, remove_worktree
from cpmux.voice.recorder import record_and_transcribe
from cpmux.voice.synthesizer import synthesize_plan
from cpmux.voice.transcriber import DEFAULT_TRANSCRIBE_MODEL, VoiceError, transcribe

app = typer.Typer(
    add_completion=True,
    no_args_is_help=True,
    rich_markup_mode="rich",
    help=(
        "Run parallel GitHub Copilot CLI agents from a YAML plan. Each item uses an isolated "
        "git worktree and branch and opens a draft PR by default."
    ),
    epilog=(
        "[bold]Quick start[/bold]\n\n"
        "cpmux init → cpmux up cpmux.yml --dry-run → cpmux up cpmux.yml → cpmux dash\n\n"
        "Run-scoped commands target the latest run in the current repository unless --run is given."
    ),
)
console = theme.out


def _version(value: bool) -> None:
    if value:
        console.print(f"cpmux {__version__}")
        raise typer.Exit()


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version, is_eager=True, help="Show version and exit."
    ),
) -> None:
    pass


def _load_plan_or_exit(file: Path) -> Plan:
    try:
        return load_plan(file)
    except ConfigError as exc:
        hint = "create one with `cpmux init`, or generate one with `cpmux plan`." if not Path(file).exists() else None
        theme.print_error(str(exc), hint=hint)
        raise typer.Exit(1)


def _run_id_or_exit(run: str | None, root: Path = Path(".")) -> str:
    run_id = run or latest_run_id(root)
    if not run_id:
        theme.print_error(
            f"no cpmux runs found in `{root.resolve()}`.",
            hint="start one with `cpmux up <plan.yml>`, or preview it with `--dry-run`.",
        )
        raise typer.Exit(1)

    return run_id


def _resolve_record(run: str | None, key: str) -> tuple[RunPaths, SessionRecord]:
    run_id = _run_id_or_exit(run)
    paths = RunPaths(Path("."), run_id)
    if not paths.record_file(key).exists():
        theme.print_error(
            f"no session `{key}` in run `{run_id}`.",
            hint=f"list the run's items with `cpmux ls --run {run_id}`.",
        )
        raise typer.Exit(1)

    return paths, paths.read_record(key)


def _require_tool(name: str, hint: str) -> None:
    if shutil.which(name) is None:
        theme.print_error(f"`{name}` was not found on PATH.", hint=hint)
        raise typer.Exit(1)


_COPILOT_HINT = "install the GitHub Copilot CLI and run `copilot` once to authenticate."
_GH_HINT = "install the GitHub CLI and run `gh auth login`, or rerun with `--no-pr`."


def _display_argv(argv: list[str]) -> str:
    parts: list[str] = []
    redact_next = False
    for token in argv:
        if redact_next:
            parts.append(f"<prompt:{len(token)} chars>")
            redact_next = False
            continue
        parts.append(token)
        if token == "-p":
            redact_next = True

    return shlex.join(parts)


def _plan_table(resolved: list[ResolvedItem]) -> Table:
    show_env = any(item.env for item in resolved)
    table = theme.table(title="resolved plan")
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


_STARTER_PLAN = """\
# One Copilot session per item — see the README for all options
system: |
  Shared guidance added to every item's prompt.
defaults:
  model: gpt-5.5
items:
  - fix the flaky login test
  - add pagination to the notifications list
"""


@app.command(rich_help_panel="Create & run")
def init(
    output: Path = typer.Argument(Path("cpmux.yml"), dir_okay=False, help="Plan file to create."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing file."),
) -> None:
    """Write a starter cpmux plan."""

    if output.exists() and not force:
        theme.print_error(f"`{output}` already exists.", hint="pass `--force` to overwrite it.")
        raise typer.Exit(1)

    output.write_text(_STARTER_PLAN, encoding="utf-8")
    theme.print_success(f"wrote {output}.")
    theme.print_hint(f"edit it, then preview with `cpmux up {output} --dry-run`.")


@app.command(rich_help_panel="Create & run")
def up(
    file: Path = typer.Argument(Path("cpmux.yml"), dir_okay=False, help="cpmux plan file (default: cpmux.yml)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Resolve and print the plan; spawn nothing."),
    detach: bool = typer.Option(
        True,
        "--detach/--foreground",
        "-d/-f",
        help="Run in the background and return (default); --foreground stays attached.",
    ),
    concurrency: int | None = typer.Option(None, "--concurrency", "-j", help="Max parallel sessions."),
    pr: bool = typer.Option(True, "--pr/--no-pr", help="Open one draft PR per item (default: on)."),
    deps: Deps | None = typer.Option(None, "--deps", help="Override dependency strategy."),
    strip_github_token: bool = typer.Option(
        True,
        "--strip-github-token/--no-strip-github-token",
        help="Unset GITHUB_TOKEN/GH_TOKEN for gh and git push (keyring fallback).",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Spawn one Copilot session per item."""

    if dry_run:
        resolved = _load_plan_or_exit(file).resolve()
        publish = "one draft PR per item" if pr else "local commits only"
        parallel = str(concurrency) if concurrency else "plan default"
        console.print(_plan_table(resolved))
        theme.print_hint(
            f"dry run — nothing is created · {len(resolved)} session(s) · max {parallel} concurrent "
            f"· publish: {publish} · deps: {str(deps) if deps else 'per item'}"
        )
        console.print("\n[bold]spawn commands[/bold] (redacted, not executable):")
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


@contextmanager
def _quiet_terminal() -> Iterator[None]:
    if not sys.stdin.isatty():
        yield
        return

    fd = sys.stdin.fileno()
    try:
        saved = termios.tcgetattr(fd)
    except termios.error:
        yield
        return

    quiet = termios.tcgetattr(fd)
    quiet[3] &= ~(termios.ECHO | termios.ICANON)
    try:
        termios.tcsetattr(fd, termios.TCSANOW, quiet)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        termios.tcflush(fd, termios.TCIFLUSH)


def _launch_run(file: Path, options: Options, detach: bool, yes: bool) -> None:
    plan = _load_plan_or_exit(file)
    resolved = plan.resolve()

    _require_tool("copilot", _COPILOT_HINT)
    if options.open_pr:
        _require_tool("gh", _GH_HINT)

    try:
        supervisor = Supervisor.create(plan, ".", options, str(file))
    except GitError as exc:
        theme.print_error(str(exc))
        raise typer.Exit(1)

    console.print(_plan_table(resolved))
    action = f"open {len(resolved)} draft PR(s)" if options.open_pr else "commit locally (no PR)"
    prompt = (
        f"Start {len(resolved)} Copilot session(s) in separate worktrees (max {supervisor.concurrency} concurrent) "
        f"and {action}? Premium requests may be consumed."
    )
    if not yes and not typer.confirm(prompt):
        theme.print_hint("cancelled; nothing was started.")
        return

    supervisor.prepare()
    if detach:
        daemon.launch_detached(supervisor.run_id, str(supervisor.repo_root))
        run_id = supervisor.run_id
        theme.print_success(f"started run {run_id} in the background ({len(resolved)} item(s)).")
        theme.print_hint(f"watch:     cpmux dash --run {run_id}")
        theme.print_hint(f"or:        cpmux attach --run {run_id}")
        theme.print_hint(f"stop:      cpmux down --run {run_id}")
        return

    daemon.write_owner(supervisor.paths, os.getpid())
    interrupted = False
    try:
        with _quiet_terminal():
            records = asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        interrupted = True
        records = list(supervisor.records.values())
    finally:
        daemon.clear_owner(supervisor.paths)

    if interrupted:
        theme.print_warning(f"run {supervisor.run_id} interrupted; sessions were stopped.")
        raise typer.Exit(130)

    _print_completion_summary(supervisor.run_id, records)

    if any(record.status in TERMINAL_FAILURE for record in records):
        raise typer.Exit(1)


def _print_completion_summary(run_id: str, records: list[SessionRecord]) -> None:
    done = sum(record.status in SUCCESS for record in records)
    failed = sum(record.status in TERMINAL_FAILURE for record in records)

    table = theme.table(title=f"cpmux · run {run_id}")
    table.add_column("item", style="bold")
    table.add_column("result")
    table.add_column("elapsed", justify="right")
    table.add_column("PR / reason", overflow="fold")

    for record in records:
        detail = record.pr_url or ""
        if record.status in TERMINAL_FAILURE and record.error:
            detail = record.error.splitlines()[0]
        elapsed = record.elapsed_seconds
        table.add_row(
            record.key,
            theme.status_text(record.status),
            theme.format_duration(elapsed) if elapsed is not None else "-",
            detail or "-",
        )

    console.print(table)

    if failed:
        theme.print_error(
            f"run {run_id} finished with {failed} failed item(s).",
            hint=f"inspect a failure with `cpmux logs <item> --run {run_id}`.",
        )
    else:
        theme.print_success(f"run {run_id} finished: {done} item(s) completed.")


@app.command(rich_help_panel="Create & run")
def plan(
    output: Path = typer.Argument(Path("cpmux.yml"), dir_okay=False, help="Output cpmux file."),
    text: str | None = typer.Option(None, "--text", help="Plan text (skips the editor)."),
    voice: bool = typer.Option(False, "--voice", help="Record a plan from the mic (Enter to stop)."),
    audio: Path | None = typer.Option(None, "--audio", exists=True, dir_okay=False, help="Audio file to transcribe."),
    transcribe_model: str = typer.Option(
        DEFAULT_TRANSCRIBE_MODEL, "--transcribe-model", help="faster-whisper model (e.g. small, large-v3-turbo)."
    ),
    model: str = typer.Option("gpt-5.5", "--model", help="Copilot model for plan synthesis."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing output file."),
    up: bool = typer.Option(False, "--up", help="Launch the generated plan."),
    pr: bool = typer.Option(True, "--pr/--no-pr", help="With --up, open one draft PR per item (default: on)."),
    detach: bool = typer.Option(
        True, "--detach/--foreground", "-d/-f", help="With --up, run in the background (default)."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip launch confirmation."),
) -> None:
    """Create a cpmux plan."""

    if sum([bool(text), voice, audio is not None]) > 1:
        theme.print_error("`--text`, `--voice`, and `--audio` are mutually exclusive; choose one.")
        raise typer.Exit(1)

    if output.exists() and not force:
        theme.print_error(f"`{output}` already exists.", hint="pass `--force` to overwrite it.")
        raise typer.Exit(1)

    try:
        transcript = _resolve_transcript(text, audio, voice, transcribe_model)
        console.print(f"[dim]transcript:[/dim] {escape(transcript)}")
        yaml_text = synthesize_plan(transcript, model)
    except VoiceError as exc:
        theme.print_error(str(exc))
        raise typer.Exit(1)

    output.write_text(yaml_text, encoding="utf-8")
    theme.print_success(f"wrote {output}.")
    console.print(Syntax(yaml_text, "yaml", theme="ansi_dark", background_color="default"))

    if up:
        _launch_run(output, Options(open_pr=pr), detach, yes)
    else:
        theme.print_hint(f"review it, then run `cpmux up {output}` (add `--dry-run` to preview).")


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
    return record_and_transcribe(transcribe_model)


@app.command(rich_help_panel="Monitor")
def ls(run: str | None = typer.Option(None, "--run", help="Run id (default: latest).")) -> None:
    """Show run status."""

    root = Path(".")
    run_id = run or latest_run_id(root)
    if not run_id:
        theme.print_hint("no cpmux runs yet — start one with `cpmux up <plan.yml>`.")
        return

    _print_run_summary(root, run_id)


@app.command(rich_help_panel="Monitor")
def attach(run: str | None = typer.Option(None, "--run", help="Run id (default: latest).")) -> None:
    """Monitor a run read-only (Ctrl-C to exit)."""

    root = Path(".")
    run_id = _run_id_or_exit(run, root)
    paths = RunPaths(root, run_id)

    records: list[SessionRecord] = []
    try:
        with _quiet_terminal(), Live(console=console, refresh_per_second=4) as live:
            while True:
                _, records = load_run(root, run_id)
                records = daemon.reconcile(paths, records, persist=False)
                live.update(_run_table(run_id, records, paths))
                if all(record.status in TERMINAL for record in records):
                    break
                time.sleep(0.5)
    except KeyboardInterrupt:
        return

    if any(record.status in TERMINAL_FAILURE for record in records):
        raise typer.Exit(1)


@app.command(rich_help_panel="Monitor")
def dash(run: str | None = typer.Option(None, "--run", help="Run id (default: latest).")) -> None:
    """Open a run dashboard."""

    run_id = _run_id_or_exit(run)

    from cpmux.ui.dashboard import CpmuxApp

    CpmuxApp(".", run_id).run()


@app.command(rich_help_panel="Interact")
def enter(
    key: str = typer.Argument(..., help="Item key to open."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
) -> None:
    """Open an item's Copilot session."""

    _, record = _resolve_record(run, key)
    _require_tool("copilot", _COPILOT_HINT)
    if not Path(record.worktree).exists():
        theme.print_error(f"worktree `{record.worktree}` is missing; the run may have been cleaned.")
        raise typer.Exit(1)

    os.execvp("copilot", resume_interactive_argv(record.session_id, record.worktree))


@app.command(rich_help_panel="Interact")
def send(
    key: str = typer.Argument(..., help="Item key to message."),
    message: str = typer.Argument(..., help="Follow-up prompt."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
) -> None:
    """Send a follow-up prompt to an item."""

    paths, record = _resolve_record(run, key)
    _require_tool("copilot", _COPILOT_HINT)
    if not Path(record.worktree).exists():
        theme.print_error(f"worktree `{record.worktree}` is missing; the run may have been cleaned.")
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


@app.command(rich_help_panel="Monitor")
def logs(
    key: str = typer.Argument(..., help="Item key to show."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    raw: bool = typer.Option(False, "--raw", help="Print raw JSONL."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new events."),
) -> None:
    """Print a session transcript."""

    run_id = _run_id_or_exit(run)
    paths = RunPaths(Path("."), run_id)
    if not paths.record_file(key).exists():
        theme.print_error(
            f"no session `{key}` in run `{run_id}`.",
            hint=f"list the run's items with `cpmux ls --run {run_id}`.",
        )
        raise typer.Exit(1)

    transcript = paths.transcript(key)
    if not transcript.exists() and not follow:
        theme.print_hint(f"no transcript events for `{key}`; use `-f` to wait.")
        return

    if follow and theme.err.is_terminal:
        theme.err.print(f"[dim]following {run_id}/{key} — Ctrl-C to stop[/dim]")

    consumed = _emit_transcript(transcript.read_text(encoding="utf-8"), raw) if transcript.exists() else 0
    if follow:
        _follow_transcript(transcript, raw, consumed)


@app.command(rich_help_panel="Monitor")
def search(
    query: str = typer.Argument(..., help="Text to find (literal unless --regex)."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    all_runs: bool = typer.Option(False, "--all", help="Search every run; conflicts with --run."),
    regex: bool = typer.Option(False, "--regex", help="Interpret QUERY as a regular expression."),
    fts: bool = typer.Option(False, "--fts", help="Rank matches via Copilot's full-text index."),
) -> None:
    """Search session transcripts."""

    if fts and regex:
        theme.print_error("`--regex` and `--fts` cannot be combined; choose one.")
        raise typer.Exit(1)
    if all_runs and run:
        theme.print_error("`--all` and `--run` cannot be combined; choose one.")
        raise typer.Exit(1)
    if regex:
        try:
            re.compile(query)
        except re.error as exc:
            theme.print_error(f"`{query}` is not a valid regex: {exc}.", hint="omit `--regex` for a literal search.")
            raise typer.Exit(1)

    root = Path(".")
    if all_runs:
        run_ids = all_run_ids(root)
    else:
        latest = run or latest_run_id(root)
        run_ids = [latest] if latest else []
    if not run_ids:
        theme.print_error(
            "no cpmux runs found here.",
            hint="start one with `cpmux up <plan.yml>`.",
        )
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
    if not hits:
        theme.print_hint(f"no matches for `{query}`.")
        return

    by_label: dict[str, list] = {}
    for hit in hits:
        by_label.setdefault(hit.label, []).append(hit)

    for label, group in by_label.items():
        console.print(Text.assemble((label, "bold cyan"), (f"  ({len(group)})", "dim")))
        for hit in group:
            line = Text("  ")
            line.append(f"{hit.role}  ", style="dim")
            line.append_text(_highlight(hit.snippet, query, regex))
            console.print(line)

    console.print(f"[dim]{len(hits)} match(es) in {len(by_label)} session(s)[/dim]")


def _highlight(snippet: str, query: str, regex: bool) -> Text:
    text = Text(snippet)
    try:
        pattern = re.compile(query if regex else re.escape(query), re.IGNORECASE)
    except re.error:
        return text

    for match in pattern.finditer(snippet):
        text.stylize("bold yellow", match.start(), match.end())

    return text


def _search_fts(query: str, label_by_session: dict[str, str]) -> None:
    try:
        hits = search_sessions(list(label_by_session), query)
    except (InvalidFtsQuery, CopilotStoreUnavailable) as exc:
        theme.print_error(str(exc))
        raise typer.Exit(1)

    for hit in hits:
        label = label_by_session.get(hit.session_id, hit.session_id)
        console.print(f"[cyan]{label}[/cyan] {escape(hit.snippet)}")

    console.print(f"[dim]{len(hits)} hit(s)[/dim]")


@app.command(rich_help_panel="Stop & clean up")
def rm(
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
    force: bool = typer.Option(False, "--force", "-f", help="Delete worktrees with uncommitted changes."),
    purge: bool = typer.Option(False, "--purge", help="Also delete run history so it leaves `cpmux ls`."),
) -> None:
    """Remove a run's git worktrees."""

    root = Path(".")
    run_id = _run_id_or_exit(run, root)

    if daemon.owner_alive(RunPaths(root, run_id)):
        theme.print_error(
            f"run {run_id} is still active.",
            hint=f"stop it first with `cpmux down --run {run_id}`.",
        )
        raise typer.Exit(1)

    manifest, records = load_run(root, run_id)
    scope = "worktree(s) and run history" if purge else "worktree(s)"
    kept = "" if purge else " Branches, PRs, and run history are kept."
    if not yes and not typer.confirm(f"Remove {len(records)} {scope} for run {run_id}?{kept}"):
        theme.print_hint("cancelled; nothing was removed.")
        raise typer.Exit()

    failed = [record.key for record in records if not remove_worktree(manifest.repo_root, record.worktree, force=force)]
    prune_worktrees(manifest.repo_root)

    if failed:
        for key in failed:
            theme.print_error(
                f"could not remove worktree for `{key}`; it may have uncommitted changes.",
                hint="commit or discard them, or pass `--force` to delete anyway.",
            )
        raise typer.Exit(1)

    if purge:
        delete_run(root, run_id)
        theme.print_success(f"removed {len(records)} worktree(s) and purged run {run_id}.")
    else:
        theme.print_success(f"removed {len(records)} worktree(s) for run {run_id}.")


@app.command(rich_help_panel="Stop & clean up")
def down(
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Stop a run and its sessions."""

    root = Path(".")
    run_id = _run_id_or_exit(run, root)
    paths = RunPaths(root, run_id)

    _, records = load_run(root, run_id)
    live = [record.key for record in records if daemon.pid_alive(record.pid)]
    scope = (["daemon"] if daemon.owner_alive(paths) else []) + ([f"{len(live)} live session(s)"] if live else [])
    if not scope:
        theme.print_hint(f"run {run_id} is already stopped.")
        return

    if not yes and not typer.confirm(f"Stop run {run_id} ({', '.join(scope)})? Worktrees are kept."):
        theme.print_hint("cancelled; nothing was stopped.")
        raise typer.Exit()

    signalled = daemon.stop(paths, records)
    theme.print_success(f"stopped {signalled} process(es) for run {run_id}.")
    theme.print_hint(f"remove the worktrees later with `cpmux rm --run {run_id}`.")


@app.command(rich_help_panel="Stop & clean up")
def kill(
    key: str = typer.Argument(..., help="Item key to stop."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Stop a running session."""

    paths, record = _resolve_record(run, key)
    if not yes and not typer.confirm(f"Stop session {key}? Its worktree is kept."):
        theme.print_hint("cancelled; nothing was stopped.")
        raise typer.Exit()

    if daemon.kill_session(paths, record):
        theme.print_success(f"stopped session {key}.")
    else:
        theme.print_hint(f"session {key} was not running.")


@app.command(name="_daemon", hidden=True)
def _daemon_command(run_id: str = typer.Argument(...)) -> None:
    supervisor = Supervisor.from_run(".", run_id)
    try:
        records = asyncio.run(supervisor.run(headless=True))
    finally:
        daemon.clear_owner(supervisor.paths)

    if any(record.status in TERMINAL_FAILURE for record in records):
        raise typer.Exit(1)


def _render_event(event: dict) -> None:
    text = event_text(event)
    if text is not None:
        # Text inputs bypass the repr highlighter
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
            if transcript.exists():
                text = transcript.read_text(encoding="utf-8")
                consumed += _emit_transcript(text[consumed:], raw)
            time.sleep(0.5)
    except KeyboardInterrupt:
        if theme.err.is_terminal:
            theme.err.print("[dim]stopped following; session continues.[/dim]")


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


def _run_table(run_id: str, records: list[SessionRecord], paths: RunPaths) -> Table:
    done = sum(record.status in SUCCESS for record in records)
    active = sum(record.status in ACTIVE for record in records)
    failed = sum(record.status in TERMINAL_FAILURE for record in records)
    title = f"cpmux · run {run_id} · {done}/{len(records)} done · {active} active · {failed} failed"

    table = theme.table(title=title)
    table.add_column("item", style="bold", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("elapsed", justify="right", no_wrap=True)
    table.add_column("activity", overflow="ellipsis")
    table.add_column("branch / PR", no_wrap=True)

    for record in records:
        activity = ""
        if record.status in ACTIVE:
            activity = _tail_last_assistant(paths.transcript(record.key))
        elif record.status in TERMINAL_FAILURE and record.error:
            activity = record.error.splitlines()[0][:80]
        elapsed = record.elapsed_seconds
        table.add_row(
            record.key,
            theme.status_text(record.status),
            theme.format_duration(elapsed) if elapsed is not None else "-",
            activity,
            record.pr_url or record.branch,
        )

    return table


def _print_run_summary(root: Path, run_id: str | None) -> None:
    run_id = _run_id_or_exit(run_id, root)
    paths = RunPaths(root, run_id)

    _, records = load_run(root, run_id)
    records = daemon.reconcile(paths, records, persist=False)

    console.print(_run_table(run_id, records, paths))
    if any(record.status in ACTIVE for record in records):
        theme.print_hint(f"live view: cpmux attach --run {run_id}")


def main() -> None:
    """Run the cpmux CLI."""

    app()


if __name__ == "__main__":
    main()
