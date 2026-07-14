# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from pathlib import Path


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
