"""Exercise connection reuse through the real HTTP chat route and middleware."""
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import proxy
from gateway import admin_store
from gateway.runtime_auth import hash_api_key


def test_chat_opens_fewer_admin_connections_and_revocation_is_immediate(tmp_path, monkeypatch):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    owner = admin_store.create_service_account("chat-test-owner", path=db)
    profile = admin_store.create_profile("chat-test-profile", {}, path=db)
    app = admin_store.create_app(
        "chat-test-app", kind="service", owner_service_account_id=owner["id"], path=db,
    )
    key = "hotpath-http-fixture"
    credential = admin_store.create_credential_hash(
        subject_kind="app", subject_id=app["id"], app_id=app["id"],
        hash=hash_api_key(key), prefix="fixture", profile_id=profile["id"], path=db,
    )
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", db)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", db)
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "LAN_BYPASS_ENABLED", False)
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

    @contextmanager
    def separate_connections(path):
        # The original resolver opened a separate connection in each helper.
        yield None

    with patch.object(admin_store, "runtime_auth_connection", separate_connections), patch.object(
        admin_store, "_conn", wraps=admin_store._conn,
    ) as connections:
        before = client.post("/v1/chat/completions", json=body, headers=headers)
        before_count = connections.call_count
    with patch.object(admin_store, "_conn", wraps=admin_store._conn) as connections:
        after = client.post("/v1/chat/completions", json=body, headers=headers)
        after_count = connections.call_count

    assert before.status_code == after.status_code == 200
    assert before.json()["choices"] == after.json()["choices"]
    # Both the handler and firehose resolve this five-lookup identity
    # (credential, session-App check, App, owner, profile): 10 -> 2.
    assert before_count - after_count == 8
    assert len(forwarded) == 2
    admin_store.revoke_credential(credential["id"], path=db)
    denied = client.post("/v1/chat/completions", json=body, headers=headers)
    assert denied.status_code == 401
    assert len(forwarded) == 2
