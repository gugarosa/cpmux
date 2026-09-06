# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import json
import math
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
from typing import Any

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
from cpmux.engine.intake import issues_plan
from cpmux.engine.interact import run_followup, run_interactive
from cpmux.engine.ownership import (
    OwnershipError,
    file_lease,
    matching_process,
    process_owner_alive,
)
from cpmux.engine.reporting import run_report
from cpmux.engine.review import (
    diff_snapshot,
    run_feedback,
    run_finalization,
    run_verification,
)
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
    Status,
    event_data,
    parse_line,
)
from cpmux.ui.render import event_text
from cpmux.ui.search import TranscriptHit, search_transcripts
from cpmux.vcs.git import GitError, prune_worktrees, remove_worktree, run_git
from cpmux.vcs.issues import IssueError, fetch_issues
from cpmux.vcs.pr import PRError
from cpmux.voice.recorder import record_and_transcribe
from cpmux.voice.synthesizer import synthesize_plan
from cpmux.voice.transcriber import DEFAULT_TRANSCRIBE_MODEL, VoiceError, transcribe

app = typer.Typer(
    add_completion=True,
    no_args_is_help=True,
    rich_markup_mode="rich",
    pretty_exceptions_show_locals=False,
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
        hint = (
            "create one with `cpmux init`, or generate one with `cpmux plan`."
            if isinstance(exc.__cause__, FileNotFoundError)
            else None
        )
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

    try:
        paths = RunPaths(root, run_id)
    except ValueError as exc:
        theme.print_error(str(exc))
        raise typer.Exit(1)

    if not paths.manifest.exists():
        theme.print_error(
            f"no run `{run_id}` in `{root.resolve()}`.",
            hint="list existing runs with `cpmux ls`.",
        )
        raise typer.Exit(1)

    return run_id


def _session_paths(run: str | None, key: str) -> RunPaths:
    run_id = _run_id_or_exit(run)
    paths = RunPaths(Path("."), run_id)
    try:
        record_file = paths.record_file(key)
    except ValueError as exc:
        theme.print_error(str(exc))
        raise typer.Exit(1)

    if not record_file.exists():
        theme.print_error(
            f"no session `{key}` in run `{run_id}`.",
            hint=f"list the run's items with `cpmux ls --run {run_id}`.",
        )
        raise typer.Exit(1)

    return paths


def _resolve_record(run: str | None, key: str) -> tuple[RunPaths, SessionRecord]:
    paths = _session_paths(run, key)

    return paths, paths.read_record(key)


def _require_tool(name: str, hint: str) -> None:
    if shutil.which(name) is None:
        theme.print_error(f"`{name}` was not found on PATH.", hint=hint)
        raise typer.Exit(1)


@contextmanager
def _operation_errors() -> Iterator[None]:
    try:
        yield
    except (OwnershipError, GitError, PRError, IssueError, ConfigError, OSError, ValueError) as exc:
        theme.print_error(str(exc))
        raise typer.Exit(1) from exc


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
    show_commands = any(item.setup or item.checks for item in resolved)
    show_base_from = any(item.base_from is not None for item in resolved)
    table = theme.table(title="resolved plan")
    table.add_column("item", style="bold")
    table.add_column("model")
    table.add_column("effort")
    table.add_column("branch")
    table.add_column("perms")
    table.add_column("deps on")
    if show_base_from:
        table.add_column("base from")
    if show_commands:
        table.add_column("setup / checks")
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
        if show_base_from:
            row.append(item.base_from or "-")
        if show_commands:
            row.append(f"{len(item.setup)} / {len(item.checks)}")
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
    concurrency: int | None = typer.Option(None, "--concurrency", "-j", min=1, max=64, help="Max parallel sessions."),
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
        plan = _load_plan_or_exit(file)
        resolved = plan.resolve()
        publish = "one draft PR per item" if pr else "local commits only"
        parallel = str(concurrency) if concurrency else "plan default"
        console.print(_plan_table(resolved))
        if plan.defaults.premium_budget is not None:
            theme.print_hint(
                f"soft admission budget: {plan.defaults.premium_budget} premium request(s), not a hard cap."
            )
        theme.print_hint(
            f"dry run — nothing is created · {len(resolved)} session(s) · max {parallel} concurrent "
            f"· publish: {publish} · deps: {str(deps) if deps else 'per item'}"
        )
        console.print("\n[bold]spawn commands[/bold] (redacted, not executable):")
        for item in resolved:
            argv = item.spawn_argv(f"<worktree>/{item.key}", "<session-id>", "<log-dir>")
            console.print(f"  [cyan]{item.key}[/cyan]: {_display_argv(argv)}")
        for item in resolved:
            for phase, commands in (("setup", item.setup), ("check", item.checks)):
                for command in commands:
                    console.print(
                        Text(
                            f"  {item.key} · {phase} · {command.name or phase} · "
                            f"{command.timeout_seconds:g}s: {command.command}"
                        )
                    )
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

    if run_git(["rev-parse", "--verify", "--quiet", "HEAD"], supervisor.repo_root, check=False).returncode != 0:
        theme.print_error(
            "this repository has no commits yet.",
            hint="make an initial commit (`git commit`) so cpmux has a base to branch from.",
        )
        raise typer.Exit(1)

    if options.open_pr:
        configured = set(run_git(["remote"], supervisor.repo_root, check=False).stdout.split())
        missing = sorted({item.remote for item in resolved} - configured)
        if missing:
            theme.print_error(
                f"git remote `{missing[0]}` is not configured; pull requests cannot be pushed.",
                hint="add it with `git remote add ...`, or rerun with `--no-pr`.",
            )
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

    if detach:
        with _operation_errors():
            supervisor.prepare()
            daemon.launch_detached(supervisor.run_id, str(supervisor.repo_root))
        run_id = supervisor.run_id
        theme.print_success(f"started run {run_id} in the background ({len(resolved)} item(s)).")
        theme.print_hint(f"watch:     cpmux dash --run {run_id}")
        theme.print_hint(f"or:        cpmux attach --run {run_id}")
        theme.print_hint(f"stop:      cpmux down --run {run_id}")
        return

    interrupted = False
    try:
        with _operation_errors(), _quiet_terminal():
            records = asyncio.run(supervisor.run())
    except KeyboardInterrupt:
        interrupted = True
        records = list(supervisor.records.values())
    if interrupted:
        theme.print_warning(f"run {supervisor.run_id} interrupted; sessions were stopped.")
        raise typer.Exit(130)

    _print_completion_summary(supervisor.run_id, records)

    if any(record.status in TERMINAL_FAILURE for record in records):
        raise typer.Exit(1)


def _print_completion_summary(run_id: str, records: list[SessionRecord]) -> None:
    done = sum(record.status in SUCCESS for record in records)
    failed = sum(record.status in TERMINAL_FAILURE for record in records)
    premium = sum(record.premium_requests or 0 for record in records)

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

    if premium:
        theme.print_hint(f"{premium} premium request(s) consumed.")


@app.command(rich_help_panel="Create & run")
def retry(
    keys: list[str] | None = typer.Argument(
        None, help="Items to retry (default: failed, blocked, stopped or unstarted)."
    ),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    resume: bool = typer.Option(
        False, "--resume", help="Resume the existing native conversation instead of a new one."
    ),
    fresh: bool = typer.Option(
        False, "--fresh", help="Rerun setup and the task in a new conversation, keeping Git edits."
    ),
    budget: int | None = typer.Option(None, "--budget", min=1, help="Replace the soft run premium-request budget."),
    detach: bool = typer.Option(
        False, "--detach", "-d", help="Continue in the background after startup is acknowledged."
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip recovery confirmation."),
) -> None:
    """Retry selected work without replaying successful items or losing history."""

    if resume and fresh:
        theme.print_error("`--resume` and `--fresh` are mutually exclusive.")
        raise typer.Exit(1)
    run_id = _run_id_or_exit(run)
    mode = "resume" if resume else "fresh" if fresh else "retry"
    with _operation_errors():
        supervisor = Supervisor.from_run(".", run_id)
        daemon.reconcile(supervisor.paths, list(supervisor.records.values()))
        selected = [
            record
            for key, record in supervisor.records.items()
            if (key in keys if keys else record.status in TERMINAL_FAILURE or record.status == Status.PENDING)
        ]
        if any(resume or fresh or not record.agent_complete for record in selected):
            _require_tool("copilot", _COPILOT_HINT)
        if supervisor.options.open_pr:
            _require_tool("gh", _GH_HINT)
        if not yes and not typer.confirm(
            f"Recover {len(selected)} item(s) in run {run_id} using {mode}? Worktrees are kept; usage may increase."
        ):
            theme.print_hint("cancelled; no recovery was queued.")
            return
        queued = supervisor.prepare_retry(keys, mode, budget)
        if detach:
            daemon.launch_detached(run_id, str(supervisor.repo_root))
            theme.print_success(f"queued {len(queued)} item(s) in run {run_id}.")
            theme.print_hint(f"watch: cpmux dash --run {run_id}")
            return
        try:
            with _quiet_terminal():
                records = asyncio.run(supervisor.run())
        except KeyboardInterrupt:
            theme.print_warning(f"recovery for run {run_id} interrupted; worktrees are kept.")
            raise typer.Exit(130)
    _print_completion_summary(run_id, records)
    if any(record.status in TERMINAL_FAILURE for record in records):
        raise typer.Exit(1)


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
        True, "--detach/--foreground", "-d", help="With --up, run in the background (default)."
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
        return record_and_transcribe(transcribe_model)
    if audio is not None:
        theme.print_hint(f"transcribing with `{transcribe_model}` (the model downloads on first use)...")
        return transcribe(audio, transcribe_model)
    if text:
        return text
    return _compose_in_editor()


@app.command(rich_help_panel="Create & run")
def issues(
    references: list[str] | None = typer.Argument(None, help="Issue numbers or same-repository issue URLs."),
    repository: str | None = typer.Option(
        None, "--repo", help="GitHub [host/]owner/repository (default: current repository)."
    ),
    query: str | None = typer.Option(
        None, "--query", help="GitHub issue search expression instead of explicit references."
    ),
    limit: int = typer.Option(20, "--limit", min=1, max=100, help="Maximum number of issues to import."),
    template: Path | None = typer.Option(
        None, "--template", dir_okay=False, help="Plan supplying defaults and profiles, not items."
    ),
    profile: str | None = typer.Option(None, "--profile", help="Execution profile selected from the template."),
    output: Path = typer.Option(Path("cpmux.yml"), "--output", "-o", dir_okay=False, help="Editable output plan."),
    force: bool = typer.Option(False, "--force", "-f", help="Overwrite an existing output file."),
) -> None:
    """Import GitHub issues into an editable plan without running an agent."""

    if output.exists() and not force:
        theme.print_error(f"`{output}` already exists.", hint="pass `--force` to overwrite it.")
        raise typer.Exit(1)
    _require_tool("gh", "install the GitHub CLI and run `gh auth login` for the target host.")
    with _operation_errors():
        base = load_plan(template) if template is not None else None
        imported = fetch_issues(".", references or [], repository=repository, query=query, limit=limit)
        contents = issues_plan(imported, template=base, profile=profile)
        with output.open("w" if force else "x", encoding="utf-8") as handle:
            handle.write(contents)
    theme.print_success(f"wrote {len(imported)} issue(s) to {output}; no agents were started.")
    theme.print_hint(f"review it, then preview with `cpmux up {output} --dry-run`.")


def _compose_in_editor() -> str:
    try:
        composed = click.edit(extension=".md")
    except click.ClickException as exc:
        raise VoiceError(f"`editor` failed: {str(exc).removesuffix('.')}.") from exc
    if composed is None or not composed.strip():
        raise VoiceError("`plan` text is None or blank.")
    return composed.strip()


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
def report(
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    as_json: bool = typer.Option(
        False, "--json", help="Emit a versioned summary without prompts or environment values."
    ),
) -> None:
    """Report attempts, verification, and delivery without changing run state."""

    run_id = _run_id_or_exit(run)
    with _operation_errors():
        summary = run_report(".", run_id)
    if as_json:
        typer.echo(json.dumps(summary, indent=2))
        return

    table = theme.table(title=f"cpmux · {run_id} · {'paused' if summary['paused'] else 'run report'}")
    table.add_column("item", style="bold")
    table.add_column("status")
    table.add_column("phase")
    table.add_column("attempts", justify="right")
    table.add_column("checks")
    table.add_column("candidate / PR", overflow="fold")
    for item in summary["items"]:
        table.add_row(
            item["key"],
            item["status"],
            item["phase"],
            str(len(item["attempts"])),
            item["verification"]["status"],
            item["pr_url"] or (item["candidate_sha"] or "-")[:12],
        )
    console.print(table)
    if summary["owner_error"]:
        theme.print_warning(summary["owner_error"])
    theme.print_hint(
        f"reported usage: {summary['reported_premium_requests']} premium request(s); "
        f"{summary['items_with_unknown_usage']} item(s) have unknown usage."
    )


@app.command(rich_help_panel="Monitor")
def attach(run: str | None = typer.Option(None, "--run", help="Run id (default: latest).")) -> None:
    """Monitor a run (Ctrl-C to stop watching)."""

    root = Path(".")
    run_id = _run_id_or_exit(run, root)
    paths = RunPaths(root, run_id)

    records: list[SessionRecord] = []
    try:
        with _operation_errors(), _quiet_terminal(), Live(console=console, refresh_per_second=4) as live:
            while True:
                manifest, records = load_run(root, run_id)
                records = daemon.reconcile(paths, records)
                live.update(_run_table(run_id, records, paths))
                if len(records) == len(manifest.item_keys) and all(record.status in TERMINAL for record in records):
                    break
                if len(records) < len(manifest.item_keys) and not daemon.owner_alive(paths):
                    raise ValueError(f"`run={run_id}` has missing session records and no live owner.")
                if not daemon.owner_alive(paths) and not any(
                    process_owner_alive(paths.session_owner(record.key)) for record in records
                ):
                    raise ValueError(
                        f"`run={run_id}` has unresolved work without a live owner. Inspect `cpmux report`."
                    )
                time.sleep(0.5)
    except KeyboardInterrupt:
        return

    if any(record.status in TERMINAL_FAILURE for record in records):
        raise typer.Exit(1)


@app.command(rich_help_panel="Monitor")
def wait(
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    timeout: float | None = typer.Option(None, "--timeout", min=0, help="Maximum wait in seconds; timeout exits 124."),
    as_json: bool = typer.Option(False, "--json", help="Print the final versioned run report."),
    notify: bool = typer.Option(False, "--notify", help="Ring the terminal bell once when all items are terminal."),
) -> None:
    """Wait for terminal outcomes (0 success, 1 failure, 2 unowned work, 124 timeout)."""

    if timeout is not None and not math.isfinite(timeout):
        theme.print_error("`--timeout` must be finite.")
        raise typer.Exit(1)
    run_id = _run_id_or_exit(run)
    paths = RunPaths(".", run_id)
    deadline = time.monotonic() + timeout if timeout is not None else None
    code = 0
    with _operation_errors():
        while True:
            manifest, records = load_run(".", run_id)
            daemon.reconcile(paths, records)
            terminal = len(records) == len(manifest.item_keys) and all(record.status in TERMINAL for record in records)
            if terminal:
                code = int(any(record.status in TERMINAL_FAILURE for record in records))
                if notify and theme.err.is_terminal:
                    theme.err.bell()
                break
            if not daemon.owner_alive(paths) and not any(
                process_owner_alive(paths.session_owner(record.key)) for record in records
            ):
                code = 2
                break
            if deadline is not None and time.monotonic() >= deadline:
                code = 124
                break
            time.sleep(0.2)

        if as_json:
            typer.echo(json.dumps(run_report(".", run_id), indent=2))
        elif code in {0, 1}:
            _print_completion_summary(run_id, records)
        elif code == 2:
            theme.print_error(
                f"`run={run_id}` has unfinished work but no live owner.", hint="use `cpmux retry` to recover."
            )
        else:
            theme.print_warning(f"waiting for run {run_id} timed out; its work continues.")
    if code:
        raise typer.Exit(code)


@app.command(rich_help_panel="Interact")
def pause(run: str | None = typer.Option(None, "--run", help="Run id (default: latest).")) -> None:
    """Pause admission of queued items without stopping active work."""

    run_id = _run_id_or_exit(run)
    with _operation_errors():
        daemon.set_paused(RunPaths(".", run_id), True)
    theme.print_success(f"paused queue {run_id}; active work continues.")


@app.command(rich_help_panel="Interact")
def unpause(run: str | None = typer.Option(None, "--run", help="Run id (default: latest).")) -> None:
    """Allow queued work in an active run without overriding its soft budget."""

    run_id = _run_id_or_exit(run)
    with _operation_errors():
        daemon.set_paused(RunPaths(".", run_id), False)
    theme.print_success(f"unpaused queue {run_id}; an idle run still requires `cpmux retry`.")


@app.command(rich_help_panel="Monitor")
def dash(run: str | None = typer.Option(None, "--run", help="Run id (default: latest).")) -> None:
    """Open a run dashboard."""

    run_id = _run_id_or_exit(run)

    from cpmux.ui.dashboard import CpmuxApp

    CpmuxApp(".", run_id).run()


@app.command(rich_help_panel="Interact")
def enter(
    key: str = typer.Argument(..., help="Item key to open (from `cpmux ls`)."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
) -> None:
    """Open an item's Copilot session."""

    paths, record = _resolve_record(run, key)
    _require_tool("copilot", _COPILOT_HINT)
    if not Path(record.worktree).exists():
        theme.print_error(f"worktree `{record.worktree}` is missing; the run may have been cleaned.")
        raise typer.Exit(1)

    with _operation_errors():
        code = asyncio.run(run_interactive(paths, record))
    if code != 0:
        raise typer.Exit(1)


@app.command(rich_help_panel="Interact")
def diff(
    key: str = typer.Argument(..., help="Item whose changes should be reviewed."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    as_json: bool = typer.Option(False, "--json", help="Emit the source revision and diff as JSON."),
) -> None:
    """Show a read-only diff and its revision token for targeted feedback."""

    with _operation_errors():
        paths, record = _resolve_record(run, key)
        snapshot = diff_snapshot(paths, record)
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "revision": snapshot.revision,
                    "base_sha": snapshot.base_sha,
                    "head_sha": snapshot.head_sha,
                    "text": snapshot.text,
                },
                indent=2,
            )
        )
        return
    theme.print_hint(f"revision: {snapshot.revision}")
    console.print(Syntax(snapshot.text or "(no changes)", "diff", theme="ansi_dark", background_color="default"))


@app.command(rich_help_panel="Interact")
def feedback(
    key: str = typer.Argument(..., help="Reviewed item to repair."),
    message: str = typer.Argument(..., help="Review feedback for the shown revision."),
    revision: str = typer.Option(..., "--revision", help="Revision token from `cpmux diff`."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    file_path: str | None = typer.Option(
        None, "--file", help="Optional relative source path providing review context."
    ),
    line: int | None = typer.Option(None, "--line", min=1, help="Optional one-based line; requires --file."),
) -> None:
    """Send review feedback only if the source still matches the reviewed diff."""

    _require_tool("copilot", _COPILOT_HINT)
    with _operation_errors():
        paths, record = _resolve_record(run, key)
        state = asyncio.run(run_feedback(paths, record, message, revision, file_path=file_path, line=line))
    if state.last_text:
        console.print(Text(state.last_text))
    _print_session_outcome(record)


def _print_session_outcome(record: SessionRecord) -> None:
    console.print(Text.assemble((record.key, "bold"), " ", theme.status_text(record.status)))
    if record.pr_url:
        console.print(Text(record.pr_url))
    if record.status in TERMINAL_FAILURE:
        theme.print_error(record.error or f"`session={record.key}` did not succeed.")
        raise typer.Exit(1)


@app.command(rich_help_panel="Interact")
def verify(
    key: str = typer.Argument(..., help="Item whose local candidate should be checked."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
) -> None:
    """Commit and verify a local candidate without pushing or opening a PR."""

    with _operation_errors():
        paths, record = _resolve_record(run, key)
        asyncio.run(run_verification(paths, record))
    _print_session_outcome(record)
    if record.verification is None:
        theme.print_warning("no successful acceptance receipt was recorded; this candidate is not verified.")
    else:
        theme.print_success(f"verified candidate {record.verification.commit_sha[:12]}; nothing was pushed.")


@app.command(rich_help_panel="Interact")
def finalize(
    key: str = typer.Argument(..., help="Item to finalize or update on its matching open PR."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
) -> None:
    """Explicitly verify and deliver an item using its persisted PR settings."""

    with _operation_errors():
        paths, record = _resolve_record(run, key)
        asyncio.run(run_finalization(paths, record))
    _print_session_outcome(record)


@app.command(rich_help_panel="Interact")
def send(
    key: str = typer.Argument(..., help="Item key to message (from `cpmux ls`)."),
    message: str = typer.Argument(..., help="Follow-up prompt."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
) -> None:
    """Send a follow-up prompt to an item."""

    paths, record = _resolve_record(run, key)
    _require_tool("copilot", _COPILOT_HINT)
    if not Path(record.worktree).exists():
        theme.print_error(f"worktree `{record.worktree}` is missing; the run may have been cleaned.")
        raise typer.Exit(1)

    with _operation_errors():
        state = asyncio.run(run_followup(paths, record, message))

    if state.last_text:
        console.print(f"[bold green]🤖 assistant[/bold green] {escape(state.last_text)}")
    else:
        console.print(f"[dim]session {record.status}[/dim]")

    if state.status in TERMINAL_FAILURE:
        if state.error:
            theme.print_error(state.error)
        raise typer.Exit(1)


@app.command(rich_help_panel="Monitor")
def logs(
    key: str = typer.Argument(..., help="Item key to show (from `cpmux ls`)."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    raw: bool = typer.Option(False, "--raw", help="Print raw JSONL."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new events."),
) -> None:
    """Print a session transcript."""

    paths = _session_paths(run, key)

    transcript = paths.transcript(key)
    if not transcript.exists() and not follow:
        theme.print_hint(f"no transcript events for `{key}`; use `-f` to wait.")
        return

    if follow and theme.err.is_terminal:
        theme.err.print(f"[dim]following {paths.run_id}/{key} — Ctrl-C to stop[/dim]")

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
        if not run_ids:
            theme.print_error(
                "no cpmux runs found here.",
                hint="start one with `cpmux up <plan.yml>`.",
            )
            raise typer.Exit(1)
    else:
        run_ids = [_run_id_or_exit(run, root)]

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

    by_label: dict[str, list[TranscriptHit]] = {}
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
    paths = RunPaths(root, run_id)
    with _operation_errors(), file_lease(paths.owner_lock):
        if daemon.owner_alive(paths):
            theme.print_error(
                f"run {run_id} is still active.",
                hint=f"stop it first with `cpmux down --run {run_id}`.",
            )
            raise typer.Exit(1)

        manifest, records = load_run(root, run_id)
        for record in records:
            if record.pid is not None and matching_process(record.pid, record.pid_created_at) is not None:
                raise OwnershipError(f"`session={record.key}` still has a live child. Stop the run before removal.")
        scope = "worktree(s) and run history" if purge else "worktree(s)"
        kept = "" if purge else " Branches, PRs, and run history are kept."
        if not yes and not typer.confirm(f"Remove {len(records)} {scope} for run {run_id}?{kept}"):
            theme.print_hint("cancelled; nothing was removed.")
            raise typer.Exit()

        removed = 0
        failed = []
        for record in records:
            existed = Path(record.worktree).exists()
            if remove_worktree(manifest.repo_root, record.worktree, force=force):
                if existed:
                    removed += 1
            else:
                failed.append(record.key)
        prune_worktrees(manifest.repo_root)

        worktrees_dir = paths.worktrees_dir
        if worktrees_dir.exists() and not any(worktrees_dir.iterdir()):
            worktrees_dir.rmdir()

        if failed:
            for key in failed:
                theme.print_error(
                    f"could not remove worktree for `{key}`; it may have uncommitted changes.",
                    hint="commit or discard them, or pass `--force` to delete anyway.",
                )
            raise typer.Exit(1)

        if purge:
            delete_run(root, run_id)
            theme.print_success(f"removed {removed} worktree(s) and purged run {run_id}.")
        else:
            theme.print_success(f"removed {removed} worktree(s) for run {run_id}.")


@app.command(rich_help_panel="Stop & clean up")
def down(
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Stop a run and its sessions."""

    root = Path(".")
    run_id = _run_id_or_exit(run, root)
    paths = RunPaths(root, run_id)

    with _operation_errors():
        _, records = load_run(root, run_id)
        unfinished = [record.key for record in records if record.status not in TERMINAL]
        scope = (["run owner"] if daemon.owner_alive(paths) else []) + (
            [f"{len(unfinished)} unfinished item(s)"] if unfinished else []
        )
    if not scope:
        theme.print_hint(f"run {run_id} is already stopped.")
        return

    if not yes and not typer.confirm(f"Stop run {run_id} ({', '.join(scope)})? Worktrees are kept."):
        theme.print_hint("cancelled; nothing was stopped.")
        raise typer.Exit()

    with _operation_errors():
        signalled = daemon.stop(paths, records)
    theme.print_success(f"stopped {signalled} process(es) for run {run_id}.")
    theme.print_hint(f"remove the worktrees later with `cpmux rm --run {run_id}`.")


@app.command(rich_help_panel="Stop & clean up")
def kill(
    key: str = typer.Argument(..., help="Item key to stop (from `cpmux ls`)."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation."),
) -> None:
    """Stop a running session."""

    paths, record = _resolve_record(run, key)
    if not yes and not typer.confirm(f"Stop session {key}? Its worktree is kept."):
        theme.print_hint("cancelled; nothing was stopped.")
        raise typer.Exit()

    with _operation_errors():
        stopped = daemon.kill_session(paths, record)
    if stopped:
        theme.print_success(f"requested stop for session {key}.")
    else:
        theme.print_hint(f"session {key} was not running.")


@app.command(name="_daemon", hidden=True)
def _daemon_command(run_id: str = typer.Argument(...)) -> None:
    with _operation_errors():
        supervisor = Supervisor.from_run(".", run_id)
        records = asyncio.run(supervisor.run(headless=True, startup_wait_seconds=5.0))

    if any(record.status in TERMINAL_FAILURE for record in records):
        raise typer.Exit(1)


def _render_event(event: dict[str, Any]) -> None:
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
    stopped = sum(record.status == Status.KILLED for record in records)
    failed = sum(record.status in TERMINAL_FAILURE for record in records) - stopped
    premium = sum(record.premium_requests or 0 for record in records)
    title = f"cpmux · run {run_id} · {done}/{len(records)} done · {active} active · {failed} failed"
    if stopped:
        title += f" · {stopped} stopped"
    if premium:
        title += f" · {premium} premium"
    if paths.pause_file.exists():
        title += " · queue paused"

    table = theme.table(title=title)
    table.add_column("item", style="bold", ratio=2, no_wrap=True, overflow="ellipsis")
    table.add_column("status", no_wrap=True)
    table.add_column("elapsed", justify="right", no_wrap=True)
    table.add_column("activity", ratio=3, overflow="ellipsis")
    table.add_column("branch / PR", ratio=2, no_wrap=True, overflow="ellipsis")

    for record in records:
        activity = ""
        commands = record.attempts[-1].commands if record.attempts else []
        if commands and commands[-1].status == "running":
            activity = f"{record.phase}: {commands[-1].name}"
        elif record.status in ACTIVE:
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

    with _operation_errors():
        _, records = load_run(root, run_id)
        records = daemon.reconcile(paths, records)

    console.print(_run_table(run_id, records, paths))
    if any(record.status in ACTIVE for record in records):
        theme.print_hint(f"live view: cpmux attach --run {run_id}")


def main() -> None:
    """Run the cpmux CLI."""

    app()


if __name__ == "__main__":
    main()
