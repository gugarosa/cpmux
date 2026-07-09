# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import shutil
import subprocess
import sys
from pathlib import Path

from cmux.logging import get_logger

logger = get_logger(__name__)


class GitError(Exception):
    """Raised when a git command fails or a repository precondition is unmet."""


def run_git(
    args: list[str],
    cwd: str | Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in `cwd`.

    Args:
        args: Arguments passed after `git`.
        cwd: Working directory for the command.
        env: Environment for the subprocess, or `None` to inherit.
        check: Whether to raise on a non-zero exit.

    Returns:
        The completed git process.

    Raises:
        GitError: If `check` is set and git exits non-zero.

    """
    proc = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True)

    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise GitError(f"`git {' '.join(args)}` failed: {detail}.")

    return proc


def is_git_repo(path: str | Path) -> bool:
    """Return whether `path` is inside a git work tree.

    Args:
        path: Path to probe.

    Returns:
        `True` if `path` is inside a git work tree.

    """
    proc = run_git(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False)

    return proc.returncode == 0 and proc.stdout.strip() == "true"


def repo_root(path: str | Path) -> Path:
    """Return the top-level directory of the repository containing `path`.

    Args:
        path: Path inside the repository.

    Returns:
        The repository's top-level directory.

    Raises:
        GitError: If `path` is not inside a git repository.

    """
    if not is_git_repo(path):
        raise GitError(f"`{path}` is not inside a git repository.")

    return Path(run_git(["rev-parse", "--show-toplevel"], cwd=path).stdout.strip())


def resolve_base(root: str | Path, remote: str, base: str) -> tuple[str, str]:
    """Resolve `base` to `(branch, sha)` from remote, then local, then HEAD.

    Args:
        root: Repository root.
        remote: Remote name to check first.
        base: Base branch name.

    Returns:
        The base branch name and the resolved commit sha.

    """
    for ref in (f"refs/remotes/{remote}/{base}", f"refs/heads/{base}"):
        proc = run_git(["rev-parse", "--verify", "--quiet", ref], cwd=root, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return base, proc.stdout.strip()

    return base, run_git(["rev-parse", "HEAD"], cwd=root).stdout.strip()


def add_worktree(root: str | Path, worktree: str | Path, branch: str, base_sha: str) -> None:
    """Create a new worktree on a fresh branch off `base_sha`.

    Args:
        root: Repository root.
        worktree: Path for the new worktree.
        branch: Name of the branch to create.
        base_sha: Commit the branch starts from.

    """
    Path(worktree).parent.mkdir(parents=True, exist_ok=True)

    run_git(["worktree", "add", "-b", branch, str(worktree), base_sha], cwd=root)


def branch_exists(root: str | Path, branch: str) -> bool:
    """Return whether a local branch already exists in the repository.

    Args:
        root: Repository root.
        branch: Branch name to check.

    Returns:
        `True` if the local branch exists.

    """
    proc = run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=root, check=False)

    return proc.returncode == 0


def remove_worktree(root: str | Path, worktree: str | Path, force: bool = True) -> None:
    """Remove a worktree directory, ignoring any failure.

    Args:
        root: Repository root.
        worktree: Path of the worktree to remove.
        force: Whether to pass `--force`.

    """
    args = ["worktree", "remove", str(worktree)]
    if force:
        args.append("--force")

    run_git(args, cwd=root, check=False)


def prune_worktrees(root: str | Path) -> None:
    """Prune administrative files for removed worktrees.

    Args:
        root: Repository root.

    """
    run_git(["worktree", "prune"], cwd=root, check=False)


def has_changes(worktree: str | Path, base_sha: str) -> bool:
    """Return whether the worktree has uncommitted edits or commits past `base_sha`.

    Args:
        worktree: Path of the worktree.
        base_sha: Commit to compare HEAD against.

    Returns:
        `True` if there are staged, unstaged, or committed changes.

    """
    if run_git(["status", "--porcelain"], cwd=worktree).stdout.strip():
        return True

    ahead = run_git(["rev-list", "--count", f"{base_sha}..HEAD"], cwd=worktree).stdout.strip()

    return ahead not in ("", "0")


def provision_deps(root: str | Path, worktree: str | Path, strategy: str) -> None:
    """Seed `node_modules` for a fresh worktree, ignoring any failure.

    Args:
        root: Repository root holding the source `node_modules`.
        worktree: Worktree to seed.
        strategy: One of `skip`, `symlink`, `copy`, or `install`.

    """
    src = Path(root) / "node_modules"
    dst = Path(worktree) / "node_modules"
    if strategy == "skip" or dst.exists():
        return

    try:
        if strategy == "symlink" and src.is_dir():
            dst.symlink_to(src.resolve(), target_is_directory=True)
        elif strategy == "copy" and src.is_dir():
            if sys.platform == "darwin":
                subprocess.run(["cp", "-cR", str(src), str(dst)], check=False)
            else:
                shutil.copytree(src, dst, symlinks=True)
        elif strategy == "install":
            _install_deps(worktree)
    except OSError as exc:
        logger.warning(f"`deps={strategy}` could not seed node_modules: {exc}.")


def _install_deps(worktree: str | Path) -> None:
    worktree_path = Path(worktree)
    if (worktree_path / "pnpm-lock.yaml").exists():
        cmd = ["pnpm", "install", "--frozen-lockfile"]
    elif (worktree_path / "package-lock.json").exists():
        cmd = ["npm", "ci"]
    elif (worktree_path / "yarn.lock").exists():
        cmd = ["yarn", "install", "--frozen-lockfile"]
    else:
        return

    if shutil.which(cmd[0]) is None:
        logger.warning(f"`deps=install` skipped: `{cmd[0]}` is not on PATH.")
        return

    proc = subprocess.run(cmd, cwd=str(worktree_path), capture_output=True, text=True)
    if proc.returncode != 0:
        logger.warning(f"`deps=install` failed in `{worktree_path.name}`: {proc.stderr.strip()}.")
