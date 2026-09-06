# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from rich.live import Live
from rich.table import Table

from cpmux import theme
from cpmux.config import CommandSpec, Deps, Plan, ResolvedItem
from cpmux.engine.delivery import finalize_item, run_steps
from cpmux.engine.interact import followup_argv
from cpmux.engine.ownership import OwnershipError, process_created_at, run_owner
from cpmux.engine.session import SessionRunner
from cpmux.engine.store import RunManifest, RunPaths, SessionRecord, new_run_id
from cpmux.events import (
    ACTIVE,
    SUCCESS,
    TERMINAL,
    TERMINAL_FAILURE,
    SessionState,
    Status,
)
from cpmux.logging import get_logger
from cpmux.vcs import git, pr

logger = get_logger(__name__)


@dataclass
class Options:
    """Runtime options for a run.

    Attributes:
        concurrency: Concurrent session limit.
        open_pr: Whether to open pull requests.
        strip_github_token: Whether to remove GitHub tokens.
        deps_override: Dependency provisioning override.

    """

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
        premium_budget: int | None = None,
    ) -> None:
        """Initialize a supervisor.

        Args:
            repo_root: Repository root.
            run_id: Run identifier.
            resolved: Resolved run items.
            options: Runtime options.
            concurrency: Concurrent session limit.
            system: System prompt.
            config_path: Configuration file path.
            premium_budget: Soft run-wide admission limit on reported premium requests.

        """

        self.repo_root = Path(repo_root)
        self.run_id = run_id
        self.resolved = resolved
        self.options = options
        self.concurrency = concurrency
        self.system = system
        self.config_path = config_path
        self.premium_budget = premium_budget

        self.paths = RunPaths(self.repo_root, run_id)
        self.console = theme.err
        self.records: dict[str, SessionRecord] = {}
        self.live_states: dict[str, SessionState] = {}
        self.runners: dict[str, SessionRunner] = {}
        self._live: Live | None = None
        self._started_at: float | None = None
        self._last_persist: dict[str, float] = {}

    @classmethod
    def create(cls, plan: Plan, start_path: str, options: Options, config_path: str = "") -> "Supervisor":
        """Initialize a new run.

        Args:
            plan: Run plan.
            start_path: Path within the repository.
            options: Runtime options.
            config_path: Configuration file path.

        Returns:
            New run supervisor.

        Raises:
            git.GitError: Path is outside a git repository.

        """

        repo_root = git.repo_root(start_path)
        concurrency = options.concurrency or plan.defaults.concurrency

        return cls(
            repo_root,
            new_run_id(),
            plan.resolve(),
            options,
            concurrency,
            plan.system,
            config_path,
            plan.defaults.premium_budget,
        )

    @classmethod
    def from_run(cls, start_path: str, run_id: str) -> "Supervisor":
        """Load a supervisor from a persisted run.

        Args:
            start_path: Repository root containing the persisted run.
            run_id: Run identifier.

        Returns:
            Restored run supervisor.

        Raises:
            ValueError: Execution configuration or session records are incomplete.
            OSError: Run history cannot be read.

        """

        manifest = RunManifest.model_validate_json(RunPaths(start_path, run_id).manifest.read_text(encoding="utf-8"))
        if set(manifest.item_keys) != {item.key for item in manifest.resolved}:
            raise ValueError(
                f"`run={run_id}` has incomplete resolved configuration. Restore its manifest before recovery."
            )
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
            manifest.premium_budget,
        )

        for key in manifest.item_keys:
            if not supervisor.paths.record_file(key).exists():
                raise ValueError(f"`session={key}` has no record. Restore run history before recovery.")
            supervisor.records[key] = supervisor.paths.read_record(key)

        return supervisor

    def prepare(self) -> None:
        """Create the manifest, worktrees, prompts, and session records.

        Git setup failures are stored on the affected item's record so other
        items can still run.

        Raises:
            OSError: Run artifacts cannot be created or persisted.

        """

        with run_owner(self.paths, "prepare"):
            self._prepare()

    def _prepare(self) -> None:
        git.ignore_runtime_state(self.repo_root)
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
                premium_budget=self.premium_budget,
            )
        )

        for item in self.resolved:
            worktree = self.paths.worktree(item.key)
            record = SessionRecord(
                key=item.key,
                name=item.name,
                slug=item.slug,
                branch=item.branch,
                base=item.base,
                model=item.model,
                session_id=str(uuid4()),
                worktree=str(worktree),
                permission_flags=item.permissions.to_flags(),
                env=dict(item.env),
                timeout_seconds=item.timeout_seconds,
                pr_title=item.pr_title,
                pr_body=item.pr_body,
                base_from=item.base_from,
                source=item.source,
            )

            self.records[item.key] = record
            self.paths.ensure_session_dirs(item.key)
            self.paths.prompt_file(item.key).write_text(item.effective_prompt(), encoding="utf-8")
            self.paths.write_record(record)

        for item in self.resolved:
            if item.base_from is not None:
                continue
            record = self.records[item.key]
            try:
                self._prepare_worktree(item, record)
            except git.GitError as exc:
                record.begin_attempt("initial")
                record.phase = "setup"
                record.status = Status.FAILED
                record.error = str(exc)
                record.finish_attempt()
                logger.warning(f"`{item.key}` worktree setup failed: {str(exc).removesuffix('.')}.")

            self.paths.write_record(record)

    def _prepare_worktree(self, item: ResolvedItem, record: SessionRecord) -> None:
        worktree = Path(record.worktree)
        if worktree.exists():
            if git.repo_root(worktree).resolve() != worktree.resolve():
                raise git.GitError(f"`{worktree}` is not the recorded item worktree.")
            branch = git.run_git(["symbolic-ref", "--short", "HEAD"], worktree).stdout.strip()
            if branch != record.branch:
                raise git.GitError(f"`{worktree}` is on `{branch}`, not the recorded `{record.branch}`.")
        else:
            if record.base_sha and git.branch_exists(self.repo_root, record.branch):
                raise git.GitError(
                    f"`{worktree}` is missing but `{record.branch}` exists. Restore that worktree before retrying."
                )
            if item.base_from is not None:
                parent = self.records[item.base_from]
                if parent.status not in SUCCESS or not parent.candidate_sha:
                    raise git.GitError(f"`base_from={item.base_from}` has no successful candidate commit.")
                record.base_sha = parent.candidate_sha
                record.base = parent.base if parent.candidate_sha == parent.base_sha else parent.branch
                if (
                    self.options.open_pr
                    and record.base == parent.branch
                    and parent.delivery_sha != parent.candidate_sha
                ):
                    raise git.GitError(f"`base_from={item.base_from}` has not delivered its candidate branch.")
            elif not record.base_sha:
                _, record.base_sha = git.resolve_base(self.repo_root, item.remote, item.base)
            if git.branch_exists(self.repo_root, record.branch):
                record.branch = f"{item.branch}-{self.run_id[-6:]}"
            self.paths.write_record(record)
            git.add_worktree(self.repo_root, worktree, record.branch, record.base_sha)

        git.require_paths_exist(worktree, item.permissions.add_dir)
        strategy = self.options.deps_override or item.deps
        if strategy != Deps.install:
            git.provision_deps(self.repo_root, worktree, strategy)

    def prepare_retry(
        self,
        keys: list[str] | None = None,
        mode: Literal["retry", "resume", "fresh"] = "retry",
        premium_budget: int | None = None,
    ) -> list[str]:
        """Queue selected terminal items without discarding worktrees or history.

        Ordinary retry reuses successful agent work after a delivery/check failure.
        Otherwise it starts a new native conversation. Resume explicitly continues
        an observed native session; fresh always reruns setup and the original task
        in a new conversation. None of these operations resets Git changes.

        Args:
            keys: Explicit item keys, or failed/blocked/stopped/unstarted items when omitted.
            mode: Stage-aware retry, native resume, or fresh conversation.
            premium_budget: Optional replacement soft admission ceiling.

        Returns:
            Queued item keys in plan order.

        Raises:
            OwnershipError: Another run or session operation is active.
            ValueError: Selection, mode, state, or budget is invalid.
            OSError: State cannot be read or persisted.

        """

        if mode not in {"retry", "resume", "fresh"}:
            raise ValueError(f"`mode={mode}` is not a recovery mode.")
        if premium_budget is not None and premium_budget < 1:
            raise ValueError("`premium_budget` must be positive.")
        with run_owner(self.paths, "prepare-retry"):
            for record in self.records.values():
                self.paths.refresh_record(record)
            selected = (
                set(keys)
                if keys
                else {
                    key
                    for key, record in self.records.items()
                    if record.status in TERMINAL_FAILURE or record.status == Status.PENDING
                }
            )
            if not selected:
                raise ValueError("`retry` has no failed items. Select item keys explicitly to rerun completed work.")
            unknown = selected - self.records.keys()
            if unknown:
                raise ValueError(f"`retry` references unknown items {sorted(unknown)}.")
            for key in selected:
                record = self.records[key]
                if record.status not in TERMINAL and record.status != Status.PENDING:
                    raise ValueError(f"`session={key}` is not terminal. Stop or reconcile it before retrying.")
                if mode == "resume" and not record.native_started:
                    raise ValueError(f"`session={key}` has no observed native session to resume. Use retry or --fresh.")

            if premium_budget is not None:
                manifest = RunManifest.model_validate_json(self.paths.manifest.read_text(encoding="utf-8"))
                manifest.premium_budget = premium_budget
                self.paths.write_manifest(manifest)
                self.premium_budget = premium_budget
            self.paths.stop_file().unlink(missing_ok=True)
            self.paths.ready_file.unlink(missing_ok=True)
            for key in selected:
                record = self.records[key]
                record.recovery_mode = mode
                record.status = Status.PENDING
                self.paths.stop_file(key).unlink(missing_ok=True)
                self.paths.write_record(record)
            return [item.key for item in self.resolved if item.key in selected]

    async def run(self, headless: bool = False, *, startup_wait_seconds: float = 0.0) -> list[SessionRecord]:
        """Run items within the concurrency limit.

        Args:
            headless: Disable the live table.
            startup_wait_seconds: Bounded owner-handoff wait for a detached launch.

        Returns:
            Final session records.

        """

        with run_owner(self.paths, wait_seconds=startup_wait_seconds):
            if not self.records:
                self._prepare()
            for record in self.records.values():
                self.paths.refresh_record(record)
            self.paths.ready_file.touch()
            return await self._run(headless)

    async def _run(self, headless: bool) -> list[SessionRecord]:
        semaphore = asyncio.Semaphore(self.concurrency)
        done_events = {item.key: asyncio.Event() for item in self.resolved}
        self._started_at = time.monotonic()

        if headless:
            await self._run_all(semaphore, done_events)
        else:
            with Live(self._render(), console=self.console, refresh_per_second=8, transient=True) as live:
                self._live = live
                await self._run_all(semaphore, done_events)
                live.update(self._render())

        return list(self.records.values())

    async def _run_all(self, semaphore: asyncio.Semaphore, done_events: dict[str, asyncio.Event]) -> None:
        tasks = {item.key: asyncio.create_task(self._run_item(item, semaphore, done_events)) for item in self.resolved}

        async def watch_cancellation() -> None:
            while any(not task.done() for task in tasks.values()):
                stop_run = self.paths.stop_file().exists()
                for key, task in tasks.items():
                    if (stop_run or self.paths.stop_file(key).exists()) and not task.done() and not task.cancelling():
                        task.cancel()
                await asyncio.sleep(0.1)

        watcher = asyncio.create_task(watch_cancellation())
        try:
            await asyncio.gather(*tasks.values())
        finally:
            watcher.cancel()
            for task in tasks.values():
                if not task.done() and not task.cancelling():
                    task.cancel()
            await asyncio.gather(watcher, *tasks.values(), return_exceptions=True)

    async def _run_item(
        self,
        item: ResolvedItem,
        semaphore: asyncio.Semaphore,
        done_events: dict[str, asyncio.Event],
    ) -> None:
        record = self.records[item.key]
        if record.status in TERMINAL:
            done_events[item.key].set()
            return
        finalizing = False
        try:
            mode = record.recovery_mode
            if mode == "fresh" or (mode == "retry" and not record.agent_complete):
                record.session_id = str(uuid4())
                record.native_started = False
            if mode in {"resume", "fresh"}:
                record.agent_complete = False
                record.verification = None
            if mode == "fresh":
                record.setup_complete = False
            record.begin_attempt(mode or "initial")
            record.recovery_mode = None
            record.status = Status.PENDING
            record.phase = "pending"
            self.paths.write_record(record)
            dependencies = list(dict.fromkeys([*item.depends_on, *([item.base_from] if item.base_from else [])]))
            for dep in dependencies:
                await done_events[dep].wait()

            failed_dep = next(
                (dep for dep in dependencies if self.records[dep].status not in SUCCESS),
                None,
            )
            if failed_dep is not None:
                record.status = Status.BLOCKED
                record.error = f"dependency `{failed_dep}` did not succeed."
                self.paths.write_record(record)
                return

            async with semaphore:
                while self.paths.pause_file.exists():
                    await asyncio.sleep(0.1)
                if self.paths.stop_file().exists() or self.paths.stop_file(item.key).exists():
                    record.status = Status.KILLED
                    record.error = f"`session={item.key}` was stopped before execution."
                    return
                premium = sum(entry.premium_requests or 0 for entry in self.records.values())
                if not record.agent_complete and self.premium_budget is not None and premium >= self.premium_budget:
                    record.status = Status.BLOCKED
                    record.error = (
                        f"`premium_budget={self.premium_budget}` reached with {premium} reported requests. "
                        "Retry with an increased --budget to admit this item."
                    )
                    return
                self._prepare_worktree(item, record)
                if not record.setup_complete:
                    setup = list(item.setup)
                    if (self.options.deps_override or item.deps) == Deps.install and not (
                        Path(record.worktree) / "node_modules"
                    ).exists():
                        command = git.dependency_install_command(record.worktree)
                        if command is not None:
                            setup.insert(0, CommandSpec(name="dependencies", command=shlex.join(command)))
                    if not await run_steps(self.paths, record, setup, "setup", self._refresh):
                        return
                    record.setup_complete = True

                if record.agent_complete:
                    finalizing = True
                    await self._finalize(item, record)
                    return
                record.phase = "agent"
                record.status = Status.STARTING
                self.paths.write_record(record)
                self._refresh()

                argv = item.spawn_argv(
                    self.paths.worktree(item.key), record.session_id, self.paths.copilot_log_dir(item.key)
                )
                if mode == "resume":
                    argv = followup_argv(
                        record.session_id,
                        record.worktree,
                        record.model,
                        record.permission_flags,
                        "Continue the original task from its interrupted state. "
                        "Complete the remaining work and update the cpmux PR draft when ready.",
                    )
                runner = SessionRunner(item.key, argv, self.paths.transcript(item.key), env=item.env)
                self.runners[item.key] = runner
                state = await runner.run(
                    self._on_update,
                    on_spawn=lambda pid: self._on_spawn(record, pid),
                    timeout_seconds=item.timeout_seconds,
                    stop_requested=lambda: self.paths.stop_file().exists() or self.paths.stop_file(item.key).exists(),
                )

                record.pid = None
                record.pid_created_at = None
                record.exit_code = state.exit_code
                if state.premium_requests is not None:
                    record.record_usage(state.premium_requests)
                record.files_modified = state.files_modified or record.files_modified
                record.error = state.error

                if state.status == Status.DONE:
                    record.agent_complete = True
                    record.status = Status.FINALIZING
                    self.paths.write_record(record)
                    self._refresh()
                    finalizing = True
                    await self._finalize(item, record)
                else:
                    record.status = state.status

                self.paths.write_record(record)
                self._refresh()
        except asyncio.CancelledError:
            if record.status not in TERMINAL:
                if finalizing:
                    record.status = Status.FAILED
                    record.error = "run cancelled during finalization; inspect the worktree and remote."
                else:
                    record.status = Status.KILLED
            if self.paths.stop_file(item.key).exists() and not self.paths.stop_file().exists():
                return
            raise
        except (OSError, git.GitError, pr.PRError, OwnershipError) as exc:
            record.status = Status.FAILED
            record.error = str(exc)
            logger.error(f"`{item.key}` execution failed: {str(exc).removesuffix('.')}.")
        finally:
            if record.status not in TERMINAL:
                record.status = Status.FAILED
                record.error = record.error or f"`{item.key}` execution ended without a terminal outcome."
            record.finish_attempt()
            self.paths.write_record(record)
            done_events[item.key].set()
            self._refresh()

    def _on_spawn(self, record: SessionRecord, pid: int) -> None:
        record.pid = pid
        record.pid_created_at = process_created_at(pid)
        record.pid_is_group = True
        record.attempts[-1].agent_started = True
        self.paths.write_record(record)

    async def _finalize(self, item: ResolvedItem, record: SessionRecord) -> None:
        try:
            await finalize_item(
                self.paths, record, item, self.options.open_pr, self.options.strip_github_token, self._refresh
            )
        except (OSError, pr.PRError, git.GitError) as exc:
            record.status = Status.FAILED
            record.error = record.error or str(exc)
            logger.error(f"`{item.key}` finalization failed: {str(exc).removesuffix('.')}.")

    def _on_update(self, key: str, state: SessionState, event: dict[str, Any]) -> None:
        self.live_states[key] = state
        record = self.records[key]
        status = state.status if state.status not in TERMINAL else Status.RUNNING
        usage_changed = (
            state.premium_requests is not None and state.premium_requests != record.attempts[-1].premium_requests
        )
        changed = status != record.status or not record.native_started or usage_changed
        record.native_started = True
        if state.premium_requests is not None:
            record.record_usage(state.premium_requests)
        record.last_activity_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if changed or time.monotonic() - self._last_persist.get(key, 0.0) >= 1.0:
            record.status = status
            self.paths.write_record(record)
            self._last_persist[key] = time.monotonic()

        self._refresh()

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._render())

    def _render(self) -> Table:
        table = theme.table(title=self._title())
        table.add_column("item", style="bold", ratio=2, no_wrap=True, overflow="ellipsis")
        table.add_column("status", no_wrap=True)
        table.add_column("elapsed", no_wrap=True, justify="right")
        table.add_column("detail", ratio=3, overflow="ellipsis")
        table.add_column("branch / PR", ratio=2, no_wrap=True, overflow="ellipsis")

        for item in self.resolved:
            record = self.records[item.key]
            live = self.live_states.get(item.key)
            status = record.status

            detail = ""
            commands = record.attempts[-1].commands if record.attempts else []
            if commands and commands[-1].status == "running":
                detail = f"{record.phase}: {commands[-1].name}"
            elif live and status in ACTIVE:
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
        stopped = sum(status == Status.KILLED for status in statuses)
        failed = sum(status in TERMINAL_FAILURE for status in statuses) - stopped
        premium = sum(record.premium_requests or 0 for record in self.records.values())
        wall = theme.format_duration(time.monotonic() - self._started_at) if self._started_at else "0:00"

        title = f"cpmux · run {self.run_id} · {done}/{len(statuses)} done · {active} active · {failed} failed"
        if stopped:
            title += f" · {stopped} stopped"
        if premium:
            title += f" · {premium} premium"
        if self.paths.pause_file.exists():
            title += " · queue paused"

        return f"{title} · {wall}"
