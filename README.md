# cpmux

[![PyPI](https://img.shields.io/pypi/v/cpmux.svg)](https://pypi.org/project/cpmux/)
[![Python](https://img.shields.io/pypi/pyversions/cpmux.svg)](https://pypi.org/project/cpmux/)
[![CI](https://github.com/gugarosa/cpmux/actions/workflows/ci.yml/badge.svg)](https://github.com/gugarosa/cpmux/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**A declarative multiplexer for GitHub Copilot CLI agents: "tmuxinator for `copilot` sessions."**

Write one YAML file with a shared system prompt and a task list. cpmux starts one headless
`copilot` session per task, each in its own git worktree and branch, and opens a draft PR.
Monitor and steer all sessions, verify exact candidate commits, and recover selected work
without replaying successful items.

## Install

Requires macOS or Linux, Python ≥ 3.12, [`uv`](https://docs.astral.sh/uv/), and the
[`copilot`](https://docs.github.com/copilot/how-tos/copilot-cli), `git`, and `gh` CLIs on your
`PATH`.

```bash
uv tool install cpmux
```

Or from source, for development:

```bash
git clone https://github.com/gugarosa/cpmux
cd cpmux
uv tool install --editable .
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

Each PR's title and description are written by the session itself from the changes it made,
following the target repository's pull-request template when one exists. If a session
doesn't produce one, cpmux falls back to the item name and prompt.

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
  remote: origin           # git remote to push branches and open PRs on
  branch_template: cpmux/{slug}  # each item's branch name; {slug} or {id}, e.g. gderosa/{slug}
  concurrency: 6           # max sessions running at once (1–64)
  deps: symlink            # seed a worktree's node_modules: symlink | copy | install | skip
  port_base: 3000          # give each item a unique port (3000, 3001, …) via $PORT
  port_env: PORT           # rename the port variable (default $PORT)
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
`branch`, `labels`, `draft`, `paths`, `depends_on`, `base_from`, `env`, `include_system`,
`profile`, `setup`, `checks`, `timeout_seconds`, and imported issue `source` metadata.

The `pr` block also accepts `title_template` and `body_template` — the fallback title and body
used when a session writes no `.cpmux-pr.md`; both expand `{name}`, `{slug}`, and `{prompt}`,
while `branch_template` expands `{slug}` and `{id}`. `permissions` may be a bare preset or a
mapping that extends one —
`{preset: edit, allow: [...], deny: [...], add_dir: [...], allow_url: [...]}` — adding
`copilot` tool and network rules. Only `full`/`yolo` let the agent run `git push` itself; the
other presets keep push denied so cpmux owns delivery. An optional top-level `version` selects
the schema (currently only `1`).

An item's **key** is its `id` when set, otherwise a slug of its `name` or `prompt`. Pass keys
to `enter`, `send`, `logs`, and `kill`; `cpmux ls` and `--dry-run` print them. Any string field
expands `${VAR}` and `${VAR:-default}` from the environment once. Escape a reference as
`$${VAR}` to keep the literal `${VAR}` for the agent or a shell command; substituted values
are never expanded a second time. Set `include_system: false` to omit the shared prompt.

Identifiers must be normalized relative names, without absolute paths, `.`/`..` components,
or aliases such as `team//task`. Safe namespaced item keys such as `frontend/login` remain
valid, but an item's key cannot be a parent of another item's key: their worktrees must
not overlap. Run IDs passed to `--run` follow the same relative-path rules.

Set `port_base` when items run dev servers: each item gets `port_base + index` in its
environment (as `$PORT`, or `port_env` to rename it), so parallel servers do not collide. An
item's own `env` takes precedence, and `env` values reach the session's subprocess.

## Setup profiles and verified delivery

Declare the commands a worktree needs and the checks required before cpmux delivers
its changes. Named profiles can be reused across items:

```yaml
profiles:
  python:
    setup:
      - name: dependencies
        command: uv sync --locked --extra dev
        timeout_seconds: 180
    checks:
      - name: tests
        command: uv run pytest -q
        timeout_seconds: 300

defaults:
  profile: python
  timeout_seconds: 1800

items:
  - Fix the pagination regression
```

Commands may also be strings, such as `checks: ["uv run pytest -q"]`. Their default
timeout is 300 seconds. For setup and checks, explicit item values override the
selected profile, whose omitted fields inherit run defaults. An explicit empty list
disables inherited commands for that item.
The existing `deps: install` Node-lockfile convenience also runs as an owned setup
command (300-second timeout); a failed installation now prevents agent launch.
`timeout_seconds` limits each automated agent turn, including headless follow-ups,
not the entire run. Native interactive `enter` remains user-controlled.

Setup must succeed before the agent starts. After the agent finishes, cpmux commits a
local candidate, runs its configured checks, and records command outcomes and the exact
checked commit. Failed checks, changed source, or a moved HEAD prevent cpmux from pushing
that candidate or opening a PR. Ignored test/build outputs are allowed. A successful
receipt records passing commands with matching HEAD and non-ignored source before and
afterward, not a hermetic or tamper-proof build. Tool versions, inherited environment,
ignored files and external services are outside its fingerprint; passing checks do not
establish complete correctness.

These are trusted POSIX shell commands running with your account and item environment,
outside the agent's permission presets. Review the plan before launching it. Worktrees
and permission presets are not operating-system sandboxes. Commands must finish their
work in the foreground; cpmux stops and fails commands that leave background children.
No checks configured means **not configured**, never an implicit verification pass.

`cpmux report` shows attempts, candidate commits, check status, and delivery. Add `--json`
for a versioned, read-only report. Dedicated prompt, environment, raw-command, and
complete-log fields are omitted. Arbitrary error text can contain command output or
secrets; reports are not automatically redacted, so inspect them before sharing.
Source edits after verification make its receipt stale.

## Recover, review, and deliver

Reconnecting is different from rerunning:

```bash
cpmux attach                          # only watch the existing run
cpmux retry                           # retry failed/blocked/stopped/unstarted items
cpmux retry pagination --resume        # explicitly continue its observed native session
cpmux retry pagination --fresh         # new conversation and setup, retaining Git edits
cpmux retry pagination --detach --yes  # recover in the background
```

An ordinary retry reuses successful agent work after a check or delivery failure. It
rechecks/delivers the candidate without another agent turn. If the agent itself failed,
retry starts a new conversation with the original task. All modes retain worktrees,
commits, PR references, attempts, and reported usage; none resets or rebases your edits.
Use explicit item keys to rerun successful work. A missing previously created worktree
must be restored rather than silently recreated from an old base.
Runs use their saved task/setup/check configuration; editing the original YAML affects
new runs, not an existing retry. The soft admission budget can be revised explicitly
with `retry --budget`.

Review and repair are explicit operations:

```bash
cpmux diff pagination                 # diff plus a revision token
cpmux feedback pagination "Fix the boundary case" --revision TOKEN
cpmux verify pagination               # commit/check locally, never push
cpmux finalize pagination             # check and deliver according to the run's --pr/--no-pr setting
```

Feedback refuses a changed revision under the same lease used by the agent turn. Follow-up
and interactive turns invalidate prior verification and do not automatically push.
Finalization may reuse a still-current successful receipt; an existing PR must still
be open and match the intended repository and branches before it can be updated.
`verify` always runs the configured checks again. PR updates confirm the delivered
head commit and refresh only cpmux's verification section, preserving human-written
title and description text. PR creation and updates target the configured remote's
repository; cross-repository PR heads are not adopted.
Git's effective push URL determines that repository, including `remote.<name>.pushurl`
and URL rewriting. A remote with multiple push destinations is rejected.

## Ordering and stacked changes

`depends_on` waits for successful predecessors but still branches independently.
Use `base_from` when a task actually needs another item's completed code:

```yaml
items:
  - id: api
    prompt: Add the pagination API.
  - id: client
    prompt: Add the client using the pagination API.
    base_from: api
  - id: documentation
    prompt: Document the existing architecture independently.
    depends_on: [api]
```

The child's worktree is created only after its predecessor succeeds, at the predecessor's
recorded candidate commit. Its PR targets that predecessor's branch; a no-change parent
uses its original base instead. Failed parents block their children without creating
misleading worktrees. Ordering and inheritance edges share cycle validation.
Only one code predecessor is supported: there is no automatic multi-parent merge,
rebase, or retargeting of already-created children when a parent later changes.

## Queue and resource visibility

`cpmux pause` stops admitting queued items while active agents/checks finish.
`cpmux unpause` resumes admission in a running supervisor, not an idle run.
`defaults.premium_budget: 40` sets a **soft** run admission ceiling, based on reported
premium requests. In-flight sessions and unknown usage can exceed it; it is not a
hard spending cap or a dollar estimate. Once reached, unstarted agent work is blocked.
Use `cpmux retry --budget 60` to increase the ceiling. Check/delivery-only recovery and
explicit follow-ups do not require new automated-agent admission.

`cpmux report --json` reports known usage alongside missing-usage indicators, including
follow-ups whose earlier attempts had known usage. Child RSS, when available, describes
only the identified child process, not its descendants or a whole-machine memory limit.

`cpmux wait` exits with `0` for complete success, `1` for a terminal failure, `2` for
unfinished work without a live owner, or `124` for `--timeout SECONDS`. Waiting does not
cancel healthy work. Add `--json` for the final report or `--notify` for one opt-in terminal
bell on completion.

### Machine-readable reports

`report --json` and `wait --json` use report schema version `1`. The report reads
persisted state and inspects source/process identity; it does not reconcile or mutate
the run. The `wait` command itself may reconcile abandoned work before producing it.

| Surface | Fields |
|---|---|
| Run identity and state | `schema_version`, `run_id`, `managed`, `owner_error`, `paused`, `expected_items`, `recorded_items` |
| Admission and usage | `premium_budget`, `budget_reached`, `reported_premium_requests`, `items_with_unknown_usage` |
| Each `items` entry | `key`, `name`, `status`, `phase`, `attention`, `error`, `elapsed_seconds`, `last_activity_at` |
| Item source and delivery | `branch`, `base`, `base_sha`, `base_from`, `depends_on`, `source`, `candidate_sha`, `delivery_sha`, `pr_url`, `verification` |
| Item resource/history data | `reported_premium_requests`, `usage_incomplete`, `process`, `attempts` |

`verification.status` is `not_configured`, `missing`, `running`, `failed`, `passed`,
or `stale`. A receipt also identifies its attempt, commit, Git tree, configured-check
fingerprint and completion time. `source` contains issue metadata, never its body.
Each attempt records its mode, phase, outcome, timestamps, exit/error, candidate,
whether an agent started, reported usage, and setup/check command results. Command
results include display name, phase, status, exit code, duration, log path and error,
not the raw command or complete log.

`null` usage means unreported, not zero. A known cumulative amount can still have
`usage_incomplete: true` after a later unreported turn. `managed: null` with
`owner_error` means ownership could not be established, not that the run is stopped.

## Dashboard

`cpmux dash` orders failures and stale/unverified work before completed work. The queue
shows the real outcome, attempt and reported usage; the selected item's header adds
activity and source/delivery context. Below 84 columns, Enter opens a full-width detail
view and Escape returns to the queue.

| Keys | Action |
|---|---|
| `1` / `2` / `3` / `4` | All / attention / ready for review / failed views |
| `j` / `k` or arrows in the queue | Move through tasks |
| `t` / `d` / `c` / `i` | Transcript / immutable diff / checks and attempt logs / details |
| Enter / Escape | Focus the detail pane / return focus to the queue |
| Page Up / Page Down | Scroll the focused transcript, diff, checks or details |
| `s` | Send a follow-up; in Diff, send feedback bound to the displayed revision |
| `v` / `f` / `u` | Re-run checks / finalize / update an existing PR |
| `p` / `x` / `r` | Pause or unpause admission / stop the item / refresh the report |
| `e` / `o` / `/` | Enter native Copilot / open the PR / search transcripts |
| `q` | Close the dashboard after cleaning up its own operations |

Run-state loading and Git inspection run off the UI event loop. Source inspections
are throttled to five seconds between completed reports unless lifecycle changes or
`r` request an earlier refresh; slow I/O can delay a report without blocking navigation.
`d` explicitly loads a new immutable diff. Scrolling is retained across refreshes, and
transcripts are read incrementally rather than reread in full for every update.

Closing a monitor leaves a healthy external background run alone. Follow-ups and
verification/finalization started **inside this dashboard** are owned by it: `q` cancels
and waits for their cleanup, including outstanding Git work, before exiting. Failures
are shown as failures, and missing verification or usage is never displayed as a pass
or fabricated zero. Conflicting write actions are disabled while the supervisor owns
the run; engine leases still recheck ownership when an operation actually starts.

## Commands

Run-scoped commands accept `--run <id>` and default to the latest run.

| Group | Command | What it does |
|---|---|---|
| **Create** | `cpmux init [FILE]` | Write a starter plan (defaults to `cpmux.yml`). Flag: `--force/-f`. |
| | `cpmux plan [FILE]` | Compose a plan in your editor, or from text, speech, or audio. Flags: `--text`, `--voice`, `--audio` (mutually exclusive), `--transcribe-model`, `--model`, `--force/-f`, `--up`, `--pr/--no-pr`, `--detach/--foreground/-d`, `--yes/-y`. |
| | `cpmux issues [REFS...]` | Import issue numbers or URLs into an editable plan without running an agent. Flags: `--query`, `--repo`, `--limit`, `--template`, `--profile`, `--output`, `--force`. |
| **Launch** | `cpmux up [FILE]` | Spawn one session per item (defaults to `cpmux.yml`). Flags: `--dry-run`, `--detach/--foreground/-d/-f` (background by default), `--concurrency/-j`, `--pr/--no-pr`, `--deps`, `--strip-github-token/--no-strip-github-token`, `--yes/-y`. |
| | `cpmux retry [KEYS...]` | Selectively recover work. Flags: `--resume`, `--fresh`, `--budget`, `--detach/-d`, `--yes/-y`. Foreground by default. |
| **Monitor** | `cpmux ls` | Snapshot each item's status, elapsed time, and activity. |
| | `cpmux report` | Read-only attempt, verification, and delivery summary. Flag: `--json`. |
| | `cpmux attach` | Live status monitor; reconnects to a background run (Ctrl-C to detach). |
| | `cpmux dash` | Attention-first TUI with transcript, diff, checks and details. |
| | `cpmux wait` | Wait for completion with useful exit codes. Flags: `--timeout`, `--json`, `--notify`. |
| | `cpmux logs KEY` | Print a transcript; `--follow/-f` to stream, `--raw` for the JSONL. |
| | `cpmux search QUERY` | Search across transcripts; `--all` for every run, `--regex`, `--fts` to rank via Copilot's index. |
| **Steer** | `cpmux enter KEY` | Drop into an interactive copilot session, resumed in place. |
| | `cpmux send KEY "…"` | Append a follow-up turn and print the reply. |
| | `cpmux diff KEY` | Show the current change and its revision token. Flag: `--json`. |
| | `cpmux feedback KEY "…" --revision TOKEN` | Send feedback only if the reviewed source is still current. Optional context: `--file`, `--line`. |
| | `cpmux verify KEY` | Commit/check a candidate locally without pushing it. |
| | `cpmux finalize KEY` | Explicitly deliver or update the matching open PR, respecting `--no-pr`. |
| | `cpmux pause` / `cpmux unpause` | Control admission, leaving active work untouched. |
| | `cpmux kill KEY` | Stop one running session. Flag: `--yes/-y`. |
| **Teardown** | `cpmux down` | Stop a run's background daemon and any live sessions. Flag: `--yes/-y`. |
| | `cpmux rm` | Remove the run's git worktrees. Flags: `--yes/-y`, `--force/-f` (delete uncommitted work), `--purge` (also delete run history). |

`ls`, `attach`, `wait`, `retry`, and `dash` reconcile crashed runs: they may stop orphaned session
processes and persist failed records. Detaching a monitor does not stop a healthy
managed run.

Mutating operations use run/session leases. A running supervisor excludes follow-up and
interactive writers; independent sessions in an idle run may be operated separately.
Conflicting writers fail clearly instead of racing the same native session or record.
Cancellation targets PID-and-creation-time identities, not a reused PID from old history.
Unverifiable live process identities remain unresolved and require manual inspection;
cpmux neither signals an unknown process nor clears its identity to admit another writer.
Cancellation waits for in-flight Git finalization instead of releasing its lease while
a Git operation still runs. `down` may report that cancellation is queued rather than
forcibly abandoning those operations and their possible remote effects.
Subprocesses retain the underlying lease descriptors if their controller crashes;
reconciliation cannot admit another writer until those jobs finish or are stopped.

### Upgrading to 0.2

Finish or stop pre-0.2 runs with the version that started them before upgrading, then
start a new plan. PID-only owner metadata is deliberately rejected, not converted.
The superseded PID-only Python helpers are removed; ownership uses one creation-time
identity API, with no old/new execution path or compatibility wrapper.
The engine's interactive operation is async, like follow-up and review operations;
there is no separate synchronous compatibility implementation.

## GitHub issue intake

```bash
cpmux issues 42 57 --output issues.yml
cpmux issues --repo owner/repository --query "is:open label:ready" --limit 10
cpmux issues 42 --template team-plan.yml --profile python
```

Issue intake is read-only: it never comments, assigns, closes, or runs an issue.
Numbers and canonical issue URLs must belong to one repository. The configured `gh`
host and authentication are respected. A generated plan keeps stable issue IDs, URLs,
and source title/labels/update-time snapshots. Review the issue text and the editable
plan before executing it; imported text is not trusted instruction or shell configuration.
Literal `${...}` content is escaped rather than expanded into local environment values.

A template contributes its system prompt, defaults and profiles, **not its old items**.
Imports do not silently mix previously queued tasks into the new batch.
Issue plans use `cpmux/{id}` branches unless the template explicitly supplies a branch
pattern, so repeated issue titles remain distinct. Branch collisions in any plan are
rejected before worktree creation.
Queries are used as written: include `is:open` or `is:closed` to restrict state. An explicit
selection larger than `--limit` is rejected rather than truncated. Enterprise hosts can
come from the current repository, `GH_HOST`, or `--repo HOST/OWNER/REPOSITORY`.

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
uv tool install "cpmux[voice]"
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
      copilot-logs/             copilot's own --log-dir
      attempts/<number>/        setup/check logs and source-bound verification receipt
  worktrees/<run_id>/<key>/     one git worktree per item
```

The local Git exclude file protects `.cpmux/` when the repository does not already ignore
it; cpmux does not modify the tracked `.gitignore` for this.

## Examples

See [`examples/minimal.yaml`](examples/minimal.yaml) and a twelve-issue frontend run
in [`examples/frontend.yaml`](examples/frontend.yaml). [`examples/verified-python.yaml`](examples/verified-python.yaml)
combines an explicit Python profile, soft usage limit and a two-item code stack.

## Development

```bash
uv sync --extra dev
uv run pytest
```

Conventions, architecture invariants, and implementation boundaries live in [`CONVENTIONS.md`](CONVENTIONS.md).
