# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import fcntl
import shutil
import subprocess
import sys
from pathlib import Path

from cpmux.logging import get_logger
from cpmux.process import inherited_fds

logger = get_logger(__name__)


class GitError(Exception):
    """Raised when git fails or a repository precondition is unmet."""


def run_git(
    args: list[str],
    cwd: str | Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a Git command.

    Args:
        args: Git command arguments.
        cwd: Directory in which to run Git.
        env: Environment variables for the subprocess.
        check: Whether to raise when Git exits unsuccessfully.

    Returns:
        The completed Git process.

    Raises:
        GitError: The command failed and check is enabled.

    """

    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
            pass_fds=inherited_fds(),
        )
    except FileNotFoundError as exc:
        raise GitError("`git` was not found on PATH; install git.") from exc

    if check and proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        raise GitError(f"`git {' '.join(args)}` failed: {detail.removesuffix('.')}.")

    return proc


def is_git_repo(path: str | Path) -> bool:
    """Check whether a path is inside a Git worktree.

    Args:
        path: Path to inspect.

    Returns:
        Whether the path is inside a Git worktree.

    """

    proc = run_git(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False)

    return proc.returncode == 0 and proc.stdout.strip() == "true"


def repo_root(path: str | Path) -> Path:
    """Find the repository root containing a path.

    Args:
        path: Path inside the repository.

    Returns:
        The repository root.

    Raises:
        GitError: The path is not inside a Git repository.

    """

    if not is_git_repo(path):
        raise GitError(f"`{path}` is not inside a git repository.")

    return Path(run_git(["rev-parse", "--show-toplevel"], cwd=path).stdout.strip())


def resolve_base(root: str | Path, remote: str, base: str) -> tuple[str, str]:
    """Resolve a base branch to a commit.

    Args:
        root: Repository root.
        remote: Remote used to resolve the branch.
        base: Base branch name.

    Returns:
        The base branch name and resolved commit SHA.

    """

    for ref in (f"refs/remotes/{remote}/{base}", f"refs/heads/{base}"):
        proc = run_git(["rev-parse", "--verify", "--quiet", ref], cwd=root, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return base, proc.stdout.strip()

    logger.warning(f"`base={base}` was not found; branching from `HEAD`.")

    return base, run_git(["rev-parse", "HEAD"], cwd=root).stdout.strip()


def add_worktree(root: str | Path, worktree: str | Path, branch: str, base_sha: str) -> None:
    """Create a worktree and branch from a commit.

    Args:
        root: Repository root.
        worktree: Path for the new worktree.
        branch: Branch to create.
        base_sha: Commit from which to create the branch.

    """

    Path(worktree).parent.mkdir(parents=True, exist_ok=True)
    run_git(["worktree", "add", "-b", branch, str(worktree), base_sha], cwd=root)


def ignore_runtime_state(root: str | Path) -> None:
    """Keep local orchestration records out of Git without editing tracked files.

    Args:
        root: Repository whose local exclude file should protect runtime state.

    Raises:
        GitError: Git metadata cannot be located.
        OSError: The local exclude file cannot be updated.

    """

    ignored = run_git(["check-ignore", "--quiet", ".cpmux/"], cwd=root, check=False)
    if ignored.returncode == 0:
        return
    if ignored.returncode != 1:
        raise GitError(f"`git check-ignore` failed: {ignored.stderr.strip().removesuffix('.')}.")
    exclude = Path(run_git(["rev-parse", "--git-path", "info/exclude"], cwd=root).stdout.strip())
    if not exclude.is_absolute():
        exclude = Path(root) / exclude
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            handle.seek(0)
            text = handle.read()
            if "/.cpmux/" not in text.splitlines():
                handle.write(("" if not text or text.endswith("\n") else "\n") + "/.cpmux/\n")
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def require_paths_exist(worktree: str | Path, paths: list[str]) -> None:
    """Require paths to exist in a worktree.

    Args:
        worktree: Worktree root.
        paths: Relative paths that must exist.

    Raises:
        GitError: A path does not exist in the worktree.

    """

    root = Path(worktree)
    missing = [directory for directory in paths if not (root / directory).exists()]
    if missing:
        raise GitError(f"`{missing[0]}` does not exist in the worktree.")


def branch_exists(root: str | Path, branch: str) -> bool:
    """Check whether a local branch exists.

    Args:
        root: Repository root.
        branch: Local branch name.

    Returns:
        Whether the branch exists.

    """

    proc = run_git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=root, check=False)

    return proc.returncode == 0


def remove_worktree(root: str | Path, worktree: str | Path, force: bool = True) -> bool:
    """Remove a worktree.

    Args:
        root: Repository root.
        worktree: Worktree path to remove.
        force: Whether to force removal.

    Returns:
        Whether the worktree is absent or removal succeeded.

    """

    if not Path(worktree).exists():
        return True

    args = ["worktree", "remove", str(worktree)]
    if force:
        args.append("--force")

    return run_git(args, cwd=root, check=False).returncode == 0


def prune_worktrees(root: str | Path) -> None:
    """Prune metadata for removed worktrees.

    Args:
        root: Repository root.

    """

    run_git(["worktree", "prune"], cwd=root, check=False)


def has_changes(worktree: str | Path, base_sha: str) -> bool:
    """Check whether a worktree has edits or new commits.

    Args:
        worktree: Worktree path to inspect.
        base_sha: Commit against which to check new commits.

    Returns:
        Whether the worktree has edits or new commits.

    """

    if run_git(["status", "--porcelain"], cwd=worktree).stdout.strip():
        return True

    ahead = run_git(["rev-list", "--count", f"{base_sha}..HEAD"], cwd=worktree).stdout.strip()

    return ahead not in ("", "0")


def head_commit(worktree: str | Path) -> str:
    """Read the worktree's exact current commit.

    Args:
        worktree: Git worktree to inspect.

    Returns:
        Full commit object identifier.

    Raises:
        GitError: The worktree has no readable HEAD.

    """

    return run_git(["rev-parse", "--verify", "HEAD"], cwd=worktree).stdout.strip()


def commit_tree(worktree: str | Path, commit: str) -> str:
    """Read the tree belonging to a recorded commit.

    Args:
        worktree: Repository containing the commit.
        commit: Commit object identifier.

    Returns:
        Full tree object identifier.

    Raises:
        GitError: The commit cannot be resolved.

    """

    return run_git(["rev-parse", "--verify", f"{commit}^{{tree}}"], cwd=worktree).stdout.strip()


def require_clean_revision(worktree: str | Path, commit: str) -> None:
    """Require HEAD and all non-ignored files to match a recorded candidate.

    Args:
        worktree: Worktree to inspect.
        commit: Expected candidate commit.

    Raises:
        GitError: HEAD moved or tracked/untracked source changed.

    """

    if head_commit(worktree) != commit:
        raise GitError(f"`{worktree}` HEAD changed after the candidate was recorded. Verify the new revision.")
    if run_git(["status", "--porcelain", "--untracked-files=all"], cwd=worktree).stdout.strip():
        raise GitError(f"`{worktree}` has changes after verification began. Commit and verify the new revision.")


def provision_deps(root: str | Path, worktree: str | Path, strategy: str) -> None:
    """Provision worktree dependencies.

    Args:
        root: Repository root containing dependencies.
        worktree: Worktree to provision.
        strategy: Dependency provisioning strategy.

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
        logger.warning(f"`deps={strategy}` could not seed `node_modules`: {str(exc).removesuffix('.')}.")


def dependency_install_command(worktree: str | Path) -> list[str] | None:
    """Select the existing Node lockfile's reproducible install command.

    Args:
        worktree: Directory containing a supported package-manager lockfile.

    Returns:
        Install arguments, or None when no supported lockfile exists.

    """

    worktree_path = Path(worktree)

    if (worktree_path / "pnpm-lock.yaml").exists():
        return ["pnpm", "install", "--frozen-lockfile"]
    if (worktree_path / "package-lock.json").exists():
        return ["npm", "ci"]
    if (worktree_path / "yarn.lock").exists():
        return ["yarn", "install", "--frozen-lockfile"]
    return None


def _install_deps(worktree: str | Path) -> None:
    worktree_path = Path(worktree)
    cmd = dependency_install_command(worktree)
    if cmd is None:
        return

    if shutil.which(cmd[0]) is None:
        logger.warning(f"`deps=install` skipped: `{cmd[0]}` is not on `PATH`.")
        return

    proc = subprocess.run(cmd, cwd=str(worktree_path), capture_output=True, text=True)
    if proc.returncode != 0:
        logger.warning(f"`deps=install` failed in `{worktree_path.name}`: {proc.stderr.strip().removesuffix('.')}.")
