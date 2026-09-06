# Changelog

All notable changes to cpmux are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/gugarosa/cpmux/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/gugarosa/cpmux/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/gugarosa/cpmux/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/gugarosa/cpmux/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/gugarosa/cpmux/releases/tag/v0.0.1
