# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from rich.live import Live
from rich.table import Table

from cmux import theme
from cmux.config import Plan, ResolvedItem
from cmux.engine.session import SessionRunner
from cmux.engine.store import RunManifest, RunPaths, SessionRecord, new_run_id
from cmux.events import ACTIVE, SUCCESS, TERMINAL_FAILURE, SessionState, Status
from cmux.logging import get_logger
from cmux.vcs import git, pr

logger = get_logger(__name__)


@dataclass
class Options:
    """Runtime options for a run."""

    concurrency: int | None = None
    open_pr: bool = True
    strip_github_token: bool = True
    deps_override: str | None = None


class Supervisor:
    """Drive run worktrees, sessions, and pull requests."""

    def __init__(
        self,
        repo_root: str | Path,
        run_id: str,
        resolved: list[ResolvedItem],
        options: Options,
        concurrency: int,
        system: str = "",
        config_path: str = "",
    ) -> None:
        self.repo_root = Path(repo_root)
        self.run_id = run_id
        self.resolved = resolved
        self.options = options
        self.concurrency = concurrency
        self.system = system
        self.config_path = config_path

        self.paths = RunPaths(self.repo_root, run_id)
        self.console = theme.err
        self.records: dict[str, SessionRecord] = {}
        self.live_states: dict[str, SessionState] = {}
        self.runners: dict[str, SessionRunner] = {}
        self._live: Live | None = None
        self._started_at: float | None = None

    @classmethod
    def create(cls, plan: Plan, start_path: str, options: Options, config_path: str = "") -> "Supervisor":
        """Create a supervisor for a new run.

        Raises:
            git.GitError: `start_path` is outside a git repository.

        """

        repo_root = git.repo_root(start_path)
        concurrency = options.concurrency or plan.defaults.concurrency

        return cls(repo_root, new_run_id(), plan.resolve(), options, concurrency, plan.system, config_path)

    @classmethod
    def from_run(cls, start_path: str, run_id: str) -> "Supervisor":
        """Reconstruct a supervisor from a persisted run."""

        manifest = RunManifest.model_validate_json(RunPaths(start_path, run_id).manifest.read_text())
        options = Options(
            concurrency=manifest.concurrency,
            open_pr=manifest.open_pr,
            strip_github_token=manifest.strip_github_token,
            deps_override=manifest.deps_override,
        )
        supervisor = cls(
            manifest.repo_root,
            run_id,
            manifest.resolved,
            options,
            manifest.concurrency or 4,
            manifest.system,
            manifest.config_path,
        )

        for key in manifest.item_keys:
            if supervisor.paths.record_file(key).exists():
                supervisor.records[key] = supervisor.paths.read_record(key)

        return supervisor

    def prepare(self) -> None:
        """Write the manifest and create one worktree and record per item."""

        self.paths.write_manifest(
            RunManifest(
                run_id=self.run_id,
                repo_root=str(self.repo_root),
                config_path=self.config_path,
                system=self.system,
                item_keys=[item.key for item in self.resolved],
                resolved=self.resolved,
                open_pr=self.options.open_pr,
                concurrency=self.concurrency,
                strip_github_token=self.options.strip_github_token,
                deps_override=self.options.deps_override,
            )
        )

        for item in self.resolved:
            record = SessionRecord(
                key=item.key,
                name=item.name,
                slug=item.slug,
                branch=item.branch,
                base=item.base,
                model=item.model,
                session_id=str(uuid4()),
                worktree=str(self.paths.worktree(item.key)),
                permission_flags=item.permissions.to_flags(),
                env=dict(item.env),
            )
            self.records[item.key] = record
            self.paths.ensure_session_dirs(item.key)
            self.paths.prompt_file(item.key).write_text(item.prompt)

            try:
                _, record.base_sha = git.resolve_base(self.repo_root, item.remote, item.base)
                if git.branch_exists(self.repo_root, record.branch):
                    record.branch = f"{item.branch}-{self.run_id[-6:]}"
                git.add_worktree(self.repo_root, self.paths.worktree(item.key), record.branch, record.base_sha)
                git.provision_deps(
                    self.repo_root, self.paths.worktree(item.key), self.options.deps_override or item.deps
                )
            except git.GitError as exc:
                record.status = Status.FAILED
                record.error = str(exc)
                logger.warning(f"`{item.key}` worktree setup failed: {exc}.")

            self.paths.write_record(record)

    async def run(self, headless: bool = False) -> list[SessionRecord]:
        """Run all items under the concurrency limit.

        Args:
            headless: Whether to disable the live table.

        Returns:
            Final item records.

        """

        sem = asyncio.Semaphore(self.concurrency)
        done_events = {item.key: asyncio.Event() for item in self.resolved}
        self._started_at = time.monotonic()

        if headless:
            await self._run_all(sem, done_events)
        else:
            with Live(self._render(), console=self.console, refresh_per_second=8, transient=True) as live:
                self._live = live
                await self._run_all(sem, done_events)
                live.update(self._render())

        return list(self.records.values())

    async def _run_all(self, sem: asyncio.Semaphore, done: dict[str, asyncio.Event]) -> None:
        tasks = [asyncio.create_task(self._run_item(item, sem, done)) for item in self.resolved]
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            for runner in self.runners.values():
                runner.terminate()
            raise

    async def _run_item(self, item: ResolvedItem, sem: asyncio.Semaphore, done: dict[str, asyncio.Event]) -> None:
        record = self.records[item.key]
        try:
            for dep in item.depends_on:
                await done[dep].wait()
            if record.status == Status.FAILED:
                return

            failed_dep = next(
                (dep for dep in item.depends_on if self.records[dep].status not in SUCCESS),
                None,
            )
            if failed_dep is not None:
                record.status = Status.FAILED
                record.error = f"dependency `{failed_dep}` did not succeed."
                self.paths.write_record(record)
                return

            async with sem:
                record.mark_started()
                record.status = Status.STARTING
                self.paths.write_record(record)
                self._refresh()

                argv = item.spawn_argv(
                    self.paths.worktree(item.key), record.session_id, self.paths.copilot_log_dir(item.key)
                )
                runner = SessionRunner(item.key, argv, self.paths.transcript(item.key), env=item.env)
                self.runners[item.key] = runner
                state = await runner.run(self._on_update, on_spawn=lambda pid: self._on_spawn(record, pid))

                record.exit_code = state.exit_code
                record.premium_requests = state.premium_requests
                record.files_modified = state.files_modified
                record.error = state.error
                if state.status == Status.DONE:
                    record.status = Status.FINALIZING
                    self.paths.write_record(record)
                    self._refresh()
                    await self._finalize(item, record)
                else:
                    record.status = state.status
                self.paths.write_record(record)
                self._refresh()
        finally:
            record.mark_ended()
            self.paths.write_record(record)
            done[item.key].set()
            self._refresh()

    def _on_spawn(self, record: SessionRecord, pid: int) -> None:
        record.pid = pid
        self.paths.write_record(record)

    async def _finalize(self, item: ResolvedItem, record: SessionRecord) -> None:
        try:
            if self.options.open_pr:
                await self._open_pr(item, record)
            else:
                await self._commit_local(item, record)
        except Exception as exc:
            record.status = Status.FAILED
            record.error = record.error or str(exc)
            logger.error(f"`{item.key}` finalization failed: {exc}.")

    async def _open_pr(self, item: ResolvedItem, record: SessionRecord) -> None:
        worktree = self.paths.worktree(item.key)
        try:
            if not await asyncio.to_thread(git.has_changes, worktree, record.base_sha):
                record.status = Status.NO_CHANGES
                return

            record.status = Status.OPENING_PR
            self.paths.write_record(record)
            self._refresh()
            record.pr_url = await asyncio.to_thread(
                pr.open_pull_request,
                worktree,
                item.remote,
                item.base,
                record.branch,
                item.pr_title,
                item.pr_body,
                item.labels,
                item.draft,
                f"{item.pr_title}\n\ncmux item: {item.key}",
                self.options.strip_github_token,
            )
            record.status = Status.DONE
        except (pr.PRError, git.GitError) as exc:
            record.status = Status.FAILED
            record.error = str(exc)
            logger.error(f"`{item.key}` pull request failed: {exc}.")

    async def _commit_local(self, item: ResolvedItem, record: SessionRecord) -> None:
        worktree = self.paths.worktree(item.key)
        try:
            committed = await asyncio.to_thread(
                pr.commit_all,
                worktree,
                f"{item.pr_title}\n\ncmux item: {item.key}",
                pr.gh_env(self.options.strip_github_token),
            )
            record.status = Status.DONE if committed else Status.NO_CHANGES
        except pr.PRError as exc:
            record.status = Status.FAILED
            record.error = str(exc)
            logger.error(f"`{item.key}` local commit failed: {exc}.")

    def _on_update(self, key: str, state: SessionState, event: dict) -> None:
        self.live_states[key] = state
        record = self.records[key]
        status = Status.FINALIZING if state.status == Status.DONE else state.status
        if status != record.status:
            record.status = status
            self.paths.write_record(record)
        self._refresh()

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    def _render(self) -> Table:
        table = theme.table(title=self._title())
        table.add_column("item", style="bold", no_wrap=True)
        table.add_column("status", no_wrap=True)
        table.add_column("elapsed", no_wrap=True, justify="right")
        table.add_column("detail", overflow="ellipsis")
        table.add_column("branch / PR", no_wrap=True)

        for item in self.resolved:
            record = self.records[item.key]
            live = self.live_states.get(item.key)
            status = record.status

            detail = ""
            if live and status in ACTIVE:
                detail = f"[{live.current_tool}] " if live.current_tool else ""
                detail += live.summary_line
            if record.error and status in TERMINAL_FAILURE:
                detail = record.error.splitlines()[0][:80]

            elapsed = record.elapsed_seconds
            table.add_row(
                item.key,
                theme.status_text(status),
                theme.format_duration(elapsed) if elapsed is not None else "-",
                detail,
                record.pr_url or record.branch,
            )

        return table

    def _title(self) -> str:
        statuses = [record.status for record in self.records.values()]
        done = sum(status in SUCCESS for status in statuses)
        active = sum(status in ACTIVE for status in statuses)
        failed = sum(status in TERMINAL_FAILURE for status in statuses)
        wall = theme.format_duration(time.monotonic() - self._started_at) if self._started_at else "0:00"

        return f"cmux · run {self.run_id} · {done}/{len(statuses)} done · {active} active · {failed} failed · {wall}"
