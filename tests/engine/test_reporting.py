# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

import json

from cpmux.config import Plan
from cpmux.engine.delivery import check_fingerprint
from cpmux.engine.reporting import run_report
from cpmux.engine.store import (
    AttemptRecord,
    CommandResult,
    RunManifest,
    RunPaths,
    SessionRecord,
    VerificationReceipt,
)
from cpmux.events import Status
from cpmux.vcs import git


def _run(tmp_path):
    item = Plan.model_validate(
        {
            "items": [
                {
                    "id": "a",
                    "prompt": "PRIVATE_PROMPT",
                    "env": {"TOKEN": "PRIVATE_ENV_VALUE"},
                    "checks": [{"name": "tests", "command": "printf PRIVATE_COMMAND"}],
                }
            ]
        }
    ).resolve()[0]
    paths = RunPaths(tmp_path, "run1")
    paths.write_manifest(
        RunManifest(run_id="run1", repo_root=str(tmp_path), config_path="", item_keys=["a"], resolved=[item])
    )
    record = SessionRecord(
        key="a",
        name="A",
        slug="a",
        branch="main",
        base="main",
        model="model",
        session_id="sid",
        worktree=str(tmp_path),
        status=Status.DONE,
        env=item.env,
        attempts=[
            AttemptRecord(
                number=1,
                mode="initial",
                session_id="sid",
                commands=[
                    CommandResult(
                        name="tests",
                        command="printf PRIVATE_COMMAND",
                        phase="check",
                        status="passed",
                        exit_code=0,
                        ended_at="2026-09-06T00:00:00+00:00",
                        log_path=str(paths.run_dir / "check.log"),
                    )
                ],
            )
        ],
    )
    paths.write_record(record)
    return paths, record, item


def test_run_report_omits_prompts_environment_and_raw_commands(tmp_path):
    paths, _, _ = _run(tmp_path)

    report = run_report(tmp_path, "run1")
    encoded = json.dumps(report)

    assert report["schema_version"] == 1
    assert report["items"][0]["attempts"][0]["commands"][0]["name"] == "tests"
    assert "PRIVATE_PROMPT" not in encoded
    assert "PRIVATE_ENV_VALUE" not in encoded
    assert "PRIVATE_COMMAND" not in encoded
    assert not paths.owner_lock.exists()


def test_run_report_does_not_reconcile_or_mutate_records(tmp_path):
    paths, record, _ = _run(tmp_path)
    record.status = Status.RUNNING
    paths.write_record(record)
    before = paths.record_file("a").read_bytes()

    report = run_report(tmp_path, "run1")

    assert report["items"][0]["status"] == "running"
    assert paths.record_file("a").read_bytes() == before
    assert not paths.owner_lock.exists()


def test_run_report_marks_receipts_stale_after_source_changes(git_repo):
    git.ignore_runtime_state(git_repo)
    paths, record, item = _run(git_repo)
    head = git.head_commit(git_repo)
    record.candidate_sha = head
    record.verification = VerificationReceipt(
        attempt=1,
        commit_sha=head,
        tree_sha=git.commit_tree(git_repo, head),
        check_fingerprint=check_fingerprint(item, record),
    )
    paths.write_record(record)

    assert run_report(git_repo, "run1")["items"][0]["verification"]["status"] == "passed"
    record.attempts[0].commands[0].status = "failed"
    paths.write_record(record)
    assert run_report(git_repo, "run1")["items"][0]["verification"]["status"] == "stale"
    record.attempts[0].commands[0].status = "passed"
    paths.write_record(record)
    (git_repo / "README.md").write_text("changed after checks")
    report = run_report(git_repo, "run1")

    assert report["items"][0]["verification"]["status"] == "stale"
    assert report["items"][0]["attention"] == "needs_verification"
    assert paths.read_record("a").verification == record.verification


def test_run_report_distinguishes_missing_followup_usage_from_prior_known_usage(tmp_path):
    paths, record, _ = _run(tmp_path)
    record.premium_requests = 4
    record.begin_attempt("followup")
    record.attempts[-1].agent_started = True
    record.status = Status.DONE
    record.finish_attempt()
    paths.write_record(record)

    report = run_report(tmp_path, "run1")

    assert report["reported_premium_requests"] == 4
    assert report["items_with_unknown_usage"] == 1
    assert report["items"][0]["usage_incomplete"]
    assert report["items"][0]["attempts"][-1]["reported_premium_requests"] is None


def test_run_report_does_not_put_verified_no_change_work_in_the_review_queue(git_repo):
    git.ignore_runtime_state(git_repo)
    paths, record, item = _run(git_repo)
    head = git.head_commit(git_repo)
    record.status = Status.NO_CHANGES
    record.base_sha = head
    record.candidate_sha = head
    record.verification = VerificationReceipt(
        attempt=1,
        commit_sha=head,
        tree_sha=git.commit_tree(git_repo, head),
        check_fingerprint=check_fingerprint(item, record),
    )
    paths.write_record(record)

    summary = run_report(git_repo, "run1")["items"][0]

    assert summary["verification"]["status"] == "passed"
    assert summary["attention"] == "completed"
