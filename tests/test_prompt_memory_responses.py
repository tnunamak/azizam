from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store, media_jobs, prompt_memory, receipt_store
from gateway.policy import DEFAULT_PROFILE
from gateway.runtime_auth import RuntimeIdentity, hash_api_key
from tests.test_anthropic_schema_conformance import chat_completion


def _files(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


def _assert_no_bytes(root: Path, sentinel: str) -> None:
    needle = sentinel.encode("utf-8")
    leaks = [path for path in _files(root) if needle in path.read_bytes()]
    assert leaks == []


@pytest.fixture
def responses_gateway(tmp_path, monkeypatch):
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    admin_db = data / "admin.db"
    responses_db = data / "responses.db"
    receipts_db = data / "receipts.db"
    media_db = data / "media_jobs.db"
    admin_store.init_db(admin_db)
    proxy.responses_store.init_db(responses_db)
    receipt_store.init_db(receipts_db)
    media_jobs.init_db(media_db)
    prompt_memory.init_db(path=media_db)

    user = admin_store.upsert_oidc_user(
        "local", "maker", email="maker@example.test", path=admin_db
    )
    app = admin_store.create_app(
        "Maker app", kind="personal", owner_user_id=user["id"], path=admin_db
    )
    raw_key = "prompt-memory-responses-key"
    key_hash = hash_api_key(raw_key)
    identity = RuntimeIdentity(
        name="Maker app key",
        key_hash=key_hash,
        downstream={},
        profile=DEFAULT_PROFILE,
        app_id=app["id"],
    )

    def lookup(candidate_hash: str, **_kwargs):
        return identity if candidate_hash == key_hash else None

    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin_db)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", admin_db)
    monkeypatch.setattr(proxy.responses_store, "DB_PATH", responses_db)
    monkeypatch.setattr(receipt_store, "DB_PATH", receipts_db)
    monkeypatch.setattr(media_jobs, "DB_PATH", media_db)
    monkeypatch.setattr(prompt_memory, "DB_PATH", media_db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", cache)
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "LAN_BYPASS_ENABLED", False)
    monkeypatch.setattr(proxy, "runtime_identity_for_app_credential_hash", lookup)

    sent: list[dict] = []
    real_response_save = proxy.responses_store.save
    real_response_get = proxy.responses_store.get
    real_receipt_record = receipt_store.record

    def save_response(response_id: str, **kwargs):
        kwargs.setdefault("path", responses_db)
        return real_response_save(response_id, **kwargs)

    def get_response(response_id: str, **kwargs):
        kwargs.setdefault("path", responses_db)
        return real_response_get(response_id, **kwargs)

    def record_receipt(receipt, **kwargs):
        kwargs.setdefault("path", receipts_db)
        return real_receipt_record(receipt, **kwargs)

    monkeypatch.setattr(proxy.responses_store, "save", save_response)
    monkeypatch.setattr(proxy.responses_store, "get", get_response)
    monkeypatch.setattr(receipt_store, "record", record_receipt)

    async def fake_forward(request, auth_context, cc_body, *, stream):
        sent.append(cc_body)
        return httpx.Response(
            200,
            json=chat_completion({"role": "assistant", "content": "ok"}),
            request=httpx.Request("POST", "http://gateway/v1/chat/completions"),
        )

    with patch.object(proxy, "_forward_chat_completions", fake_forward):
        yield {
            "client": TestClient(proxy.app),
            "headers": {"Authorization": f"Bearer {raw_key}"},
            "data": data,
            "cache": cache,
            "responses_db": responses_db,
            "media_db": media_db,
            "admin_db": admin_db,
            "person_id": user["id"],
            "sent": sent,
        }


def _post_response(fixture, body: dict):
    return fixture["client"].post(
        "/v1/responses",
        headers=fixture["headers"],
        json={"model": "default", **body},
    )


def test_owned_personal_app_keeps_stored_response_by_default(responses_gateway):
    sentinel = "responses-default-keep-sentinel"
    response = _post_response(responses_gateway, {"input": sentinel})
    assert response.status_code == 200, response.text

    row = proxy.responses_store.get(response.json()["id"], path=responses_gateway["responses_db"])
    assert row is not None
    assert row["request_json"]["input"] == sentinel


@pytest.mark.parametrize(
    "body",
    [
        {"input": "responses-store-false-sentinel", "store": False},
        {
            "input": "responses-extension-forget-sentinel",
            "extensions": {"com.azizam.prompts": {"mode": "forget"}},
        },
    ],
)
def test_store_false_and_prompt_forget_do_not_persist_prompt_bytes(responses_gateway, body):
    sentinel = body["input"]
    with patch.object(proxy.responses_store, "save", wraps=proxy.responses_store.save) as save:
        response = _post_response(responses_gateway, body)
    assert response.status_code == 200, response.text
    assert save.call_count == 0
    assert proxy.responses_store.get(response.json()["id"], path=responses_gateway["responses_db"]) is None
    assert responses_gateway["sent"][0]["messages"][-1]["content"] == sentinel
    _assert_no_bytes(responses_gateway["data"], sentinel)
    _assert_no_bytes(responses_gateway["cache"], sentinel)


def test_owner_kill_switch_disables_responses_store_and_leaves_no_prompt_bytes(responses_gateway):
    sentinel = "responses-kill-switch-sentinel"
    admin_store.put_operator_setting(
        "prompt_memory",
        {"default_remember": True, "never_keep": True, "retention_cap": "never"},
        path=responses_gateway["admin_db"],
    )

    with patch.object(proxy.responses_store, "save", wraps=proxy.responses_store.save) as save:
        response = _post_response(responses_gateway, {"input": sentinel})
    assert response.status_code == 200, response.text
    assert save.call_count == 0
    assert proxy.responses_store.get(response.json()["id"], path=responses_gateway["responses_db"]) is None
    assert responses_gateway["sent"][0]["messages"][-1]["content"] == sentinel
    _assert_no_bytes(responses_gateway["data"], sentinel)
    _assert_no_bytes(responses_gateway["cache"], sentinel)


def test_owner_kill_purges_responses_saved_before_it_was_enabled(responses_gateway):
    response = _post_response(responses_gateway, {"input": "older-stored-response-sentinel"})
    assert response.status_code == 200, response.text
    response_id = response.json()["id"]
    assert proxy.responses_store.get(response_id, path=responses_gateway["responses_db"])

    proxy._purge_prompt_memory_on_kill()

    assert proxy.responses_store.get(response_id, path=responses_gateway["responses_db"]) is None
