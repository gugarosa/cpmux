# cpmux

**A declarative multiplexer for GitHub Copilot CLI agents: "tmuxinator for `copilot` sessions."**

Write one YAML file with a shared system prompt and a task list. cpmux starts one headless
`copilot` session per task, each in its own git worktree and branch, and opens a draft PR.
Monitor and steer all sessions.

## Install

Requires Python ≥ 3.12 and the [`copilot`](https://docs.github.com/copilot/how-tos/copilot-cli),
`git`, and `gh` CLIs on your `PATH`.

```bash
pip install git+https://github.com/gugarosa/cpmux
```

Or from source, for development:

```bash
git clone https://github.com/gugarosa/cpmux
cd cpmux
pip install -e .
```

## Quickstart

From the root of the GitHub repository you want to change, create `cpmux.yml` (or run
`cpmux init` for a starter):

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
cpmux up --dry-run         # preview the resolved plan
cpmux up --yes             # start the run in the background
cpmux attach               # watch it (Ctrl-C stops watching, not the run)
```

By default, `cpmux up` starts the run in the background and opens one draft PR per item.
Pass `--foreground` to stay attached and watch inline (Ctrl-C then stops the run).

## The cpmux file

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
  base: main               # branch to fork from and open PRs against
  branch_template: cpmux/{slug}  # each item's branch name; e.g. gderosa/{slug}
  concurrency: 6           # max sessions running at once (1–64)
  deps: symlink            # seed a worktree's node_modules: symlink | copy | install | skip
  port_base: 3000          # give each item a unique port (3000, 3001, …) via $PORT
  pr:
    draft: true
    labels: [cpmux]

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
to `enter`, `send`, `logs`, and `kill`; `cpmux ls` and `--dry-run` print them. Any string field
expands `${VAR}` and `${VAR:-default}` from the environment. Set `include_system: false` to
omit the shared prompt for an item.

Set `port_base` when items run dev servers: each item gets `port_base + index` in its
environment (as `$PORT`, or `port_env` to rename it), so parallel servers do not collide. An
item's own `env` takes precedence, and `env` values reach the session's subprocess.

## Commands

Run-scoped commands accept `--run <id>` and default to the latest run.

| Group | Command | What it does |
|---|---|---|
| **Create** | `cpmux init [FILE]` | Write a starter plan (defaults to `cpmux.yml`). Flag: `--force/-f`. |
| | `cpmux plan [FILE]` | Compose a plan in your editor, or from text, speech, or audio. Flags: `--text`, `--voice`, `--audio` (mutually exclusive), `--transcribe-model`, `--model`, `--force/-f`, `--up`, `--pr/--no-pr`, `--detach/-d`, `--yes/-y`. |
| **Launch** | `cpmux up [FILE]` | Spawn one session per item (defaults to `cpmux.yml`). Flags: `--dry-run`, `--detach/--foreground` (background by default), `--concurrency/-j`, `--pr/--no-pr`, `--deps`, `--strip-github-token/--no-strip-github-token`, `--yes/-y`. |
| **Monitor** | `cpmux ls` | Snapshot each item's status, elapsed time, and activity. |
| | `cpmux attach` | Live, read-only monitor; reconnects to a background run (Ctrl-C to detach). |
| | `cpmux dash` | Interactive TUI: session list, live transcript, search. |
| | `cpmux logs KEY` | Print a transcript; `--follow/-f` to stream, `--raw` for the JSONL. |
| | `cpmux search QUERY` | Search across transcripts; `--all` for every run, `--regex`, `--fts` to rank via Copilot's index. |
| **Steer** | `cpmux enter KEY` | Drop into an interactive copilot session, resumed in place. |
| | `cpmux send KEY "…"` | Append a follow-up turn and print the reply. |
| | `cpmux kill KEY` | Stop one running session. Flag: `--yes/-y`. |
| **Teardown** | `cpmux down` | Stop a run's background daemon and any live sessions. Flag: `--yes/-y`. |
| | `cpmux rm` | Remove the run's git worktrees. Flags: `--yes/-y`, `--force/-f` (delete uncommitted work), `--purge` (also delete run history). |

## Composing a plan

Compose a cpmux file in your editor by default, or from text, speech, or audio:

```bash
cpmux plan issues.yml                    # compose in $EDITOR → cpmux file
cpmux plan issues.yml --text "fix the flaky login test and paginate the notifications"
cpmux plan issues.yml --voice            # record from the mic (Enter to stop) instead
cpmux plan issues.yml --audio memo.wav   # transcribe an existing recording instead
cpmux plan issues.yml --up               # generate and launch it
```

`cpmux plan` opens your `$EDITOR` to describe the work (or takes `--text`), then asks `copilot`
to produce a validated cpmux file. Add `--up` to launch it. With `--voice` or `--audio`,
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) transcribes speech on-device.
Audio stays local. `--voice` shows a live transcript as you speak (a fast model streams
partials while recording; your chosen model produces the accurate final text on stop).

The `cpmux[voice]` extra installs `sounddevice` and `faster-whisper`. `--text` and the editor
need neither:

```bash
pip install "cpmux[voice] @ git+https://github.com/gugarosa/cpmux"
brew install portaudio     # macOS only: sounddevice needs PortAudio
```

The default transcription model is `large-v3-turbo` (near-`large-v3` accuracy, much faster
decoding). It downloads on first use (~1.6 GB) and is cached. Pick a lighter one with
`--transcribe-model` (for example, `small`, `distil-large-v3`, or `base`); larger models are
more accurate but slower on CPU.

## How it works

- **One item, one session.** Each task becomes a headless `copilot -p` run with a
  pre-assigned `--session-id`.
- **Separate worktrees.** Each session runs in its own `git worktree` on a `cpmux/<slug>`
  branch off `origin/<base>`.
- **cpmux owns delivery.** Sessions run with `git push` denied. cpmux commits each worktree and
  opens one draft PR per item. With `--no-pr`, it commits locally and stops.
- **JSONL monitoring.** cpmux reads copilot's `--output-format json` event stream and writes it
  to disk. Runs continue after detach and can be reattached. Crashed sessions resolve to a
  terminal state.

```
issues.yaml ──cpmux up──►  session  fix-login-test    → worktree ─ branch ─ draft PR
   system:  …             session  paginate-list     → worktree ─ branch ─ draft PR
   items:   … ───────────►session  dark-mode-contrast→ worktree ─ branch ─ draft PR
                          session  …                    (parallel · isolated)
                                    │
                monitor and steer: cpmux attach · dash · ls · logs · search
```

## What a run leaves on disk

cpmux writes under a gitignored `.cpmux/`:

```
.cpmux/
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
