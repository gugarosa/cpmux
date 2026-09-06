# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import shlex
import subprocess
import sys
import time

import psutil
import pytest

from cpmux.engine.daemon import reconcile
from cpmux.engine.ownership import BusyError, file_lease
from cpmux.engine.store import RunPaths, SessionRecord
from cpmux.events import Status
from cpmux.process import complete, inherit_lease, inherited_fds


def test_inherit_lease_restores_its_execution_context(tmp_path):
    assert inherited_fds() == ()
    with (tmp_path / "lease").open("wb") as handle:
        with inherit_lease(handle.fileno()):
            assert inherited_fds() == (handle.fileno(),)
        assert inherited_fds() == ()


@pytest.mark.parametrize("fails", [False, True])
def test_complete_waits_through_repeated_cancellation_and_preserves_failures(fails):
    async def scenario():
        future = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(complete(future))
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert not future.cancelled()
        if fails:
            future.set_exception(ValueError("operation failed"))
        else:
            future.set_result(42)
        with pytest.raises(ValueError if fails else asyncio.CancelledError):
            await task

    asyncio.run(scenario())


@pytest.mark.parametrize("adapter", ["git", "pr"])
def test_run_sync_git_subprocess_retains_lease_after_controller_death(git_repo, adapter):
    paths = RunPaths(git_repo, "crash-run")
    record = SessionRecord(
        key="a",
        name="a",
        slug="a",
        branch="feature",
        base="main",
        model="m",
        session_id="sid",
        worktree=str(git_repo),
    )
    record.begin_attempt("initial")
    record.status = Status.FINALIZING
    record.phase = "delivery"
    paths.write_record(record)
    marker = git_repo / "child-started"
    release = git_repo / "release-child"
    child_code = (
        f"from pathlib import Path; import os, time\nPath({str(marker)!r}).write_text(str(os.getpid()))\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(0.02)\n"
    )
    alias = f"alias.cpmux-wait=!{shlex.quote(sys.executable)} -c {shlex.quote(child_code)}"
    args = ["-c", alias, "cpmux-wait"]
    invocation = (
        f"run_sync(git.run_git, {args!r}, {str(git_repo)!r})"
        if adapter == "git"
        else f"run_sync(pr._run, {['git', *args]!r}, {str(git_repo)!r}, pr.gh_env(False))"
    )
    controller_code = (
        "import asyncio\n"
        "from cpmux.engine.store import RunPaths\n"
        "from cpmux.engine.ownership import run_owner\n"
        "from cpmux.process import run_sync\n"
        "from cpmux.vcs import git, pr\n"
        "async def main():\n"
        f"    with run_owner(RunPaths({str(git_repo)!r}, 'crash-run')):\n"
        f"        await {invocation}\n"
        "asyncio.run(main())\n"
    )
    controller = subprocess.Popen(
        [sys.executable, "-c", controller_code], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    descendants = []
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            assert controller.poll() is None
            time.sleep(0.02)
        assert marker.exists()
        descendants = psutil.Process(controller.pid).children(recursive=True)
        controller.kill()
        controller.wait(timeout=3)

        with pytest.raises(BusyError):
            with file_lease(paths.owner_lock):
                pytest.fail("Git lost ownership when its controller died")
        reconcile(paths, [record])
        assert record.status == Status.FINALIZING
        assert paths.read_record("a").status == Status.FINALIZING

        release.touch()
        with file_lease(paths.owner_lock, wait_seconds=3):
            pass
        reconcile(paths, [record])
        assert record.status == Status.FAILED
    finally:
        release.touch()
        if controller.poll() is None:
            controller.kill()
        controller.wait()
        _, alive = psutil.wait_procs(descendants, timeout=3)
        for process in alive:
            try:
                process.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(alive, timeout=3)
