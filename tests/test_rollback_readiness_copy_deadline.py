"""A positive snapshot budget includes private DB/WAL copying and verification."""

from __future__ import annotations

import builtins
import json
from pathlib import Path
import runpy
import shutil
import tempfile
from types import SimpleNamespace

import pytest

from gateway import workflow_run
from tests.test_rollback_readiness_cli import SCRIPT, directory_state, run_cli, wal_without_shm_store


@pytest.mark.parametrize("expires_at", [
    "db-copy", "wal-copy", "copy-verification", "backup-verification", "within-budget",
])
def test_missing_shm_copy_uses_one_deadline_and_preserves_source(tmp_path, monkeypatch, capsys, expires_at):
    seed_path, path = wal_without_shm_store(tmp_path, status="succeeded")
    wal = Path(str(path) + "-wal")
    before = directory_state(path.parent)
    expected = run_cli(tmp_path, seed_path, "--json")
    assert expected.returncode == 0, expected.stdout
    expected_body = json.loads(expected.stdout)
    assert expected_body["ready"] is True
    temporary = tmp_path / "temporary-snapshots"

    namespace = runpy.run_path(str(SCRIPT), run_name="readiness_copy_deadline_test")
    main = namespace["main"]
    globals_ = main.__globals__
    budget = 5.0
    clock = {"now": 0.0, "fingerprints": 0}
    reads = []
    evaluations = []
    monkeypatch.setitem(globals_, "BACKUP_TIMEOUT_SECONDS", budget)
    monkeypatch.setitem(globals_, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    monkeypatch.setitem(globals_, "tempfile", SimpleNamespace(
        TemporaryDirectory=lambda **kwargs: tempfile.TemporaryDirectory(dir=temporary, **kwargs)))

    class CopyReader:
        """Force Python copying even on shutil's fast-copy path, then advance mid-file."""

        def __init__(self, raw, source):
            self.raw, self.source = raw, source

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.raw.__exit__(*args)

        def fileno(self):
            # shutil.copyfile falls back to its Python loop when fast copy cannot get an fd.
            raise OSError("use the observable Python copy path")

        def record(self, size):
            if size:
                reads.append((self.source, size))
                if expires_at == ("wal-copy" if self.source == wal else "db-copy"):
                    clock["now"] = budget + 1.0

        def read(self, size=-1):
            # A DB is at least one SQLite page, so expiry follows a partial copy,
            # not a whole-file copy. No real sleeping or wall-clock race is used.
            assert size > 0
            block = self.raw.read(min(size, 1024))
            self.record(len(block))
            return block

        def readinto(self, buffer):
            size = self.raw.readinto(memoryview(buffer)[:1024])
            self.record(size)
            return size

    def copying_open(file, mode="r", *args, **kwargs):
        raw = builtins.open(file, mode, *args, **kwargs)
        if mode == "rb" and Path(file) in (path, wal):
            return CopyReader(raw, Path(file))
        return raw

    # Both the base's shutil.copyfile and the fixed explicit chunk loop use
    # this same observable source stream. Fingerprinting uses Path.open.
    monkeypatch.setattr(shutil, "open", copying_open, raising=False)
    monkeypatch.setitem(globals_, "open", copying_open)
    original_fingerprint = globals_["fingerprint"]

    def fingerprint(file):
        result = original_fingerprint(file)
        clock["fingerprints"] += 1
        target = {"copy-verification": 4, "backup-verification": 6}.get(expires_at)
        if clock["fingerprints"] == target:
            clock["now"] = budget + 1.0
        return result

    monkeypatch.setitem(globals_, "fingerprint", fingerprint)
    for name in ("rollback_job_inventory", "assert_rollback_ready"):
        original = getattr(workflow_run, name)

        def recorded(*args, _name=name, _original=original, **kwargs):
            evaluations.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(workflow_run, name, recorded)

    try:
        code = main(["--store", str(path), "--json"])
    finally:
        # Includes DB/WAL bytes and mtimes, directory mtime and the file set.
        assert directory_state(path.parent) == before
        assert list(temporary.iterdir()) == []
    output = capsys.readouterr()
    assert output.err == ""
    assert len(output.out.splitlines()) == 1
    body = json.loads(output.out)
    assert reads, "the fixture must use the missing-SHM private-copy path"
    if expires_at == "within-budget":
        assert clock["now"] < budget
        assert code == expected.returncode
        assert body == expected_body
        # The readiness check may read the inventory again itself; the copy path only has to reach both.
        assert evaluations[0] == "rollback_job_inventory" and "assert_rollback_ready" in evaluations
    else:
        assert clock["now"] > budget > 0
        assert code == 3
        assert body == {"ready": False, "reason": "store_unreadable", "job_ids": [], "inventory": []}
        assert evaluations == [], "expired snapshots must never reach readiness evaluation"
        if expires_at in ("db-copy", "wal-copy"):
            target = wal if expires_at == "wal-copy" else path
            copied = [size for source, size in reads if source == target]
            assert copied == [1024], "stop between chunks, not after copying the entire file"
            assert sum(copied) < target.stat().st_size
