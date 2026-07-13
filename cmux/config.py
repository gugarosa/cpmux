# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

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
    """Reasoning effort for `copilot --effort`."""

    none = "none"
    minimal = "minimal"
    low = "low"
    medium = "medium"
    high = "high"
    xhigh = "xhigh"
    max = "max"


class Preset(StrEnum):
    """Permission preset (`yolo` is an alias of `full`)."""

    readonly = "readonly"
    edit = "edit"
    full = "full"
    yolo = "yolo"


class Deps(StrEnum):
    """Dependency setup for a fresh worktree."""

    symlink = "symlink"
    copy = "copy"
    install = "install"
    skip = "skip"


def interpolate_env(value: str) -> str:
    """Expand `${VAR}` and `${VAR:-default}` references in a string.

    Raises:
        ValueError: Unset variable without a fallback.

    """

    def repl(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        if name in os.environ:
            return os.environ[name]
        if default is not None:
            return default
        raise ValueError(f"`{name}` is unset; use `${{{name}:-default}}`.")

    return _ENV_RE.sub(repl, value)


def _walk_interpolate(obj: Any) -> Any:
    if isinstance(obj, str):
        return interpolate_env(obj)
    if isinstance(obj, list):
        return [_walk_interpolate(value) for value in obj]
    if isinstance(obj, dict):
        return {key: _walk_interpolate(value) for key, value in obj.items()}

    return obj


def slugify(text: str) -> str:
    """Convert text to a branch- and worktree-safe slug."""

    text = text.strip().lower().splitlines()[0] if text.strip() else "task"
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")

    return (text or "task")[:50]


class Permissions(BaseModel):
    """Permission preset with allow, deny, and network options."""

    model_config = ConfigDict(extra="forbid")

    preset: Preset = Preset.edit
    allow: list[str] = Field(default_factory=list)
    deny: list[str] = Field(default_factory=list)
    add_dir: list[str] = Field(default_factory=list)
    allow_url: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_preset(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"preset": data}
        return data

    def to_flags(self) -> list[str]:
        """Return `copilot` permission flags."""

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

        return flags


class PRSettings(BaseModel):
    """Pull-request defaults applied unless overridden."""

    model_config = ConfigDict(extra="forbid")

    draft: bool = True
    labels: list[str] = Field(default_factory=list)
    title_template: str = "{name}"
    body_template: str = "Automated by cmux.\n\n{prompt}"


class Defaults(BaseModel):
    """Defaults inherited by all items."""

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
    port_base: int | None = Field(default=None, ge=1, le=65535)
    port_env: str = "PORT"

    @field_validator("port_env")
    @classmethod
    def _validate_env_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
            raise ValueError(f"`port_env` must be a valid environment variable name, got `{value}`.")

        return value


class Item(BaseModel):
    """Task prompt with optional per-item overrides."""

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
    def _accept_string_shorthand(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"prompt": data}
        return data

    @computed_field  # type: ignore[prop-decorator]
    @property
    def slug(self) -> str:
        """Branch- and worktree-safe slug from name, id, or prompt."""

        return slugify(self.name or self.id or self.prompt)

    @property
    def key(self) -> str:
        """Stable identifier: explicit id when set, else the slug."""

        return self.id or self.slug


class ResolvedItem(BaseModel):
    """Resolved session configuration."""

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
        """Build the headless `copilot` command."""

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


class Plan(BaseModel):
    """Parsed cmux run configuration."""

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
    def _validate_unique_keys_and_dependencies(cls, items: list[Item]) -> list[Item]:
        keys = [item.key for item in items]
        dupes = {key for key in keys if keys.count(key) > 1}
        if dupes:
            raise ValueError(
                f"`items` contains duplicate identifiers {sorted(dupes)}; assign distinct `name` or `id` values."
            )

        known = set(keys)
        for item in items:
            missing = [dep for dep in item.depends_on if dep not in known]
            if missing:
                raise ValueError(
                    f"`depends_on` for `{item.key}` references unknown ids {missing}; known ids: {sorted(known)}."
                )

        return items

    def resolve(self) -> list[ResolvedItem]:
        """Resolve items in declaration order."""

        defaults = self.defaults
        resolved: list[ResolvedItem] = []

        for index, item in enumerate(self.items):
            permissions = item.permissions or defaults.permissions
            if item.paths:
                permissions = permissions.model_copy(update={"add_dir": [*permissions.add_dir, *item.paths]})

            branch = item.branch or defaults.branch_template.format(slug=item.slug, id=item.key)
            prompt = item.prompt
            if item.include_system and self.system.strip():
                prompt = f"{self.system.strip()}\n\n---\n\n{item.prompt.strip()}"
            pr_settings = defaults.pr
            display_name = item.name or item.slug

            env = dict(item.env)
            if defaults.port_base is not None:
                env = {defaults.port_env: str(defaults.port_base + index), **env}

            resolved.append(
                ResolvedItem(
                    key=item.key,
                    name=display_name,
                    slug=item.slug,
                    prompt=prompt,
                    model=item.model or defaults.model,
                    effort=item.effort or defaults.effort,
                    permissions=permissions,
                    branch=branch,
                    base=item.base or defaults.base,
                    labels=list(dict.fromkeys([*pr_settings.labels, *item.labels])),
                    draft=pr_settings.draft if item.draft is None else item.draft,
                    depends_on=list(item.depends_on),
                    env=env,
                    deps=defaults.deps,
                    remote=defaults.remote,
                    pr_title=pr_settings.title_template.format(name=display_name, slug=item.slug),
                    pr_body=pr_settings.body_template.format(
                        name=display_name, slug=item.slug, prompt=item.prompt.strip()
                    ),
                )
            )

        return resolved


class ConfigError(Exception):
    """Raised when a cmux YAML file is missing or invalid."""


def load_plan(path: str | Path) -> Plan:
    """Parse and validate a cmux YAML file.

    Raises:
        ConfigError: Missing or invalid file.

    """

    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"`{config_path}` config file does not exist.")

    try:
        raw = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"`{config_path}` is not valid YAML: {exc}.") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"`{config_path}` top-level YAML must be a mapping, got `{type(raw).__name__}`.")

    try:
        return Plan.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"`{config_path}` is not a valid cmux plan:\n{_format_validation(exc)}.") from exc


def _format_validation(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"  {location}: {error['msg']}")

    return "\n".join(lines)
