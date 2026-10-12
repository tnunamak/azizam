"""Exercise connection reuse through the real HTTP chat route and middleware."""
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store
from gateway.runtime_auth import hash_api_key
from tests.support.access import service_app


def test_chat_opens_one_connection_per_identity_lookup_and_revocation_is_immediate(tmp_path, monkeypatch):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    app = service_app("chat-test-app", path=db)
    key = "hotpath-http-fixture"
    credential = admin_store.create_credential_hash(
        subject_kind="app", subject_id=app["id"], app_id=app["id"],
        hash=hash_api_key(key), prefix="fixture", path=db,
    )
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", db)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", db)
    monkeypatch.setattr(proxy, "LLM_BACKEND_HOSTS", {"kobold": "http://127.0.0.1:5051"})
    monkeypatch.setattr(proxy, "get_active_backend", AsyncMock(return_value="kobold"))
    monkeypatch.setattr(proxy, "current_llm_model_for_backend", AsyncMock(return_value="fixture-model"))
    monkeypatch.setattr(proxy, "_service_availability_response", lambda *args, **kwargs: None)
    forwarded = []

    async def fake_send(execution_request, **kwargs):
        forwarded.append(execution_request)
        return AsyncMock(), httpx.Response(200, json={
            "id": "chatcmpl_fixture", "object": "chat.completion", "model": "fixture-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"},
                         "finish_reason": "stop"}],
        }, request=httpx.Request("POST", "http://fixture/v1/chat/completions"))

    monkeypatch.setattr(proxy, "send_llm_execution_request", fake_send)
    client = TestClient(proxy.app)
    body = {"model": "current", "messages": [{"role": "user", "content": "ping"}]}
    headers = {"authorization": f"Bearer {key}"}

    lookups = []
    real_scope = admin_store.runtime_auth_connection

    @contextmanager
    def counted(path=admin_store.DB_PATH):
        lookups.append(path)
        with real_scope(path) as connection:
            yield connection

    with patch.object(admin_store, "runtime_auth_connection", counted):
        first = client.post("/v1/chat/completions", json=body, headers=headers)
        first_count = len(lookups)
        second = client.post("/v1/chat/completions", json=body, headers=headers)
        second_count = len(lookups) - first_count

    assert first.status_code == second.status_code == 200
    assert first.json()["choices"] == second.json()["choices"]
    # The identity (credential, App, accountable person and both plans) is one
    # SELECT on one connection; the handler and the firehose each resolve it.
    assert first_count == second_count == 2, (first_count, second_count)
    assert len(forwarded) == 2
    admin_store.revoke_credential(credential["id"], path=db)
    denied = client.post("/v1/chat/completions", json=body, headers=headers)
    assert denied.status_code == 401
    assert len(forwarded) == 2
