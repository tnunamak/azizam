from __future__ import annotations

from pathlib import Path
from dataclasses import replace
import json
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

import proxy
from tests.support.access import access, plan_for, service_app  # noqa: E402
from gateway import admin_store, media_jobs, prompt_memory, receipt_store
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
        "Maker app", kind="personal", owner_user_id=user["id"], access_plan="person", path=admin_db
    )
    raw_key = "prompt-memory-responses-key"
    key_hash = hash_api_key(raw_key)
    identity = RuntimeIdentity(
        name="Maker app key",
        key_hash=key_hash,
        downstream={},
        access=access(plan_for("default-local")),
        app_id=app["id"],
        person_id=user["id"],
    )

    identities = {key_hash: identity}

    def lookup(candidate_hash: str, **_kwargs):
        return identities.get(candidate_hash)

    monkeypatch.setattr(proxy.ADMIN_STORE, "path", admin_db)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", admin_db)
    monkeypatch.setattr(proxy.responses_store, "DB_PATH", responses_db)
    monkeypatch.setattr(receipt_store, "DB_PATH", receipts_db)
    monkeypatch.setattr(media_jobs, "DB_PATH", media_db)
    monkeypatch.setattr(prompt_memory, "DB_PATH", media_db)
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", cache)
    monkeypatch.setattr(proxy, "runtime_identity_for_app_credential_hash", lookup)

    sent: list[dict] = []
    real_response_save = proxy.responses_store.save
    real_response_get = proxy.responses_store.get
    real_response_delete = proxy.responses_store.delete
    real_walk_chain = proxy.responses_store.walk_chain
    real_receipt_record = receipt_store.record

    def save_response(response_id: str, **kwargs):
        kwargs.setdefault("path", responses_db)
        return real_response_save(response_id, **kwargs)

    def get_response(response_id: str, **kwargs):
        kwargs.setdefault("path", responses_db)
        return real_response_get(response_id, **kwargs)

    def walk_chain(response_id: str, **kwargs):
        kwargs["path"] = responses_db
        return real_walk_chain(response_id, **kwargs)

    def delete_response(response_id: str, **kwargs):
        kwargs["path"] = responses_db
        return real_response_delete(response_id, **kwargs)

    def record_receipt(receipt, **kwargs):
        kwargs.setdefault("path", receipts_db)
        return real_receipt_record(receipt, **kwargs)

    monkeypatch.setattr(proxy.responses_store, "save", save_response)
    monkeypatch.setattr(proxy.responses_store, "get", get_response)
    monkeypatch.setattr(proxy.responses_store, "walk_chain", walk_chain)
    monkeypatch.setattr(proxy.responses_store, "delete", delete_response)
    monkeypatch.setattr(receipt_store, "record", record_receipt)

    async def fake_forward(request, auth_context, cc_body, *, stream):
        sent.append(cc_body)
        return httpx.Response(
            200,
            json=chat_completion({"role": "assistant", "content": "ok"}),
            request=httpx.Request("POST", "http://gateway/v1/chat/completions"),
        )

    # Use the same teardown stack as tests that replace this forwarder, so
    # their cleanup cannot restore this fixture's fake into later test files.
    monkeypatch.setattr(proxy, "_forward_chat_completions", fake_forward)
    yield {
        "client": TestClient(proxy.app),
        "headers": {"Authorization": f"Bearer {raw_key}"},
        "data": data,
        "cache": cache,
        "responses_db": responses_db,
        "media_db": media_db,
        "admin_db": admin_db,
        "person_id": user["id"],
        "app_id": app["id"],
        "identity": identity,
        "identities": identities,
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
    assert row["person_id"] == responses_gateway["person_id"]
    assert row["app_id"] == responses_gateway["app_id"]


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


@pytest.mark.parametrize("scope", ["other_app", "other_person", "same_person_other_app", "same_maker_new_key"])
def test_response_reads_delete_and_continuation_use_person_and_app(responses_gateway, scope):
    made = _post_response(responses_gateway, {"input": "private-conversation"})
    assert made.status_code == 200, made.text
    response_id = made.json()["id"]
    identity = responses_gateway["identity"]
    candidate = replace(identity, name="Maker app key", key_hash=hash_api_key("other-responses-key"))
    if scope in {"other_app", "same_person_other_app"}:
        candidate = replace(candidate, app_id="other-app")
    if scope in {"other_app", "other_person"}:
        other = admin_store.upsert_oidc_user("local", "other", path=responses_gateway["admin_db"])
        candidate = replace(candidate, person_id=other["id"])
    responses_gateway["identities"][candidate.key_hash] = candidate
    headers = {"Authorization": "Bearer other-responses-key"}
    client = responses_gateway["client"]
    expected = 200 if scope == "same_maker_new_key" else 404
    for suffix in ("", "/input_items"):
        response = client.get(f"/v1/responses/{response_id}{suffix}", headers=headers)
        assert response.status_code == expected, response.text
    response = client.post("/v1/responses", headers=headers, json={
        "model": "default", "input": "continue", "previous_response_id": response_id,
    })
    assert response.status_code == expected, response.text
    response = client.delete(f"/v1/responses/{response_id}", headers=headers)
    assert response.status_code == expected, response.text


@pytest.mark.parametrize("scope", ["service_app", "legacy_key"])
@pytest.mark.parametrize("explicit_store", [False, True])
def test_no_person_requires_explicit_store_and_durable_scope(responses_gateway, monkeypatch, scope, explicit_store):
    identity = responses_gateway["identity"]
    candidate = replace(identity, person_id=None)
    if scope == "legacy_key":
        candidate = replace(candidate, app_id=None)
    auth = proxy.AuthContext(identity=candidate)
    monkeypatch.setattr(proxy, "authenticate_request", lambda *_a, **_kw: auth)
    body = {"input": "no-person-response-prompt"}
    if explicit_store:
        body["store"] = True
    response = _post_response(responses_gateway, body)
    assert response.status_code == 200, response.text
    stored = explicit_store
    row = proxy.responses_store.get(response.json()["id"], path=responses_gateway["responses_db"])
    assert (row is not None) == stored
    assert response.json()["store"] is stored
    retrieved = responses_gateway["client"].get(f"/v1/responses/{response.json()['id']}")
    assert retrieved.status_code == (200 if stored else 404)


def test_legacy_response_with_only_display_name_is_invisible(responses_gateway):
    proxy.responses_store.save(
        "old-response", created_at=1, model="default", previous_response_id=None,
        request_json={"input": "old-private-prompt"}, response_json={"id": "old-response"},
        api_key_name=responses_gateway["identity"].name,
    )
    client = responses_gateway["client"]
    headers = responses_gateway["headers"]
    for suffix in ("", "/input_items"):
        response = client.get(f"/v1/responses/old-response{suffix}", headers=headers)
        assert response.status_code == 404
    response = _post_response(responses_gateway, {"input": "continue", "previous_response_id": "old-response"})
    assert response.status_code == 404


def test_streamed_response_persists_the_same_person_and_app_scope(responses_gateway, monkeypatch):
    from tests.test_anthropic_schema_conformance import _FakeClient, chunk, sse
    upstream = sse([chunk({"content": "ok"}), chunk({}, "stop")])
    async def forward(*_args, **_kwargs):
        return _FakeClient(), httpx.Response(200, content=upstream)
    monkeypatch.setattr(proxy, "_forward_chat_completions", forward)
    response = _post_response(responses_gateway, {"input": "stream-private-prompt", "stream": True})
    assert response.status_code == 200, response.text
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    completed = next(event["response"] for event in events if event["type"] == "response.completed")
    row = proxy.responses_store.get(completed["id"], path=responses_gateway["responses_db"])
    assert row["person_id"] == responses_gateway["person_id"]
    assert row["app_id"] == responses_gateway["app_id"]


@pytest.mark.parametrize("stream", [False, True])
def test_forget_during_active_response_cannot_restore_conversation(responses_gateway, monkeypatch, stream):
    from tests.test_anthropic_schema_conformance import _FakeClient, chunk, sse

    clock = [100]
    monkeypatch.setattr(proxy.time, "time", lambda: clock[0])
    older = _post_response(responses_gateway, {"input": "conversation-before-erasure"})
    assert older.status_code == 200, older.text
    older_id = older.json()["id"]
    assert proxy.responses_store.get(older_id) is not None

    async def forward(*_args, **_kwargs):
        # This is the erase operation while inference is still in progress.
        # Keep memory enabled so only the durable cutoff can prevent revival.
        proxy._purge_person_prompt_memory(responses_gateway["person_id"])
        if stream:
            return _FakeClient(), httpx.Response(200, content=sse([chunk({"content": "ok"}), chunk({}, "stop")]))
        return httpx.Response(200, json=chat_completion({"role": "assistant", "content": "ok"}))

    monkeypatch.setattr(proxy, "_forward_chat_completions", forward)
    sentinel = "conversation-in-flight-during-erasure"
    response = _post_response(responses_gateway, {"input": sentinel, "stream": stream})
    assert response.status_code == 200, response.text
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        response_id = next(event["response"]["id"] for event in events if event["type"] == "response.completed")
    else:
        response_id = response.json()["id"]
    assert proxy.responses_store.get(older_id) is None
    assert proxy.responses_store.get(response_id) is None
    _assert_no_bytes(responses_gateway["data"], sentinel)
    with prompt_memory.retention_guard(responses_gateway["admin_db"], responses_gateway["person_id"]) as policy:
        assert policy["remember"] is True
        assert policy["forgot_before"] == 100

    # Later requests can remember again, without resurrecting the erased chain.
    clock[0] = 101
    async def later_forward(*_args, **_kwargs):
        return httpx.Response(200, json=chat_completion({"role": "assistant", "content": "ok"}))
    monkeypatch.setattr(proxy, "_forward_chat_completions", later_forward)
    later = _post_response(responses_gateway, {"input": "conversation-after-erasure"})
    assert later.status_code == 200, later.text
    assert proxy.responses_store.get(later.json()["id"]) is not None


@pytest.mark.parametrize("app_kind", ["service", "another_person"])
def test_admin_minted_shared_key_does_not_default_to_storing_responses(responses_gateway, app_kind):
    from gateway.admin_api import AdminContext
    from gateway.admin_runtime import AdminSQLiteStore, runtime_identity_for_app_credential_hash
    gateway = responses_gateway
    path = gateway["admin_db"]
    owner = admin_store.upsert_oidc_user("local", "minting-owner", path=path)
    if app_kind == "service":
        sa = admin_store.ensure_service_account("shared-responses", path=path)
        app = service_app("Shared", path=path)
    else:
        app = admin_store.get_app(gateway["app_id"], path=path)
    key = AdminSQLiteStore(path).create_credential(app_id=app["id"], data={}, actor=AdminContext(
        subject=owner["id"], actor_kind="user", roles=frozenset({"owner"}),
    ))["api_key"]
    identity = runtime_identity_for_app_credential_hash(hash_api_key(key), path=path)
    gateway["identities"][hash_api_key(key)] = identity
    response = gateway["client"].post("/v1/responses", headers={"Authorization": f"Bearer {key}"},
        json={"model": "default", "input": "shared-response-sentinel"})
    assert response.status_code == 200, response.text
    assert proxy.responses_store.get(response.json()["id"], path=gateway["responses_db"]) is None
    _assert_no_bytes(gateway["data"], "shared-response-sentinel")
