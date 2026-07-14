# cpmux — Conventions

Rules and invariants for adding to or changing cpmux. cpmux adopts the **phitrain conventions**
(microsoft/aifsdk `.github/rules/` R1–R18 and `phitrain/CONVENTIONS.md`) as its style
rules. **Code defines behavior; this file lists the applicable rules.**

For user-facing setup and usage see `README.md`.

## Architecture invariants

These invariants keep cpmux composable; do not violate them.

- **One worktree per item.** Every item runs in its own `git worktree` on a unique
  `cpmux/<slug>` branch off `origin/<base>`. Items never share a working tree.
- **Agents are edit-only; the orchestrator ships.** Sessions run with `git push`
  denied (`--deny-tool='shell(git push)'`). The orchestrator, never the agent,
  commits the diff, pushes the branch, and opens exactly one draft PR per item.
- **Monitor via JSONL, never PTY.** Session state is derived from
  `copilot --output-format json` (a JSONL event stream), tee'd to disk. Do not
  screen-scrape a terminal.
- **Pre-assigned session ids.** The orchestrator assigns each session's
  `--session-id` UUID up front, so a session is always addressable for status,
  resume, and recovery.
- **cpmux owns only `.cpmux/`.** copilot keeps its own transcripts and resumable
  session store under `~/.copilot`; reuse it read-only rather than duplicating it.
- **A run has one owner.** Its pid is recorded in `daemon.json`: the foreground `up`
  process, or the detached daemon. A live owner means the run is managed. A stale
  owner (present but dead) marks a crash, so non-terminal sessions reconcile to a terminal
  state instead of remaining "running" indefinitely.
- **Config precedence is `item > defaults > built-in`.** Resolution happens once in
  `Plan.resolve()`; downstream code consumes `ResolvedItem`, never re-merges.

## Package structure

Modules are grouped by domain. Shared foundation modules stay at the package root.

```
cpmux/
  config.py  events.py  logging.py      foundation: config model, JSONL/status, logging
  engine/    supervisor session daemon store interact   run lifecycle + state
  vcs/       git pr                       git worktrees + PR automation
  voice/     recorder transcriber synthesizer   speech → transcript → cpmux plan
  ui/        cli dashboard search render  Typer commands, TUI, transcript rendering
```

- **Layering is one-directional:** `ui` → {`engine`, `voice`} → `vcs` → foundation. A layer
  may import only the layers below it; foundation imports no subpackage. This keeps `engine`
  headless without the TUI. Shared code moves to the lowest layer that needs it
  (status-to-colour lives in `ui/render.py`
  because only the UI reads it; the JSONL `event_data` unwrap lives in `events.py`
  because the engine needs it too).
- **A subpackage must be a cohesive domain** with a few focused modules,
  not a thin split of one concern. Heavy or optional third-party deps (`sounddevice`,
  `faster-whisper` behind the `voice` extra) are imported lazily in the function that
  needs them to keep the core install and `--help` light.
- **Absolute imports only**, and `__init__.py` stays empty apart from the header —
  import from the module, not the package.
- **Tests mirror source 1:1**, so `engine/store.py` is tested by
  `tests/engine/test_store.py`. Shared fixtures live in `tests/conftest.py`.

## `.cpmux/` layout

Repo-local and gitignored. cpmux stores orchestration bookkeeping here; nothing here is committed.

```
.cpmux/
  runs/<run_id>/
    manifest.json                 resolved run config
    sessions/<key>/
      prompt.md                   the exact prompt sent to copilot
      transcript.jsonl            raw tee of copilot --output-format json
      session.json                per-session record (status, branch, PR...)
      copilot-logs/               copilot's own --log-dir
  worktrees/<run_id>/<key>/       one git worktree per item
```

## Code style

Adopted from phitrain (rule ids in parentheses).

- Python 3.12+ syntax. Use `X | None`, never `Optional[X]`. Use builtin generics
  (`dict[str, Any]`, `list[str]`); import only `Any`, `Literal`, `Annotated`, … from
  `typing`. ABCs (`Callable`, `Iterable`, …) come from `collections.abc`. (R2)
- Every `.py` file starts with the two-line copyright/license header.
- Imports are top-level and absolute (`from cpmux.x import y`). Order: stdlib →
  third-party → local, blank-separated.
- Public functions, classes, and their `__init__` carry Google-style docstrings
  (single-sentence summary; one-line `Args:`/`Returns:`/`Raises:` entries). A regular
  class keeps a one-line class summary and documents its constructor `Args:` on
  `__init__`. Private helpers (`_name`) and framework-dispatched overrides (Textual
  `compose`/`on_<event>`/lifecycle hooks) carry none. No semicolons or
  `defaults to <X>` tails in entries. (R3, R13)
- A docstring keeps one blank line before its closing `"""`, and one blank line
  after the closing `"""` before the first statement or field.
- Data classes (Pydantic models and `@dataclass`, which have no explicit `__init__`)
  document every field in an `Attributes:` section, one line per field
  (`name: what it holds.`).
- Logging uses `get_logger(__name__)` from `cpmux.logging`; **never `print()` in
  library code** (the CLI presentation layer uses Rich and `typer.echo`). Diagnostic
  `logger.warning`/`logger.error` use a backticked offender and trailing period:
  `` f"`name=value` <verb-phrase>." ``; `logger.info`/`logger.debug` stay plain. (R14)
- Raised error messages use `` f"`<name>` <verb-phrase>[, but got <value>]." `` with a
  trailing period and `is None`/`is True` prose. (R1)
- Validation uses `if/raise` with a specific exception, never `assert`. Bare `except:`
  is forbidden.
- Comments explain **why**, not **what**: default to none, one-liner preference,
  3-line hard cap, no banner/section separators, no trailing period. (R8)
- Insert a single blank line at each phase transition in function bodies ≥ 12 LOC. (R11)
- Inline first; extract a helper/constant/parameter only on a second call-site. (R16)
- Double quotes for strings. Readable prose stays within 120 characters. (R9)

**Deliberate divergence: config uses Pydantic v2, not `@dataclass`.** phitrain models
config with `@dataclass` + `__post_init__` because it uses OmegaConf. cpmux's declarative
YAML needs string→item coercion, discriminated unions,
`${ENV}` interpolation, and precise validation errors, all idiomatic in Pydantic v2.
The config models in `config.py` and on-disk records in `engine/store.py` are therefore
Pydantic `BaseModel`s. Everything else follows phitrain.

## CLI conventions (`ui/cli.py`)

- One `command()` function per verb, aggregated on the Typer `app`.
- Every `Option(...)` whose parameter name contains an underscore exposes both the
  `--snake_case` and `--kebab-case` aliases.
- Validate inputs with `if/raise <SpecificError>`; surface operational failures with
  `logger.error(...)` followed by `raise typer.Exit(1)`, with no hand-rolled `"Error:"`
  prefix, no `typer.echo(..., err=True)`.
- Presentation (status tables, transcripts) uses Rich; raw machine output uses
  `typer.echo`. Each `command()` carries a single-sentence docstring for `--help`.
- Short-lived subprocesses use `subprocess.run(..., capture_output=True, text=True,
  check=False)`; streaming/long-running children use `asyncio` subprocesses.

## Tests

- Tests mirror the source layout: `tests/<subpackage>/test_<module>.py`, with foundation
  modules tested at the `tests/` root and shared fixtures in `tests/conftest.py`.
- Test functions are named `test_<function_or_class_name>_<behavior>`: lead with the exact
  function, method, or class under test (snake_cased, any leading underscore dropped), then
  the behavior — e.g. `test_resolve_base_falls_back_to_head`,
  `test_run_paths_resolve_under_run_dir`. They are plain functions with no docstrings or type
  hints.
- Asserts are bare `assert <expr>`, with no failure-message strings; the test name carries
  the intent. (R15)

## Tooling

black + isort (`profile = black`) + flake8, all at line-length 120, wired through
`.pre-commit-config.yaml`.

```bash
isort cpmux tests && black cpmux tests && flake8 cpmux tests
pytest
```

## Status and roadmap

- **Current:** foreground and detached (`--detach`) runs; `ls`/`attach` live monitor; an
  interactive Textual `dash` (session list + live transcript + `/` search + `e` to drop into a
  native `copilot --resume`); `enter`/`send` interaction; cross-session `search` (with `--fts`
  over copilot's own index); `logs --follow`; `down`/`kill`/`rm`; per-item dev-server ports
  (`port_base`); `depends_on` ordering; voice/text/audio plan composition; crash reconciliation
  via the run owner.
- **Remaining:** ACP transport for live permission prompts; optional remote `/delegate` mode.
