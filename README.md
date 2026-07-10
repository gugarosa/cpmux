# cmux

**A declarative multiplexer for GitHub Copilot CLI agents: "tmuxinator for `copilot` sessions."**

Write one YAML file with a shared system prompt and task list. cmux spawns one headless
`copilot` session per task, each in its own git worktree and branch, and opens a draft PR.
Use cmux to watch, search, and steer the run.

## Why

Hand one agent a backlog of 12–15 issues and its context mixes tasks. cmux gives each task
an isolated session under a shared system prompt. Tasks run in parallel, do not share a
working tree, and each gets a self-contained PR. Each session starts clean and stays focused
on one task.

## How it works

- **One item, one session.** Each task becomes a headless `copilot -p` run with a
  pre-assigned `--session-id`, so it stays addressable for status, resume, and recovery.
- **Separate worktrees.** Every session runs in its own `git worktree` on a `cmux/<slug>`
  branch off `origin/<base>`; sessions do not share a working tree.
- **Agents edit, cmux ships.** Sessions run with `git push` denied. cmux, not the agent,
  commits each worktree and opens one draft PR per item. With `--no-pr`, it commits locally
  and stops.
- **Monitored over JSONL, not a terminal.** State is reduced from copilot's
  `--output-format json` event stream, tee'd to disk. A run survives detach/reconnect, and
  a crashed session reconciles to a terminal state instead of hanging.

```
issues.yaml ──cmux up──►  session  fix-login-test    → worktree ─ branch ─ draft PR
   system:  …             session  paginate-list     → worktree ─ branch ─ draft PR
   items:   … ───────────►session  dark-mode-contrast→ worktree ─ branch ─ draft PR
                          session  …                    (parallel · isolated)
                                    │
                cmux attach · dash · ls · logs · search — one place to watch and steer
```

## Install

```bash
git clone https://github.com/gugarosa/cmux
cd cmux
pip install -e .
```

Requires Python ≥ 3.12 and the [`copilot`](https://docs.github.com/copilot/how-tos/copilot-cli),
`git`, and `gh` CLIs on your `PATH`.

## Quickstart

```bash
cmux up issues.yaml --dry-run   # resolve and preview: item keys, models, branches, spawn commands
cmux up issues.yaml --detach    # spawn the fleet in the background, return immediately
cmux ls                         # snapshot each item's status (this is where item keys are shown)
cmux attach                     # live, read-only monitor; reconnects to a background run
cmux logs fix-login-test -f     # stream one session's transcript
```

## The cmux file

A run has a shared `system` prompt, run-wide `defaults`, and `items`. Each item is either a
bare prompt string or a mapping of per-item overrides:

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

Each item's **key** is its `id` when set, otherwise a slug of its `name` or `prompt`. Pass
keys to `enter`, `send`, `logs`, and `kill`; `cmux ls` and `--dry-run` print them. Any string
field expands `${VAR}` and `${VAR:-default}` from the environment. An item may set
`include_system: false` to opt out of the shared prompt.

**Item overrides:** `name`, `id`, `model`, `effort`, `permissions`, `base`, `branch`, `labels`,
`draft`, `paths` (extra directories the session may read), `depends_on` (keys that must finish
first), `env`, and `include_system`.

## Dictating a plan

You can dictate the file:

```bash
cmux voice issues.yml            # record from the mic (Enter to stop) → cmux file
cmux voice issues.yml --up       # …and launch it straight away
cmux voice issues.yml --audio memo.wav   # transcribe an existing recording instead
cmux voice issues.yml --text "fix the flaky login test and paginate the notifications"
```

`cmux voice` transcribes speech, asks `copilot` to synthesize a plan, validates it against
the schema above, and writes the file. Add `--up` to launch it. Speech-to-text runs through
**Foundry Local**, the same on-device engine Copilot's own `/voice` uses, so audio stays on
your machine. Install the optional extra and pull a Whisper model once:

```bash
pip install "cmux[voice]"     # sounddevice + foundry-local-sdk
foundry model run whisper-large-v3   # one-time model download
```

Point to another OpenAI-compatible `/audio/transcriptions` server with
`--endpoint`/`CMUX_FOUNDRY_ENDPOINT`, or skip audio with `--text`.

## Commands

Every read/monitor command accepts `--run <id>` and defaults to the latest run.

| Group | Command | What it does |
|---|---|---|
| **Compose** | `cmux voice [FILE]` | Dictate tasks into a cmux file. Flags: `--text`, `--audio`, `--transcribe-model`, `--endpoint`, `--model`, `--up`, `--pr/--no-pr`, `--detach/-d`, `--yes/-y`. |
| **Launch** | `cmux up FILE` | Spawn one session per item. Flags: `--dry-run`, `--detach/-d`, `--concurrency/-j`, `--pr/--no-pr`, `--deps`, `--yes/-y`. |
| **Monitor** | `cmux ls` | Snapshot each item's status. |
| | `cmux attach` | Live, read-only monitor; reconnects to a background run (Ctrl-C to detach). |
| | `cmux dash` | Interactive TUI: session list, live transcript, search. |
| | `cmux logs KEY` | Print a transcript; `--follow/-f` to stream, `--raw` for the JSONL. |
| | `cmux search QUERY` | Full-text search across transcripts; `--all` for every run, `--regex`. |
| **Steer** | `cmux enter KEY` | Drop into an interactive copilot session, resumed in place. |
| | `cmux send KEY "…"` | Append a follow-up turn and print the reply. |
| | `cmux kill KEY` | Stop one running session. |
| **Teardown** | `cmux down` | Stop a run's background daemon and any live sessions. |
| | `cmux rm` | Remove the run's git worktrees. |

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
