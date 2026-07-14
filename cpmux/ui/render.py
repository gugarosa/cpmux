# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from rich.text import Text

from cpmux import theme
from cpmux.events import SUCCESS, TERMINAL_FAILURE, Status, event_data


def deps_cell(deps: list[str], status_by_key: dict[str, Status]) -> Text:
    """Render dependencies by status.

    Args:
        deps: Dependency keys.
        status_by_key: Statuses keyed by dependency.

    Returns:
        Styled dependency text.

    """

    if not deps:
        return Text("-", style="dim")

    cell = Text()
    for index, dep in enumerate(deps):
        if index:
            cell.append(", ", style="dim")
        status = status_by_key.get(dep)
        if status is None:
            style = "magenta"
        elif status in SUCCESS:
            style = "green"
        elif status in TERMINAL_FAILURE:
            style = "red"
        else:
            style = "yellow"
        cell.append(dep, style=style)

    return cell


def event_text(event: dict) -> Text | None:
    """Render a transcript event.

    Args:
        event: Transcript event.

    Returns:
        Styled event text or None for empty or unsupported events.

    """

    event_type = event.get("type", "")
    data = event_data(event)

    if event_type == "user.message":
        return Text.assemble((f"{theme.icon('🧑', '>')} user ", "bold blue"), str(data.get("content", "")).strip())
    if event_type == "assistant.message":
        text = str(data.get("content", "")).strip()
        return Text.assemble((f"{theme.icon('🤖', '*')} assistant ", "bold green"), text) if text else None
    if event_type == "tool.execution_start":
        glyph = theme.icon("🔧", "#")
        return Text.assemble((f"{glyph} tool ", "cyan"), str(data.get("toolName") or data.get("name") or ""))
    if event_type == "session.error":
        message = str(data.get("message") or data.get("error") or "session error").strip()
        return Text.assemble((f"{theme.icon('✖', 'x')} error ", "bold red"), message)
    if event_type == "result":
        exit_code = event.get("exitCode")
        return Text(f"— result exit={exit_code}", style="dim" if exit_code == 0 else "red")

    return None
