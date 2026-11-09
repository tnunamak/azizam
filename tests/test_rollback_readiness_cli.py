"""Subprocess contract for the read-only rollback readiness command.

Written before the CLI. Do not replace the rollback functions with stubs.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from gateway import media_jobs
from gateway.job_envelope import build_job_envelope

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "rollback_readiness.py"


def seed_job(path, job_id, *, status="queued", pinned=False):
    # Same production seeding as test_workflow_run_contracts.py:624-648.
    envelope = build_job_envelope(
        source_kind="workflow", source_id="pinned.workflow",
        contract={"a": 1}, binding={"b": 1}, template=None,
        protocol_id="p", adapter_id="a", interface_version=1,
        service_id="s", origin="http://engine.test",
    ) if pinned else None
    media_jobs.create(
        media_jobs.MediaJob(intent="workflow.run", job_id=job_id, status=status),
        path=path, envelope=envelope, require_envelope=envelope is not None,
    )


def seed(path, case):
    if case == "missing":
        return 3, "store_missing", [], []
    if case == "text":
        path.write_text("not a sqlite database\n", encoding="utf-8")
        return 3, "store_unreadable", [], []
    if case == "directory":
        path.mkdir()
        return 3, "store_unreadable", [], []
    if case == "uninitialized":
        # A 0-byte file (a wrong path, or a store truncated by a failed
        # deploy) holds no job table, so it proves nothing: refuse it.
        with sqlite3.connect(path):
            pass
        return 3, "store_unreadable", [], []
    if case == "other-tables":
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE sentinel (x)")
        return 3, "store_unreadable", [], []
    if case == "scalar-job-json":
        # Valid JSON that is not an object must refuse, not crash.
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE media_jobs "
                "(job_id TEXT PRIMARY KEY, workflow TEXT, status TEXT, job_json TEXT)"
            )
            connection.execute(
                "INSERT INTO media_jobs VALUES (?, ?, ?, ?)",
                ("odd", "saved.workflow", "queued", '"42"'),
            )
        return 3, "store_unreadable", [], []
    if case == "old-schema":
        # Historical schema without application initialization, matching
        # test_workflow_run_contracts.py:544-563.
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE media_jobs "
                "(job_id TEXT PRIMARY KEY, workflow TEXT, status TEXT, job_json TEXT)"
            )
            connection.execute(
                "INSERT INTO media_jobs VALUES (?, ?, ?, ?)",
                ("old-queued", "saved.workflow", "queued", "{}"),
            )
        return 0, None, [], [{
            "job_id": "old-queued", "workflow": "saved.workflow",
            "status": "queued", "envelope_version": None,
            "reason": "pin_not_recorded",
        }]

    # This also covers a checkpointed WAL-mode database: init_db sets WAL,
    # closes its connection, and leaves no source companions behind.
    media_jobs.init_db(path)
    if case == "empty":
        return 0, None, [], []
    if case == "running":
        seed_job(path, "running-one", status="running")
        return 2, "running_jobs", ["running-one"], [{
            "job_id": "running-one", "workflow": None, "status": "running",
            "envelope_version": None, "reason": "pin_not_recorded",
        }]
    if case == "retained-deleted":
        seed_job(path, "deleted-one", status="succeeded")
        media_jobs.tombstone_job("deleted-one", now=1, path=path)
        return 2, "retained_deleted_jobs", ["deleted-one"], []
    assert case == "queued-v2"
    seed_job(path, "queued-v2", pinned=True)
    return 2, "queued_jobs_unreadable_by_older_build", ["queued-v2"], [{
        "job_id": "queued-v2", "workflow": "pinned.workflow", "status": "queued",
        "envelope_version": 2, "reason": "definition_unavailable",
    }]


def source_state(path):
    if not path.exists():
        return None
    if path.is_dir():
        return ("directory", path.stat().st_mtime_ns, sorted(p.name for p in path.iterdir()))
    return (path.read_bytes(), path.stat().st_mtime_ns)


def run_cli(tmp_path, path, *args):
    temporary = tmp_path / "temporary-snapshots"
    temporary.mkdir()
    before = source_state(path)
    companions = [Path(str(path) + suffix) for suffix in ("-wal", "-journal", "-shm")]
    assert all(not item.exists() for item in companions)
    environment = dict(os.environ, TMPDIR=str(temporary), TEMP=str(temporary),
                       TMP=str(temporary), PYTHONDONTWRITEBYTECODE="1")
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--store", str(path), *args],
            cwd=ROOT, env=environment, text=True, capture_output=True,
            check=False, timeout=30,
        )
    finally:
        assert source_state(path) == before, "the source bytes or mtime changed"
        assert all(not item.exists() for item in companions), "source SQLite companion appeared"
        assert list(temporary.iterdir()) == [], "temporary snapshot was not deleted"
    assert result.stderr == "", result.stderr
    return result


@pytest.mark.parametrize("json_output", [False, True], ids=["text", "json"])
@pytest.mark.parametrize("case", [
    "empty", "running", "retained-deleted", "queued-v2",
    "old-schema", "uninitialized", "other-tables", "scalar-job-json",
    "missing", "text", "directory",
])
def test_readiness_cli_preserves_source_and_reports_inventory(tmp_path, case, json_output):
    # URI punctuation must name this exact path, never become URI options.
    path = tmp_path / "source ?#% mode=rw.db"
    code, reason, job_ids, inventory = seed(path, case)
    result = run_cli(tmp_path, path, *(["--json"] if json_output else []))
    assert result.returncode == code, result.stdout
    if json_output:
        assert json.loads(result.stdout) == {
            "ready": code == 0, "reason": reason,
            "job_ids": job_ids, "inventory": inventory,
        }
        assert len(result.stdout.splitlines()) == 1
    else:
        lines = result.stdout.splitlines()
        assert lines[-1] == ("READY" if code == 0 else f"REFUSED {reason}")
        # Human mode still gives one complete inventory row per line.
        rows = [json.loads(line) for line in lines if line.startswith("{")]
        assert rows == inventory
        job_lines = [line for line in lines if line.startswith("JOB_IDS ")]
        assert job_lines == (["JOB_IDS " + json.dumps(job_ids)] if job_ids else [])
        assert len(lines) == len(inventory) + bool(job_ids) + 1


def test_store_argument_is_required_and_never_checks_a_default(tmp_path):
    # Place a ready-looking file in the cwd: it must not be selected implicitly.
    path = tmp_path / "media_jobs.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
    before = source_state(path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        cwd=tmp_path, text=True, capture_output=True, check=False, timeout=30,
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )
    assert result.returncode == 2
    assert "--store" in result.stderr
    assert result.stdout == ""
    assert source_state(path) == before
    assert all(not Path(str(path) + suffix).exists() for suffix in ("-wal", "-journal", "-shm"))


def test_a_live_wal_is_read_not_ignored_and_the_store_and_wal_are_untouched(tmp_path):
    path = tmp_path / "live.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE media_jobs "
            "(job_id TEXT PRIMARY KEY, workflow TEXT, status TEXT, job_json TEXT)"
        )
        connection.execute("INSERT INTO media_jobs VALUES ('live', 'w', 'running', '{}')")
        connection.commit()
        wal = Path(str(path) + "-wal")
        assert wal.stat().st_size > 0
        # A reader may update its read mark in the existing -shm, so only the database and the WAL must keep their bytes.
        files = [path, wal]
        before = {item: source_state(item) for item in files}
        temporary = tmp_path / "temporary-snapshots"
        temporary.mkdir()
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--store", str(path), "--json"],
            cwd=ROOT, text=True, capture_output=True, check=False, timeout=30,
            env=dict(os.environ, TMPDIR=str(temporary), TEMP=str(temporary),
                     TMP=str(temporary), PYTHONDONTWRITEBYTECODE="1"),
        )
        assert result.stderr == ""
        assert result.returncode == 2, "the running job lives only in the WAL; an immutable read would call the store ready"
        assert json.loads(result.stdout) == {
            "ready": False, "reason": "running_jobs", "job_ids": ["live"],
            "inventory": [{"envelope_version": None, "job_id": "live", "reason": "pin_not_recorded",
                           "status": "running", "workflow": "w"}],
        }
        assert {item: source_state(item) for item in files} == before
        assert not Path(str(path) + "-journal").exists()
        assert list(temporary.iterdir()) == []
    finally:
        connection.close()


def directory_state(path):
    """Include directory mtime to catch even a companion created then removed."""
    return path.stat().st_mtime_ns, {
        item.name: source_state(item) for item in path.iterdir()
    }


def store_state(path):
    """directory_state without the SHM bytes. Any SQLite reader of a live WAL store may write its read marks or
    rebuild the shared index in the SHM file; the store's data is the DB and WAL bytes, and the file set."""
    mtime, files = directory_state(path)
    return mtime, {name: (True if name.endswith("-shm") else state) for name, state in files.items()}


def test_locked_wal_backup_refuses_by_deadline_without_source_or_temp_changes(tmp_path):
    source_directory = tmp_path / "locked-source"
    source_directory.mkdir()
    path = source_directory / "locked.db"
    temporary = tmp_path / "temporary-snapshots"
    temporary.mkdir()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute(
            "CREATE TABLE media_jobs "
            "(job_id TEXT PRIMARY KEY, workflow TEXT, status TEXT, job_json TEXT)"
        )
        connection.execute("INSERT INTO media_jobs VALUES ('locked', 'w', 'running', '{}')")
        connection.commit()
        # Establish SHM before taking exclusive locking mode. This must exercise
        # the existing live-source backup path, not the private missing-SHM copy.
        assert Path(str(path) + "-wal").stat().st_size > 0
        assert Path(str(path) + "-shm").exists()
        assert connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone() == ("exclusive",)
        connection.execute("BEGIN EXCLUSIVE")
        connection.execute("UPDATE media_jobs SET workflow = 'held'")
        connection.commit()
        assert Path(str(path) + "-shm").exists()
        before = store_state(source_directory)
        # run_path returns a namespace copy; patch the function's actual globals.
        wrapper = (
            "import runpy, sys\n"
            "namespace = runpy.run_path(sys.argv[1], run_name='readiness_deadline_test')\n"
            "main = namespace['main']\n"
            "main.__globals__['BACKUP_TIMEOUT_SECONDS'] = 0.2\n"
            "raise SystemExit(main(sys.argv[2:]))\n"
        )
        try:
            result = subprocess.run(
                [sys.executable, "-c", wrapper, str(SCRIPT), "--store", str(path)],
                cwd=ROOT, text=True, capture_output=True, check=False, timeout=3,
                env=dict(os.environ, TMPDIR=str(temporary), TEMP=str(temporary),
                         TMP=str(temporary), PYTHONDONTWRITEBYTECODE="1"),
            )
        finally:
            # The parent still holds the exclusive lock at every assertion.
            assert store_state(source_directory) == before
            assert list(temporary.iterdir()) == []
        assert result.returncode == 3, result.stdout
        assert result.stdout.splitlines() == ["REFUSED store_unreadable"]
        assert result.stderr == ""
    finally:
        connection.close()


def test_wal_without_shm_is_inspected_privately_and_matches_quiesced_store(tmp_path):
    seed_directory = tmp_path / "seed"
    source_directory = tmp_path / "source"
    seed_directory.mkdir()
    source_directory.mkdir()
    seed_path = seed_directory / "seed.db"
    path = source_directory / "copy ?#%.db"
    connection = sqlite3.connect(seed_path)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute(
            "CREATE TABLE media_jobs "
            "(job_id TEXT PRIMARY KEY, workflow TEXT, status TEXT, job_json TEXT)"
        )
        connection.execute("INSERT INTO media_jobs VALUES ('wal-only', 'w', 'running', '{}')")
        connection.commit()
        seed_wal = Path(str(seed_path) + "-wal")
        assert seed_wal.stat().st_size > 0
        # Copy while the writer is still open, before its final-close checkpoint.
        path.write_bytes(seed_path.read_bytes())
        Path(str(path) + "-wal").write_bytes(seed_wal.read_bytes())
    finally:
        connection.close()
    assert not Path(str(path) + "-shm").exists()
    assert sorted(item.name for item in source_directory.iterdir()) == [
        path.name, path.name + "-wal",
    ]
    before = directory_state(source_directory)
    # This is the same committed store after its final-close checkpoint.
    expected = run_cli(tmp_path, seed_path, "--json")
    assert expected.returncode == 2
    expected_body = json.loads(expected.stdout)
    assert expected_body["job_ids"] == ["wal-only"]
    temporary = tmp_path / "temporary-snapshots"
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--store", str(path), "--json"],
            cwd=ROOT, text=True, capture_output=True, check=False, timeout=30,
            env=dict(os.environ, TMPDIR=str(temporary), TEMP=str(temporary),
                     TMP=str(temporary), PYTHONDONTWRITEBYTECODE="1"),
        )
    finally:
        assert directory_state(source_directory) == before, "source companion or bytes changed"
        assert list(temporary.iterdir()) == [], "temporary copy or snapshot was not deleted"
    assert result.stderr == ""
    assert result.returncode == expected.returncode
    assert json.loads(result.stdout) == expected_body
    assert len(result.stdout.splitlines()) == 1
