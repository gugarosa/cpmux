# cmux

**A declarative multiplexer for GitHub Copilot CLI agents: "tmuxinator for `copilot` sessions."**

Write one YAML file with a shared system prompt and a task list. cmux starts one headless
`copilot` session per task, each in its own git worktree and branch, and opens a draft PR.
Monitor and steer all sessions.

## Install

Requires Python ≥ 3.12 and the [`copilot`](https://docs.github.com/copilot/how-tos/copilot-cli),
`git`, and `gh` CLIs on your `PATH`.

```bash
pip install cmux
```

Or from source:

```bash
git clone https://github.com/gugarosa/cmux
cd cmux
pip install -e .
```

## Quickstart

From the root of the GitHub repository you want to change, create `cmux.yml` (or run
`cmux init` for a starter):

```yaml
system: |
  Make the smallest change that fully addresses the task.
  Follow the repository's conventions and add or update tests.

items:
  - Fix the broken install link in the README
  - name: pagination-regression
    prompt: Add a regression test for the pagination helper.
```

Preview the plan, start the sessions in the background, and watch the run:

```bash
cmux up --dry-run          # cmux.yml is the default
cmux up --detach --yes
cmux attach
```

By default, cmux opens one draft PR per item. Press Ctrl-C to stop watching without stopping
the run.

## The cmux file

A file has a shared `system` prompt, run-wide `defaults`, and `items`. Each item is either a
prompt string or a mapping:

```yaml
system: |
  Make the smallest change that fully fixes the issue, follow the surrounding
  conventions, and add or update a test.

defaults:
  model: gpt-5.5           # any `copilot --model` id
  effort: medium           # none | minimal | low | medium | high | xhigh | max
  permissions: edit        # readonly | edit | full (yolo)
  base: main               # branch PRs are opened against
  concurrency: 6           # max sessions running at once (1–64)
  deps: symlink            # seed a worktree's node_modules: symlink | copy | install | skip
  port_base: 3000          # give each item a unique port (3000, 3001, …) via $PORT
  pr:
    draft: true
    labels: [cmux]

items:
  - Fix the flaky login test              # bare string → key is the slug "fix-the-flaky-login-test"
  - Paginate the notifications list

  - name: dark-mode-contrast              # mapping → key is the slug of `name`
    prompt: Fix the dark-mode contrast on secondary buttons; it fails WCAG AA.
    model: claude-opus-4.8
    effort: high
    paths: [src/components/buttons]
    labels: [a11y]
    depends_on: [fix-the-flaky-login-test]
```

Item mappings accept `prompt`, `name`, `id`, `model`, `effort`, `permissions`, `base`,
`branch`, `labels`, `draft`, `paths`, `depends_on`, `env`, and `include_system`.

An item's **key** is its `id` when set, otherwise a slug of its `name` or `prompt`. Pass keys
to `enter`, `send`, `logs`, and `kill`; `cmux ls` and `--dry-run` print them. Any string field
expands `${VAR}` and `${VAR:-default}` from the environment. Set `include_system: false` to
omit the shared prompt for an item.

Set `port_base` when items run dev servers: each item gets `port_base + index` in its
environment (as `$PORT`, or `port_env` to rename it), so parallel servers do not collide. An
item's own `env` takes precedence, and `env` values reach the session's subprocess.

## Commands

Run-scoped commands accept `--run <id>` and default to the latest run.

| Group | Command | What it does |
|---|---|---|
| **Create** | `cmux init [FILE]` | Write a starter plan (defaults to `cmux.yml`). Flag: `--force/-f`. |
| | `cmux plan [FILE]` | Compose a plan in your editor, or from text, speech, or audio. Flags: `--text`, `--voice`, `--audio` (mutually exclusive), `--transcribe-model`, `--model`, `--force/-f`, `--up`, `--pr/--no-pr`, `--detach/-d`, `--yes/-y`. |
| **Launch** | `cmux up [FILE]` | Spawn one session per item (defaults to `cmux.yml`). Flags: `--dry-run`, `--detach/-d`, `--concurrency/-j`, `--pr/--no-pr`, `--deps`, `--strip-github-token/--no-strip-github-token`, `--yes/-y`. |
| **Monitor** | `cmux ls` | Snapshot each item's status, elapsed time, and activity. |
| | `cmux attach` | Live, read-only monitor; reconnects to a background run (Ctrl-C to detach). |
| | `cmux dash` | Interactive TUI: session list, live transcript, search. |
| | `cmux logs KEY` | Print a transcript; `--follow/-f` to stream, `--raw` for the JSONL. |
| | `cmux search QUERY` | Search across transcripts; `--all` for every run, `--regex`, `--fts` to rank via Copilot's index. |
| **Steer** | `cmux enter KEY` | Drop into an interactive copilot session, resumed in place. |
| | `cmux send KEY "…"` | Append a follow-up turn and print the reply. |
| | `cmux kill KEY` | Stop one running session. Flag: `--yes/-y`. |
| **Teardown** | `cmux down` | Stop a run's background daemon and any live sessions. Flag: `--yes/-y`. |
| | `cmux rm` | Remove the run's git worktrees. Flags: `--yes/-y`, `--force/-f` (delete uncommitted work). |

## Composing a plan

Compose a cmux file in your editor by default, or from text, speech, or audio:

```bash
cmux plan issues.yml                    # compose in $EDITOR → cmux file
cmux plan issues.yml --text "fix the flaky login test and paginate the notifications"
cmux plan issues.yml --voice            # record from the mic (Enter to stop) instead
cmux plan issues.yml --audio memo.wav   # transcribe an existing recording instead
cmux plan issues.yml --up               # generate and launch it
```

`cmux plan` opens your `$EDITOR` to describe the work (or takes `--text`), then asks `copilot`
to produce a validated cmux file. Add `--up` to launch it. With `--voice` or `--audio`,
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) transcribes speech on-device.
Audio stays local.

The `cmux[voice]` extra installs `sounddevice` and `faster-whisper`. `--text` and the editor
need neither:

```bash
pip install "cmux[voice]"
brew install portaudio     # macOS only: sounddevice needs PortAudio
```

The default transcription model is `base`. Select another faster-whisper model with
`--transcribe-model` (for example, `tiny`, `small`, `medium`, or `large-v3`). Models download
on first use and are cached; larger models are more accurate but slower.

## How it works

- **One item, one session.** Each task becomes a headless `copilot -p` run with a
  pre-assigned `--session-id`.
- **Separate worktrees.** Each session runs in its own `git worktree` on a `cmux/<slug>`
  branch off `origin/<base>`.
- **cmux owns delivery.** Sessions run with `git push` denied. cmux commits each worktree and
  opens one draft PR per item. With `--no-pr`, it commits locally and stops.
- **JSONL monitoring.** cmux reads copilot's `--output-format json` event stream and writes it
  to disk. Runs continue after detach and can be reattached. Crashed sessions resolve to a
  terminal state.

```
issues.yaml ──cmux up──►  session  fix-login-test    → worktree ─ branch ─ draft PR
   system:  …             session  paginate-list     → worktree ─ branch ─ draft PR
   items:   … ───────────►session  dark-mode-contrast→ worktree ─ branch ─ draft PR
                          session  …                    (parallel · isolated)
                                    │
                monitor and steer: cmux attach · dash · ls · logs · search
```

## What a run leaves on disk

cmux writes under a gitignored `.cmux/`:

```
.cmux/
  runs/<run_id>/
    manifest.json               resolved run config
    sessions/<key>/
      prompt.md                 the exact prompt sent (system + item)
      transcript.jsonl          raw tee of copilot --output-format json
      session.json              per-session record (status, branch, PR url, …)
  worktrees/<run_id>/<key>/     one git worktree per item
```

## Examples

See [`examples/minimal.yaml`](examples/minimal.yaml) and a twelve-issue frontend run
in [`examples/frontend.yaml`](examples/frontend.yaml).

## Development

```bash
pip install -e .
pytest
```

Conventions, architecture invariants, and roadmap live in [`CONVENTIONS.md`](CONVENTIONS.md).
