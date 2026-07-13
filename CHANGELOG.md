# Changelog

All notable changes to cmux are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/gugarosa/cmux/compare/v0.0.1...HEAD
[0.0.1]: https://github.com/gugarosa/cmux/releases/tag/v0.0.1
