# Changelog

All notable changes to cpmux are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `cpmux plan --voice` now shows a live transcript while you speak: a fast model streams
  partial text during recording, and the configured model produces the accurate final
  transcription when you stop.

### Changed

- `cpmux up` now runs in the background by default; pass `--foreground`/`-f` to stay
  attached and watch inline.
- Voice dictation now defaults to the `large-v3-turbo` model (was `base`) and enables
  VAD filtering, substantially improving transcription accuracy (first use downloads
  ~1.6 GB, cached afterward; override with `--transcribe-model`).
- Voice plan synthesis now instructs the model to preserve every dictated detail instead
  of producing a concise summary, so plans no longer drop tasks or constraints.

### Fixed

- Live views (`up --foreground`, `attach`) no longer corrupt the terminal when arrow
  keys or other input are pressed: keystroke echo is suppressed while a live view renders.

## [0.1.0]

### Changed

- Renamed the project from `cmux` to `cpmux` (the `cmux` name was taken on PyPI):
  the command, package, `.cpmux/` state directory, `CPMUX_*` environment
  variables, and the default `cpmux/{slug}` branch prefix all change accordingly.

### Added

- `cpmux rm --purge` to delete a run's on-disk history so it leaves `cpmux ls`.
- Preflight validation that each item's `paths` exist in its worktree, failing
  early with a clear error instead of a late `copilot` failure.
- `branch_template` documented in the voice-plan schema so a spoken branch scope
  maps to the branch, not `base`.

### Fixed

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

[Unreleased]: https://github.com/gugarosa/cpmux/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/gugarosa/cpmux/compare/v0.0.1...v0.1.0
[0.0.1]: https://github.com/gugarosa/cpmux/releases/tag/v0.0.1
