"""A chat request from a new member is kept once, with its accepted body and no credential in any store.

docs/design/REQUEST_RETENTION.md sections 3.3 (decision order), 3.5 (parts and the secret floor) and 3.6 (one write
path). The upstream is the fake-upstream harness, so the request goes through the real route, auth, registry and
writer. The app key the caller sends is the canary: it must not appear in any file the gateway writes.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from gateway import (
    admin_store,
    media_jobs,
    prompt_memory,
    receipt_store,
    request_history,
    request_history_capture,
    retention,
)
from gateway.receipts import RouteReceipt
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.support import gateway as gateway_support
from tests.support.gateway import gateway_client, register_litellm, use_admin_db
from tests.registry_test_helpers import write_service_registry_for_test
from tests.test_engine_dispatch_wiring import _registry

import proxy

SENTENCE = "a sentence the member asked the chat route"


def _mint_member_key(db: Path, person_id: str) -> str:
    """A personal App key minned by its owner: the only kind of key whose requests have a person (section 4)."""
    from gateway.runtime_auth import hash_api_key

    app = admin_store.create_app("Member app", kind="personal", owner_user_id=person_id, access_plan="person", path=db)
    key = f"opk_member_{app['id']}"
    admin_store.create_credential_hash(subject_kind="app", subject_id=app["id"], app_id=app["id"],
                                       hash=hash_api_key(key), prefix=key[:16], created_by=person_id, path=db)
    gateway_support._APP_KEY["key"] = key
    return key


@pytest.fixture
def chat_gateway(fake_upstreams, monkeypatch, tmp_path):
    fake = fake_upstreams.asgi(load_scenario("openai-compatible/chat-text", model="garage-chat", content="ok"),
                               host="litellm.fk.test")
    db = use_admin_db(monkeypatch, tmp_path)
    person_id = admin_store.upsert_oidc_user("https://fixture.test", "fixture-person", path=db)["id"]
    _mint_member_key(db, person_id)
    media = tmp_path / "media_jobs.db"
    media_jobs.init_db(media)
    monkeypatch.setattr(media_jobs, "DB_PATH", media)
    receipt_store.init_db(receipt_store.DB_PATH)
    register_litellm(monkeypatch, db, fake.base_url)
    registry = proxy.ADMIN_STORE.get_service_registry()
    registry["services"].update(_registry("http://garage.fk.test/v1")["services"])
    write_service_registry_for_test(registry, path=db)
    return tmp_path, media, db


def _post(body: dict):
    async def call():
        async with gateway_client() as client:
            return await client.post("/v1/chat/completions", json=body)
    return asyncio.run(call())


def _settle_staged_media_capture(admin_db: Path, media_db: Path, person_id: str, request_id: str,
                                asset_id: str, *, created_at: int) -> None:
    """Run the same combined writer after a Library delete wins the media transaction."""
    capture = object.__new__(request_history_capture._Capture)
    capture._owner = SimpleNamespace(_admin_path=lambda: admin_db)
    capture.media_path = media_db
    decision = retention.Decision(
        person_id, True, "remembered", created_at=created_at,
        kind=retention.REQUEST_HISTORY, parts=("accepted",),
    )
    record = {
        "record_id": request_history.new_record_id(), "request_id": request_id,
        "person_id": person_id, "app_id": None, "origin_kind": "app_key", "origin_id": "fixture-key",
        "created_at": created_at,
        "parts": {"accepted": {"headers": {}, "body": {"model": "image"}}},
        "omissions_by_part": {"accepted": []}, "policy_version": 1,
        "summary": {"route": "/v1/images/generations", "model": "image", "status": 200, "latency_ms": 1},
    }
    prompt = {
        "asset_id": asset_id, "person_id": person_id, "prompt_text": "a late staged prompt",
        "metadata": None, "created_at": created_at, "kept_until": None,
    }
    capture._write_media_capture(
        decision, record, created_at, [(prompt, None)], request_id,
        retention.WriteOutcome(False, "write_failed"),
    )


def _publish_prompt_job(media_db: Path, person_id: str, request_id: str, asset_id: str) -> None:
    media_jobs.create(media_jobs.MediaJob(
        request_id=request_id, parent_request_id=request_id, intent="image.generate",
        status=media_jobs.STATUS_SUCCEEDED,
        assets=[{"asset_id": asset_id, "media_type": "image/png"}],
    ), path=media_db)
    assert prompt_memory.put(
        asset_id, person_id, "the visible prompt", kept_until=None, path=media_db,
    )


def _capture_stores(monkeypatch, tmp_path) -> tuple[str, Path, Path]:
    admin_db = use_admin_db(monkeypatch, tmp_path)
    person_id = admin_store.upsert_oidc_user(
        "https://fixture.test", "late-capture-person", path=admin_db,
    )["id"]
    for group, kind in (
        (retention.GROUP_PROMPTS, retention.LIBRARY_PROMPT),
        (retention.GROUP_REQUEST_HISTORY, retention.REQUEST_HISTORY),
    ):
        admin_store.put_prompt_memory_preference(
            person_id, remember=True, retention=retention.bundled().member_defaults(kind)["retention"],
            group=group, path=admin_db,
        )
    media_db = tmp_path / "media_jobs.db"
    media_jobs.init_db(media_db)
    return person_id, media_db, admin_db


def _kept_rows(media: Path) -> list[sqlite3.Row]:
    """The kept records. A database with no kept request has no kept_requests table yet, which is no rows."""
    conn = sqlite3.connect(media)
    conn.row_factory = sqlite3.Row
    try:
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='kept_requests'").fetchone() is None:
            return []
        return conn.execute("SELECT * FROM kept_requests ORDER BY created_at").fetchall()
    finally:
        conn.close()


def test_a_chat_request_from_a_new_member_is_kept_once_with_its_accepted_body(chat_gateway):
    tmp_path, media, _ = chat_gateway
    body = {"model": "garage-chat", "messages": [{"role": "user", "content": SENTENCE}]}
    response = _post(body)
    assert response.status_code == 200, response.text
    rows = _kept_rows(media)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "request_history"
    assert row["person_id"]
    assert row["kept_until"] is not None
    assert SENTENCE in row["parts_json"]
    assert "received" not in row["parts_json"]  # default parts: accepted only (registry/retention.yml)


def test_item_delete_before_capture_settles_does_not_restore_prompt_or_history(monkeypatch, tmp_path):
    person_id, media_db, admin_db = _capture_stores(monkeypatch, tmp_path)
    request_id, asset_id = "receipt_deleted_before_settle", "img_deleted_before_settle"
    _publish_prompt_job(media_db, person_id, request_id, asset_id)

    assert prompt_memory.delete_asset_for_person(asset_id, person_id, path=media_db) == 1
    _settle_staged_media_capture(
        admin_db, media_db, person_id, request_id, asset_id, created_at=int(time.time()) - 1,
    )

    assert prompt_memory.get(asset_id, person_id, path=media_db) is None
    with sqlite3.connect(media_db) as conn:
        assert conn.execute("SELECT 1 FROM kept_requests WHERE request_id=?", (request_id,)).fetchone() is None


def test_library_delete_all_before_capture_settles_does_not_restore_prompt_or_history(monkeypatch, tmp_path):
    person_id, media_db, admin_db = _capture_stores(monkeypatch, tmp_path)
    request_id, asset_id = "receipt_erased_before_settle", "img_erased_before_settle"
    _publish_prompt_job(media_db, person_id, request_id, asset_id)

    assert prompt_memory.delete_person(person_id, path=media_db) == 1
    _settle_staged_media_capture(
        admin_db, media_db, person_id, request_id, asset_id, created_at=int(time.time()) - 1,
    )

    assert prompt_memory.get(asset_id, person_id, path=media_db) is None
    with sqlite3.connect(media_db) as conn:
        assert conn.execute("SELECT 1 FROM kept_requests WHERE request_id=?", (request_id,)).fetchone() is None


def test_no_app_key_or_authorization_value_reaches_any_file_the_gateway_writes(chat_gateway):
    tmp_path, media, db = chat_gateway
    _post({"model": "garage-chat", "messages": [{"role": "user", "content": SENTENCE}]})
    needle = gateway_support._APP_KEY["key"].encode()
    leaks = [path for path in tmp_path.rglob("*") if path.is_file() and needle in path.read_bytes()]
    assert leaks == []


def test_media_prompt_and_history_roll_back_together_when_the_second_write_fails(monkeypatch, tmp_path):
    """A failed history insert after the prompt write must leave neither row committed (section 3.6)."""
    admin_db = use_admin_db(monkeypatch, tmp_path)
    person_id = admin_store.upsert_oidc_user("https://fixture.test", "media-person", path=admin_db)["id"]
    app_record = admin_store.create_app("Media app", kind="personal", owner_user_id=person_id,
                                        access_plan="person", path=admin_db)
    for group, kind in ((retention.GROUP_PROMPTS, retention.LIBRARY_PROMPT),
                        (retention.GROUP_REQUEST_HISTORY, retention.REQUEST_HISTORY)):
        admin_store.put_prompt_memory_preference(
            person_id, remember=True,
            retention=retention.bundled().member_defaults(kind)["retention"],
            group=group, path=admin_db,
        )

    media_db = tmp_path / "media_jobs.db"
    media_jobs.init_db(media_db)
    receipts_db = tmp_path / "receipts.db"
    receipt_store.init_db(receipts_db)
    monkeypatch.setattr(receipt_store, "DB_PATH", receipts_db)
    asset_id = "img_atomic"
    request_id = "req_atomic_media"
    injected: list[bool] = []

    def fail_history_insert(_conn, _record):
        injected.append(True)
        raise RuntimeError("injected crash between media retention writes")

    monkeypatch.setattr(request_history, "insert_record", fail_history_insert)
    app = FastAPI()
    app.add_middleware(
        request_history_capture.RequestHistoryMiddleware,
        admin_path=lambda: admin_db,
        media_path=lambda: media_db,
    )

    @app.post("/v1/images/generations")
    async def generate(request: Request):
        request.state.request_history_identity = SimpleNamespace(identity=SimpleNamespace(
            person_id=person_id, app_id=app_record["id"], auth_kind="app_key", credential_id="credential-media",
        ))
        await request.json()
        receipt_store.record(RouteReceipt(
            protocol="openai.images", intent="image.generate", path="/v1/images/generations",
            request_id=request_id, app_id=app_record["id"], status="succeeded",
        ), path=receipts_db)
        media_jobs.create(media_jobs.MediaJob(
            request_id=request_id, intent="image.generate", status=media_jobs.STATUS_SUCCEEDED,
            assets=[{"asset_id": asset_id, "media_type": "image/png"}],
        ), path=media_db)
        assert prompt_memory.safe_put(
            asset_id, person_id, "a painted orchard", kept_until=None, path=media_db, admin_path=admin_db,
        )
        return {"created": True}

    response = TestClient(app).post(
        "/v1/images/generations", json={"model": "image", "prompt": "a painted orchard"},
    )

    assert response.status_code == 200, response.text
    assert injected == [True]
    assert prompt_memory.get(asset_id, person_id, path=media_db) is None
    with sqlite3.connect(media_db) as conn:
        rows = conn.execute("SELECT record_id FROM kept_requests WHERE request_id=?", (request_id,)).fetchall()
    assert rows == []


def test_a_member_who_keeps_replies_also_gets_the_status_and_the_reply_body(chat_gateway):
    """The received part is kept only for a member whose parts include it (section 3.5); default is accepted only."""
    tmp_path, media, db = chat_gateway
    conn = sqlite3.connect(db)
    person = conn.execute("SELECT id FROM users WHERE subject='fixture-person' OR email LIKE 'fixture%' LIMIT 1").fetchone()
    conn.execute("UPDATE prompt_memory_preferences SET parts_json=? WHERE user_id=? AND grp='request_history'",
                 ('["accepted", "received"]', person[0]))
    conn.commit()
    conn.close()
    response = _post({"model": "garage-chat", "messages": [{"role": "user", "content": SENTENCE}]})
    assert response.status_code == 200, response.text
    row = _kept_rows(media)[-1]
    parts = json.loads(row["parts_json"])
    assert set(parts) == {"accepted", "received"}
    assert parts["received"]["status"] == 200
    assert "ok" in json.dumps(parts["received"]["body"])
