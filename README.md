# cmux

**A declarative, guided multiplexer for GitHub Copilot CLI agents — "tmuxinator for `copilot` sessions."**

You write one YAML file: a shared **system prompt** plus a list of **items** (tasks).
`cmux` spawns one isolated headless `copilot` session per item — each in its own **git
worktree**, on its own **branch**, opening its own **draft PR** — and gives you one place to
watch, search, and steer all of them.

> Why: when you hand a single agent 12–15 issues, it loses context between them. `cmux` gives
> every issue its own session so nothing bleeds together, and you supervise the whole fleet at
> once. Token cost and context duplication are the price of isolation — and that's the point.

## Status

**v0 (walking skeleton).** Working today: `up` (spawn a run), `ls` (status), `logs` (a
transcript), `rm` (clean up worktrees), plus `--dry-run`. The background daemon and the
interactive Textual dashboard (`attach` / `enter` / `search`) are on the roadmap.

## Install

```bash
git clone https://github.com/gugarosa/cmux
cd cmux
pip install -e .
```

Requires Python ≥ 3.11 and the [`copilot`](https://docs.github.com/copilot/how-tos/copilot-cli)
CLI, plus `git` and `gh` on your PATH.

## The YAML

```yaml
version: 1

system: |            # prepended to every item's prompt
  You are a careful engineer. Make the smallest change that fixes the issue and add a test.

defaults:            # item > defaults > built-in
  model: gpt-5.5
  effort: medium
  permissions: edit          # readonly | edit | full (a.k.a. yolo)
  base: main
  branch_template: cmux/{slug}
  concurrency: 6
  deps: symlink              # symlink | copy | install | skip  (node_modules seeding)
  pr:
    draft: true
    labels: [cmux]

items:               # each item is a bare string OR a mapping of overrides
  - Fix the flaky login test
  - Paginate the notifications list
  - name: Migrate settings form to react-hook-form
    prompt: Rewrite ProfileForm.tsx to use react-hook-form + zod.
    model: claude-opus-4.8
    effort: high
    paths: [src/components/settings]
    labels: [refactor]
    depends_on: []
```

See [`examples/`](examples/) for a minimal file and the realistic 12-issue frontend run.

## Usage

```bash
cmux up issues.yaml --dry-run     # resolve + print the plan and exact copilot commands
cmux up issues.yaml               # spawn the fleet (asks before doing anything)
cmux up issues.yaml --no-pr -y    # commit locally, don't open PRs, don't prompt
cmux ls                           # status table of the latest run
cmux logs migrate-settings-form   # a session's transcript
cmux rm                           # remove the run's worktrees
```

## How it works

- **Engine.** Each item runs as `copilot -p "<system>\n---\n<task>" -C <worktree>
  --output-format json --session-id <uuid> …`. The JSONL event stream is tee'd to
  `.cmux/runs/<id>/sessions/<key>/transcript.jsonl` and folded into a live status.
- **Isolation.** One `git worktree` per item on `cmux/<slug>`, branched off `origin/<base>`.
  Sessions run **edit-only** (`--deny-tool='shell(git push)'`): the agent edits and runs tests,
  but never pushes.
- **PRs.** After a session finishes, `cmux` (not the agent) commits the diff, pushes the branch,
  and opens exactly one draft PR — idempotently.
- **State.** Everything lives under a repo-local, gitignored `.cmux/`. Copilot keeps its own
  resumable session state in `~/.copilot`.

## Safety

- Agents are edit-only by default; `git push` is denied and PRs are **draft**.
- `cmux` never touches your base branch — all work happens in per-item worktrees.
- On machines where the ambient `GITHUB_TOKEN` is a fine-grained PAT without repo scope, `cmux`
  unsets it for `gh`/`git push` (fall back to your keyring login). Disable with
  `--no-strip-github-token`.

## Roadmap

- **v1:** background daemon + thin client; Rich dashboard with live transcript and cross-session
  full-text search; `enter <id>` drops into a native `copilot --resume`; queued follow-ups;
  crash recovery.
- **v2:** Textual embedded panes; ACP transport for live permission prompts; optional remote
  `/delegate` mode; `depends_on` DAG; dev-server/port management.

## License

MIT © Gustavo de Rosa
