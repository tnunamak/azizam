"""Final prompt-memory regressions: upgrade, missing cache, locks and erasure."""
from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import proxy
from gateway import accounts, admin_store, media_jobs, prompt_memory, video_media_job_http
from gateway.admin_runtime import person_for_app_credential


@pytest.fixture
def household(tmp_path, monkeypatch):
    admin, jobs, responses = (tmp_path / name for name in ('admin.db', 'jobs.db', 'responses.db'))
    admin_store.init_db(admin)
    accounts.initialize(admin)
    media_jobs.init_db(jobs)
    proxy.responses_store.init_db(responses)
    monkeypatch.setattr(proxy.ADMIN_STORE, 'path', admin)
    monkeypatch.setattr(prompt_memory, 'DB_PATH', jobs)
    monkeypatch.setattr(media_jobs, 'DB_PATH', jobs)
    monkeypatch.setattr(proxy.responses_store, 'DB_PATH', responses)
    monkeypatch.setattr(proxy, 'IMAGE_CACHE_DIR', tmp_path / 'cache')
    monkeypatch.setattr(proxy, 'IMAGE_MEDIA_JOB_PAYLOAD_DIR', tmp_path / 'payloads')
    monkeypatch.setattr(proxy, '_EPHEMERAL_MEDIA_JOB_PAYLOADS', {})
    monkeypatch.setattr(proxy, '_EPHEMERAL_MEDIA_JOB_PAYLOAD_SIZES', {})
    people = [admin_store.upsert_oidc_user('test', name, path=admin) for name in ('owner', 'robin', 'other')]
    apps = [admin_store.create_app(name, kind='personal', owner_user_id=user['id'], path=admin)
            for name, user in zip(('owner', 'robin', 'other'), people)]
    auth = proxy.AuthContext(identity=proxy.AuthIdentity(name='Robin', key_hash='sha256:robin',
        app_id=apps[1]['id'], person_id=people[1]['id']))
    return SimpleNamespace(root=tmp_path, admin=admin, jobs=jobs, owner=people[0], robin=people[1],
                           other=people[2], app=apps[1], other_app=apps[2], auth=auth)


@pytest.mark.parametrize('action,target_kind', [('credential.create', 'credential'),
    ('credential.self_create', 'credential'), ('credential.create', 'account_link')])
@pytest.mark.parametrize('minter', ['owner', 'robin', 'automation'])
def test_upgrade_backfills_legacy_creator_from_creation_audit(household, action, target_kind, minter):
    hh = household
    key = admin_store.create_credential_hash(subject_kind='app', subject_id=hh.app['id'],
        hash='sha256:legacy', prefix='test', path=hh.admin)
    actor = getattr(hh, minter)['id'] if minter != 'automation' else 'svc_automation'
    admin_store.record_audit(action, actor_kind='service_account' if minter == 'automation' else 'user',
        actor_id=actor, target_kind=target_kind, target_id=key['id'], path=hh.admin)
    admin_store.init_db(hh.admin)
    upgraded = admin_store.find_credential_by_hash('sha256:legacy', path=hh.admin)
    expected = hh.robin['id'] if minter == 'robin' else ''
    assert upgraded['created_by'] == expected
    assert person_for_app_credential(hh.app, upgraded) == (hh.robin['id'] if minter == 'robin' else None)
    admin_store.init_db(hh.admin)
    assert admin_store.find_credential_by_hash('sha256:legacy', path=hh.admin)['created_by'] == expected


def test_upgrade_preserves_unknown_legacy_and_explicit_creator(household, capsys):
    hh = household
    for suffix, creator in [('unknown', None), ('known', hh.robin['id'])]:
        key = admin_store.create_credential_hash(subject_kind='app', subject_id=hh.app['id'],
            hash=f'sha256:{suffix}', prefix='test', created_by=creator, path=hh.admin)
        if creator:
            admin_store.record_audit('credential.create', actor_kind='user', actor_id=hh.owner['id'],
                target_kind='credential', target_id=key['id'], path=hh.admin)
    admin_store.init_db(hh.admin)
    unknown = admin_store.find_credential_by_hash('sha256:unknown', path=hh.admin)
    assert unknown['created_by'] is None
    assert person_for_app_credential(hh.app, unknown) is None
    assert admin_store.find_credential_by_hash('sha256:known', path=hh.admin)['created_by'] == hh.robin['id']
    assert 'unresolved=1' in capsys.readouterr().out


@pytest.mark.parametrize('cache_state', ['missing', 'empty'])
def test_maintenance_does_not_erase_prompts_without_cache_inventory(household, cache_state):
    hh = household
    if cache_state == 'empty':
        proxy.IMAGE_CACHE_DIR.mkdir()
    asset = 'img_' + 'a' * 64
    assert prompt_memory.put(asset, hh.robin['id'], 'saved text', kept_until=None, admin_path=hh.admin)
    proxy._maintain_prompt_memory()
    assert prompt_memory.get(asset, hh.robin['id'])['prompt_text'] == 'saved text'


def test_request_policy_reads_do_not_wait_for_admin_writer(household):
    hh = household
    with sqlite3.connect(hh.admin) as lock:
        lock.execute('BEGIN IMMEDIATE')
        decision = prompt_memory.decide_for_person(hh.robin['id'], {'prompt': 'private'}, admin_path=hh.admin)
        # The writer holds its lock for the whole block, so a read that waited
        # on it would end in storage_unavailable, not keep.
        assert decision.keep
        # The final write guard remains conservative while a mutation is pending.
        assert not prompt_memory.safe_put('video_locked', hh.robin['id'], 'private',
                                         kept_until=None, admin_path=hh.admin)
    assert prompt_memory.get('video_locked', hh.robin['id']) is None


def _job(hh, *, other=False, seeded=True, created_at=None):
    auth = hh.auth if not other else proxy.AuthContext(identity=proxy.AuthIdentity(
        name='Other', key_hash='sha256:other', app_id=hh.other_app['id'], person_id=hh.other['id']))
    body = {'prompt': 'dedupe secret', **({'seed': 12} if seeded else {})}
    request_id, digest, mode = proxy._image_media_job_request_id(body=body, auth_context=auth,
        idempotency_key=None, fallback_request_id='request_other' if other else 'request_robin')
    job = media_jobs.MediaJob(intent='image.generate', request_id=request_id,
        app_id=hh.other_app['id'] if other else hh.app['id'], metadata={'request_hash': digest, 'idempotency': mode},
        created_at=created_at or int(time.time()) - 10)
    return media_jobs.create(job, path=hh.jobs), digest


@pytest.mark.parametrize('seeded', [False, True])
def test_delete_erases_prompt_hash_and_seeded_dedupe_id_without_harming_other(household, seeded):
    hh = household
    job, digest = _job(hh, seeded=seeded)
    original_request_id = job.request_id
    other, other_digest = _job(hh, other=True, seeded=seeded)
    proxy._purge_person_prompt_memory(hh.robin['id'])
    retained = media_jobs.get(job.job_id, audience='internal', path=hh.jobs)
    assert digest not in json.dumps(retained)
    assert not retained['request_id'].startswith('imgjob_seeded_')
    assert media_jobs.get(other.job_id, audience='internal', path=hh.jobs)['metadata']['request_hash'] == other_digest
    # A stale worker must not restore the old digest or dedupe key.
    media_jobs.upsert(job, path=hh.jobs)
    assert digest not in json.dumps(media_jobs.get(job.job_id, audience='internal', path=hh.jobs))
    if seeded:
        assert media_jobs.get_by_request_id(original_request_id, path=hh.jobs) is None
        with sqlite3.connect(hh.jobs) as conn:
            assert conn.execute('SELECT request_id FROM media_jobs WHERE job_id=?', (job.job_id,)).fetchone()[0] is None
    else:
        assert media_jobs.get_by_request_id(original_request_id, path=hh.jobs)['job_id'] == job.job_id


def test_household_kill_erases_all_prompt_derivatives(household):
    hh = household
    job, digest = _job(hh)
    proxy._purge_prompt_memory_on_kill()
    assert digest not in json.dumps(media_jobs.get(job.job_id, audience='internal', path=hh.jobs))


def test_video_request_hash_is_not_derived_from_prompt():
    context = video_media_job_http.VideoRequestContext(workflow='test')
    body = {'prompt': 'guessable video text'}
    first = video_media_job_http.job_from_openai_video_request(body, context)
    second = video_media_job_http.job_from_openai_video_request(body, context)
    assert first.metadata['request_hash'] != second.metadata['request_hash']


def test_disable_retains_prompt_and_chat_then_remove_erases_both(household):
    hh = household
    asset = 'img_' + 'b' * 64
    assert prompt_memory.put(asset, hh.robin['id'], 'keep while disabled', kept_until=None, admin_path=hh.admin)
    proxy.responses_store.save('resp_disabled', request_json={'input': 'keep chat'}, response_json={},
        api_key_name='Robin', app_id=hh.app['id'], person_id=hh.robin['id'], created_at=int(time.time()) - 10,
        model='test', previous_response_id=None, path=proxy.responses_store.DB_PATH)
    accounts.change_person(hh.robin['id'], hh.owner['id'], disabled=True, path=hh.admin)
    proxy._maintain_prompt_memory()
    assert prompt_memory.get(asset, hh.robin['id']) is not None
    assert proxy.responses_store.get('resp_disabled', path=proxy.responses_store.DB_PATH) is not None
    assert prompt_memory.recorded_erasure_cutoff(hh.robin['id'], admin_path=hh.admin) == 0
    accounts.change_person(hh.robin['id'], hh.owner['id'], disabled=False, path=hh.admin)
    proxy._maintain_prompt_memory()
    assert prompt_memory.get(asset, hh.robin['id']) is not None
    accounts.change_person(hh.robin['id'], hh.owner['id'], remove=True, path=hh.admin)
    proxy._maintain_prompt_memory()
    assert prompt_memory.get(asset, hh.robin['id']) is None
    assert proxy.responses_store.get('resp_disabled', path=proxy.responses_store.DB_PATH) is None


def test_creator_backfill_uses_short_transactions(household):
    hh = household
    with sqlite3.connect(hh.admin) as conn:
        conn.executemany(
            "INSERT INTO credentials(id,subject_kind,subject_id,app_id,hash,prefix,created_at) "
            "VALUES(?,'app',?,?,?,'test',1)",
            [(f'cred_batch_{i:03}', hh.app['id'], hh.app['id'], f'sha256:batch{i}') for i in range(205)],
        )
        conn.executemany(
            "INSERT INTO audit_events(id,created_at,actor_kind,actor_id,action,target_kind,target_id) "
            "VALUES(?,1,'user',?,'credential.create','credential',?)",
            [(f'aud_batch_{i:03}', hh.owner['id'], f'cred_batch_{i:03}') for i in range(205)],
        )
    batches = []
    audit_locks = []
    writes = 0
    with sqlite3.connect(hh.admin) as conn:
        def trace(statement):
            nonlocal writes
            if statement.startswith('SELECT actor_kind'):
                audit_locks.append(conn.in_transaction)
            if statement.startswith('UPDATE credentials'):
                writes += 1
            if statement == 'COMMIT':
                batches.append(writes)
                writes = 0
        conn.set_trace_callback(trace)
        admin_store._backfill_credential_creators(conn)
        assert batches == [100, 100, 5]
        assert audit_locks and not any(audit_locks)
        assert conn.execute("SELECT COUNT(*) FROM credentials WHERE created_by='' ").fetchone()[0] == 205


def test_missing_preference_under_lock_degrades_to_dont_store(household):
    hh = household
    with sqlite3.connect(hh.admin) as conn:
        conn.execute('DELETE FROM prompt_memory_preferences WHERE user_id=?', (hh.robin['id'],))
    statements: list[str] = []
    real_conn = admin_store._conn

    @contextlib.contextmanager
    def traced_conn(path=admin_store.DB_PATH):
        with real_conn(path) as conn:
            conn.set_trace_callback(statements.append)
            yield conn

    with sqlite3.connect(hh.admin) as lock, patch.object(admin_store, '_conn', traced_conn):
        lock.execute('BEGIN IMMEDIATE')
        decision = prompt_memory.decide_for_person(hh.robin['id'], {'prompt': 'private'}, admin_path=hh.admin)
        # The request path must not queue behind the writer: it asks for no busy
        # wait before it asks for its own write lock.
        assert statements.index('PRAGMA busy_timeout=0') < statements.index('BEGIN IMMEDIATE')
        assert decision.keep is False
        assert decision.reason == 'storage_unavailable'


def test_explicit_prompt_deadline_still_expires_prompts(household):
    hh = household
    expiry = int(time.time()) + 30
    assert prompt_memory.put('img_' + 'c' * 64, hh.robin['id'], 'expired',
        kept_until=expiry, admin_path=hh.admin)
    assert prompt_memory.prune(now=expiry + 1) == 1


def test_delete_scrubs_legacy_credential_scoped_video_job(household):
    hh = household
    credential = admin_store.create_credential_hash(subject_kind='app', subject_id=hh.app['id'],
        hash='sha256:old-key', prefix='test', path=hh.admin)
    job = media_jobs.create(media_jobs.MediaJob(intent='video.generate', owner_key_hash=credential['hash'],
        metadata={'request_hash': 'legacy-video-body-hash'}, created_at=int(time.time()) - 10), path=hh.jobs)
    proxy._purge_person_prompt_memory(hh.robin['id'])
    assert 'legacy-video-body-hash' not in json.dumps(media_jobs.get(job.job_id, audience='internal', path=hh.jobs))


def test_delete_preserves_new_jobs_but_scrubs_late_old_insert(household):
    hh = household
    cutoff = int(time.time()) - 20
    old = media_jobs.MediaJob(intent='image.generate', app_id=hh.app['id'],
        request_id='imgjob_seeded_old', metadata={'request_hash': 'old'}, created_at=cutoff - 1)
    proxy._purge_person_prompt_memory(hh.robin['id'], cutoff=cutoff)
    media_jobs.create(old, path=hh.jobs)
    assert 'request_hash' not in media_jobs.get(old.job_id, audience='internal', path=hh.jobs)['metadata']
    new, digest = _job(hh, created_at=cutoff + 1)
    proxy._maintain_prompt_memory()
    assert media_jobs.get(new.job_id, audience='internal', path=hh.jobs)['metadata']['request_hash'] == digest


def test_delete_preserves_caller_idempotency_and_plain_request_ids(household):
    hh = household
    request_id, digest, mode = proxy._image_media_job_request_id(body={'prompt': 'private'},
        auth_context=hh.auth, idempotency_key='client-retry-123', fallback_request_id='original')
    job = media_jobs.create(media_jobs.MediaJob(intent='image.generate', request_id=request_id,
        app_id=hh.app['id'], metadata={'request_hash': digest, 'idempotency': mode}), path=hh.jobs)
    proxy._purge_person_prompt_memory(hh.robin['id'])
    retained = media_jobs.get_by_request_id(request_id, audience='internal', path=hh.jobs)
    assert retained['job_id'] == job.job_id
    assert 'request_hash' not in retained['metadata']


def test_unseeded_image_request_token_is_not_prompt_derived(household):
    hh = household
    kwargs = dict(body={'prompt': 'guessable text'}, auth_context=hh.auth,
                  idempotency_key=None, fallback_request_id='ordinary')
    first, digest, mode = proxy._image_media_job_request_id(**kwargs)
    second, next_digest, next_mode = proxy._image_media_job_request_id(**kwargs)
    assert first == second == 'ordinary'
    assert mode == next_mode == 'request'
    assert digest != next_digest


def test_erasure_before_first_media_job_store_initialization(tmp_path):
    path = tmp_path / 'not_initialized.db'
    cutoff = int(time.time())
    assert media_jobs.erase_prompt_derivatives({'app:member'}, before=cutoff, path=path) == 0
    media_jobs.init_db(path)
    job = media_jobs.create(media_jobs.MediaJob(intent='image.generate', app_id='member',
        created_at=cutoff - 1, request_id='imgjob_seeded_old', metadata={'request_hash': 'forgotten'}), path=path)
    retained = media_jobs.get(job.job_id, audience='internal', path=path)
    assert retained['request_id'] == ''
    assert 'request_hash' not in retained['metadata']
