# cpmux — Conventions

Rules and invariants for adding to or changing cpmux. cpmux adopts the **phitrain conventions**
(microsoft/aifsdk `.github/rules/` R1–R18 and `phitrain/CONVENTIONS.md`) as its style
rules. **Code defines behavior; this file lists the applicable rules.**

For user-facing setup and usage see `README.md`.

## Architecture invariants

These invariants keep cpmux composable; do not violate them.

- **One worktree per item.** Every item runs in its own `git worktree` on a unique
  `cpmux/<slug>` branch off `origin/<base>`, or an explicit `base_from` predecessor's
  recorded successful commit. Items never share a working tree. `depends_on` alone
  does not inherit code; a failed parent blocks its children.
- **The orchestrator owns delivery by default.** The default edit preset denies
  `git push`; explicit `full`/`yolo` permissions retain their documented broader access.
  The orchestrator finalizes initial run items using their PR settings, or commits locally
  under `--no-pr`. Follow-up turns do not automatically finalize Git changes.
- **Monitor via JSONL, never PTY.** Session state is derived from
  `copilot --output-format json` (a JSONL event stream), tee'd to disk. Do not
  screen-scrape a terminal.
- **Pre-assigned session ids.** The orchestrator assigns each session's
  `--session-id` UUID up front, so a session is always addressable for status,
  resume, and recovery.
- **cpmux owns only `.cpmux/`.** copilot keeps its own transcripts and resumable
  session store under `~/.copilot`; reuse it read-only rather than duplicating it.
- **A run has one writer.** A run-wide lease excludes competing mutations. Idle-run
  session operations hold a shared run lease and an exclusive per-session lease.
  `owner.json` records PID plus creation time so stale PIDs are never sufficient
  authority to signal a process. Cancellation requests do not overwrite live owner state.
  Owned subprocesses inherit lease descriptors, so a crashed controller cannot release
  a still-running Git operation's lock. Close descriptors rather than explicitly unlocking
  inherited file descriptions; preserve `pass_fds` in subprocess adapters.
- **Delivery is source-bound.** Explicit setup commands precede agent execution.
  Required checks run against a committed, clean candidate; changing HEAD or source
  invalidates their receipt. Delivery pushes that exact candidate, never a later HEAD.
- **Recovery is explicit.** Reconnect only monitors. Stage-aware retry preserves
  successful agent work; native resume and a fresh conversation are deliberate modes.
  Attempts and accumulated usage survive every mode, and no recovery resets Git edits.
- **Feedback targets a revision.** Compare the reviewed diff token under the session
  lease before starting repair. Follow-ups do not silently push changes.
- **Budgets are soft admission controls.** Report missing usage instead of inventing
  zero cost. In-flight work can exceed a ceiling; pausing affects only queued admission.
- **Config precedence is `item > defaults > built-in`.** Resolution is centralized in
  `Plan.resolve()`, which validation also exercises before accepting a plan; downstream code
  consumes `ResolvedItem`, never re-merges. Setup/check configuration resolves as
  `item > selected profile > run defaults`, with explicit empty lists respected.

## Package structure

Modules are grouped by domain. Shared foundation modules stay at the package root.

```
cpmux/
  config.py  events.py  logging.py  process.py  theme.py   foundation and presentation primitives
  engine/    supervisor session daemon store ownership   run lifecycle + state
             commands delivery interact review reporting intake   execution + operations
  vcs/       git pr issues                Git worktrees + GitHub adapters
  voice/     recorder transcriber synthesizer   speech → transcript → cpmux plan
  ui/        cli dashboard search render  Typer commands, TUI, transcript rendering
```

- **Layering is one-directional:** `ui` → {`engine`, `voice`} → `vcs` → foundation. A layer
  may import only the layers below it; foundation imports no subpackage. This keeps `engine`
  headless without the TUI. Shared code moves to the lowest layer that needs it
  (shared status presentation lives in `theme.py`; the JSONL `event_data` unwrap
  lives in `events.py`; prompt/PR protocol vocabulary lives with configuration).
- **Adapters stay thin.** CLI commands and Textual workers own input and presentation.
  Shared follow-up execution and outcome persistence live in `engine/interact.py`;
  YAML parsing and validation live in `config.py` for files and generated plans alike.
  Extracting these operations does not change worker scheduling or ownership policy.
- **A subpackage must be a cohesive domain** with a few focused modules,
  not a thin split of one concern. Heavy or optional third-party deps (`sounddevice`,
  `faster-whisper` behind the `voice` extra) are imported lazily in the function that
  needs them to keep the core install and `--help` light.
- **Absolute imports only.** Subpackage `__init__.py` files stay empty apart from the
  header; the root initializer exposes the release version. Import behavior from its
  defining module rather than adding package facades.
- **Tests mirror source 1:1**, so `engine/store.py` is tested by
  `tests/engine/test_store.py`. Shared fixtures live in `tests/conftest.py`.

## `.cpmux/` layout

Repo-local and gitignored. cpmux stores orchestration bookkeeping here; nothing here is committed.

```
.cpmux/
  runs/<run_id>/
    manifest.json                 resolved run config
    owner.json / owner.lock       identity metadata and stable-inode run lease
    paused / stop / ready         owner-consumed control and startup markers
    sessions/<key>/
      prompt.md                   the exact prompt sent to copilot
      transcript.jsonl            raw tee of copilot --output-format json
      session.json                per-session record (status, branch, PR...)
      copilot-logs/               copilot's own --log-dir
      owner.json / session.lock   idle-run operation ownership
      attempts/<number>/          local setup/check outputs and verification artifacts
  worktrees/<run_id>/<key>/       one git worktree per item
```

## Code style

Adopted from phitrain (rule ids in parentheses).

- Python 3.12+ syntax. Use `X | None`, never `Optional[X]`. Use builtin generics
  (`dict[str, Any]`, `list[str]`); import only `Any`, `Literal`, `Annotated`, … from
  `typing`. ABCs (`Callable`, `Iterable`, …) come from `collections.abc`. (R2)
- Every `.py` file starts with the two-line copyright/license header.
- Imports are top-level and absolute (`from cpmux.x import y`). Order: stdlib →
  third-party → local, blank-separated. The deliberate feature-local imports for
  optional audio backends and the dashboard remain lazy to preserve the light CLI.
- Public functions, classes, and their `__init__` carry Google-style docstrings
  (single-sentence summary; one-line `Args:`/`Returns:`/`Raises:` entries). A regular
  class keeps a one-line class summary and documents its constructor `Args:` on
  `__init__`. Private helpers (`_name`) and framework-dispatched overrides (Textual
  `compose`/`on_<event>`/`action_<name>`/lifecycle hooks) carry none. Methods of
  private helper classes follow the private-helper rule. No semicolons or
  `defaults to <X>` tails in entries. (R3, R13)
- A multiline docstring keeps one blank line before its closing `"""`. Every
  docstring keeps one blank line after its closing `"""` before the first statement
  or field. Summary-only class, property, and CLI-command docstrings stay on one
  line, following the existing Black formatting and single-summary convention.
- Public I/O contracts explain mutation, persistence, resource ownership, cancellation,
  and whether failures are raised or returned. Do not merely repeat annotated signatures.
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
- Multi-word options use `--kebab-case` (e.g. `--dry-run`, `--transcribe-model`,
  `--no-pr`); single-letter shortcuts are unique within a command.
- Validate inputs with `if/raise <SpecificError>`; surface operational failures with
  `theme.print_error(...)` followed by `raise typer.Exit(1)`, with no hand-rolled `"Error:"`
  prefix or `typer.echo(..., err=True)`. Library diagnostics continue to use logging.
- Presentation (status tables, transcripts) uses Rich; raw machine output uses
  `typer.echo`. Each `command()` carries a single-sentence docstring for `--help`;
  Typer's argument and option declarations document its command-line parameters.
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

The release version lives in `cpmux/__init__.py`. Hatch reads it for wheel and source
distribution metadata, and uv watches that file to invalidate cached build metadata.
Update that value and regenerate `uv.lock` when changing versions.
Typer owns command parsing. Click is retained for its editor and ANSI utilities,
which current Typer does not export; translate editor failures at that adapter boundary.
The Typer floor supports union annotations and current Click versions, and PyYAML
starts at a version installable on Python 3.12. CI also exercises lowest direct
dependency resolution rather than treating the development lock as a compatibility claim.

```bash
isort cpmux tests && black cpmux tests && flake8 cpmux tests
pytest
```

## Current capabilities and boundaries

- **Current:** foreground and detached (`--detach`) runs; `ls`/`attach`/`wait` monitoring;
  attention-first Textual `dash`; `enter`/`send` and revision-bound feedback;
  explicit verification/finalization; selective retry, pause and soft usage admission;
  cross-session `search` (with `--fts` over Copilot's own index); `logs --follow`;
  `down`/`kill`/`rm`; per-item dev-server ports (`port_base`); `depends_on` ordering and
  explicit single-parent `base_from` stacks; voice/text/audio composition and read-only
  GitHub issue plan intake; identity-checked crash reconciliation.
- **Not implemented:** ACP/live permission prompting, remote `/delegate` execution,
  automatic multi-parent merges/rebases, hard monetary spending caps, or hermetic
  execution. Native Copilot owns its conversation store; cpmux does not duplicate it.
