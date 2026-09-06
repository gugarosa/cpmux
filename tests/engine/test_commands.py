# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import os
import shlex
import signal
import sys
import time
from contextlib import suppress

import psutil
import pytest

from cpmux.config import CommandSpec
from cpmux.engine.commands import run_command


def _python_command(source):
    return f"exec {shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _process_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _process_active(pid):
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def _kill_process_group(pid):
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def test_run_command_repeated_cancellation_still_reaps_and_records_cleanup(tmp_path):
    log = tmp_path / "repeated-cancellation.log"
    spec = CommandSpec(
        command=_python_command(
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
        )
    )
    processes = []
    updates = []

    async def scenario():
        task = asyncio.create_task(
            run_command(
                spec,
                tmp_path,
                log,
                "check",
                on_spawn=lambda pid: processes.append(psutil.Process(pid)),
                on_update=lambda result: updates.append(result.model_copy()),
            )
        )
        try:
            async with asyncio.timeout(3):
                while not log.exists() or "ready" not in log.read_text():
                    await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
            assert updates[-1].status == "cancelled"
            assert updates[-1].ended_at is not None
            assert updates[-1].exit_code == -signal.SIGKILL
            assert not processes[0].is_running()
        finally:
            for process in processes:
                with suppress(psutil.NoSuchProcess):
                    process.kill()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_run_command_succeeds_logs_and_merges_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CPMUX_PARENT_VALUE", "parent")
    log_path = tmp_path / "logs" / "success.log"
    update_objects = []
    update_statuses = []
    pids = []
    command = _python_command(
        "import os; print(os.environ['CPMUX_PARENT_VALUE']); print(os.environ['CPMUX_ITEM_VALUE'])"
    )

    def on_update(result):
        update_objects.append(result)
        update_statuses.append(result.status)

    result = asyncio.run(
        run_command(
            CommandSpec(command=command),
            tmp_path,
            log_path,
            "setup",
            env={"CPMUX_ITEM_VALUE": "item"},
            on_spawn=pids.append,
            on_update=on_update,
        )
    )

    assert result.name == "setup"
    assert result.status == "passed"
    assert result.exit_code == 0
    assert result.error is None
    assert result.ended_at is not None
    assert result.duration_seconds >= 0
    assert log_path.read_text().splitlines() == ["parent", "item"]
    assert len(pids) == 1
    assert update_statuses == ["running", "passed"]
    assert update_objects[0] is result
    assert update_objects[1] is result


def test_run_command_returns_nonzero_failure_with_bounded_tail(tmp_path):
    log_path = tmp_path / "failure.log"
    command = _python_command(
        "import sys; sys.stderr.write('x' * 20000); sys.stderr.write('\\nTAIL-MARKER\\n'); raise SystemExit(7)"
    )

    result = asyncio.run(run_command(CommandSpec(command=command, name="lint"), tmp_path, log_path, "check"))

    assert result.name == "lint"
    assert result.status == "failed"
    assert result.exit_code == 7
    assert "exited with status 7" in result.error
    assert "TAIL-MARKER" in result.error
    assert len(result.error.encode()) < 9000
    assert log_path.stat().st_size > 20000


def test_run_command_handles_high_volume_combined_output(tmp_path):
    log_path = tmp_path / "volume.log"
    command = _python_command(
        "import sys; "
        "sys.stdout.buffer.write(b'o' * 1000000); sys.stdout.buffer.flush(); "
        "sys.stderr.buffer.write(b'e' * 1000000); sys.stderr.buffer.flush()"
    )

    result = asyncio.run(run_command(CommandSpec(command=command), tmp_path, log_path, "check"))

    assert result.status == "passed"
    assert log_path.stat().st_size == 2000000


def test_run_command_times_out_and_kills_sigterm_ignoring_process(tmp_path):
    log_path = tmp_path / "timeout.log"
    command = _python_command(
        "import os, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "print(os.getpid(), flush=True); "
        "time.sleep(60)"
    )
    pid = None

    try:
        result = asyncio.run(
            run_command(CommandSpec(command=command, timeout_seconds=0.5), tmp_path, log_path, "check")
        )
        pid = int(log_path.read_text().strip())

        assert result.status == "timed_out"
        assert result.exit_code == -signal.SIGKILL
        assert "timed out after 0.5 seconds" in result.error
        assert not _process_exists(pid)
    finally:
        if pid is not None:
            _kill_process_group(pid)


def test_run_command_honors_explicit_stop_and_reaps_process(tmp_path):
    log_path = tmp_path / "cancelled.log"
    command = _python_command("import os, time; print(os.getpid(), flush=True); time.sleep(60)")
    started = time.monotonic()
    pid = None

    try:
        result = asyncio.run(
            run_command(
                CommandSpec(command=command),
                tmp_path,
                log_path,
                "setup",
                stop_requested=lambda: time.monotonic() - started >= 0.2,
            )
        )
        pid = int(log_path.read_text().strip())

        assert result.status == "cancelled"
        assert result.exit_code == -signal.SIGTERM
        assert "`command` was cancelled." in result.error
        assert not _process_exists(pid)
    finally:
        if pid is not None:
            _kill_process_group(pid)


def test_run_command_task_cancellation_cleans_up_and_reraises(tmp_path):
    log_path = tmp_path / "task-cancelled.log"
    command = _python_command("import time; time.sleep(60)")
    pids = []
    updates = []

    async def scenario():
        spawned = asyncio.Event()

        def on_spawn(pid):
            pids.append(pid)
            spawned.set()

        task = asyncio.create_task(
            run_command(
                CommandSpec(command=command),
                tmp_path,
                log_path,
                "setup",
                on_spawn=on_spawn,
                on_update=lambda result: updates.append(result.model_copy()),
            )
        )
        try:
            await asyncio.wait_for(spawned.wait(), timeout=2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=3)

    try:
        asyncio.run(scenario())

        assert len(pids) == 1
        assert not _process_exists(pids[0])
        assert [result.status for result in updates] == ["running", "cancelled"]
        assert updates[-1].ended_at is not None
        assert updates[-1].exit_code is not None
    finally:
        for pid in pids:
            _kill_process_group(pid)


def test_run_command_rejects_and_stops_background_children(tmp_path):
    child_pid_file = tmp_path / "child.pid"
    child_ready_file = tmp_path / "child.ready"
    child_source = (
        "import signal, time; "
        "from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
        f"Path({str(child_ready_file)!r}).write_text('ready'); "
        "time.sleep(60)"
    )
    command = (
        f"{shlex.quote(sys.executable)} -c {shlex.quote(child_source)} & child=$!; "
        f"while [ ! -f {shlex.quote(str(child_ready_file))} ]; do sleep 0.01; done; "
        f"echo $child > {shlex.quote(str(child_pid_file))}"
    )
    pids = []
    try:
        result = asyncio.run(
            run_command(
                CommandSpec(command=command, timeout_seconds=5),
                tmp_path,
                tmp_path / "background.log",
                "setup",
                on_spawn=pids.append,
            )
        )
        child_pid = int(child_pid_file.read_text())

        assert result.status == "failed"
        assert "background children" in result.error
        assert not _process_active(child_pid)
    finally:
        for pid in pids:
            _kill_process_group(pid)


def test_run_command_callback_failure_propagates_after_cleanup(tmp_path):
    log_path = tmp_path / "callback.log"
    command = _python_command("import time; time.sleep(60)")
    pids = []

    def fail_update(result):
        raise RuntimeError("callback failed")

    try:
        with pytest.raises(RuntimeError, match="callback failed"):
            asyncio.run(
                run_command(
                    CommandSpec(command=command),
                    tmp_path,
                    log_path,
                    "setup",
                    on_spawn=pids.append,
                    on_update=fail_update,
                )
            )

        assert len(pids) == 1
        assert not _process_exists(pids[0])
    finally:
        for pid in pids:
            _kill_process_group(pid)


def test_run_command_log_open_failure_propagates(tmp_path):
    with pytest.raises(IsADirectoryError):
        asyncio.run(run_command(CommandSpec(command="true"), tmp_path, tmp_path, "setup"))


def test_run_command_returns_explicit_startup_failure(tmp_path):
    updates = []
    log_path = tmp_path / "startup.log"

    result = asyncio.run(
        run_command(
            CommandSpec(command="true"),
            tmp_path / "missing-worktree",
            log_path,
            "setup",
            on_update=updates.append,
        )
    )

    assert result.status == "failed"
    assert result.exit_code is None
    assert "`command` failed to start: FileNotFoundError:" in result.error
    assert log_path.read_text().strip() == result.error
    assert updates == [result]
