# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from pathlib import Path
from typing import Any

import psutil

from cpmux.engine.delivery import verified_candidate
from cpmux.engine.ownership import OwnershipError, matching_process, process_owner_alive
from cpmux.engine.store import RunPaths, SessionRecord, load_run
from cpmux.events import ACTIVE, SUCCESS, TERMINAL_FAILURE, Status
from cpmux.vcs import git


def run_report(repo_root: str | Path, run_id: str) -> dict[str, Any]:
    """Describe persisted outcomes without reconciling or mutating a run.

    Prompt, environment, raw-command, and complete-log fields are excluded.
    Error text can contain command output or secrets and is not automatically redacted.
    Process memory reports cover the recorded child, not its descendants.

    Args:
        repo_root: Repository containing run history.
        run_id: Run identifier.

    Returns:
        Versioned machine-readable run summary.

    Raises:
        OSError: Run history cannot be read.
        ValueError: Stored run data is invalid.

    """

    paths = RunPaths(repo_root, run_id)
    manifest, records = load_run(repo_root, run_id)
    items = {item.key: item for item in manifest.resolved}
    summaries: list[dict[str, Any]] = []

    for record in records:
        item = items.get(record.key)
        verification: dict[str, Any] = {
            "status": "not_configured" if item is not None and not item.checks else "missing"
        }
        if item is not None and item.checks and record.attempts:
            checks = [command for command in record.attempts[-1].commands if command.phase == "check"]
            if any(command.status in {"failed", "timed_out", "cancelled"} for command in checks):
                verification["status"] = "failed"
            elif any(command.status == "running" for command in checks):
                verification["status"] = "running"
        if record.verification is not None:
            verification.update(record.verification.model_dump(mode="json"))
            try:
                git.require_clean_revision(record.worktree, record.verification.commit_sha)
                same_checks = item is not None and verified_candidate(
                    item,
                    record,
                    record.verification.commit_sha,
                    git.commit_tree(record.worktree, record.verification.commit_sha),
                )
                verification["status"] = "passed" if same_checks else "stale"
                if not same_checks:
                    verification["reason"] = (
                        "Configuration, source tree, or recorded command evidence no longer matches."
                    )
            except git.GitError as exc:
                verification["status"] = "stale"
                verification["reason"] = str(exc)

        process: dict[str, Any] | None = None
        if record.pid is not None:
            try:
                child = matching_process(record.pid, record.pid_created_at)
                if child is not None:
                    process = {"pid": child.pid, "rss_bytes": child.memory_info().rss}
            except (OwnershipError, psutil.Error) as exc:
                process = {"pid": record.pid, "error": str(exc)}

        attention = "pending"
        if record.status in TERMINAL_FAILURE:
            attention = "failed"
        elif record.status in ACTIVE:
            attention = "running"
        elif verification["status"] == "stale":
            attention = "needs_verification"
        elif record.status == Status.NO_CHANGES or record.candidate_sha == record.base_sha:
            attention = "completed"
        elif verification["status"] == "passed" and record.candidate_sha:
            attention = "ready_for_review"
        elif record.pr_url:
            attention = "delivered_unverified"
        elif record.status in SUCCESS:
            attention = "completed"
        usage_incomplete = record.premium_requests is None or any(
            attempt.agent_started and attempt.premium_requests is None for attempt in record.attempts
        )

        summaries.append(
            {
                "key": record.key,
                "name": record.name,
                "status": record.status.value,
                "phase": record.phase,
                "attention": attention,
                "branch": record.branch,
                "base": record.base,
                "base_sha": record.base_sha,
                "base_from": record.base_from,
                "depends_on": item.depends_on if item is not None else [],
                "source": record.source.model_dump(mode="json") if record.source is not None else None,
                "candidate_sha": record.candidate_sha,
                "delivery_sha": record.delivery_sha,
                "pr_url": record.pr_url,
                "error": record.error,
                "elapsed_seconds": record.elapsed_seconds,
                "last_activity_at": record.last_activity_at,
                "reported_premium_requests": record.premium_requests,
                "usage_incomplete": usage_incomplete,
                "verification": verification,
                "process": process,
                "attempts": [
                    {
                        "number": attempt.number,
                        "mode": attempt.mode,
                        "status": attempt.status.value if attempt.ended_at else record.status.value,
                        "phase": attempt.phase if attempt.ended_at else record.phase,
                        "started_at": attempt.started_at,
                        "ended_at": attempt.ended_at,
                        "exit_code": attempt.exit_code,
                        "error": attempt.error,
                        "candidate_sha": attempt.candidate_sha,
                        "agent_started": attempt.agent_started,
                        "reported_premium_requests": attempt.premium_requests,
                        "commands": [
                            {
                                "name": command.name,
                                "phase": command.phase,
                                "status": command.status,
                                "exit_code": command.exit_code,
                                "duration_seconds": command.duration_seconds,
                                "log_path": command.log_path,
                                "error": command.error,
                            }
                            for command in attempt.commands
                        ],
                    }
                    for attempt in record.attempts
                ],
            }
        )

    try:
        managed: bool | None = process_owner_alive(paths.owner_file)
        owner_error = None
    except OwnershipError as exc:
        managed = None
        owner_error = str(exc)

    reported_premium = sum(record.premium_requests or 0 for record in records)
    return {
        "schema_version": 1,
        "run_id": run_id,
        "managed": managed,
        "owner_error": owner_error,
        "paused": paths.pause_file.exists(),
        "expected_items": len(manifest.item_keys),
        "recorded_items": len(records),
        "reported_premium_requests": reported_premium,
        "items_with_unknown_usage": sum(item["usage_incomplete"] for item in summaries),
        "premium_budget": manifest.premium_budget,
        "budget_reached": manifest.premium_budget is not None and reported_premium >= manifest.premium_budget,
        "items": summaries,
    }
