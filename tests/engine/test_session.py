# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import os
import signal
import sys

import pytest

from cpmux.engine.session import SessionRunner
from cpmux.events import Status


def _fake_argv(lines):
    script = "import json\n" + "\n".join(f"print(json.dumps({line!r}))" for line in lines)
    return [sys.executable, "-c", script]


def test_run_success_reduces_state_and_writes_transcript(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    argv = _fake_argv(
        [
            {"type": "assistant.message", "data": {"content": "hi there"}},
            {"type": "result", "sessionId": "sid-1", "exitCode": 0, "usage": {"premiumRequests": 2}},
        ]
    )
    runner = SessionRunner("k1", argv, transcript)

    state = asyncio.run(runner.run())

    assert state.status == Status.DONE
    assert state.last_text == "hi there"
    assert state.session_id == "sid-1"
    assert state.exit_code == 0
    assert state.premium_requests == 2
    assert transcript.exists()
    lines = [line for line in transcript.read_text().splitlines() if line.strip()]
    assert len(lines) == 2


@pytest.mark.parametrize(
    ("key", "argv", "expected_exit_code"),
    [
        pytest.param(
            "k2",
            _fake_argv([{"type": "result", "sessionId": "sid-2", "exitCode": 1}]),
            1,
            id="result-event-exit-code",
        ),
        pytest.param(
            "k3",
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            3,
            id="process-exit-code",
        ),
    ],
)
def test_run_failure_sets_failed_status(tmp_path, key, argv, expected_exit_code):
    transcript = tmp_path / "transcript.jsonl"
    runner = SessionRunner(key, argv, transcript)

    state = asyncio.run(runner.run())

    assert state.status == Status.FAILED
    assert state.exit_code == expected_exit_code


def test_run_invokes_on_update_callback(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    argv = _fake_argv(
        [
            {"type": "assistant.message", "data": {"content": "hello"}},
            {"type": "result", "sessionId": "sid-4", "exitCode": 0},
        ]
    )
    runner = SessionRunner("k4", argv, transcript)
    events = []

    asyncio.run(runner.run(on_update=lambda key, state, event: events.append(event)))

    assert len(events) >= 1
    assert any(event.get("type") == "assistant.message" for event in events)


def test_run_merges_env_overrides_and_keeps_parent_environment(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    script = (
        "import json, os\n"
        "print(json.dumps({'type': 'assistant.message', 'data': "
        "{'content': os.environ.get('PORT', '') + '|' + ('yes' if os.environ.get('PATH') else 'no')}}))\n"
        "print(json.dumps({'type': 'result', 'sessionId': 's', 'exitCode': 0}))"
    )
    runner = SessionRunner("k", [sys.executable, "-c", script], transcript, env={"PORT": "3005"})

    state = asyncio.run(runner.run())

    assert state.last_text == "3005|yes"


async def _reap(runner):
    if runner.proc is not None:
        if runner.proc.returncode is None:
            os.killpg(runner.proc.pid, signal.SIGKILL)
        await runner.proc.stdout.read()
        await runner.proc.wait()


def test_run_nonzero_process_exit_overrides_successful_result(tmp_path):
    argv = _fake_argv([{"type": "result", "exitCode": 0}])
    argv[-1] += "\nimport sys\nprint('shutdown failed', file=sys.stderr)\nsys.exit(7)"

    state = asyncio.run(SessionRunner("a", argv, tmp_path / "transcript.jsonl").run())

    assert state.status == Status.FAILED
    assert state.exit_code == 7
    assert state.error == "shutdown failed"


def test_run_preserves_session_error_without_result(tmp_path):
    argv = _fake_argv([{"type": "session.error", "data": {"message": "provider failed"}}])

    state = asyncio.run(SessionRunner("a", argv, tmp_path / "transcript.jsonl").run())

    assert state.status == Status.FAILED
    assert state.error == "provider failed"


def test_run_accepts_an_explicit_successful_result_after_recovery(tmp_path):
    argv = _fake_argv(
        [
            {"type": "session.error", "data": {"message": "transient failure"}},
            {"type": "result", "exitCode": 0},
        ]
    )

    state = asyncio.run(SessionRunner("a", argv, tmp_path / "transcript.jsonl").run())

    assert state.status == Status.DONE
    assert state.exit_code == 0


def test_run_reads_an_unterminated_final_event(tmp_path):
    script = "import json, sys\nsys.stdout.write(json.dumps({'type': 'result', 'exitCode': 1}))"
    transcript = tmp_path / "transcript.jsonl"

    state = asyncio.run(SessionRunner("a", [sys.executable, "-c", script], transcript).run())

    assert state.status == Status.FAILED
    assert state.exit_code == 1
    assert transcript.read_text() == '{"type": "result", "exitCode": 1}'


def test_run_retains_failure_diagnostics_after_large_stderr(tmp_path):
    script = "import sys\nsys.stderr.write('x' * (5 * 1024 * 1024) + '\\nshutdown failed\\n')\nsys.exit(1)"

    state = asyncio.run(SessionRunner("a", [sys.executable, "-c", script], tmp_path / "transcript.jsonl").run())

    assert state.status == Status.FAILED
    assert len(state.error) == 500
    assert state.error.endswith("\nshutdown failed")


@pytest.mark.parametrize("size", [1024 * 1024 + 128, 5 * 1024 * 1024])
def test_run_preserves_large_events_and_continues_reading(tmp_path, size):
    script = (
        "import json\n"
        f"print(json.dumps({{'type': 'assistant.message', 'data': {{'content': 'x' * {size}}}}}))\n"
        "print(json.dumps({'type': 'result', 'exitCode': 0}))"
    )
    transcript = tmp_path / "transcript.jsonl"
    runner = SessionRunner("a", [sys.executable, "-c", script], transcript)
    events = []

    async def scenario():
        try:
            return await asyncio.wait_for(
                runner.run(on_update=lambda key, state, event: events.append(event["type"])), 5
            )
        finally:
            await _reap(runner)

    state = asyncio.run(scenario())

    assert state.status == Status.DONE
    assert state.last_text == "x" * size
    assert events == ["assistant.message", "result"]
    assert len(transcript.read_text().splitlines()) == 2


@pytest.mark.parametrize("ignore_term", [False, True])
def test_run_cancellation_reaps_the_process(tmp_path, ignore_term):
    script = (
        "import json, signal, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + "print(json.dumps({'type': 'assistant.message', 'data': {'content': 'ready'}}), flush=True)\n"
        "time.sleep(60)"
    )
    runner = SessionRunner("a", [sys.executable, "-c", script], tmp_path / "transcript.jsonl")

    async def scenario():
        ready = asyncio.Event()
        task = asyncio.create_task(runner.run(on_update=lambda *args: ready.set()))
        try:
            await asyncio.wait_for(ready.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 8)

            assert runner.proc.returncode is not None
        finally:
            task.cancel()
            await _reap(runner)
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("callback", ["on_spawn", "on_update"])
def test_run_callback_errors_propagate_and_reap_the_process(tmp_path, callback):
    script = (
        "import json, time\n"
        "print(json.dumps({'type': 'assistant.message', 'data': {'content': 'ready'}}), flush=True)\n"
        "time.sleep(60)"
    )
    runner = SessionRunner("a", [sys.executable, "-c", script], tmp_path / "transcript.jsonl")

    def fail(*args):
        raise ValueError("callback failed")

    async def scenario():
        try:
            with pytest.raises(ValueError, match="callback failed"):
                await asyncio.wait_for(runner.run(**{callback: fail}), 5)

            assert runner.proc.returncode is not None
        finally:
            await _reap(runner)

    asyncio.run(scenario())


def test_run_missing_executable_returns_an_actionable_failure(tmp_path):
    executable = str(tmp_path / "missing-copilot")
    runner = SessionRunner("a", [executable], tmp_path / "transcript.jsonl")

    state = asyncio.run(runner.run())

    assert state.status == Status.FAILED
    assert executable in state.error
