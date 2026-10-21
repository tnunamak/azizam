"""Round-2 cleanup contracts: maintenance never acts on a stale "off" decision
(P2-19), and erasure removes rows at once while cached-file stripping is a
durable, visible, retried queue that never blocks the request (P2-20)."""
from __future__ import annotations

import asyncio
import threading
import time
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

import proxy
from tests.support.access import auth_identity  # noqa: E402
from gateway import admin_store, media_jobs, media_metadata, prompt_memory
from gateway import prompt_metadata_migration as pmm
from gateway.admin_oidc import AdminSession
from gateway.me_prompt_memory import create_me_prompt_memory_router

_buffer = BytesIO()
Image.new("RGB", (2, 2), (1, 2, 3)).save(_buffer, format="PNG")
PNG = _buffer.getvalue()
SENTINEL = "r2 cleanup sentinel"


@pytest.fixture
def hh(tmp_path, monkeypatch):
    admin = tmp_path / "data/admin.db"
    jobs = tmp_path / "data/media_jobs.db"
    responses = tmp_path / "data/responses.db"
    admin_store.init_db(admin)
    media_jobs.init_db(jobs)
    proxy.responses_store.init_db(responses)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs)
    monkeypatch.setattr(prompt_memory, "DB_PATH", jobs)
    monkeypatch.setattr(proxy.responses_store, "DB_PATH", responses)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOADS", {})
    monkeypatch.setattr(proxy, "_EPHEMERAL_MEDIA_JOB_PAYLOAD_SIZES", {})
    robin = admin_store.upsert_oidc_user("issuer", "robin", email="robin@example.test", path=admin)
    app = admin_store.create_app("Try", kind="personal", owner_user_id=robin["id"], access_plan="person", path=admin)
    (tmp_path / "cache").mkdir()
    return SimpleNamespace(root=tmp_path, admin=admin, jobs=jobs, robin=robin, app=app)


def _legacy_file(hh, digest: str = "a" * 64):
    path = hh.root / "cache" / f"{digest}.png"
    path.write_bytes(media_metadata.embed_png_text(PNG, {"prompt": SENTINEL}))
    proxy._write_asset_metadata(f"img_{digest}", {"owner_key_hashes": [f"app:{hh.app['id']}"]})
    return path


def _save_response(hh, response_id: str, created_at: int) -> None:
    proxy.responses_store.save(
        response_id, request_json={"input": SENTINEL}, response_json={"id": response_id},
        api_key_name="Try", app_id=hh.app["id"], person_id=hh.robin["id"], created_at=created_at,
        model="test", previous_response_id=None, path=proxy.responses_store.DB_PATH,
    )


def _cutoff(hh) -> int:
    with prompt_memory.retention_guard(hh.admin, hh.robin["id"]) as policy:
        return policy["forgot_before"]


# --- P2-19: maintenance and stale decisions --------------------------------

def test_maintenance_keeps_prompt_saved_after_member_turns_memory_back_on(hh, monkeypatch):
    person = hh.robin["id"]
    prompt_memory.put_member(person, remember=False, retention="never", path=hh.admin)
    real_tighten = prompt_memory.tighten_retention
    state = {"done": False}

    def tighten(*args, **kwargs):
        # Robin turns memory back on while a maintenance pass is in progress.
        if not state["done"]:
            state["done"] = True
            prompt_memory.put_member(person, remember=True, retention="never", path=hh.admin)
            time.sleep(1.1)
            assert prompt_memory.put("img_" + "d" * 64, person, "fresh after re-enable",
                                     kept_until=None, admin_path=hh.admin)
            _save_response(hh, "resp_fresh", int(time.time()))
        return real_tighten(*args, **kwargs)

    monkeypatch.setattr(prompt_memory, "tighten_retention", tighten)
    monkeypatch.setattr(prompt_memory, "prune", lambda **_kwargs: 0)
    proxy._maintain_prompt_memory()
    assert state["done"]
    assert prompt_memory.get("img_" + "d" * 64, person)["prompt_text"] == "fresh after re-enable"
    assert proxy.responses_store.get("resp_fresh", path=proxy.responses_store.DB_PATH) is not None


def test_maintenance_does_not_advance_cutoff_for_member_who_stays_off(hh):
    person = hh.robin["id"]
    switched_off = int(time.time()) - 100
    admin_store.put_prompt_memory_preference(person, remember=False, retention="never",
                                             updated_by=person, now=switched_off, path=hh.admin)
    prompt_memory.mark_person_erasure(person, admin_path=hh.admin, now=switched_off)
    proxy._maintain_prompt_memory()
    assert _cutoff(hh) == switched_off


def test_maintenance_retries_erasure_when_off_switch_never_recorded_a_cutoff(hh):
    # The API callback may fail after the preference was saved. Maintenance
    # then erases up to the moment memory went off, not up to "now".
    person = hh.robin["id"]
    switched_off = int(time.time()) - 50
    prompt_memory.put("img_" + "e" * 64, person, SENTINEL, kept_until=None,
                      admin_path=hh.admin, created_at=switched_off - 10)
    _save_response(hh, "resp_old", switched_off - 10)
    admin_store.put_prompt_memory_preference(person, remember=False, retention="never",
                                             updated_by=person, now=switched_off, path=hh.admin)
    proxy._maintain_prompt_memory()
    assert prompt_memory.get("img_" + "e" * 64, person) is None
    assert proxy.responses_store.get("resp_old", path=proxy.responses_store.DB_PATH) is None
    assert _cutoff(hh) == switched_off


def test_maintenance_ignores_stale_kill_switch_reading(hh, monkeypatch):
    # The locked policy, not an earlier unlocked read, decides a household purge.
    person = hh.robin["id"]
    assert prompt_memory.put("img_" + "f" * 64, person, "kept", kept_until=None, admin_path=hh.admin)
    stale = {**prompt_memory.household_settings(admin_path=hh.admin), "never_keep": True}
    monkeypatch.setattr(prompt_memory, "household_settings", lambda **_kwargs: stale)
    monkeypatch.setattr(prompt_memory, "prune", lambda **_kwargs: 0)
    proxy._maintain_prompt_memory()
    assert prompt_memory.get("img_" + "f" * 64, person)["prompt_text"] == "kept"


# --- P2-20: rows now, files queued ----------------------------------------

class _WatchedScanLock:
    """The migration scan lock, reporting a blocked acquire by any other thread.

    The route under test may take the lock on a thread of its own (the ASGI
    portal), so the spy watches every thread but the owner, not one thread.
    """

    def __init__(self, real, settled):
        self._real = real
        self._settled = settled
        self._owner = threading.get_ident()
        self.blocked = False

    def acquire(self, blocking=True, timeout=-1):
        if blocking and threading.get_ident() != self._owner:
            if self._real.acquire(False):
                return True
            self.blocked = True
            self._settled.set()
        return self._real.acquire(blocking, timeout)

    def release(self):
        self._real.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *_exc):
        self.release()


def _run_with_scan_lock_held(fn):
    """Run fn on a thread while the scan lock is held; report whether it returned.

    Success is the thread finishing BEFORE the lock is released; a failure is
    its blocking on the lock. The waits only bound a hang; they are not the oracle.
    """
    settled = threading.Event()
    done = threading.Event()
    out = {}
    lock = _WatchedScanLock(pmm._SCAN_LOCK, settled)

    def run():
        try:
            out["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread
            out["error"] = exc
        finally:
            done.set()
            settled.set()

    with patch.object(pmm, "_SCAN_LOCK", lock):
        assert lock.acquire(timeout=5)
        worker = threading.Thread(target=run, daemon=True)
        try:
            worker.start()
            settled.wait(3.0)
            finished = done.is_set() and not lock.blocked
        finally:
            lock.release()
        worker.join(30)
        assert not worker.is_alive(), "the worker never finished after the lock was released"
    if "error" in out:
        raise out["error"]
    return finished, out.get("result")


def test_member_erasure_returns_while_scan_lock_is_held_and_rows_are_gone(hh):
    person = hh.robin["id"]
    path = _legacy_file(hh)
    prompt_memory.put("img_" + "a" * 64, person, SENTINEL, kept_until=None, admin_path=hh.admin)
    _save_response(hh, "resp_private", int(time.time()) - 5)

    finished, result = _run_with_scan_lock_held(lambda: proxy._purge_person_prompt_memory(person))
    assert finished, "erasure waited for the migration scan lock"
    assert prompt_memory.get("img_" + "a" * 64, person) is None
    assert proxy.responses_store.get("resp_private", path=proxy.responses_store.DB_PATH) is None
    assert result["pending"] is True
    assert result["metadata_stripping"]["status"] == "queued"
    assert person in proxy._pending_prompt_purges()
    assert SENTINEL.encode() in path.read_bytes()

    proxy._drain_prompt_purges()
    assert SENTINEL.encode() not in path.read_bytes()
    assert proxy._pending_prompt_purges() == []
    assert proxy._prompt_purge_status(person)["pending"] is False


def test_kill_switch_returns_while_scan_lock_is_held(hh):
    person = hh.robin["id"]
    path = _legacy_file(hh, "b" * 64)
    prompt_memory.put("img_" + "b" * 64, person, SENTINEL, kept_until=None, admin_path=hh.admin)

    finished, result = _run_with_scan_lock_held(proxy._purge_prompt_memory_on_kill)
    assert finished, "kill switch stripped the cache inside the request"
    assert prompt_memory.get("img_" + "b" * 64, person) is None
    assert result["pending"] is True
    assert proxy._pending_prompt_purges() == ["*"]
    # Every member sees the household cleanup as their own pending work.
    assert proxy._prompt_purge_status(person)["pending"] is True

    proxy._drain_prompt_purges()
    assert SENTINEL.encode() not in path.read_bytes()
    assert proxy._prompt_purge_status("*")["pending"] is False


def test_failed_file_strip_stays_queued_with_visible_status_then_retries(hh, monkeypatch):
    person = hh.robin["id"]
    path = _legacy_file(hh)
    real_strip = media_metadata.strip_metadata
    monkeypatch.setattr(media_metadata, "strip_metadata",
                        lambda *_a, **_kw: (_ for _ in ()).throw(media_metadata.MediaMetadataError("failed")))
    proxy._purge_person_prompt_memory(person)
    proxy._drain_prompt_purges()
    status = proxy._prompt_purge_status(person)
    assert status["pending"] is True
    stripping = status["metadata_stripping"]
    assert stripping["status"] == "retrying"
    assert stripping["attempts"] == 1
    assert stripping["files_pending"] == 1
    assert stripping["last_error"] == "MediaMetadataError"
    assert SENTINEL not in repr(status)

    monkeypatch.setattr(media_metadata, "strip_metadata", real_strip)
    proxy._maintain_prompt_memory()
    assert SENTINEL.encode() not in path.read_bytes()
    assert proxy._prompt_purge_status(person)["pending"] is False


def test_erasure_requested_during_a_drain_is_not_lost(hh, monkeypatch):
    person = hh.robin["id"]
    proxy._purge_person_prompt_memory(person)
    real_strip = pmm.strip_cached_assets

    def strip(*args, **kwargs):
        # A second delete arrives after the drain chose this file set.
        proxy._purge_person_prompt_memory(person)
        monkeypatch.setattr(pmm, "strip_cached_assets", real_strip)
        return real_strip(*args, **kwargs)

    monkeypatch.setattr(pmm, "strip_cached_assets", strip)
    proxy._drain_prompt_purges()
    assert proxy._pending_prompt_purges() == [person]
    proxy._drain_prompt_purges()
    assert proxy._pending_prompt_purges() == []


def test_maintenance_loop_drains_queue_soon_after_erasure(hh, monkeypatch):
    drained = threading.Event()
    monkeypatch.setattr(proxy, "_maintain_prompt_memory", lambda: None)
    monkeypatch.setattr(proxy, "_drain_prompt_purges", drained.set)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(proxy._prompt_memory_maintenance_loop(stop))
        await asyncio.sleep(0.1)
        await asyncio.to_thread(proxy._purge_person_prompt_memory, hh.robin["id"])
        woke = await asyncio.to_thread(drained.wait, 5)
        stop.set()
        await asyncio.wait_for(task, 5)
        return woke

    assert asyncio.run(scenario())


def _me_client(hh, *, off_callback=None):
    app = FastAPI()
    app.include_router(create_me_prompt_memory_router(
        session_provider=lambda _request: AdminSession(
            subject=hh.robin["id"], email="robin@example.test", role="none",
            issued_at=time.time(), expires_at=time.time() + 3600),
        household_provider=lambda: {},
        admin_db_path=hh.admin,
        prompt_memory_path=hh.jobs,
        on_person_prompt_purge=proxy._purge_person_prompt_memory,
        on_person_memory_off=off_callback or (lambda person, cutoff: proxy._purge_person_prompt_memory(person, cutoff=cutoff)),
        purge_status_provider=proxy._prompt_purge_status,
    ))
    return TestClient(app)


def test_settings_api_reports_queued_file_cleanup_until_it_finishes(hh):
    person = hh.robin["id"]
    path = _legacy_file(hh)
    prompt_memory.put("img_" + "a" * 64, person, SENTINEL, kept_until=None, admin_path=hh.admin)
    client = _me_client(hh)
    assert "prompt_purge" not in client.get("/me/api/prompt-memory").json()

    finished, deleted = _run_with_scan_lock_held(
        lambda: client.delete("/me/api/prompt-memory/prompts"))
    assert finished, "the delete route waited for the migration scan lock"
    assert deleted.status_code == 202
    assert deleted.json()["deleted_prompts"] == 1
    assert deleted.json()["prompt_purge"]["retry_scheduled"] is True
    assert prompt_memory.get("img_" + "a" * 64, person) is None

    pending = client.get("/me/api/prompt-memory").json()["prompt_purge"]
    assert pending["pending"] is True
    assert pending["metadata_stripping"]["status"] == "queued"

    proxy._drain_prompt_purges()
    assert SENTINEL.encode() not in path.read_bytes()
    assert "prompt_purge" not in client.get("/me/api/prompt-memory").json()


# --- Integration: retry intent without stale or repeated work --------------

def _spool(hh, job_id: str, created_at: int):
    import json
    auth = proxy.AuthContext(identity=auth_identity(
        name="Try", key_hash="sha256:robin", app_id=hh.app["id"], person_id=hh.robin["id"]))
    request = SimpleNamespace(headers={"host": "testserver"}, url=SimpleNamespace(scheme="http", netloc="testserver"))
    # An active job, so the orphan sweep keeps the spool.
    media_jobs.create(media_jobs.MediaJob(job_id=job_id, intent="image.generate", workflow="test"), path=hh.jobs)
    proxy._persist_image_media_job_payload(job_id, body={"prompt": SENTINEL}, request=request, auth_context=auth)
    path = proxy._image_media_job_payload_path(job_id)
    payload = json.loads(path.read_text())
    payload["created_at"] = created_at
    path.write_text(json.dumps(payload))
    proxy._EPHEMERAL_MEDIA_JOB_PAYLOADS.pop(job_id, None)
    return path


def _count_queue_writes(monkeypatch):
    calls = []
    real = proxy._queue_prompt_purge
    monkeypatch.setattr(proxy, "_queue_prompt_purge", lambda *a, **k: (calls.append(a), real(*a, **k)))
    return calls


def test_maintenance_retries_spools_and_files_when_off_switch_never_queued(hh, monkeypatch):
    person = hh.robin["id"]
    switched_off = int(time.time()) - 50
    path = _legacy_file(hh)
    old_spool = _spool(hh, "mj_old", switched_off - 10)
    new_spool = _spool(hh, "mj_new", switched_off + 10)
    admin_store.put_prompt_memory_preference(person, remember=False, retention="never",
                                             updated_by=person, now=switched_off, path=hh.admin)
    proxy._maintain_prompt_memory()
    assert not old_spool.exists()
    assert proxy._load_image_media_job_payload("mj_old")["body"]["extensions"][prompt_memory.NAMESPACE] == {"mode": "forget"}
    assert new_spool.exists(), "a spool made after the cutoff was purged"
    assert SENTINEL.encode() not in path.read_bytes()
    assert proxy._pending_prompt_purges() == []

    calls = _count_queue_writes(monkeypatch)
    proxy._maintain_prompt_memory()
    assert calls == [], "a covered cutoff was queued again"
    assert new_spool.exists()


def test_maintenance_retries_kill_switch_cleanup_once_per_switch_on(hh, monkeypatch):
    import json
    import sqlite3
    person = hh.robin["id"]
    turned_on = int(time.time()) - 50
    path = _legacy_file(hh)
    prompt_memory.put("img_" + "a" * 64, person, SENTINEL, kept_until=None, admin_path=hh.admin)
    spool = _spool(hh, "mj_old", turned_on - 10)
    with sqlite3.connect(hh.admin) as conn:
        conn.execute("INSERT OR REPLACE INTO operator_settings(key, value_json, updated_at, updated_by) "
                     "VALUES ('retention', ?, ?, NULL)", (json.dumps({"never_keep": True}), turned_on))
    proxy._maintain_prompt_memory()
    assert prompt_memory.get("img_" + "a" * 64, person) is None
    assert not spool.exists()
    assert SENTINEL.encode() not in path.read_bytes()
    assert proxy._pending_prompt_purges() == []

    calls = _count_queue_writes(monkeypatch)
    proxy._maintain_prompt_memory()
    assert calls == []


@pytest.mark.parametrize("legacy_schema", [False, True])
def test_simultaneous_first_queue_writes_upgrade_schema_once(hh, legacy_schema):
    import sqlite3
    state = proxy._prompt_migration_state_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    if legacy_schema:
        with sqlite3.connect(state) as conn:
            conn.execute("CREATE TABLE prompt_purge_queue (person_id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO prompt_purge_queue VALUES ('usr_legacy')")
    people = [f"usr_{index}" for index in range(12)]
    barrier = threading.Barrier(len(people))
    errors = []

    def worker(person_id):
        barrier.wait()
        try:
            proxy._queue_prompt_purge(person_id, 1)
            proxy._prompt_purge_status(person_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(person,)) for person in people]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    # A live worker would outlast the restored paths of this test.
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    expected = set(people) | ({"usr_legacy"} if legacy_schema else set())
    assert set(proxy._pending_prompt_purges()) == expected


def test_owner_can_poll_queued_household_cleanup(hh):
    from gateway.admin_api import AdminContext
    from gateway.settings_api import create_settings_router

    proxy._queue_prompt_purge("*", int(time.time()))
    app = FastAPI()
    app.include_router(create_settings_router(
        auth=lambda _request: AdminContext(subject="owner", roles=frozenset({"owner"}), auth_method="session"),
        admin_path=hh.admin, config_provider=lambda: {},
        purge_status_provider=lambda: proxy._prompt_purge_status("*"),
    ))
    client = TestClient(app)
    response = client.get("/admin/api/settings/prompt-memory")
    assert response.status_code == 200
    assert response.json()["prompt_purge"]["pending"] is True
    assert response.json()["prompt_purge"]["metadata_stripping"]["status"] == "queued"
    proxy._drain_prompt_purges()
    assert "prompt_purge" not in client.get("/admin/api/settings/prompt-memory").json()


def test_spool_failure_keeps_get_pending_and_retries_after_reenable(hh, monkeypatch):
    person = hh.robin["id"]
    path = _legacy_file(hh)
    spool = _spool(hh, "mj_retry", int(time.time()) - 10)
    real_purge = proxy._purge_prompt_payload_files
    def fail(*_args, **_kwargs):
        raise OSError("fixture spool failure")
    monkeypatch.setattr(proxy, "_purge_prompt_payload_files", fail)
    client = _me_client(hh)
    response = client.put("/me/api/prompt-memory", json={"remember": False, "retention": "never"})
    assert response.status_code == 202
    assert client.get("/me/api/prompt-memory").json()["prompt_purge"]["pending"] is True
    assert SENTINEL.encode() in path.read_bytes()
    proxy._drain_prompt_purges()
    assert proxy._prompt_purge_status(person)["metadata_stripping"]["status"] == "retrying"
    # The setting can change while the durable queue still needs spool cleanup.
    prompt_memory.put_member(person, remember=True, retention="never", path=hh.admin)
    monkeypatch.setattr(proxy, "_purge_prompt_payload_files", real_purge)
    proxy._maintain_prompt_memory()
    assert not spool.exists()
    assert SENTINEL.encode() not in path.read_bytes()
    assert "prompt_purge" not in client.get("/me/api/prompt-memory").json()


@pytest.mark.parametrize("failed_step", ["prune_person_before", "mark_person_erasure"])
def test_get_reports_unqueued_erasure_intent(hh, monkeypatch, failed_step):
    person = hh.robin["id"]
    _legacy_file(hh)
    real_step = getattr(prompt_memory, failed_step)
    monkeypatch.setattr(prompt_memory, failed_step, lambda *_a, **_k: (_ for _ in ()).throw(OSError("fixture DB failure")))
    client = _me_client(hh)
    assert client.put("/me/api/prompt-memory", json={"remember": False, "retention": "never"}).status_code == 202
    status = client.get("/me/api/prompt-memory").json()["prompt_purge"]
    assert status["pending"] is True
    assert status["metadata_stripping"]["last_error"] == "cleanup_not_queued"
    monkeypatch.setattr(prompt_memory, failed_step, real_step)
    proxy._maintain_prompt_memory()
    assert "prompt_purge" not in client.get("/me/api/prompt-memory").json()


def test_delayed_off_callback_preserves_new_prompt_chat_and_spool(hh, monkeypatch):
    person = hh.robin["id"]
    switched_off = int(time.time()) - 20
    monkeypatch.setattr(admin_store, "_now", lambda: switched_off)
    def delayed_off(person_id, cutoff):
        assert cutoff == switched_off
        admin_store.put_prompt_memory_preference(person, remember=True, retention="never", now=switched_off + 10, path=hh.admin)
        assert prompt_memory.put("img_" + "d" * 64, person, "fresh prompt", kept_until=None,
                                 created_at=switched_off + 10, admin_path=hh.admin)
        _save_response(hh, "resp_fresh", switched_off + 10)
        _spool(hh, "mj_fresh", switched_off + 10)
        return proxy._purge_person_prompt_memory(person_id, cutoff=cutoff)
    client = _me_client(hh, off_callback=delayed_off)
    response = client.put("/me/api/prompt-memory", json={"remember": False, "retention": "never"})
    assert response.status_code == 202
    assert prompt_memory.get("img_" + "d" * 64, person)["prompt_text"] == "fresh prompt"
    assert proxy.responses_store.get("resp_fresh", path=proxy.responses_store.DB_PATH) is not None
    assert proxy._image_media_job_payload_path("mj_fresh").exists()


def test_household_spool_retry_removes_old_partial_files_only(hh):
    import os
    cutoff = int(time.time()) - 20
    directory = proxy.IMAGE_MEDIA_JOB_PAYLOAD_DIR
    directory.mkdir()
    old = directory / "mj_old.json.tmp"
    new = directory / "mj_new.json.tmp"
    for path, written_at in ((old, cutoff - 5), (new, cutoff + 5)):
        path.write_text(SENTINEL)
        os.utime(path, (written_at, written_at))
    proxy._queue_prompt_purge("*", cutoff)
    proxy._purge_queued_payloads("*")
    assert not old.exists()
    assert new.exists()


@pytest.mark.parametrize("household", [False, True])
def test_database_erasure_retries_after_reenable_and_keeps_new_prompts(hh, monkeypatch, household):
    from gateway.admin_api import AdminContext
    from gateway.settings_api import create_settings_router

    person = hh.robin["id"]
    cutoff = int(time.time()) - 20
    old_asset, fresh_asset = "img_" + "1" * 64, "img_" + "2" * 64
    prompt_memory.put(old_asset, person, SENTINEL, kept_until=None,
                      created_at=cutoff - 5, admin_path=hh.admin)
    _save_response(hh, "resp_old", cutoff - 5)
    monkeypatch.setattr(admin_store, "_now", lambda: cutoff)
    step = "prune_all_before" if household else "prune_person_before"
    original = getattr(prompt_memory, step)
    monkeypatch.setattr(prompt_memory, step, lambda *a, **k: (_ for _ in ()).throw(OSError("fixture DB failure")))
    if household:
        app = FastAPI()
        app.include_router(create_settings_router(
            auth=lambda _request: AdminContext(subject="owner", roles=frozenset({"owner"}), auth_method="session", csrf_token="fixture-csrf"),
            admin_path=hh.admin, config_provider=lambda: {}, prompt_memory_path=hh.jobs,
            on_prompt_memory_kill=proxy._purge_prompt_memory_on_kill,
            purge_status_provider=lambda: proxy._prompt_purge_status("*"),
        ))
        client = TestClient(app)
        route = "/admin/api/settings/prompt-memory"
        value = {"default_remember": True, "never_keep": True, "retention_cap": "never"}
        revision = client.get(route).json()["revision"]
        response = client.put(route, json=value, headers={"If-Match": revision, "x-azizam-csrf": "fixture-csrf"})
    else:
        client = _me_client(hh)
        route = "/me/api/prompt-memory"
        value = {"remember": False, "retention": "never"}
        response = client.put(route, json=value)
    assert response.status_code == 202
    assert response.json()["prompt_purge"]["database_pending"] is True
    assert prompt_memory.get(old_asset, person) is not None
    monkeypatch.setattr(prompt_memory, step, original)
    monkeypatch.setattr(admin_store, "_now", lambda: cutoff + 5)
    if household:
        value["never_keep"] = False
        client.put(route, json=value, headers={"If-Match": response.json()["revision"], "x-azizam-csrf": "fixture-csrf"})
    else:
        value["remember"] = True
        client.put(route, json=value)
    assert client.get(route).json()["prompt_purge"]["database_pending"] is True
    assert prompt_memory.put(fresh_asset, person, "fresh after re-enable", kept_until=None,
                             created_at=cutoff + 10, admin_path=hh.admin)
    # The fence also refuses a late completion of an old request after re-enable.
    assert not prompt_memory.put("img_late", person, SENTINEL, kept_until=None,
                                 created_at=cutoff - 5, admin_path=hh.admin)
    _save_response(hh, "resp_fresh", cutoff + 10)
    monkeypatch.setattr(prompt_memory, "prune", lambda **k: 0)
    proxy._maintain_prompt_memory()
    assert prompt_memory.get(old_asset, person) is None
    assert proxy.responses_store.get("resp_old", path=proxy.responses_store.DB_PATH) is None
    assert prompt_memory.get(fresh_asset, person)["prompt_text"] == "fresh after re-enable"
    assert proxy.responses_store.get("resp_fresh", path=proxy.responses_store.DB_PATH) is not None
    assert not prompt_memory.erasure_pending("*" if household else person, admin_path=hh.admin)


def test_old_delete_cannot_acknowledge_a_newer_failed_off_erasure(hh):
    person = hh.robin["id"]
    cutoff = int(time.time()) - 20

    def delayed_delete(person_id):
        result = proxy._purge_person_prompt_memory(person_id, cutoff=cutoff)
        # A different request records an off intent after this delete finished
        # its database work. Its deletion has not completed yet.
        admin_store.put_prompt_memory_preference(person_id, remember=False,
                                                 retention="never", now=cutoff + 10, path=hh.admin)
        return result

    app = FastAPI()
    app.include_router(create_me_prompt_memory_router(
        session_provider=lambda _request: AdminSession(subject=person, email=None, role="none",
            issued_at=1, expires_at=4_000_000_000),
        admin_db_path=hh.admin, prompt_memory_path=hh.jobs,
        on_person_prompt_purge=delayed_delete,
    ))
    client = TestClient(app)
    assert client.delete("/me/api/prompt-memory/prompts").status_code == 202
    assert prompt_memory.erasure_pending(person, admin_path=hh.admin)
    assert client.get("/me/api/prompt-memory").json()["prompt_purge"]["database_pending"] is True
