# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import os
import sys
from dataclasses import dataclass

from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from cmux.events import Status

STYLE_SUCCESS = "green"
STYLE_DANGER = "red"
STYLE_WARNING = "yellow"
STYLE_INFO = "cyan"
STYLE_MUTED = "dim"
STYLE_ACCENT = "magenta"


@dataclass(frozen=True)
class StatusVisual:
    """Session status glyph, ASCII fallback, style, and label."""

    glyph: str
    ascii_glyph: str
    style: str
    label: str


STATUS_VISUAL: dict[Status, StatusVisual] = {
    Status.PENDING: StatusVisual("○", ".", STYLE_MUTED, "pending"),
    Status.STARTING: StatusVisual("◐", "*", STYLE_WARNING, "starting"),
    Status.RUNNING: StatusVisual("●", ">", STYLE_INFO, "running"),
    Status.TOOL: StatusVisual("⚙", "#", STYLE_INFO, "using tool"),
    Status.IDLE: StatusVisual("◑", "~", "blue", "idle"),
    Status.FINALIZING: StatusVisual("◆", "+", STYLE_INFO, "finalizing"),
    Status.OPENING_PR: StatusVisual("⇪", "^", STYLE_ACCENT, "opening PR"),
    Status.DONE: StatusVisual("✔", "v", STYLE_SUCCESS, "done"),
    Status.NO_CHANGES: StatusVisual("∅", "=", STYLE_MUTED, "no changes"),
    Status.FAILED: StatusVisual("✖", "x", STYLE_DANGER, "failed"),
    Status.TIMED_OUT: StatusVisual("⏱", "T", STYLE_DANGER, "timed out"),
    Status.KILLED: StatusVisual("■", "!", STYLE_DANGER, "stopped"),
}

_ICON_SUCCESS = ("✓", "+")
_ICON_WARNING = ("⚠", "!")
_ICON_ERROR = ("✖", "x")
_ICON_INFO = ("›", ">")


def _ascii_only() -> bool:
    if os.environ.get("CMUX_ASCII"):
        return True

    try:
        "✔".encode(getattr(sys.stdout, "encoding", None) or "utf-8")
        return False
    except (LookupError, UnicodeEncodeError):
        return True


def _icon(icon: tuple[str, str]) -> str:
    return icon[1] if _ascii_only() else icon[0]


def icon(unicode_glyph: str, ascii_glyph: str) -> str:
    """Return the terminal-compatible glyph."""

    return ascii_glyph if _ascii_only() else unicode_glyph


out = Console(highlight=False)
err = Console(stderr=True, highlight=False)


def status_text(status: Status) -> Text:
    """Render a styled status glyph and label."""

    visual = STATUS_VISUAL[status]
    glyph = visual.ascii_glyph if _ascii_only() else visual.glyph

    return Text(f"{glyph} {visual.label}", style=visual.style)


def format_duration(seconds: float) -> str:
    """Format a duration as `M:SS`, or `H:MM:SS` past an hour."""

    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)

    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def table(title: str | None = None) -> Table:
    """Build a table with shared cmux styling."""

    return Table(
        title=title,
        box=box.SIMPLE_HEAD,
        safe_box=True,
        header_style="bold",
        title_style="bold",
        padding=(0, 1),
        pad_edge=False,
        expand=True,
    )


def print_error(message: str, hint: str | None = None) -> None:
    """Print an error and optional hint to stderr."""

    err.print(Text.assemble((f"{_icon(_ICON_ERROR)} ", STYLE_DANGER), message))
    if hint:
        err.print(Text(f"  {hint}", style=STYLE_MUTED))


def print_warning(message: str) -> None:
    """Print a warning to stderr."""

    err.print(Text.assemble((f"{_icon(_ICON_WARNING)} ", STYLE_WARNING), message))


def print_success(message: str) -> None:
    """Print a success message to stdout."""

    out.print(Text.assemble((f"{_icon(_ICON_SUCCESS)} ", STYLE_SUCCESS), message))


def print_hint(message: str) -> None:
    """Print a dim next-step hint to stdout."""

    out.print(Text(f"{_icon(_ICON_INFO)} {message}", style=STYLE_MUTED))
