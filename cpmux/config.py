# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import os
import re
import string
import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    computed_field,
    field_validator,
    model_validator,
)

from cpmux.vcs.pr import PR_DRAFT_FILENAME

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

_PR_INSTRUCTIONS = (
    "When the task is complete, write the pull-request title and description cpmux will use to a file "
    f"named `{PR_DRAFT_FILENAME}` in the repository root (your working directory). Format it as Markdown: "
    "the first line is a level-one heading holding a concise, imperative title, then a blank line, then the "
    "body. Base the description on the changes you actually made. If the repository defines a pull-request "
    "template (check `.github/`, the repository root, or `docs/` for a `PULL_REQUEST_TEMPLATE.md` file or a "
    "`PULL_REQUEST_TEMPLATE/` directory), follow its structure and fill in every applicable section "
    "truthfully; otherwise summarise the change, list the notable edits, and note how you verified them. Do "
    f"not run `git add`, `git commit`, or `git push`, and do not stage `{PR_DRAFT_FILENAME}`; cpmux commits "
    "your work and, when enabled, opens the pull request."
)


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

    Args:
        value: String containing environment references.

    Returns:
        String with environment references expanded.

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
    """Convert text to a branch- and worktree-safe slug.

    Args:
        text: Text to normalize.

    Returns:
        Branch- and worktree-safe slug.

    """

    text = text.strip().lower().splitlines()[0] if text.strip() else "task"
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")

    return (text or "task")[:50]


def validate_identifier(value: str, field: str) -> str:
    """Require a normalized relative identifier within its storage directory.

    Args:
        value: Identifier to validate.
        field: Field name used in errors.

    Returns:
        The unchanged identifier.

    Raises:
        ValueError: The identifier is blank, anchored, or contains path aliases.

    """

    path = Path(value)
    if not value.strip() or "\0" in value or not path.parts or path.anchor or ".." in path.parts or str(path) != value:
        raise ValueError(f"`{field}` must be a normalized relative identifier without `..`, but got {value!r}.")

    return value


def _validate_template(template: str, field: str, allowed: set[str]) -> str:
    try:
        names = {name for _, name, _, _ in string.Formatter().parse(template) if name}
    except ValueError as exc:
        raise ValueError(f"`{field}` is not a valid format template: {exc}.") from exc

    unknown = sorted(names - allowed)
    if unknown:
        raise ValueError(f"`{field}` uses unknown placeholder(s) {unknown}; allowed: {sorted(allowed)}.")

    return template


class Permissions(BaseModel):
    """Permission preset with allow, deny, and network options.

    Attributes:
        preset: Base permission preset.
        allow: Additional allowed tool specifications.
        deny: Additional denied tool specifications.
        add_dir: Additional accessible directories.
        allow_url: Allowed network URL patterns.

    """

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

    @field_validator("allow", "deny", "add_dir", "allow_url")
    @classmethod
    def _drop_blank(cls, value: list[str]) -> list[str]:
        return [entry for entry in value if entry.strip()]

    def to_flags(self) -> list[str]:
        """Return `copilot` permission flags.

        Returns:
            List of Copilot permission flags.

        """

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
    """Pull-request defaults applied unless overridden.

    Attributes:
        draft: Whether pull requests start as drafts.
        labels: Default pull-request labels.
        title_template: Pull-request title template.
        body_template: Pull-request body template.

    """

    model_config = ConfigDict(extra="forbid")

    draft: bool = True
    labels: list[str] = Field(default_factory=list)
    title_template: str = "{name}"
    body_template: str = "## Summary\n\n{prompt}\n"

    @field_validator("title_template")
    @classmethod
    def _validate_title_template(cls, value: str) -> str:
        return _validate_template(value, "title_template", {"name", "slug", "prompt"})

    @field_validator("body_template")
    @classmethod
    def _validate_body_template(cls, value: str) -> str:
        return _validate_template(value, "body_template", {"name", "slug", "prompt"})


class Defaults(BaseModel):
    """Defaults inherited by all items.

    Attributes:
        model: Default Copilot model.
        effort: Default reasoning effort.
        permissions: Default permission settings.
        base: Default base branch.
        branch_template: Branch name template.
        pr: Default pull-request settings.
        concurrency: Maximum concurrent sessions.
        deps: Dependency setup mode.
        remote: Git remote name.
        port_base: Starting port for item allocation.
        port_env: Environment variable receiving the allocated port.

    """

    model_config = ConfigDict(extra="forbid")

    model: str = "gpt-5.5"
    effort: Effort = Effort.medium
    permissions: Permissions = Field(default_factory=Permissions)
    base: str = "main"
    branch_template: str = "cpmux/{slug}"
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

    @field_validator("model", "base")
    @classmethod
    def _reject_empty(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"`{info.field_name}` must not be empty.")

        return value

    @field_validator("branch_template")
    @classmethod
    def _validate_branch_template(cls, value: str) -> str:
        return _validate_template(value, "branch_template", {"slug", "id"})


class Item(BaseModel):
    """Task prompt with optional per-item overrides.

    Attributes:
        prompt: Task prompt.
        name: Display name override.
        id: Stable identifier override.
        model: Copilot model override.
        effort: Reasoning effort override.
        permissions: Permission settings override.
        branch: Branch name override.
        base: Base branch override.
        labels: Additional pull-request labels.
        draft: Pull-request draft override.
        paths: Additional accessible paths.
        depends_on: Required item identifiers.
        env: Session environment variables.
        include_system: Whether to prepend the plan system prompt.

    """

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

    @field_validator("prompt")
    @classmethod
    def _reject_blank_prompt(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("`prompt` must not be blank.")

        return value

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str | None) -> str | None:
        return validate_identifier(value, "id") if value is not None else None

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
    """Resolved session configuration.

    Attributes:
        key: Stable item identifier.
        name: Display name.
        slug: Branch- and worktree-safe slug.
        prompt: Resolved task prompt (system and item).
        model: Copilot model.
        effort: Reasoning effort.
        permissions: Permission settings.
        branch: Branch name.
        base: Base branch.
        labels: Pull-request labels.
        draft: Whether the pull request is a draft.
        depends_on: Required item identifiers.
        env: Session environment variables.
        deps: Dependency setup mode.
        remote: Git remote name.
        pr_title: Pull-request title.
        pr_body: Pull-request body.

    """

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

    def effective_prompt(self) -> str:
        """Return the resolved prompt with the pull-request authoring instructions."""

        return f"{self.prompt.rstrip()}\n\n---\n\n{_PR_INSTRUCTIONS}"

    def spawn_argv(self, worktree: str | Path, session_id: str, log_dir: str | Path) -> list[str]:
        """Build the headless `copilot` command.

        Args:
            worktree: Working directory for the Copilot process.
            session_id: Copilot session identifier.
            log_dir: Directory for Copilot logs.

        Returns:
            Headless Copilot command arguments.

        """

        argv = [
            "copilot",
            "-C",
            str(worktree),
            "-p",
            self.effective_prompt(),
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
    """Parsed cpmux run configuration.

    Attributes:
        version: Configuration schema version.
        system: System prompt prepended to eligible items.
        defaults: Defaults inherited by items.
        items: Declared task items.

    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    system: str = ""
    defaults: Defaults = Field(default_factory=Defaults)
    items: Annotated[list[Item], Field(min_length=1)]

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

        paths = sorted((Path(key) for key in keys), key=lambda path: path.parts)
        for parent, child in zip(paths, paths[1:]):
            if parent in child.parents:
                raise ValueError(f"`items` contains overlapping identifiers `{parent}` and `{child}`.")

        known = set(keys)
        for item in items:
            missing = [dep for dep in item.depends_on if dep not in known]
            if missing:
                raise ValueError(
                    f"`depends_on` for `{item.key}` references unknown ids {missing}; known ids: {sorted(known)}."
                )

        pending = {item.key: set(item.depends_on) for item in items}
        while ready := [key for key, deps in pending.items() if not deps]:
            for key in ready:
                del pending[key]
            for deps in pending.values():
                deps.difference_update(ready)
        if pending:
            raise ValueError(f"`depends_on` forms a cycle among {sorted(pending)}; remove the circular dependency.")

        return items

    @model_validator(mode="after")
    def _validate_port_range(self) -> "Plan":
        base = self.defaults.port_base
        if base is not None and base + len(self.items) - 1 > 65535:
            raise ValueError(
                f"`port_base` {base} + {len(self.items)} items exceeds port 65535; lower it or split the plan."
            )

        return self

    @model_validator(mode="after")
    def _validate_resolution(self) -> "Plan":
        try:
            self.resolve()
        except (IndexError, KeyError, ValueError) as exc:
            raise ValueError(f"`templates` cannot be resolved: {exc}.") from exc

        return self

    def resolve(self) -> list[ResolvedItem]:
        """Resolve items in declaration order.

        Returns:
            Items resolved in declaration order.

        """

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
                    labels=[label for label in dict.fromkeys([*pr_settings.labels, *item.labels]) if label.strip()],
                    draft=pr_settings.draft if item.draft is None else item.draft,
                    depends_on=list(item.depends_on),
                    env=env,
                    deps=defaults.deps,
                    remote=defaults.remote,
                    pr_title=pr_settings.title_template.format(
                        name=display_name, slug=item.slug, prompt=item.prompt.strip()
                    ),
                    pr_body=pr_settings.body_template.format(
                        name=display_name, slug=item.slug, prompt=item.prompt.strip()
                    ),
                )
            )

        return resolved


class ConfigError(Exception):
    """Raised when a cpmux YAML file is missing or invalid."""


def load_plan(path: str | Path) -> Plan:
    """Parse and validate a cpmux YAML file.

    Args:
        path: YAML configuration path.

    Returns:
        Validated run plan.

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
        raise ConfigError(f"`{config_path}` is not a valid cpmux plan:\n{_format_validation(exc)}") from exc


def _format_validation(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"  {location}: {message}")

    return "\n".join(lines)
