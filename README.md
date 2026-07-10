# cmux

**A declarative multiplexer for GitHub Copilot CLI agents: "tmuxinator for `copilot` sessions."**

Write one YAML file with a shared system prompt and a task list. cmux starts one headless
`copilot` session per task, each in its own git worktree and branch, and opens a draft PR.
Monitor and steer every session from one place.

## Install

Requires Python ≥ 3.12 and the [`copilot`](https://docs.github.com/copilot/how-tos/copilot-cli),
`git`, and `gh` CLIs on your `PATH`.

```bash
git clone https://github.com/gugarosa/cmux
cd cmux
pip install -e .
```

## Quickstart

From the root of the GitHub repository you want to change, create `cmux.yml`:

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
cmux up cmux.yml --dry-run
cmux up cmux.yml --detach --yes
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

Every read/monitor command accepts `--run <id>` and defaults to the latest run.

| Group | Command | What it does |
|---|---|---|
| **Compose** | `cmux plan [FILE]` | Compose a cmux file in your editor, or from text, speech, or audio. Flags: `--text`, `--voice`, `--audio`, `--transcribe-model`, `--endpoint`, `--model`, `--up`, `--pr/--no-pr`, `--detach/-d`, `--yes/-y`. |
| **Launch** | `cmux up FILE` | Spawn one session per item. Flags: `--dry-run`, `--detach/-d`, `--concurrency/-j`, `--pr/--no-pr`, `--deps`, `--yes/-y`. |
| **Monitor** | `cmux ls` | Snapshot each item's status. |
| | `cmux attach` | Live, read-only monitor; reconnects to a background run (Ctrl-C to detach). |
| | `cmux dash` | Interactive TUI: session list, live transcript, search. |
| | `cmux logs KEY` | Print a transcript; `--follow/-f` to stream, `--raw` for the JSONL. |
| | `cmux search QUERY` | Full-text search across transcripts; `--all` for every run, `--regex`, `--fts` to rank via copilot's index. |
| **Steer** | `cmux enter KEY` | Drop into an interactive copilot session, resumed in place. |
| | `cmux send KEY "…"` | Append a follow-up turn and print the reply. |
| | `cmux kill KEY` | Stop one running session. |
| **Teardown** | `cmux down` | Stop a run's background daemon and any live sessions. |
| | `cmux rm` | Remove the run's git worktrees. |

## Composing a plan

Compose a cmux file in your editor by default, or from text, speech, or audio:

```bash
cmux plan issues.yml                    # compose in $EDITOR → cmux file
cmux plan issues.yml --text "fix the flaky login test and paginate the notifications"
cmux plan issues.yml --voice            # record from the mic (Enter to stop) instead
cmux plan issues.yml --audio memo.wav   # transcribe an existing recording instead
cmux plan issues.yml --up               # …and launch it straight away
```

`cmux plan` opens your `$EDITOR` to describe the work (or takes `--text`), asks `copilot` to
synthesize a plan, validates it, and writes the file. Add `--up` to launch it. With `--voice`
or `--audio`, speech-to-text runs through **Foundry Local**, the same on-device engine
Copilot's `/voice` uses, so audio stays on your machine.

For `--voice` or `--audio`, install the optional extra and pull a Whisper model once:

```bash
pip install "cmux[voice]"     # sounddevice + foundry-local-sdk
foundry model run whisper-large-v3   # one-time model download
```

Override the OpenAI-compatible `/audio/transcriptions` endpoint with
`--endpoint`/`CMUX_FOUNDRY_ENDPOINT`.

## How it works

- **One item, one session.** Each task becomes a headless `copilot -p` run with a
  pre-assigned `--session-id`.
- **Separate worktrees.** Each session runs in its own `git worktree` on a `cmux/<slug>`
  branch off `origin/<base>`.
- **cmux owns delivery.** Sessions run with `git push` denied. cmux commits each worktree and
  opens one draft PR per item. With `--no-pr`, it commits locally and stops.
- **JSONL monitoring.** cmux reads copilot's `--output-format json` event stream and writes it
  to disk. Runs survive detach and reconnect, and crashed sessions resolve to a terminal state.

```
issues.yaml ──cmux up──►  session  fix-login-test    → worktree ─ branch ─ draft PR
   system:  …             session  paginate-list     → worktree ─ branch ─ draft PR
   items:   … ───────────►session  dark-mode-contrast→ worktree ─ branch ─ draft PR
                          session  …                    (parallel · isolated)
                                    │
                cmux attach · dash · ls · logs · search — one place to watch and steer
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

See [`examples/minimal.yaml`](examples/minimal.yaml) and a realistic twelve-issue frontend run
in [`examples/frontend.yaml`](examples/frontend.yaml).

## Development

```bash
pip install -e .
pytest
```

Conventions, architecture invariants, and roadmap live in [`CONVENTIONS.md`](CONVENTIONS.md).
