# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

"""Build ``copilot`` invocations for interacting with an existing session.

``enter`` hands the terminal to a native interactive session; ``send`` appends a
headless follow-up turn. Both resume the same ``--session-id``, so the session's
memory and transcript stay continuous.
"""

from __future__ import annotations

from pathlib import Path


def resume_interactive_argv(session_id: str, worktree: str | Path) -> list[str]:
    """Build the argv that drops the user into an interactive resumed session."""
    return ["copilot", f"--resume={session_id}", "-C", str(worktree)]


def followup_argv(
    session_id: str,
    worktree: str | Path,
    model: str,
    permission_flags: list[str],
    message: str,
) -> list[str]:
    """Build the argv for a headless follow-up turn on an existing session."""
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
