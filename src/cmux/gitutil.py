"""Thin git helpers: repository detection, worktree lifecycle, dependency seeding."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


class GitError(Exception):
    pass


def run_git(
    args: list[str],
    cwd: str | Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def is_git_repo(path: str | Path) -> bool:
    proc = run_git(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False)
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def repo_root(path: str | Path) -> Path:
    if not is_git_repo(path):
        raise GitError(f"{path} is not inside a git repository (cmux needs one to open PRs)")
    out = run_git(["rev-parse", "--show-toplevel"], cwd=path).stdout.strip()
    return Path(out)


def resolve_base(root: str | Path, remote: str, base: str) -> tuple[str, str]:
    """Return ``(base_branch, base_sha)`` from remote, then local, then HEAD."""
    for ref in (f"refs/remotes/{remote}/{base}", f"refs/heads/{base}"):
        proc = run_git(["rev-parse", "--verify", "--quiet", ref], cwd=root, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return base, proc.stdout.strip()
    head = run_git(["rev-parse", "HEAD"], cwd=root).stdout.strip()
    return base, head


def add_worktree(root: str | Path, worktree: str | Path, branch: str, base_sha: str) -> None:
    Path(worktree).parent.mkdir(parents=True, exist_ok=True)
    run_git(["worktree", "add", "-b", branch, str(worktree), base_sha], cwd=root)


def remove_worktree(root: str | Path, worktree: str | Path, force: bool = True) -> None:
    args = ["worktree", "remove", str(worktree)]
    if force:
        args.append("--force")
    run_git(args, cwd=root, check=False)


def prune_worktrees(root: str | Path) -> None:
    run_git(["worktree", "prune"], cwd=root, check=False)


def has_changes(worktree: str | Path, base_sha: str) -> bool:
    dirty = run_git(["status", "--porcelain"], cwd=worktree).stdout.strip()
    if dirty:
        return True
    ahead = run_git(["rev-list", "--count", f"{base_sha}..HEAD"], cwd=worktree).stdout.strip()
    return ahead not in ("", "0")


def provision_deps(root: str | Path, worktree: str | Path, strategy: str) -> str | None:
    """Best-effort dependency seeding for a fresh worktree. Never raises."""
    src = Path(root) / "node_modules"
    dst = Path(worktree) / "node_modules"
    try:
        if strategy == "skip" or dst.exists():
            return None
        if strategy == "symlink":
            if src.is_dir():
                dst.symlink_to(src.resolve(), target_is_directory=True)
                return "symlinked node_modules"
        elif strategy == "copy":
            if src.is_dir():
                if sys.platform == "darwin":
                    subprocess.run(["cp", "-cR", str(src), str(dst)], check=False)
                else:
                    shutil.copytree(src, dst, symlinks=True)
                return "copied node_modules"
        elif strategy == "install":
            return _install_deps(worktree)
    except OSError as exc:
        return f"deps ({strategy}) skipped: {exc}"
    return None


def _install_deps(worktree: str | Path) -> str | None:
    wt = Path(worktree)
    if (wt / "pnpm-lock.yaml").exists():
        cmd = ["pnpm", "install", "--frozen-lockfile"]
    elif (wt / "package-lock.json").exists():
        cmd = ["npm", "ci"]
    elif (wt / "yarn.lock").exists():
        cmd = ["yarn", "install", "--frozen-lockfile"]
    else:
        return None
    if shutil.which(cmd[0]) is None:
        return f"deps install skipped: {cmd[0]} not found"
    proc = subprocess.run(cmd, cwd=str(wt), capture_output=True, text=True)
    return f"installed deps ({cmd[0]})" if proc.returncode == 0 else "deps install failed"
