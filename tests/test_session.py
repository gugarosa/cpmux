# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import sys

from cmux.events import Status
from cmux.session import SessionRunner


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


def test_run_failure_sets_failed_status_from_result(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    argv = _fake_argv([{"type": "result", "sessionId": "sid-2", "exitCode": 1}])
    runner = SessionRunner("k2", argv, transcript)

    state = asyncio.run(runner.run())

    assert state.status == Status.FAILED
    assert state.exit_code == 1


def test_run_failure_from_nonzero_exit(tmp_path):
    transcript = tmp_path / "transcript.jsonl"
    argv = [sys.executable, "-c", "import sys; sys.exit(3)"]
    runner = SessionRunner("k3", argv, transcript)

    state = asyncio.run(runner.run())

    assert state.status == Status.FAILED
    assert state.exit_code == 3


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
