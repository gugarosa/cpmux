# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from pathlib import Path


def resume_interactive_argv(session_id: str, worktree: str | Path) -> list[str]:
    """Build the argv that resumes an interactive session.

    Args:
        session_id: Copilot session id to resume.
        worktree: Working directory to run the session in.

    Returns:
        The argv list for an interactive resumed session.

    """
    return ["copilot", f"--resume={session_id}", "-C", str(worktree)]


def followup_argv(
    session_id: str,
    worktree: str | Path,
    model: str,
    permission_flags: list[str],
    message: str,
) -> list[str]:
    """Build the argv for a headless follow-up turn on an existing session.

    Args:
        session_id: Copilot session id to resume.
        worktree: Working directory to run the turn in.
        model: Model to drive the follow-up turn.
        permission_flags: Permission flags to pass through.
        message: Prompt text for the follow-up turn.

    Returns:
        The argv list for the follow-up turn.

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
