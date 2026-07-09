# cmux

**A declarative, guided multiplexer for GitHub Copilot CLI agents — "tmuxinator for `copilot` sessions."**

You write one YAML file: a shared system prompt plus a list of items (tasks). cmux spawns one
isolated headless `copilot` session per item — each in its own git worktree, on its own branch,
opening its own draft PR — and gives you one place to watch, search, and steer all of them. When you
hand a single agent 12–15 issues it loses context between them; cmux gives every issue its own
session so nothing bleeds together. v0 ships the foreground supervisor: `up`, `ls`, `logs`, `rm`.

## Installation

<details>
<summary>Install from source</summary>

```bash
git clone https://github.com/gugarosa/cmux
cd cmux
pip install -e .
```

Requires Python ≥ 3.12 and the [`copilot`](https://docs.github.com/copilot/how-tos/copilot-cli),
`git`, and `gh` CLIs on your PATH.

</details>

## Usage

A cmux file is a shared `system` prompt, `defaults`, and a list of `items` (each a bare string or a
mapping of overrides):

```yaml
version: 1
system: Make the smallest change that fixes the issue and add a test.
defaults:
  model: gpt-5.5
  permissions: edit          # readonly | edit | full
items:
  - Fix the flaky login test
  - Paginate the notifications list
  - name: Migrate settings form to react-hook-form
    prompt: Rewrite ProfileForm.tsx to use react-hook-form + zod.
    model: claude-opus-4.8
    labels: [refactor]
```

```bash
cmux up issues.yaml --dry-run     # resolve and print the plan and copilot commands
cmux up issues.yaml               # spawn the fleet in the foreground (asks first)
cmux up issues.yaml --detach      # spawn a background daemon and return immediately
cmux attach                       # live-monitor a run (reconnects to a background run)
cmux ls                           # status of the latest run
cmux enter migrate-settings-form  # drop into an interactive copilot session
cmux send migrate-settings-form "also add a test"   # append a follow-up turn
cmux search parseISO --all        # full-text search across every session
cmux logs migrate-settings-form -f  # stream a transcript live
cmux down                         # stop a background run
cmux rm                           # remove the run's worktrees
```

See [`examples/`](examples/) for a minimal file and the realistic 12-issue frontend run.

- Conventions, architecture, and roadmap: see [`CONVENTIONS.md`](CONVENTIONS.md).

## Tests

```bash
pytest
```
