# Changelog

All notable changes to cpmux are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Maintenance

- Move artifact upload/download actions to maintained Node 24 runtimes, and
  exercise the same transfer path before CI's clean-wheel installation smoke test.

## [0.2.0] - 2026-09-06

### Added

- Explicit setup and acceptance commands, reusable execution profiles, per-command
  logs/timeouts, agent timeouts, and source-bound verification receipts.
- Persistent attempts and stage-aware `retry`, including explicit native `--resume`
  and `--fresh` conversation modes without resetting worktrees or replaying successful items.
- Queue `pause`/`unpause`, a soft reported-premium admission budget, and `wait` with
  completion/failure/unowned/timeout exit codes and an optional terminal bell.
- Read-only GitHub issue intake into editable plans, source provenance, template/profile
  reuse, Enterprise host handling, and literal environment-reference escaping.
- Explicit single-predecessor `base_from` inheritance and stacked PR targets, separate
  from ordering-only `depends_on`.
- Revision-bound `diff`/`feedback`, local `verify`, and explicit `finalize` operations
  for the initial delivery or an existing matching open PR.
- An attention-first dashboard with filters, transcript/diff/checks/details views,
  explicit review actions, and narrow-screen queue-to-detail navigation.
- Versioned read-only run reports with attempts, activity, verification/delivery
  identity, reported versus missing usage, and identified child-process memory.
- A macOS Python 3.12 CI job alongside the existing Linux interpreter and package checks.
- Minimum-direct-dependency CI coverage, in addition to the locked development environment.

### Changed

- Run and session mutations use operating-system leases and PID creation-time identity.
  Detached starts require an explicit acknowledgement; competing writers fail clearly.
- Remove the superseded PID-only helper layer and reject incomplete owner metadata
  rather than maintaining a compatibility path. Finish pre-0.2 runs before upgrading.
- Required checks gate the exact candidate commit pushed by cpmux. No configured checks
  remains an explicit unverified state, not an implicit acceptance pass.
- Worktree setup records exist before execution, failed prerequisites block dependents,
  and runtime bookkeeping uses Git's local exclude file rather than a tracked `.gitignore` edit.
- Node `deps: install` runs through owned setup execution; failed installs prevent
  agent launch. Setup/check commands must finish in the foreground.
- Plan synthesis describes profiles, code inheritance, soft budgets, and literal
  references without inventing executable setup or acceptance commands.
- Correct the Typer and PyYAML minimum versions to support the actual language and
  installation path; retain Click's supported editor instead of replacing it with custom code.
- Native interactive entry is async and does not block independent session operations.

### Fixed

- Preserve cancelled operations and partial delivery outcomes in durable attempt history.
- Refuse unknown/reused process identities, stale review feedback, changed checked
  revisions, and unsafe updates to closed or mismatched PRs.
- Accumulate repeated usage events without double counting and preserve fractional
  premium requests, while exposing attempts whose usage was not reported.
- Surface incomplete run history and cleanup failures instead of claiming success.
- Wait through repeated cancellation until child cleanup finishes, and retain OS
  leases in subprocesses across controller crashes, including in-flight Git operations.
- Bind stored session keys to their requested paths before refreshing a caller.
- Resolve PRs against Git's effective push destination and verify the remote head
  after pushing; refresh only the generated evidence section of an existing PR.
- Surface editor failures consistently even when Typer uses its vendored Click implementation.
- Inspect live process-group members instead of inferring liveness from signal
  permissions, and retain identified children across process-group changes during cleanup.

## [0.1.3] - 2026-09-06

### Changed

- Align public APIs with the Google-style docstring layout, complete constructor and
  attribute documentation, and omit private/framework-handler docstrings.
- Restore top-level test imports and meaningful phase separators, and normalize
  diagnostic offenders and punctuation.
- Reuse one terminal-glyph selection path, remove a redundant voice-forwarding wrapper,
  and replace unnecessary test factories with explicit inputs and assertions.
- Clarify the crash-reconciliation side effects of monitoring commands.
- Share follow-up execution and outcome persistence between the CLI and dashboard,
  without changing worker scheduling or concurrency policy.
- Centralize YAML plan parsing and shared PR protocol vocabulary in configuration,
  removing the reverse dependency from configuration into VCS.
- Clarify public mutation, persistence, cancellation, and error contracts, and enforce
  the existing prohibition on bare exception handlers.

### Fixed

- Report unreadable files and invalid YAML encodings as contextual `ConfigError` failures.
- Reject falsey non-mapping YAML roots without misclassifying them as empty plans.
- Include actionable field diagnostics when retrying generated plans.

## [0.1.2]

### Fixed

- Preserve large JSONL events and continue draining subprocess output instead of losing
  transcripts or hanging when an event exceeds the stream buffer limit.
- Reap session processes on cancellation and callback failures, isolate startup failures,
  and retain failed outcomes even when a subprocess previously emitted a successful result.
- Persist cancelled sessions as stopped, clear completed process IDs, and report interrupted
  finalization as a failure requiring inspection of the worktree and remote.
- Reject escaping or overlapping identifiers, unresolvable templates, and out-of-range CLI
  concurrency before starting work, while preserving safe namespaced item keys.
- Surface Git staging/index and pull-request lookup failures instead of treating them as
  no changes or no existing pull request.
- Count pull-request creation as active work and report the installed release version
  consistently, using one version source for the package and build metadata. Version-only
  edits also invalidate uv's cached build metadata.

## [0.1.1] - 2026-09-01

### Changed

- Replaced pip installation instructions with isolated `uv tool` installs for cpmux,
  its voice extra, and editable source checkouts.
- Migrated development, CI, and release build tooling to uv and added a lockfile.

## [0.1.0] - 2026-07-15

First release published to PyPI.

### Added

- Pull requests opened by `cpmux up` now carry a title and description authored by the
  session from the changes it actually made, following the target repository's
  pull-request template when one exists. If a session produces none, cpmux falls back to
  the item name and a short summary of the prompt.
- `cpmux plan --voice` now shows a live transcript while you speak: a fast model streams
  partial text during recording, and the configured model produces the accurate final
  transcription when you stop.
- `cpmux rm --purge` to delete a run's on-disk history so it leaves `cpmux ls`.
- Preflight validation that each item's `paths` exist in its worktree, failing
  early with a clear error instead of a late `copilot` failure.
- `branch_template` documented in the voice-plan schema so a spoken branch scope
  maps to the branch, not `base`.

### Changed

- Renamed the project from `cmux` to `cpmux` (the `cmux` name was taken on PyPI):
  the command, package, `.cpmux/` state directory, `CPMUX_*` environment
  variables, and the default `cpmux/{slug}` branch prefix all change accordingly.
- `cpmux up` now runs in the background by default; pass `--foreground`/`-f` to stay
  attached and watch inline.
- Voice dictation now defaults to the `large-v3-turbo` model (was `base`) and enables
  VAD filtering, substantially improving transcription accuracy (first use downloads
  ~1.6 GB, cached afterward; override with `--transcribe-model`).
- Voice plan synthesis now instructs the model to preserve every dictated detail instead
  of producing a concise summary, so plans no longer drop tasks or constraints.

### Fixed

- Invalid plans are rejected up front with clear, traceback-free errors — duplicate ids,
  dependency cycles, unknown template placeholders, port overflows, and blank fields all
  report an actionable message instead of a stack trace.
- Crashed runs recover cleanly: orphaned sessions are reaped and marked failed, the run
  owner is cleared, and premium-request usage is surfaced in run summaries.
- Live views (`up --foreground`, `attach`) no longer corrupt the terminal when arrow
  keys or other input are pressed: keystroke echo is suppressed while a live view renders.
- A `--no-pr` item whose agent committed its own work now reports `done`
  (previously `no changes`).
- The dashboard follow-up now forwards each item's `env` overrides, matching
  `cpmux send`.

## [0.0.1]

Initial release.

### Added

- Declarative, guided multiplexer for GitHub Copilot CLI agents: one YAML plan
  (a shared system prompt plus a list of items) spawns one isolated headless
  `copilot` session per item, each in its own git worktree and branch.
- Commands: `init`, `up`, `plan`, `ls`, `attach`, `dash`, `logs`, `search`,
  `enter`, `send`, `kill`, `down`, `rm`.
- Interactive `dash` TUI, live `up` and `attach` monitoring, and `plan` for
  composing a plan from an editor, text, speech (`--voice`), or an audio file.
- On-device speech-to-text via faster-whisper behind the `voice` extra.

[Unreleased]: https://github.com/gugarosa/cpmux/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/gugarosa/cpmux/compare/v0.1.3...v0.2.0
[0.1.3]: https://github.com/gugarosa/cpmux/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/gugarosa/cpmux/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/gugarosa/cpmux/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/gugarosa/cpmux/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/gugarosa/cpmux/releases/tag/v0.0.1
