"""The readiness verdict includes helper work and private-directory cleanup."""

from __future__ import annotations

import json
from pathlib import Path
import runpy
import tempfile
from types import SimpleNamespace

import pytest

from gateway import workflow_run
from tests.test_rollback_readiness_cli import SCRIPT, directory_state, wal_without_shm_store


@pytest.mark.parametrize("expires_at", [
    "inventory", "readiness", "cleanup", "within-budget",
])
def test_tail_uses_one_deadline_and_removes_private_files(tmp_path, monkeypatch, capsys, expires_at):
    _, path = wal_without_shm_store(tmp_path, status="succeeded")
    temporary = tmp_path / "temporary-snapshots"
    temporary.mkdir(exist_ok=True)
    before = directory_state(path.parent)
    namespace = runpy.run_path(str(SCRIPT), run_name="readiness_tail_deadline_test")
    main = namespace["main"]
    globals_ = main.__globals__
    budget = 5.0
    clock = {"now": 0.0}
    events = []
    private_directories = []
    snapshots = []
    inventories = []

    monkeypatch.setitem(globals_, "BACKUP_TIMEOUT_SECONDS", budget)
    monkeypatch.setitem(globals_, "time", SimpleNamespace(monotonic=lambda: clock["now"]))

    def finish_stage(stage, within_budget_time):
        events.append(stage)
        if expires_at == stage:
            clock["now"] = budget + 1.0
        elif expires_at == "within-budget":
            clock["now"] = max(clock["now"], within_budget_time)

    class TrackedTemporaryDirectory(tempfile.TemporaryDirectory):
        def __init__(self, **kwargs):
            super().__init__(dir=temporary, **kwargs)
            private_directories.append(Path(self.name))

        def cleanup(self):
            # Keep the real filesystem cleanup: advancing the clock alone must
            # not stand in for removing the snapshot, its WAL or private source.
            assert any(Path(self.name).iterdir())
            super().cleanup()
            finish_stage("cleanup", 3.0)

    monkeypatch.setitem(globals_, "tempfile", SimpleNamespace(
        TemporaryDirectory=TrackedTemporaryDirectory))
    original_inventory = workflow_run.rollback_job_inventory
    original_readiness = workflow_run.assert_rollback_ready

    def record_snapshot(snapshot):
        snapshot = Path(snapshot)
        assert snapshot.parent in private_directories
        assert snapshot.is_file()
        assert snapshot != path
        snapshots.append(snapshot)

    def inventory(snapshot):
        record_snapshot(snapshot)
        result = original_inventory(snapshot)
        inventories.append(result)
        finish_stage("inventory", 1.0)
        return result

    def readiness(snapshot):
        record_snapshot(snapshot)
        events.append("readiness-enter")
        result = original_readiness(snapshot)
        finish_stage("readiness", 2.0)
        return result

    monkeypatch.setattr(workflow_run, "rollback_job_inventory", inventory)
    monkeypatch.setattr(workflow_run, "assert_rollback_ready", readiness)

    try:
        code = main(["--store", str(path), "--json"])
    finally:
        # Includes source bytes, companion files and directory/file mtimes.
        assert directory_state(path.parent) == before
        assert len(private_directories) == 1
        assert all(not directory.exists() for directory in private_directories)
        assert list(temporary.iterdir()) == []
    output = capsys.readouterr()
    assert output.err == ""
    assert len(output.out.splitlines()) == 1
    body = json.loads(output.out)
    assert snapshots, "the real helpers must receive the private snapshot"
    assert events[0] == "inventory"
    assert events.count("cleanup") == 1
    if expires_at == "within-budget":
        assert clock["now"] == 3.0 < budget
        assert code == 0
        assert body["ready"] is True
        assert body["reason"] is None
        assert body["job_ids"] == []
        assert body["inventory"] == inventories[0]
        assert "readiness" in events
    else:
        assert clock["now"] == budget + 1.0
        assert code == 3
        assert body == {
            "ready": False, "reason": "store_unreadable", "job_ids": [], "inventory": [],
        }
        if expires_at == "inventory":
            assert "readiness-enter" not in events
        else:
            assert "readiness" in events
