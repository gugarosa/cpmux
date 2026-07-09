# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

"""The cmux supervisor: create worktrees, spawn the session pool, open PRs.

A supervisor is built either fresh from a :class:`~cmux.config.Plan`
(:meth:`Supervisor.create`) or reconstructed from a persisted run
(:meth:`Supervisor.from_run`), so the same object drives a foreground run and a
detached daemon. Foreground runs render a live Rich table; headless runs only
persist state to ``.cmux/``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from rich.console import Console
from rich.live import Live
from rich.table import Table

from cmux import gitutil, pr
from cmux.config import Plan, ResolvedItem
from cmux.events import SessionState, Status
from cmux.logging import get_logger
from cmux.session import SessionRunner
from cmux.state import RunManifest, RunPaths, SessionRecord, new_run_id

logger = get_logger(__name__)

_GLYPH = {
    Status.PENDING: ("○", "dim"),
    Status.STARTING: ("◐", "yellow"),
    Status.RUNNING: ("●", "cyan"),
    Status.TOOL: ("⚙", "cyan"),
    Status.IDLE: ("◑", "blue"),
    Status.COMMITTING: ("◧", "magenta"),
    Status.PUSHING: ("⬆", "magenta"),
    Status.OPENING_PR: ("⇪", "magenta"),
    Status.DONE: ("✔", "green"),
    Status.NO_CHANGES: ("∅", "dim"),
    Status.FAILED: ("✖", "red"),
    Status.TIMED_OUT: ("⏱", "red"),
    Status.KILLED: ("✖", "red"),
}


@dataclass
class Options:
    """Runtime options for a single run."""

    concurrency: int | None = None
    open_pr: bool = True
    strip_github_token: bool = True
    deps_override: str | None = None


class Supervisor:
    """Drives a run end to end: worktrees, the session pool, and pull requests."""

    def __init__(
        self,
        repo_root: str | Path,
        run_id: str,
        resolved: list[ResolvedItem],
        options: Options,
        concurrency: int,
        system: str = "",
    ) -> None:
        self.repo_root = Path(repo_root)
        self.run_id = run_id
        self.resolved = resolved
        self.options = options
        self.concurrency = concurrency
        self.system = system
        self.paths = RunPaths(self.repo_root, run_id)
        self.console = Console()
        self.records: dict[str, SessionRecord] = {}
        self.live_states: dict[str, SessionState] = {}
        self.runners: dict[str, SessionRunner] = {}
        self._live: Live | None = None

    @classmethod
    def create(cls, plan: Plan, start_path: str, options: Options) -> Supervisor:
        """Build a fresh supervisor for a new run (raises if not a git repository)."""
        repo_root = gitutil.repo_root(start_path)
        concurrency = options.concurrency or plan.defaults.concurrency

        return cls(repo_root, new_run_id(), plan.resolve(), options, concurrency, plan.system)

    @classmethod
    def from_run(cls, start_path: str, run_id: str) -> Supervisor:
        """Reconstruct a supervisor and its records from a persisted run."""
        manifest = RunManifest.model_validate_json(RunPaths(start_path, run_id).manifest.read_text())
        options = Options(
            concurrency=manifest.concurrency,
            open_pr=manifest.open_pr,
            strip_github_token=manifest.strip_github_token,
            deps_override=manifest.deps_override,
        )
        sup = cls(manifest.repo_root, run_id, manifest.resolved, options, manifest.concurrency or 4, manifest.system)
        for key in manifest.item_keys:
            if sup.paths.record_file(key).exists():
                sup.records[key] = sup.paths.read_record(key)

        return sup

    def prepare(self) -> None:
        """Write the manifest and create one worktree and record per item."""
        self.paths.write_manifest(
            RunManifest(
                run_id=self.run_id,
                repo_root=str(self.repo_root),
                config_path="",
                system=self.system,
                item_keys=[it.key for it in self.resolved],
                resolved=self.resolved,
                open_pr=self.options.open_pr,
                concurrency=self.concurrency,
                strip_github_token=self.options.strip_github_token,
                deps_override=self.options.deps_override,
            )
        )

        for it in self.resolved:
            record = SessionRecord(
                key=it.key,
                name=it.name,
                slug=it.slug,
                branch=it.branch,
                base=it.base,
                model=it.model,
                session_id=str(uuid4()),
                worktree=str(self.paths.worktree(it.key)),
                permission_flags=it.permissions.to_flags(),
            )
            self.records[it.key] = record
            self.paths.ensure_session_dirs(it.key)
            self.paths.prompt_file(it.key).write_text(it.prompt)
            try:
                _, record.base_sha = gitutil.resolve_base(self.repo_root, it.remote, it.base)
                if gitutil.branch_exists(self.repo_root, record.branch):
                    record.branch = f"{it.branch}-{self.run_id[-6:]}"
                gitutil.add_worktree(self.repo_root, self.paths.worktree(it.key), record.branch, record.base_sha)
                gitutil.provision_deps(
                    self.repo_root, self.paths.worktree(it.key), self.options.deps_override or it.deps
                )
            except gitutil.GitError as exc:
                record.status = Status.FAILED
                record.error = str(exc)
                logger.warning(f"`{it.key}` worktree setup failed: {exc}.")
            self.paths.write_record(record)

    async def run(self, headless: bool = False) -> list[SessionRecord]:
        """Spawn every item under the concurrency pool and return the final records."""
        sem = asyncio.Semaphore(self.concurrency)
        done_events = {it.key: asyncio.Event() for it in self.resolved}

        if headless:
            await self._run_all(sem, done_events)
        else:
            with Live(self._render(), console=self.console, refresh_per_second=8) as live:
                self._live = live
                await self._run_all(sem, done_events)
                live.update(self._render())

        return list(self.records.values())

    async def _run_all(self, sem: asyncio.Semaphore, done: dict[str, asyncio.Event]) -> None:
        tasks = [asyncio.create_task(self._run_item(it, sem, done)) for it in self.resolved]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for runner in self.runners.values():
                runner.terminate()
            raise

    async def _run_item(self, it: ResolvedItem, sem: asyncio.Semaphore, done: dict[str, asyncio.Event]) -> None:
        record = self.records[it.key]
        try:
            for dep in it.depends_on:
                await done[dep].wait()
            if record.status == Status.FAILED:
                return
            failed_dep = next(
                (d for d in it.depends_on if self.records[d].status not in (Status.DONE, Status.NO_CHANGES)),
                None,
            )
            if failed_dep is not None:
                record.status = Status.FAILED
                record.error = f"dependency '{failed_dep}' did not succeed."
                self.paths.write_record(record)
                return

            async with sem:
                record.mark_started()
                record.status = Status.STARTING
                self.paths.write_record(record)
                self._refresh()

                argv = it.spawn_argv(self.paths.worktree(it.key), record.session_id, self.paths.copilot_log_dir(it.key))
                runner = SessionRunner(it.key, argv, self.paths.transcript(it.key))
                self.runners[it.key] = runner
                state = await runner.run(self._on_update, on_spawn=lambda pid: self._on_spawn(record, pid))

                record.exit_code = state.exit_code
                record.premium_requests = state.premium_requests
                record.files_modified = state.files_modified
                record.error = state.error
                record.status = state.status
                if state.status == Status.DONE and self.options.open_pr:
                    await self._open_pr(it, record)
                self.paths.write_record(record)
                self._refresh()
        finally:
            record.mark_ended()
            self.paths.write_record(record)
            done[it.key].set()
            self._refresh()

    def _on_spawn(self, record: SessionRecord, pid: int) -> None:
        record.pid = pid
        self.paths.write_record(record)

    async def _open_pr(self, it: ResolvedItem, record: SessionRecord) -> None:
        worktree = self.paths.worktree(it.key)
        try:
            if not await asyncio.to_thread(gitutil.has_changes, worktree, record.base_sha):
                record.status = Status.NO_CHANGES
                return

            record.status = Status.OPENING_PR
            self._refresh()
            record.pr_url = await asyncio.to_thread(
                pr.open_pull_request,
                worktree,
                it.remote,
                it.base,
                record.branch,
                it.pr_title,
                it.pr_body,
                it.labels,
                it.draft,
                f"{it.pr_title}\n\ncmux item: {it.key}",
                self.options.strip_github_token,
            )
            record.status = Status.DONE
        except (pr.PRError, gitutil.GitError) as exc:
            record.status = Status.FAILED
            record.error = str(exc)
            logger.error(f"`{it.key}` pull request failed: {exc}.")

    def _on_update(self, key: str, state: SessionState, ev: dict) -> None:
        self.live_states[key] = state
        self._refresh()

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    def _render(self) -> Table:
        table = Table(title=f"cmux · run {self.run_id}", expand=True)
        table.add_column("item", style="bold", no_wrap=True)
        table.add_column("status", no_wrap=True)
        table.add_column("model", no_wrap=True)
        table.add_column("detail", overflow="ellipsis")
        table.add_column("branch / PR", no_wrap=True)

        for it in self.resolved:
            record = self.records[it.key]
            live = self.live_states.get(it.key)
            status = live.status if live else record.status
            glyph, color = _GLYPH.get(status, ("?", "white"))
            detail = ""
            if live:
                detail = f"[{live.current_tool}] " if live.current_tool else ""
                detail += live.summary_line
            if record.error and status == Status.FAILED:
                detail = record.error.splitlines()[0][:80]
            table.add_row(
                it.key,
                f"[{color}]{glyph} {status}[/{color}]",
                it.model,
                detail,
                record.pr_url or record.branch,
            )

        return table
