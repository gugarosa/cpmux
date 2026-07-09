# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

"""Declarative config model for cmux.

A cmux run takes one input: a YAML file with a shared ``system`` prompt and a
list of ``items`` (each a plain string or a mapping of overrides). This module
defines the validated schema, string-to-item normalisation, ``${ENV}``
interpolation, the ``item > defaults > built-in`` precedence merge, and the
mapping from permission presets to concrete ``copilot`` flags.
"""

from __future__ import annotations

import os
import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    computed_field,
    field_validator,
    model_validator,
)

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class Effort(StrEnum):
    """Reasoning effort passed to ``copilot --effort``."""

    none = "none"
    minimal = "minimal"
    low = "low"
    medium = "medium"
    high = "high"
    xhigh = "xhigh"
    max = "max"


class Preset(StrEnum):
    """Friendly permission preset (``yolo`` is an alias of ``full``)."""

    readonly = "readonly"
    edit = "edit"
    full = "full"
    yolo = "yolo"


class Deps(StrEnum):
    """Strategy for seeding a fresh worktree's installed dependencies."""

    symlink = "symlink"
    copy = "copy"
    install = "install"
    skip = "skip"


def interpolate_env(value: str) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` references in a string."""

    def repl(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ValueError(
            f"`{name}` environment variable is not set, " f"provide a fallback with `${{{name}:-default}}`."
        )

    return _ENV_RE.sub(repl, value)


def _walk_interpolate(obj: Any) -> Any:
    if isinstance(obj, str):
        return interpolate_env(obj)
    if isinstance(obj, list):
        return [_walk_interpolate(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _walk_interpolate(v) for k, v in obj.items()}

    return obj


def slugify(text: str) -> str:
    """Convert free text into a branch- and worktree-safe slug."""
    text = text.strip().lower().splitlines()[0] if text.strip() else "task"
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")

    return (text or "task")[:50]


class Permissions(BaseModel):
    """A permission preset plus escape-hatch allow/deny lists and network knobs."""

    model_config = ConfigDict(extra="forbid")

    preset: Preset = Preset.edit
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    add_dir: list[str] = Field(default_factory=list)
    allow_url: list[str] = Field(default_factory=list)
    autonomous: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_preset(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"preset": data}
        return data

    @property
    def is_autonomous(self) -> bool:
        """Whether the agent runs without pausing to ask the user questions."""
        if self.autonomous is not None:
            return self.autonomous
        return self.preset in (Preset.full, Preset.yolo)

    def to_flags(self) -> list[str]:
        """Expand the preset, then append explicit allow/deny/network flags."""
        flags: list[str] = []
        if self.preset in (Preset.full, Preset.yolo):
            flags.append("--allow-all-tools")
        elif self.preset == Preset.edit:
            flags += ["--allow-tool=write", "--allow-tool=shell", "--deny-tool=shell(git push)"]
        elif self.preset == Preset.readonly:
            flags += ["--deny-tool=write", "--deny-tool=shell"]

        for spec in self.allow:
            flags.append(f"--allow-tool={spec}")
        for spec in self.deny:
            flags.append(f"--deny-tool={spec}")
        for directory in self.add_dir:
            flags += ["--add-dir", directory]
        for url in self.allow_url:
            flags += ["--allow-url", url]
        if self.is_autonomous:
            flags.append("--no-ask-user")

        return flags


class PRSettings(BaseModel):
    """Pull-request defaults applied to every item unless overridden."""

    model_config = ConfigDict(extra="forbid")

    draft: bool = True
    labels: list[str] = Field(default_factory=list)
    title_template: str = "{name}"
    body_template: str = "Automated by cmux.\n\n{prompt}"


class Defaults(BaseModel):
    """Run-wide defaults that each item inherits unless it overrides them."""

    model_config = ConfigDict(extra="forbid")

    model: str = "gpt-5.5"
    effort: Effort = Effort.medium
    permissions: Permissions = Field(default_factory=Permissions)
    base: str = "main"
    branch_template: str = "cmux/{slug}"
    pr: PRSettings = Field(default_factory=PRSettings)
    concurrency: int = Field(default=4, ge=1, le=64)
    deps: Deps = Deps.symlink
    remote: str = "origin"


class Item(BaseModel):
    """One task, expressed as a bare prompt string or a mapping of overrides."""

    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    name: str | None = None
    id: str | None = None
    model: str | None = None
    effort: Effort | None = None
    permissions: Permissions | None = None
    branch: str | None = None
    base: str | None = None
    labels: list[str] = Field(default_factory=list)
    draft: bool | None = None
    paths: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    include_system: bool = True

    @model_validator(mode="before")
    @classmethod
    def _string_shorthand(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"prompt": data}
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def slug(self) -> str:
        return slugify(self.name or self.id or self.prompt)

    @property
    def key(self) -> str:
        return self.id or self.slug


class Plan(BaseModel):
    """A parsed cmux run: a shared system prompt, defaults, and a list of items."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    system: str = ""
    defaults: Defaults = Field(default_factory=Defaults)
    items: Annotated[
        list[Annotated[Item | str, Field(union_mode="left_to_right")]],
        Field(min_length=1),
    ]

    @model_validator(mode="before")
    @classmethod
    def _interpolate(cls, data: Any) -> Any:
        return _walk_interpolate(data)

    @field_validator("items")
    @classmethod
    def _unique_and_wired(cls, items: list[Item]) -> list[Item]:
        keys = [it.key for it in items]
        dupes = {k for k in keys if keys.count(k) > 1}
        if dupes:
            raise ValueError(
                f"`items` contain duplicate id/slug {sorted(dupes)}, "
                f"give each conflicting item a distinct `name` or `id`."
            )

        known = set(keys)
        for it in items:
            missing = [d for d in it.depends_on if d not in known]
            if missing:
                raise ValueError(
                    f"`depends_on` of item '{it.key}' references unknown id(s) {missing}, "
                    f"known ids are {sorted(known)}."
                )

        return items

    def resolve(self) -> list[ResolvedItem]:
        """Apply precedence and compute the ready-to-spawn spec for every item."""
        defaults = self.defaults
        resolved: list[ResolvedItem] = []
        for it in self.items:
            permissions = it.permissions or defaults.permissions
            if it.paths:
                permissions = permissions.model_copy(update={"add_dir": [*permissions.add_dir, *it.paths]})
            branch = it.branch or defaults.branch_template.format(slug=it.slug, id=it.key)
            prompt = it.prompt
            if it.include_system and self.system.strip():
                prompt = f"{self.system.strip()}\n\n---\n\n{it.prompt.strip()}"
            pr = defaults.pr
            resolved.append(
                ResolvedItem(
                    key=it.key,
                    name=it.name or it.slug,
                    slug=it.slug,
                    prompt=prompt,
                    model=it.model or defaults.model,
                    effort=it.effort or defaults.effort,
                    permissions=permissions,
                    branch=branch,
                    base=it.base or defaults.base,
                    labels=list(dict.fromkeys([*pr.labels, *it.labels])),
                    draft=pr.draft if it.draft is None else it.draft,
                    depends_on=list(it.depends_on),
                    env=dict(it.env),
                    deps=defaults.deps,
                    remote=defaults.remote,
                    pr_title=pr.title_template.format(name=it.name or it.slug, slug=it.slug),
                    pr_body=pr.body_template.format(name=it.name or it.slug, slug=it.slug, prompt=it.prompt.strip()),
                )
            )

        return resolved


class ResolvedItem(BaseModel):
    """A fully merged, ready-to-spawn session spec (item > defaults > built-in)."""

    key: str
    name: str
    slug: str
    prompt: str
    model: str
    effort: Effort
    permissions: Permissions
    branch: str
    base: str
    labels: list[str]
    draft: bool
    depends_on: list[str]
    env: dict[str, str]
    deps: Deps
    remote: str
    pr_title: str
    pr_body: str

    def spawn_argv(self, worktree: str | Path, session_id: str, log_dir: str | Path) -> list[str]:
        """Build the exact headless ``copilot`` invocation for this session."""
        argv = [
            "copilot",
            "-C",
            str(worktree),
            "-p",
            self.prompt,
            "--model",
            self.model,
            "--effort",
            str(self.effort),
            "--output-format",
            "json",
            "--session-id",
            session_id,
            "--name",
            self.name,
            "--log-dir",
            str(log_dir),
        ]
        argv += self.permissions.to_flags()
        if "--no-ask-user" not in argv:
            argv.append("--no-ask-user")

        return argv


class ConfigError(Exception):
    """Raised when a cmux YAML file is missing or invalid."""


def load_plan(path: str | Path) -> Plan:
    """Load and validate a cmux YAML file into a :class:`Plan`."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"`{p}` config file does not exist.")

    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"`{p}` is not valid YAML: {exc}.") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"`{p}` top-level YAML must be a mapping, but got {type(raw).__name__}.")

    try:
        return Plan.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"`{p}` is not a valid cmux config:\n{exc}") from exc
