# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from pathlib import Path


def resume_interactive_argv(session_id: str, worktree: str | Path) -> list[str]:
    """Build argv for an interactive resume.

    Args:
        session_id: Copilot session id.
        worktree: Session working directory.

    Returns:
        Interactive resume argv.

    """

    return ["copilot", f"--resume={session_id}", "-C", str(worktree)]


def followup_argv(
    session_id: str,
    worktree: str | Path,
    model: str,
    permission_flags: list[str],
    message: str,
) -> list[str]:
    """Build argv for a headless follow-up turn.

    Args:
        session_id: Copilot session id.
        worktree: Turn working directory.
        model: Follow-up model.
        permission_flags: Permission flags.
        message: Follow-up prompt.

    Returns:
        Follow-up argv.

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
