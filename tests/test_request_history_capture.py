"""A chat request from a new member is kept once, with its accepted body and no credential in any store.

docs/design/REQUEST_RETENTION.md sections 3.3 (decision order), 3.5 (parts and the secret floor) and 3.6 (one write
path). The upstream is the fake-upstream harness, so the request goes through the real route, auth, registry and
writer. The app key the caller sends is the canary: it must not appear in any file the gateway writes.
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from gateway import media_jobs, receipt_store
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.support import gateway as gateway_support
from tests.support.gateway import gateway_client, register_litellm, use_admin_db
from tests.registry_test_helpers import write_service_registry_for_test
from tests.test_engine_dispatch_wiring import _registry

import proxy

SENTENCE = "a sentence the member asked the chat route"


@pytest.fixture
def chat_gateway(fake_upstreams, monkeypatch, tmp_path):
    fake = fake_upstreams.asgi(load_scenario("openai-compatible/chat-text", model="garage-chat", content="ok"),
                               host="litellm.fk.test")
    db = use_admin_db(monkeypatch, tmp_path)
    media = tmp_path / "media_jobs.db"
    media_jobs.init_db(media)
    monkeypatch.setattr(media_jobs, "DB_PATH", media)
    monkeypatch.setattr(receipt_store, "DB_PATH", tmp_path / "receipts.db")
    receipt_store.init_db(tmp_path / "receipts.db")
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


def _kept_rows(media: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(media)
    conn.row_factory = sqlite3.Row
    try:
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


def test_no_app_key_or_authorization_value_reaches_any_file_the_gateway_writes(chat_gateway):
    tmp_path, media, db = chat_gateway
    _post({"model": "garage-chat", "messages": [{"role": "user", "content": SENTENCE}]})
    needle = gateway_support._APP_KEY["key"].encode()
    leaks = [path for path in tmp_path.rglob("*") if path.is_file() and needle in path.read_bytes()]
    assert leaks == []
