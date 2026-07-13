# Copyright (c) 2026 Gustavo de Rosa.
# Licensed under the MIT license.

from cmux.engine.store import (
    RunManifest,
    RunPaths,
    SessionRecord,
    all_run_ids,
    delete_run,
    latest_run_id,
    load_run,
    new_run_id,
)
from cmux.events import Status


def _record(key="a"):
    return SessionRecord(
        key=key,
        name="Item A",
        slug="item-a",
        branch="cmux/item-a",
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
    run_dir = tmp_path / ".cmux" / "runs" / "run1"

    assert paths.manifest == run_dir / "manifest.json"
    assert paths.owner_file == run_dir / "owner.json"
    assert paths.session_dir("k") == run_dir / "sessions" / "k"
    assert paths.prompt_file("k") == run_dir / "sessions" / "k" / "prompt.md"
    assert paths.transcript("k") == run_dir / "sessions" / "k" / "transcript.jsonl"
    assert paths.record_file("k") == run_dir / "sessions" / "k" / "session.json"
    assert paths.copilot_log_dir("k") == run_dir / "sessions" / "k" / "copilot-logs"


def test_run_paths_worktree_resolves_under_worktrees_dir(tmp_path):
    paths = RunPaths(tmp_path, "run1")
    assert paths.worktree("k") == tmp_path / ".cmux" / "worktrees" / "run1" / "k"


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
        config_path=str(tmp_path / "cmux.yaml"),
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
            config_path=str(tmp_path / "cmux.yaml"),
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
