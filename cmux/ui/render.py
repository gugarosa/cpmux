# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from rich.text import Text

from cmux.events import SUCCESS, TERMINAL_FAILURE, Status, event_data

STATUS_COLOR: dict[Status, str] = {
    Status.DONE: "green",
    Status.NO_CHANGES: "dim",
    Status.FAILED: "red",
    Status.KILLED: "red",
    Status.TIMED_OUT: "red",
}


def deps_cell(deps: list[str], status_by_key: dict[str, Status]) -> Text:
    """Render an item's dependencies, colored by whether each still blocks it.

    Args:
        deps: Keys this item depends on.
        status_by_key: Current status of every item in the run.

    Returns:
        Styled text: green when satisfied, red when failed, yellow when still
        pending, magenta when the dependency key is unknown.

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
    """Render a JSONL transcript event.

    Args:
        event: Decoded JSONL event.

    Returns:
        Styled display text, or None if empty.

    """

    event_type = event.get("type", "")
    data = event_data(event)

    if event_type == "user.message":
        return Text.assemble(("🧑 user ", "bold blue"), str(data.get("content", "")).strip())
    if event_type == "assistant.message":
        text = str(data.get("content", "")).strip()
        return Text.assemble(("🤖 assistant ", "bold green"), text) if text else None
    if event_type == "tool.execution_start":
        return Text.assemble(("🔧 tool ", "cyan"), str(data.get("toolName") or data.get("name") or ""))
    if event_type == "result":
        return Text(f"— result exit={event.get('exitCode')}", style="dim")

    return None
