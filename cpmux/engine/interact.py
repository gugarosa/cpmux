# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from pathlib import Path

from cpmux.engine.session import SessionRunner
from cpmux.engine.store import RunPaths, SessionRecord
from cpmux.events import SessionState


def resume_interactive_argv(session_id: str, worktree: str | Path) -> list[str]:
    """Build arguments for an interactive resume.

    Args:
        session_id: Copilot session identifier.
        worktree: Session worktree.

    Returns:
        Copilot command arguments.

    """

    return ["copilot", f"--resume={session_id}", "-C", str(worktree)]


def followup_argv(
    session_id: str,
    worktree: str | Path,
    model: str,
    permission_flags: list[str],
    message: str,
) -> list[str]:
    """Build arguments for a headless follow-up turn.

    Args:
        session_id: Copilot session identifier.
        worktree: Session worktree.
        model: Copilot model.
        permission_flags: Permission arguments.
        message: Follow-up prompt.

    Returns:
        Copilot command arguments.

    """

    argv = [
        "copilot",
        "-C",
        str(worktree),
        "-p",
        message,
        f"--resume={session_id}",
        "--model",
        model,
        "--output-format",
        "json",
        *permission_flags,
    ]
    if "--no-ask-user" not in argv:
        argv.append("--no-ask-user")

    return argv


async def run_followup(paths: RunPaths, record: SessionRecord, message: str) -> SessionState:
    """Run a follow-up turn, update its record in place, and persist the outcome.

    Reported premium usage is accumulated; an absent modified-file list leaves the
    previous list intact. No automatic Git finalization or ownership arbitration
    is performed. Cancellation leaves the record unchanged after child cleanup.
    The in-memory update precedes persistence, so a write failure does not undo it.

    Args:
        paths: Storage paths for the existing run.
        record: Session to resume and update.
        message: Follow-up prompt.

    Returns:
        Terminal session state, including execution failures recorded in its error field.

    Raises:
        OSError: Transcript or record persistence fails.
        asyncio.CancelledError: The turn is cancelled after subprocess cleanup.

    """

    argv = followup_argv(record.session_id, record.worktree, record.model, record.permission_flags, message)
    state = await SessionRunner(record.key, argv, paths.transcript(record.key), env=record.env).run()

    record.status = state.status
    record.exit_code = state.exit_code
    record.error = state.error
    record.files_modified = state.files_modified or record.files_modified
    record.mark_ended()
    if state.premium_requests is not None:
        record.premium_requests = (record.premium_requests or 0) + state.premium_requests
    paths.write_record(record)

    return state
