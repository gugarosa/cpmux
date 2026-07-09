"""The cmux supervisor: create worktrees, spawn the session pool, open PRs.

v0 runs in the foreground with an asyncio pool (concurrency + ``depends_on``
ordering) and a live Rich status table. A background daemon is planned for v1.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import uuid4

from rich.console import Console
from rich.live import Live
from rich.table import Table

from . import gitutil, pr
from .config import Plan, ResolvedItem
from .events import Status, SessionState
from .session import SessionRunner
from .state import RunManifest, RunPaths, SessionRecord, new_run_id

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
    concurrency: int | None = None
    open_pr: bool = True
    strip_github_token: bool = True
    deps_override: str | None = None


class Supervisor:
    def __init__(self, plan: Plan, start_path: str, options: Options) -> None:
        self.plan = plan
        self.options = options
        self.console = Console()
        self.repo_root = gitutil.repo_root(start_path)
        self.run_id = new_run_id()
        self.paths = RunPaths(self.repo_root, self.run_id)
        self.resolved: list[ResolvedItem] = plan.resolve()
        self.records: dict[str, SessionRecord] = {}
        self.base_sha: dict[str, str] = {}
        self.live_states: dict[str, SessionState] = {}
        self.runners: dict[str, SessionRunner] = {}
        self._live: Live | None = None

    # ---- setup -------------------------------------------------------------

    def prepare(self) -> None:
        self.paths.write_manifest(
            RunManifest(
                run_id=self.run_id,
                repo_root=str(self.repo_root),
                config_path="",
                system=self.plan.system,
                item_keys=[it.key for it in self.resolved],
            )
        )
        for it in self.resolved:
            session_id = str(uuid4())
            worktree = self.paths.worktree(it.key)
            record = SessionRecord(
                key=it.key,
                name=it.name,
                slug=it.slug,
                branch=it.branch,
                base=it.base,
                model=it.model,
                session_id=session_id,
                worktree=str(worktree),
            )
            self.records[it.key] = record
            self.paths.ensure_session_dirs(it.key)
            self.paths.prompt_file(it.key).write_text(it.prompt)
            try:
                _, base_sha = gitutil.resolve_base(self.repo_root, it.remote, it.base)
                self.base_sha[it.key] = base_sha
                gitutil.add_worktree(self.repo_root, worktree, it.branch, base_sha)
                strategy = self.options.deps_override or it.deps
                gitutil.provision_deps(self.repo_root, worktree, strategy)
            except gitutil.GitError as exc:
                record.status = Status.FAILED
                record.error = str(exc)
            self.paths.write_record(record)

    # ---- run ---------------------------------------------------------------

    async def run(self) -> list[SessionRecord]:
        concurrency = self.options.concurrency or self.plan.defaults.concurrency
        sem = asyncio.Semaphore(concurrency)
        done_events = {it.key: asyncio.Event() for it in self.resolved}
        with Live(self._render(), console=self.console, refresh_per_second=8) as live:
            self._live = live
            tasks = [
                asyncio.create_task(self._run_item(it, sem, done_events))
                for it in self.resolved
            ]
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                for runner in self.runners.values():
                    runner.terminate()
                raise
            live.update(self._render())
        return list(self.records.values())

    async def _run_item(
        self, it: ResolvedItem, sem: asyncio.Semaphore, done: dict[str, asyncio.Event]
    ) -> None:
        record = self.records[it.key]
        try:
            for dep in it.depends_on:
                await done[dep].wait()
            if record.status == Status.FAILED:
                return
            failed_dep = next(
                (d for d in it.depends_on
                 if self.records[d].status not in (Status.DONE, Status.NO_CHANGES)),
                None,
            )
            if failed_dep is not None:
                record.status = Status.FAILED
                record.error = f"dependency '{failed_dep}' did not succeed"
                self.paths.write_record(record)
                return

            async with sem:
                record.mark_started()
                record.status = Status.STARTING
                self.paths.write_record(record)
                self._refresh()

                worktree = self.paths.worktree(it.key)
                argv = it.spawn_argv(
                    worktree, record.session_id, self.paths.copilot_log_dir(it.key)
                )
                runner = SessionRunner(it.key, argv, self.paths.transcript(it.key))
                self.runners[it.key] = runner
                state = await runner.run(self._on_update)

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

    async def _open_pr(self, it: ResolvedItem, record: SessionRecord) -> None:
        worktree = self.paths.worktree(it.key)
        base_sha = self.base_sha.get(it.key, "")
        try:
            changed = await asyncio.to_thread(gitutil.has_changes, worktree, base_sha)
            if not changed:
                record.status = Status.NO_CHANGES
                return
            record.status = Status.OPENING_PR
            self._refresh()
            url = await asyncio.to_thread(
                pr.open_pull_request,
                worktree,
                it.remote,
                it.base,
                it.branch,
                it.pr_title,
                it.pr_body,
                it.labels,
                it.draft,
                f"{it.pr_title}\n\ncmux item: {it.key}",
                self.options.strip_github_token,
            )
            record.pr_url = url
            record.status = Status.DONE
        except (pr.PRError, gitutil.GitError) as exc:
            record.status = Status.FAILED
            record.error = str(exc)

    # ---- rendering ---------------------------------------------------------

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
            branch_pr = record.pr_url or record.branch
            table.add_row(
                it.key,
                f"[{color}]{glyph} {status}[/{color}]",
                it.model,
                detail,
                branch_pr,
            )
        return table
