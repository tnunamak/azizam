"""Upgrade, mixed-writer, and public-boundary lifecycle regressions."""
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import media_jobs
from gateway.api import create_gateway_router
from gateway.discovery import WorkflowRegistry


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "jobs.db"
    media_jobs.init_db(path)
    return path


def client_for(db):
    app = FastAPI()
    app.include_router(create_gateway_router(WorkflowRegistry(), media_job_store_path=db))
    return TestClient(app)


def baseline_job(conn, job_id, request_id=""):
    payload = dict(job_id=job_id, request_id=request_id, intent="image.generate",
                   app_id="one", workflow=None, backend=None, metadata={}, assets=[],
                   status="queued", progress=0, created_at=1, updated_at=1)
    conn.execute(
        "INSERT INTO media_jobs(job_id,request_id,intent,status,progress,created_at,updated_at,job_json) "
        "VALUES (?,?,?,'queued',0,1,1,?)",
        (job_id, request_id or None, payload["intent"], json.dumps(payload)),
    )
    return payload


def old_insert(conn, job_id, event_type="progress"):
    return conn.execute(
        "INSERT INTO media_job_events(job_id, created_at, event_type, payload_json) VALUES (?, ?, ?, ?)",
        (job_id, 1, event_type, "{}"),
    ).lastrowid


def test_global_cursor_survives_upgrade(tmp_path):
    db = tmp_path / "v1.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(media_jobs._SCHEMA)
        conn.execute("INSERT INTO media_jobs_schema_version VALUES (1)")
        baseline_job(conn, "one")
        baseline_job(conn, "two")
        for job_id in ("one", "two", "one", "two"):
            cursor = old_insert(conn, job_id)
    assert cursor == 4
    media_jobs.init_db(db)
    media_jobs.append_event("one", "progress", path=db)
    media_jobs.append_event("two", "succeeded", path=db)
    with client_for(db) as client:
        url = "/gateway/v1/media/jobs/two/events"
        response = client.get(url, params={"after_id": cursor})
        assert response.status_code == 200
        tail = response.json()["events"]
        assert [event["event_type"] for event in tail] == ["succeeded"]
        # A saved global cursor still resumes without skipping, but callers get
        # per-job sequences, never the household-wide id.
        assert [event["sequence"] for event in tail] == [3]
        assert all("id" not in event for event in tail)
        assert client.get(url, params={"after_id": 6}).json()["events"] == []
        assert client.get(url, params={"after_sequence": 2}).json()["events"] == tail
        assert client.get(url, params={"after_sequence": 3}).json()["events"] == []
        assert client.get(url, params={"after_id": 4, "after_sequence": 2}).status_code == 400


def test_old_writer_is_sequenced_in_its_insert_transaction(db):
    job = media_jobs.create({"intent": "image.generate"}, path=db)
    media_jobs.append_event(job.job_id, "started", path=db)
    with sqlite3.connect(db) as conn:
        first_id = old_insert(conn, job.job_id)
        second_id = old_insert(conn, job.job_id)
        assert conn.execute("SELECT sequence FROM media_job_events WHERE id IN (?,?) ORDER BY id",
                            (first_id, second_id)).fetchall() == [(2,), (3,)]
    media_jobs.init_db(db)
    media_jobs.append_event(job.job_id, "succeeded", path=db)
    events = media_jobs.events(job.job_id, path=db)
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert [event["source"] for event in events] == ["gateway", "unavailable", "unavailable", "gateway"]
    assert events[1]["attempt_number"] is None


@pytest.mark.parametrize("reopen", [False, True])
def test_null_sequence_repair_preserves_published_cursors(db, reopen):
    job = media_jobs.create({"intent": "image.generate"}, path=db)
    media_jobs.append_event(job.job_id, "started", path=db)
    # Reproduce rows left by the first v2 migration before compatibility triggers.
    with sqlite3.connect(db) as conn:
        triggers = conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='media_job_events'").fetchall()
        for (name,) in triggers:
            conn.execute(f'DROP TRIGGER "{name}"')
        missing_id = old_insert(conn, job.job_id, "note")
        conn.execute("INSERT INTO media_job_events(job_id,created_at,event_type,payload_json,sequence) "
                     "VALUES (?,1,'succeeded','{}',7)", (job.job_id,))
    if reopen:
        media_jobs.init_db(db)
        media_jobs.init_db(db)
    events = media_jobs.events(job.job_id, after_sequence=0, path=db)
    assert [event["sequence"] for event in events] == [1, 7, 8]
    assert events[-1]["id"] == missing_id
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT sequence FROM media_job_events ORDER BY id").fetchall() == [(1,), (8,), (7,)]
    assert media_jobs.events(job.job_id, after_sequence=8, path=db) == []
    first_page = media_jobs.events(job.job_id, limit=2, path=db)
    last_page = media_jobs.events(job.job_id, after_id=first_page[-1]["id"], limit=2, path=db)
    assert [event["event_type"] for event in first_page + last_page] == ["started", "note", "succeeded"]


def test_baseline_job_without_scope_and_digest_refuses_replay(db):
    with sqlite3.connect(db) as conn:
        payload = baseline_job(conn, "legacy", "retry")
    with pytest.raises(media_jobs.IdempotencyConflict):
        media_jobs.create(payload, path=db)
    with client_for(db) as client:
        assert client.post("/gateway/v1/media/jobs", json=payload).status_code == 409


@pytest.mark.parametrize("field", ["owner_scope", "input_hash"])
def test_missing_stored_retry_proof_refuses_replay(db, field):
    payload = dict(intent="image.generate", app_id="one", request_id="retry")
    job = media_jobs.create(payload, path=db)
    with sqlite3.connect(db) as conn:
        data = json.loads(conn.execute("SELECT job_json FROM media_jobs").fetchone()[0])
        data.pop(field)
        conn.execute("UPDATE media_jobs SET job_json=?", (json.dumps(data),))
    with pytest.raises(media_jobs.IdempotencyConflict):
        media_jobs.create(payload, path=db)
    assert media_jobs.count(db) == 1


def test_erased_digest_refuses_replay(db):
    payload = dict(intent="image.generate", app_id="one", request_id="retry")
    job = media_jobs.create(payload, path=db)
    media_jobs.erase_prompt_derivatives({job.owner_key_hash}, before=job.created_at, path=db)
    assert media_jobs.get(job.job_id, path=db)["input_hash"] is None
    with pytest.raises(media_jobs.IdempotencyConflict):
        media_jobs.create(payload, path=db)


@pytest.mark.parametrize("created_at", [1, 9999999999])
def test_raw_create_does_not_observe_caller_lifecycle(db, monkeypatch, created_at):
    monkeypatch.setattr(media_jobs, "_now", lambda: 12345)
    with client_for(db) as client:
        response = client.post("/gateway/v1/media/jobs", json={
            "intent": "image.generate", "created_at": created_at,
            "status": "succeeded", "assets": [{"asset_id": "made-up"}],
            "progress": 100, "accepted_at": 1, "finished_at": 1,
        })
    assert response.status_code == 201
    job = response.json()
    assert job["accepted_at"] == job["queued_at"] == 12345
    assert job["status"] == "queued" and job["progress"] == 0 and job["assets"] == []
    assert job["started_at"] is job["first_output_at"] is job["finished_at"] is None


def test_store_observations_use_store_clock(db, monkeypatch):
    monkeypatch.setattr(media_jobs, "_now", lambda: 12345)
    job = media_jobs.create({"intent": "image.generate", "created_at": 1, "status": "succeeded",
                             "assets": [{"asset_id": "trusted"}]}, path=db)
    assert job.created_at == 1
    assert job.accepted_at == job.finished_at == job.first_output_at == 12345


def test_append_response_gives_callers_the_per_job_cursor_only(db):
    a = media_jobs.create({"intent": "image.generate"}, path=db)
    b = media_jobs.create({"intent": "image.generate"}, path=db)
    media_jobs.append_event(a.job_id, "started", path=db)
    with client_for(db) as client:
        url = f"/gateway/v1/media/jobs/{b.job_id}/events"
        response = client.post(url, json={"event_type": "note"})
        assert response.status_code == 201
        assert "id" not in response.json()
        assert response.json()["sequence"] == 1
        event = client.get(url).json()["events"][0]
        assert "id" not in event and event["sequence"] == 1
