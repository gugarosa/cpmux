# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import asyncio
import sys

import pytest

from cpmux.engine import interact
from cpmux.engine.interact import followup_argv, resume_interactive_argv, run_followup
from cpmux.engine.store import RunPaths, SessionRecord
from cpmux.events import SessionState, Status


def test_resume_interactive_argv_targets_session_and_worktree():
    assert resume_interactive_argv("sid", "/wt") == ["copilot", "--resume=sid", "-C", "/wt"]


def test_followup_argv_carries_message_and_stays_non_interactive():
    argv = followup_argv("sid", "/wt", "gpt-5.5", ["--allow-tool=write"], "do it")

    assert argv[0] == "copilot"
    assert "--resume=sid" in argv
    assert argv[argv.index("-p") + 1] == "do it"
    assert argv[argv.index("--model") + 1] == "gpt-5.5"
    assert argv[argv.index("-C") + 1] == "/wt"
    assert "--allow-tool=write" in argv
    assert "--output-format" in argv
    assert "--no-ask-user" in argv


def test_followup_argv_keeps_single_no_ask_user():
    argv = followup_argv("s", "/w", "m", ["--no-ask-user"], "x")
    assert argv.count("--no-ask-user") == 1


def _session(tmp_path):
    paths = RunPaths(tmp_path, "run1")
    record = SessionRecord(
        key="item",
        name="Item",
        slug="item",
        branch="cpmux/item",
        base="main",
        model="model",
        session_id="sid",
        worktree=str(tmp_path),
        permission_flags=["--deny-tool=shell(git push)"],
        env={"PORT": "3100"},
        status=Status.FAILED,
        error="earlier failure",
        agent_complete=True,
        premium_requests=3,
        files_modified=["old.py"],
        pr_url="https://example.test/pull/1",
        started_at="2026-01-01T00:00:00+00:00",
        ended_at="2026-01-01T00:01:00+00:00",
    )
    paths.write_record(record)
    return paths, record


def test_run_followup_forwards_session_context_and_persists_success(tmp_path, monkeypatch):
    paths, record = _session(tmp_path)
    original_start = record.started_at
    original_end = record.ended_at
    paths.transcript("item").write_text('{"type":"assistant.message","data":{"content":"earlier"}}\n')
    calls = []
    script = (
        "import json, os\n"
        "print(json.dumps({'type': 'assistant.message', 'data': {'content': os.environ['PORT']}}))\n"
        "print(json.dumps({'type': 'result', 'exitCode': 0, 'usage': "
        "{'premiumRequests': 2, 'codeChanges': {'filesModified': ['new.py']}}}))"
    )

    def argv(*args):
        calls.append(args)
        return [sys.executable, "-c", script]

    monkeypatch.setattr(interact, "followup_argv", argv)

    state = asyncio.run(run_followup(paths, record, "continue"))

    assert calls == [("sid", str(tmp_path), "model", ["--deny-tool=shell(git push)"], "continue")]
    assert state.last_text == "3100"
    assert record.status == Status.DONE
    assert record.exit_code == 0
    assert record.error is None
    assert record.agent_complete is True
    assert record.premium_requests == 5
    assert record.attempts[-1].premium_requests == 2
    assert record.files_modified == ["new.py"]
    assert record.started_at == original_start
    assert record.ended_at != original_end
    assert record.pr_url == "https://example.test/pull/1"
    assert paths.read_record("item") == record
    assert paths.transcript("item").read_text().splitlines()[0] == (
        '{"type":"assistant.message","data":{"content":"earlier"}}'
    )


def test_run_followup_preserves_unreported_history_on_failure(tmp_path, monkeypatch):
    paths, record = _session(tmp_path)
    script = "import json\nprint(json.dumps({'type': 'result', 'exitCode': 1}))"
    monkeypatch.setattr(interact, "followup_argv", lambda *args: [sys.executable, "-c", script])

    state = asyncio.run(run_followup(paths, record, "retry"))

    assert state.status == Status.FAILED
    assert record.exit_code == 1
    assert record.error == "exit code 1."
    assert record.agent_complete is False
    assert record.premium_requests == 3
    assert record.files_modified == ["old.py"]
    assert record.pr_url == "https://example.test/pull/1"
    assert paths.read_record("item") == record


def test_run_followup_refuses_execution_when_start_cannot_be_persisted(tmp_path, monkeypatch):
    paths, record = _session(tmp_path)
    stored = paths.read_record("item")
    calls = []

    async def success(self, *args, **kwargs):
        calls.append(self)
        return SessionState(status=Status.DONE, exit_code=0)

    def fail_write(record):
        raise OSError("disk full")

    monkeypatch.setattr(interact.SessionRunner, "run", success)
    monkeypatch.setattr(paths, "write_record", fail_write)

    with pytest.raises(OSError, match="disk full"):
        asyncio.run(run_followup(paths, record, "continue"))

    assert not calls
    assert record.status == Status.FAILED
    assert paths.read_record("item") == stored


def test_run_followup_cancellation_persists_attempt_and_preserves_prior_history(tmp_path, monkeypatch):
    paths, record = _session(tmp_path)
    stored = paths.read_record("item")

    async def cancelled(self, *args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(interact.SessionRunner, "run", cancelled)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_followup(paths, record, "continue"))

    assert record.status == Status.KILLED
    assert record.agent_complete is False
    assert record.attempts[-1].status == Status.KILLED
    assert record.attempts[-1].ended_at is not None
    assert record.premium_requests == stored.premium_requests
    assert record.files_modified == stored.files_modified
    assert record.started_at == stored.started_at
    assert record.pr_url == stored.pr_url
    assert paths.read_record("item") == record


def test_run_followup_rejects_blank_messages_without_starting_an_attempt(tmp_path):
    paths, record = _session(tmp_path)
    before = paths.record_file(record.key).read_bytes()

    with pytest.raises(ValueError, match="must not be blank"):
        asyncio.run(run_followup(paths, record, " \n "))

    assert paths.record_file(record.key).read_bytes() == before


def test_run_followup_cancellation_retains_observed_usage_without_double_counting(tmp_path, monkeypatch):
    paths, record = _session(tmp_path)

    async def cancelled(self, updated, spawned, **kwargs):
        state = SessionState(status=Status.RUNNING, premium_requests=1.5)
        updated("item", state, {})
        updated("item", state, {})
        raise asyncio.CancelledError

    monkeypatch.setattr(interact.SessionRunner, "run", cancelled)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_followup(paths, record, "continue"))

    assert record.premium_requests == 4.5
    assert record.attempts[-1].premium_requests == 1.5
    assert paths.read_record("item").premium_requests == 4.5


def test_run_interactive_sets_agent_completion_from_native_exit(tmp_path, monkeypatch):
    paths, record = _session(tmp_path)
    writes = []

    original_write = paths.write_record

    def write(current):
        writes.append(current.agent_complete)
        original_write(current)

    output = tmp_path / "interactive-env.txt"
    monkeypatch.setattr(
        interact,
        "resume_interactive_argv",
        lambda *args: [
            sys.executable,
            "-c",
            f"from pathlib import Path; import os; Path({str(output)!r}).write_text(os.environ['PORT'])",
        ],
    )
    monkeypatch.setattr(paths, "write_record", write)

    assert asyncio.run(interact.run_interactive(paths, record)) == 0
    assert output.read_text() == "3100"
    assert False in writes
    assert record.agent_complete is True
    assert record.attempts[-1].agent_started is True
    assert record.attempts[-1].premium_requests is None
    assert paths.read_record("item").agent_complete is True
    assert record.native_started is True


def test_run_interactive_keeps_the_event_loop_responsive_and_reaps_on_repeated_cancel(tmp_path, monkeypatch):
    paths, record = _session(tmp_path)
    marker = tmp_path / "interactive-ready"
    monkeypatch.setattr(
        interact,
        "resume_interactive_argv",
        lambda *args: [
            sys.executable,
            "-c",
            "import signal, time; from pathlib import Path; "
            f"signal.signal(signal.SIGTERM, signal.SIG_IGN); Path({str(marker)!r}).touch(); time.sleep(60)",
        ],
    )

    async def scenario():
        task = asyncio.create_task(interact.run_interactive(paths, record))
        async with asyncio.timeout(3):
            while not marker.exists():
                await asyncio.sleep(0.01)
        assert not task.done()
        task.cancel()
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert record.status == Status.KILLED
        assert record.pid is None
        assert paths.read_record("item") == record
        assert not paths.session_owner("item").exists()

    asyncio.run(scenario())
