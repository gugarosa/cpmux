# cmux — Conventions

Rules and invariants for adding to or changing cmux. cmux adopts the **phitrain
conventions** (microsoft/aifsdk `.github/rules/` R1–R18 and `phitrain/CONVENTIONS.md`)
as its style source of truth. **Code is the source of truth for "how it works";
this file is the source of truth for "what rules apply."**

For user-facing setup and usage see `README.md`.

## Architecture invariants

These define how cmux stays composable. Crossing them turns one bug into many.

- **One worktree per item.** Every item runs in its own `git worktree` on a unique
  `cmux/<slug>` branch off `origin/<base>`. Items never share a working tree.
- **Agents are edit-only; the orchestrator ships.** Sessions run with `git push`
  denied (`--deny-tool='shell(git push)'`). The orchestrator — never the agent —
  commits the diff, pushes the branch, and opens exactly one draft PR per item.
- **Monitor via JSONL, never PTY.** Session state is derived from
  `copilot --output-format json` (a JSONL event stream), tee'd to disk. Do not
  screen-scrape a terminal.
- **Pre-assigned session ids.** The orchestrator assigns each session's
  `--session-id` UUID up front, so a session is always addressable for status,
  resume, and recovery.
- **cmux owns only `.cmux/`.** copilot keeps its own transcripts and resumable
  session store under `~/.copilot`; reuse it read-only rather than duplicating it.
- **A run has one owner.** Its pid is recorded in `daemon.json` — the foreground `up`
  process, or the detached daemon. While the owner is alive the run is managed; a stale
  owner (present but dead) marks a crash, so non-terminal sessions reconcile to a terminal
  state instead of hanging as "running" forever.
- **Config precedence is `item > defaults > built-in`.** Resolution happens once in
  `Plan.resolve()`; downstream code consumes `ResolvedItem`, never re-merges.

## `.cmux/` layout

Repo-local and gitignored. cmux owns the orchestration bookkeeping; nothing here is committed.

```
.cmux/
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
- Every `.py` file starts with the two-line copyright/licence header.
- Imports are top-level and absolute (`from cmux.x import y`). Order: stdlib →
  third-party → local, blank-separated.
- Public functions and classes carry Google-style docstrings (single-sentence
  summary; one-line `Args:`/`Returns:`/`Raises:` entries). Private helpers (`_name`)
  carry no docstring. No semicolons or `defaults to <X>` tails in entries. (R3, R13)
- Logging uses `get_logger(__name__)` from `cmux.logging`; **never `print()` in
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

**Deliberate divergence — config uses Pydantic v2, not `@dataclass`.** phitrain models
config with `@dataclass` + `__post_init__` because it is driven by OmegaConf. cmux is a
declarative-YAML tool whose value is string→item coercion, discriminated unions,
`${ENV}` interpolation, and precise validation errors — all idiomatic in Pydantic v2.
The config models in `config.py` and the on-disk records in `state.py` are therefore
Pydantic `BaseModel`s. Everything else follows phitrain.

## CLI conventions (`cli.py`)

- One `command()` function per verb, aggregated on the Typer `app`.
- Every `Option(...)` whose parameter name contains an underscore exposes both the
  `--snake_case` and `--kebab-case` aliases.
- Validate inputs with `if/raise <SpecificError>`; surface operational failures with
  `logger.error(...)` followed by `raise typer.Exit(1)` — no hand-rolled `"Error:"`
  prefix, no `typer.echo(..., err=True)`.
- Presentation (status tables, transcripts) uses Rich; raw machine output uses
  `typer.echo`. Each `command()` carries a single-sentence docstring for `--help`.
- Short-lived subprocesses use `subprocess.run(..., capture_output=True, text=True,
  check=False)`; streaming/long-running children use `asyncio` subprocesses.

## Tests

- Tests mirror the source layout: `tests/test_<module>.py`.
- Test functions use `test_behavior_under_condition`, are plain functions, and carry no
  docstrings or type hints.
- Asserts are bare `assert <expr>` — no failure-message strings; the test name carries
  the intent. (R15)

## Tooling

black + isort (`profile = black`) + flake8, all at line-length 120, wired through
`.pre-commit-config.yaml`.

```bash
isort src tests && black src tests && flake8 src tests
pytest
```

## Status and roadmap

- **v0/v1/v2 (current):** foreground and detached (`--detach`) runs; `ls`/`attach` live
  monitor; an interactive Textual `dash` (session list + live transcript + `/` search +
  `e` to drop into a native `copilot --resume`); `enter`/`send` interaction; cross-session
  `search`; `logs --follow`; `down`/`kill`; crash reconciliation via the run owner.
- **v2 remaining:** ACP transport for live permission prompts; optional remote `/delegate`
  mode; `depends_on` DAG surfacing; dev-server/port management; reuse of copilot's own FTS
  index for search.
