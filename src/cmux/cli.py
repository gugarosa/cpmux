# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

"""cmux command-line interface (Typer).

v0 verbs: ``up`` (spawn a run), ``ls`` (status of a run), ``logs`` (a session
transcript), ``rm`` (clean up worktrees). A background daemon plus the
interactive dashboard arrive in v1.
"""

import asyncio
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from cmux import __version__
from cmux.config import ConfigError, Deps, Plan, ResolvedItem, load_plan
from cmux.events import parse_line
from cmux.gitutil import GitError, prune_worktrees, remove_worktree
from cmux.logging import get_logger
from cmux.state import RunPaths, latest_run_id, load_run
from cmux.supervisor import Options, Supervisor

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="cmux — a declarative, guided multiplexer for GitHub Copilot CLI agents.",
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


def _display_argv(argv: list[str]) -> str:
    out: list[str] = []
    skip = False
    for tok in argv:
        if skip:
            out.append(f"<prompt {len(tok)} chars>")
            skip = False
            continue
        out.append(tok)
        if tok == "-p":
            skip = True
    return " ".join(out)


def _plan_table(resolved: list[ResolvedItem]) -> Table:
    table = Table(title="resolved plan", expand=True)
    table.add_column("item", style="bold")
    table.add_column("model")
    table.add_column("effort")
    table.add_column("branch")
    table.add_column("perms")
    table.add_column("deps on")
    for it in resolved:
        table.add_row(
            it.key, it.model, str(it.effort), it.branch, it.permissions.preset, ", ".join(it.depends_on) or "-"
        )
    return table


@app.command()
def up(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Path to the cmux YAML file."),
    dry_run: bool = typer.Option(False, "--dry-run", "--dry_run", help="Resolve and print the plan; spawn nothing."),
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
    """Spawn one isolated Copilot session per item, each in its own worktree, branch, and PR."""
    plan = _load(file)
    resolved = plan.resolve()

    if dry_run:
        console.print(_plan_table(resolved))
        console.print("\n[bold]spawn commands:[/bold]")
        for it in resolved:
            argv = it.spawn_argv(f"<worktree>/{it.key}", "<session-id>", "<log-dir>")
            console.print(f"  [cyan]{it.key}[/cyan]: {_display_argv(argv)}")
        return

    options = Options(
        concurrency=concurrency,
        open_pr=pr,
        strip_github_token=strip_github_token,
        deps_override=str(deps) if deps else None,
    )
    try:
        supervisor = Supervisor(plan, ".", options)
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
    asyncio.run(supervisor.run())
    _print_summary(Path("."), supervisor.run_id)


@app.command()
def ls(run: str | None = typer.Option(None, "--run", help="Run id (default: latest).")) -> None:
    """Show the status of a run."""
    _print_summary(Path("."), run)


@app.command()
def logs(
    key: str = typer.Argument(..., help="Item key to show the transcript for."),
    run: str | None = typer.Option(None, "--run", help="Run id (default: latest)."),
    raw: bool = typer.Option(False, "--raw", help="Print the raw JSONL transcript."),
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

    for line in transcript.read_text(encoding="utf-8").splitlines():
        if raw:
            typer.echo(line)
            continue
        ev = parse_line(line)
        if ev is not None:
            _render_event(ev)


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


def _render_event(ev: dict) -> None:
    typ = ev.get("type", "")
    data = ev.get("data") if isinstance(ev.get("data"), dict) else ev
    if typ == "user.message":
        console.print(f"[bold blue]🧑 user[/bold blue] {escape(str(data.get('content', '')).strip())}")
    elif typ == "assistant.message":
        text = str(data.get("content", "")).strip()
        if text:
            console.print(f"[bold green]🤖 assistant[/bold green] {escape(text)}")
    elif typ == "tool.execution_start":
        console.print(f"[cyan]🔧 tool[/cyan] {escape(str(data.get('toolName') or data.get('name') or ''))}")
    elif typ == "result":
        console.print(f"[dim]— result exit={ev.get('exitCode')}[/dim]")


def _print_summary(root: Path, run_id: str | None) -> None:
    run_id = run_id or latest_run_id(root)
    if not run_id:
        logger.error("no cmux runs found here.")
        raise typer.Exit(1)

    _, records = load_run(root, run_id)
    table = Table(title=f"cmux · run {run_id}", expand=True)
    table.add_column("item", style="bold")
    table.add_column("status")
    table.add_column("model")
    table.add_column("branch")
    table.add_column("PR")
    for record in records:
        color = {"done": "green", "no_changes": "dim", "failed": "red"}.get(record.status, "cyan")
        table.add_row(
            record.key, f"[{color}]{record.status}[/{color}]", record.model, record.branch, record.pr_url or "-"
        )
    console.print(table)


def main() -> None:
    """Entry point for the ``cmux`` command."""
    app()


if __name__ == "__main__":
    main()
