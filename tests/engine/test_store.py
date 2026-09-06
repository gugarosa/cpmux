# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from concurrent.futures import ThreadPoolExecutor

import pytest

from cpmux.engine.store import (
    RunManifest,
    RunPaths,
    SessionRecord,
    all_run_ids,
    delete_run,
    latest_run_id,
    load_run,
    new_run_id,
)
from cpmux.events import Status


def _record(key="a"):
    return SessionRecord(
        key=key,
        name="Item A",
        slug="item-a",
        branch="cpmux/item-a",
        base="main",
        model="gpt-5.5",
        session_id="sess-123",
        worktree="/tmp/wt",
        status=Status.DONE,
    )


def test_new_run_id_returns_nonempty_string():
    run_id = new_run_id()
    assert isinstance(run_id, str)
    assert run_id


def test_new_run_id_starts_with_date_prefix():
    run_id = new_run_id()
    assert run_id[:8].isdigit()


def test_run_paths_resolve_under_run_dir(tmp_path):
    paths = RunPaths(tmp_path, "run1")
    runs = tmp_path / ".cpmux/runs/run1"
    sessions = runs / "sessions"

    assert paths.manifest == runs / "manifest.json"
    assert paths.owner_file == runs / "owner.json"
    assert paths.session_dir("k") == sessions / "k"
    assert paths.prompt_file("k") == sessions / "k/prompt.md"
    assert paths.transcript("k") == sessions / "k/transcript.jsonl"
    assert paths.record_file("k") == sessions / "k/session.json"
    assert paths.copilot_log_dir("k") == sessions / "k/copilot-logs"
    assert paths.worktree("k") == tmp_path / ".cpmux/worktrees/run1/k"


@pytest.mark.parametrize("identifier", ["", ".", "..", "../outside", "/outside", "nested/../item", "nul\0id"])
def test_run_paths_rejects_unsafe_run_ids(tmp_path, identifier):
    with pytest.raises(ValueError, match="run_id"):
        RunPaths(tmp_path, identifier)


@pytest.mark.parametrize("identifier", ["", ".", "..", "../outside", "/outside", "nested/../item", "nul\0id"])
def test_run_paths_rejects_unsafe_session_keys(tmp_path, identifier):
    paths = RunPaths(tmp_path, "run1")
    with pytest.raises(ValueError, match="key"):
        paths.session_dir(identifier)
    with pytest.raises(ValueError, match="key"):
        paths.worktree(identifier)


def test_run_paths_preserves_safe_namespaces(tmp_path):
    paths = RunPaths(tmp_path, "group/run")

    assert paths.session_dir("frontend/login") == tmp_path / ".cpmux/runs/group/run/sessions/frontend/login"
    assert paths.worktree("frontend/login") == tmp_path / ".cpmux/worktrees/group/run/frontend/login"


def test_write_record_read_record_round_trip(tmp_path):
    paths = RunPaths(tmp_path, "run1")
    record = _record()
    paths.write_record(record)
    loaded = paths.read_record("a")

    assert loaded.status == record.status
    assert loaded.branch == record.branch
    assert loaded.session_id == record.session_id
    assert loaded.model == record.model


def test_load_run_returns_manifest_and_records(tmp_path):
    paths = RunPaths(tmp_path, "run1")
    manifest = RunManifest(
        run_id="run1",
        repo_root=str(tmp_path),
        config_path=str(tmp_path / "cpmux.yaml"),
        item_keys=["a"],
    )
    paths.write_manifest(manifest)
    paths.write_record(_record())

    loaded_manifest, records = load_run(tmp_path, "run1")

    assert loaded_manifest.run_id == "run1"
    assert loaded_manifest.item_keys == ["a"]
    assert len(records) == 1
    assert records[0].key == "a"


def _write_manifest(tmp_path, run_id):
    paths = RunPaths(tmp_path, run_id)
    paths.write_manifest(
        RunManifest(
            run_id=run_id,
            repo_root=str(tmp_path),
            config_path=str(tmp_path / "cpmux.yaml"),
        )
    )


def test_all_run_ids_sorted_newest_first(tmp_path):
    _write_manifest(tmp_path, "20260101-000000-aaaaaa")
    _write_manifest(tmp_path, "20260202-000000-bbbbbb")
    assert all_run_ids(tmp_path) == [
        "20260202-000000-bbbbbb",
        "20260101-000000-aaaaaa",
    ]


def test_latest_run_id_returns_newest(tmp_path):
    _write_manifest(tmp_path, "20260101-000000-aaaaaa")
    _write_manifest(tmp_path, "20260202-000000-bbbbbb")
    assert latest_run_id(tmp_path) == "20260202-000000-bbbbbb"


def test_elapsed_seconds_is_none_before_start():
    assert _record().elapsed_seconds is None


def test_elapsed_seconds_spans_start_to_end():
    record = _record()
    record.started_at = "2026-07-10T17:00:00+00:00"
    record.ended_at = "2026-07-10T17:01:30+00:00"
    assert record.elapsed_seconds == 90.0


def test_delete_run_removes_run_history(tmp_path):
    paths = RunPaths(tmp_path, "run-x")
    paths.run_dir.mkdir(parents=True)
    (paths.run_dir / "manifest.json").write_text("{}")
    assert "run-x" in all_run_ids(tmp_path)

    delete_run(tmp_path, "run-x")

    assert "run-x" not in all_run_ids(tmp_path)


def test_write_record_uses_unique_temporary_files_for_concurrent_writers(tmp_path):
    paths = RunPaths(tmp_path, "run1")
    records = [_record().model_copy(update={"name": f"writer-{index}"}) for index in range(20)]

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(paths.write_record, records))

    assert paths.read_record("a").name in {record.name for record in records}
    assert not list(paths.session_dir("a").glob(".session.json.*"))


def test_all_run_ids_omits_incomplete_run_directories(tmp_path):
    paths = RunPaths(tmp_path, "incomplete")
    paths.run_dir.mkdir(parents=True)
    assert all_run_ids(tmp_path) == []


def test_record_usage_accumulates_attempts_without_double_counting_events():
    record = _record()
    record.premium_requests = 3
    record.begin_attempt("followup")
    record.record_usage(0.5)
    record.record_usage(0.5)
    record.record_usage(1.5)
    assert record.premium_requests == 4.5
    record.status = Status.DONE
    record.finish_attempt()
    record.begin_attempt("retry")
    record.record_usage(1)
    assert record.premium_requests == 5.5
    assert [attempt.premium_requests for attempt in record.attempts] == [1.5, 1]


def test_delete_run_does_not_hide_an_inner_missing_file_failure(tmp_path, monkeypatch):
    paths = RunPaths(tmp_path, "run")
    paths.worktrees_dir.mkdir(parents=True)

    def interrupted_removal(path):
        raise FileNotFoundError("a child disappeared before deletion")

    monkeypatch.setattr("cpmux.engine.store.rmtree", interrupted_removal)
    with pytest.raises(FileNotFoundError, match="child disappeared"):
        delete_run(tmp_path, "run")
    assert paths.worktrees_dir.exists()


def test_read_record_rejects_a_key_mismatch_before_refreshing_the_caller(tmp_path):
    paths = RunPaths(tmp_path, "run")
    record = _record("a")
    other = _record("b")
    paths.write_record(record)
    paths.write_record(other)
    before = record.model_copy(deep=True)
    paths.record_file("a").write_text(other.model_dump_json())

    with pytest.raises(ValueError, match="contains key `b`, expected `a`"):
        paths.refresh_record(record)

    assert record == before
    assert paths.read_record("b") == other
