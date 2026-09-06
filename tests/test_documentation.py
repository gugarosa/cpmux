# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import re
from pathlib import Path

from cpmux.config import load_plan, parse_plan
from cpmux.ui.cli import app

ROOT = Path(__file__).resolve().parents[1]


def test_readme_documents_every_public_command():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for command in app.registered_commands:
        if not command.hidden:
            name = command.name or command.callback.__name__
            assert f"cpmux {name}" in readme
            assert command.callback.__doc__.strip()


def test_readme_yaml_plans_resolve_with_the_current_schema():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    examples = re.findall(r"```yaml\n(.*?)```", readme, flags=re.DOTALL)
    assert examples
    for example in examples:
        assert parse_plan(example, source="README example").resolve()


def test_shipped_example_plans_resolve_with_the_current_schema():
    examples = sorted((ROOT / "examples").glob("*.yaml"))
    assert examples
    for example in examples:
        assert load_plan(example).resolve()
