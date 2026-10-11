"""Safety regressions for private cursors, retries, and trusted phase facts."""
import asyncio
import json
import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import media_jobs, receipt_store
from gateway.api import create_gateway_router
from gateway.discovery import WorkflowRegistry
from gateway.receipts import ReceiptLifecycleOwner, RouteReceipt


@pytest.fixture
def db(tmp_path):
    path = tmp_path / 'jobs.db'
    media_jobs.init_db(path)
    return path


def job(db, **kwargs):
    return media_jobs.create(media_jobs.MediaJob(intent='image.generate', **kwargs), path=db)


@pytest.mark.parametrize('audience', ['client', 'admin', 'internal'])
def test_cursors_are_local_and_pollable(db, audience):
    a, b = job(db), job(db)
    assert media_jobs.append_event(a.job_id, 'started', path=db) == 1
    media_jobs.append_event(b.job_id, 'started', path=db)
    assert media_jobs.append_event(a.job_id, 'progress', path=db) == 2
    events = media_jobs.events(a.job_id, audience=audience, after_sequence=1, path=db)
    assert [e['sequence'] for e in events] == [2]
    assert [e['id'] for e in events] == [3]


@pytest.mark.parametrize('change', [dict(app_id='other'), dict(intent='video.generate'),
                                      dict(workflow='other'), dict(metadata={'width': 20})])
def test_request_id_collision_rejected(db, change):
    payload = dict(intent='image.generate', app_id='one', request_id='retry', metadata={'width': 10})
    first = media_jobs.create(payload, path=db)
    with pytest.raises(ValueError, match='idempotency') as exc:
        media_jobs.create({**payload, **change}, path=db)
    assert exc.value.status_code == 409
    assert media_jobs.count(db) == 1
    assert media_jobs.create(payload, path=db).job_id == first.job_id


def test_api_notes_cannot_forge_lifecycle(db):
    a = job(db)
    app = FastAPI()
    app.include_router(create_gateway_router(WorkflowRegistry(), media_job_store_path=db))
    with TestClient(app) as client:
        url = f'/gateway/v1/media/jobs/{a.job_id}/events'
        for event in [{'event_type': 'started'}, {'event_type': 'succeeded'},
                      {'event_type': 'progress', 'payload': {'phase': 'finished', 'status': 'succeeded'}}]:
            assert client.post(url, json=event).status_code == 400
        response = client.post(url, json={'event_type': 'progress', 'payload': {'progress': 20}})
        assert response.status_code == 201
        events = client.get(url).json()['events']
        assert events[0]['source'] == 'client'
    assert media_jobs.get(a.job_id, path=db)['status'] == 'queued'


def test_job_phases_are_once_only(db, monkeypatch):
    monkeypatch.setattr(media_jobs, '_now', lambda: 100)
    a = job(db)
    assert a.to_dict('client')['queued_at'] == 100
    assert a.to_dict()['attempt_number'] == 1
    media_jobs.update_status(a.job_id, 'running', now=200, path=db)
    media_jobs.append_event(a.job_id, 'progress', {'phase': 'finished'}, now=220, path=db)
    media_jobs.update_status(a.job_id, 'running', now=300, path=db)
    done = media_jobs.update_status(a.job_id, 'succeeded', add_assets=[{'id': 'a'}], now=400, path=db)
    again = media_jobs.update_status(a.job_id, 'succeeded', now=500, path=db)
    assert (done['started_at'], done['first_output_at'], done['finished_at']) == (200, 400, 400)
    assert again['finished_at'] == 400
    assert again['updated_at'] == 500


def test_legacy_phases_unavailable(db, tmp_path):
    a = job(db)
    fields = ('accepted_at', 'queued_at', 'started_at', 'first_output_at', 'finished_at', 'attempt_number')
    with sqlite3.connect(db) as conn:
        data = json.loads(conn.execute('SELECT job_json FROM media_jobs').fetchone()[0])
        for field in fields:
            data.pop(field, None)
        conn.execute('UPDATE media_jobs SET job_json=?', (json.dumps(data),))
    media_jobs.init_db(db)
    media_jobs.init_db(db)
    legacy = media_jobs.get(a.job_id, audience='client', path=db)
    assert all(field in legacy and legacy[field] is None for field in fields)
    path = tmp_path / 'receipts.db'
    receipt_store.init_db(path)
    receipt_store.record({'request_id': 'legacy', 'status': 'succeeded'}, path=path)
    legacy = receipt_store.get('legacy', audience='client', path=path)
    assert legacy['finished_at_ms'] is None
    assert legacy['attempt_number'] is None


def test_receipt_phases_and_stream(tmp_path):
    path = tmp_path / 'receipts.db'
    receipt_store.init_db(path)
    receipt = RouteReceipt(protocol='test', path='/test', intent='chat', stream=True)
    async def source():
        yield b''
        yield b': keepalive\n\n'
        assert receipt.first_output_at_ms is None
        yield b'data: {"text":"hello"}\n\n'
    async def run():
        owner = ReceiptLifecycleOwner(receipt, begin=lambda r: receipt_store.begin(r, now_ms=100, path=path),
                                      finalize=lambda r: receipt_store.finalize(r, path=path))
        owner.report(status='succeeded', reason='completed')
        async for _ in owner.wrap_stream(source()):
            pass
    asyncio.run(run())
    row = receipt_store.get(receipt.request_id, audience='client', path=path)
    assert row['accepted_at_ms'] == 100
    assert row['attempt_number'] == 1
    assert row['first_output_at_ms'] <= row['finished_at_ms']
    first = receipt.finished_at_ms
    receipt.finish('succeeded', now_ms=9999999999999)
    assert receipt.finished_at_ms == first


def test_state_predicates_cover_existing_vocabulary():
    for status in media_jobs.ALL_STATUSES:
        assert media_jobs.is_terminal(status) == (status in {'succeeded', 'failed', 'canceled'})
        assert media_jobs.holds_resources(status) == (status in {'queued', 'running'})


def test_v1_event_migration_is_idempotent(tmp_path):
    path = tmp_path / 'v1.db'
    with sqlite3.connect(path) as conn:
        conn.executescript(media_jobs._SCHEMA)
        conn.execute('INSERT INTO media_jobs_schema_version VALUES (1)')
        for global_id, job_id in [(4, 'one'), (8, 'two'), (12, 'one')]:
            conn.execute('INSERT INTO media_job_events(id,job_id,created_at,event_type,payload_json) VALUES (?,?,1,?,?)',
                         (global_id, job_id, 'started', '{}'))
    media_jobs.init_db(path)
    media_jobs.init_db(path)
    events = media_jobs.events('one', after_sequence=1, audience='client', path=path)
    assert [e['sequence'] for e in events] == [2]
    assert [e['id'] for e in events] == [12]
    assert events[0]['source'] == 'unavailable'
    with sqlite3.connect(path) as conn:
        assert [r[0] for r in conn.execute('SELECT id FROM media_job_events ORDER BY id')] == [4, 8, 12]


def test_concurrent_cursors_are_unique_and_contiguous(db):
    from concurrent.futures import ThreadPoolExecutor
    a = job(db)
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: media_jobs.append_event(a.job_id, 'progress', path=db), range(12)))
    assert sorted(ids) == list(range(1, 13))


def test_request_digest_covers_input_and_ignores_forged_digest(db):
    payload = dict(intent='image.generate', app_id='one', request_id='retry', input_hash='forged')
    first = media_jobs.create(payload, input_data={'prompt': 'one'}, path=db)
    with pytest.raises(ValueError, match='idempotency'):
        media_jobs.create(payload, input_data={'prompt': 'two'}, path=db)
    media_jobs.update_status(first.job_id, 'running', metadata_patch={'step': 3}, path=db)
    assert media_jobs.create(payload, input_data={'prompt': 'one'}, path=db).job_id == first.job_id
    row = media_jobs.get(first.job_id, audience='client', path=db)
    assert 'input_hash' not in row and 'owner_scope' not in row


def test_api_conflict_is_409_and_injected_appender_cannot_bypass(db):
    a = job(db, request_id='retry')
    calls = []
    app = FastAPI()
    app.include_router(create_gateway_router(WorkflowRegistry(), media_job_store_path=db,
        media_job_event_appender=lambda *args, **kwargs: calls.append(args)))
    with TestClient(app) as client:
        response = client.post('/gateway/v1/media/jobs', json={'intent': 'video.generate', 'request_id': 'retry'})
        assert response.status_code == 409
        assert response.json()['error']['code'] == 'idempotency_conflict'
        assert client.post(f'/gateway/v1/media/jobs/{a.job_id}/events', json={'event_type': 'finished'}).status_code == 400
    assert not calls


def test_receipt_checkpoints_survive_reconstructed_finalizer(tmp_path):
    path = tmp_path / 'receipts.db'
    receipt_store.init_db(path)
    receipt = RouteReceipt(protocol='test', intent='chat')
    receipt_store.begin(receipt, now_ms=100, path=path)
    receipt.mark_phase('queued_at_ms', now_ms=110)
    receipt.mark_phase('started_at_ms', now_ms=120)
    receipt.mark_phase('first_output_at_ms', now_ms=130)
    receipt_store.update_phases(receipt, path=path)
    receipt.mark_phase('started_at_ms', now_ms=999)
    assert receipt.started_at_ms == 120
    final = RouteReceipt(protocol='test', intent='chat', request_id=receipt.request_id)
    final.finish('succeeded', now_ms=140)
    assert receipt_store.finalize(final, path=path)
    row = receipt_store.get(receipt.request_id, path=path)
    assert [row[k] for k in ('queued_at_ms', 'started_at_ms', 'first_output_at_ms', 'finished_at_ms')] == [110, 120, 130, 140]
    assert receipt_store.finalize(final, path=path) is False


def test_running_jobs_hold_limit_until_terminal(db):
    limit = media_jobs.OutstandingLimit(scope='person', maximum=1, mode='test')
    a = job(db, metadata={'mode': 'test'})
    for status in ('queued', 'running'):
        media_jobs.update_status(a.job_id, status, path=db)
        with pytest.raises(media_jobs.OutstandingLimitReached):
            media_jobs.create({'intent': 'image.generate', 'metadata': {'mode': 'test'}},
                              outstanding_limits=[limit], path=db)
    media_jobs.update_status(a.job_id, 'failed', path=db)
    media_jobs.create({'intent': 'image.generate', 'metadata': {'mode': 'test'}},
                      outstanding_limits=[limit], path=db)


def test_stale_upsert_cannot_erase_observed_phases(db):
    a = job(db)
    media_jobs.update_status(a.job_id, 'running', now=200, path=db)
    media_jobs.update_status(a.job_id, 'succeeded', now=300, path=db)
    a.status = 'succeeded'
    media_jobs.upsert(a, path=db)
    row = media_jobs.get(a.job_id, path=db)
    assert row['started_at'] == 200
    assert row['finished_at'] == 300


def test_first_output_detected_after_comment_in_same_chunk():
    receipt = RouteReceipt(protocol='test', intent='chat', stream=True)
    async def source():
        yield b': comment\n\ndata: hello\n\n'
    async def run():
        owner = ReceiptLifecycleOwner(receipt, begin=lambda r: r.begin(), finalize=lambda r: None)
        owner.report(status='succeeded', reason='completed')
        async for _ in owner.wrap_stream(source()):
            pass
    asyncio.run(run())
    assert receipt.first_output_at_ms is not None


def test_retries_keep_prior_phase_facts_and_number_new_attempt(db):
    a = job(db)
    media_jobs.update_status(a.job_id, 'running', now=200, path=db)
    retry = media_jobs.begin_attempt(a.job_id, now=300, path=db)
    assert retry['attempt_number'] == 2
    assert retry['started_at'] == 300
    assert retry['queued_at'] is None
    assert retry['previous_attempts'][0]['started_at'] == 200
    assert retry['previous_attempts'][0]['finished_at'] is None
    done = media_jobs.update_status(a.job_id, 'succeeded', now=400, path=db)
    assert done['finished_at'] == 400
    assert done['previous_attempts'] == retry['previous_attempts']
    with pytest.raises(ValueError):
        media_jobs.begin_attempt(a.job_id, path=db)


def test_new_jobs_cannot_inject_phase_history(db):
    a = job(db, started_at=1, finished_at=2, previous_attempts=[{'started_at': 1}])
    assert a.started_at is None and a.finished_at is None and a.previous_attempts == []
    with pytest.raises(ValueError):
        job(db, previous_attempts=[{'prompt': 'must not leak'}])


def test_old_attempt_cannot_fill_new_attempt_output(db):
    a = job(db)
    media_jobs.update_status(a.job_id, 'running', add_assets=[{'id': 'old'}], now=200, path=db)
    old = media_jobs.get(a.job_id, path=db)
    media_jobs.begin_attempt(a.job_id, now=300, path=db)
    media_jobs.upsert(old, path=db)
    current = media_jobs.get(a.job_id, path=db)
    assert current['attempt_number'] == 2
    assert current['started_at'] == 300
    assert current['first_output_at'] is None


def test_only_trusted_events_carry_observed_attempt_number(db):
    a = job(db)
    media_jobs.update_status(a.job_id, 'running', now=200, path=db)
    media_jobs.append_event(a.job_id, 'started', now=200, path=db)
    media_jobs.begin_attempt(a.job_id, now=300, path=db)
    media_jobs.append_event(a.job_id, 'started', now=300, path=db)
    media_jobs.append_client_event(a.job_id, 'progress', {'progress': 20}, path=db)
    rows = media_jobs.events(a.job_id, audience='client', path=db)
    assert [row['attempt_number'] for row in rows] == [1, 2, None]
    assert [row['source'] for row in rows] == ['gateway', 'gateway', 'client']
