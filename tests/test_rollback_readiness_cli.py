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
        with sqlite3.connect(path):
            pass
        return 0, None, [], []
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
    "old-schema", "uninitialized", "missing", "text", "directory",
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
            "ready": False, "reason": "running_jobs", "job_ids": ["live"], "inventory": [],
        }
        assert {item: source_state(item) for item in files} == before
        assert not Path(str(path) + "-journal").exists()
        assert list(temporary.iterdir()) == []
    finally:
        connection.close()
