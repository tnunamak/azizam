"""Protocol frontends forward the selected real credential on the loopback hop.

The Anthropic and Ollama frontends reach the chat and embeddings routes over
the gateway's own loopback hop (served in process at 127.0.0.1:PROXY_PORT).
The inner hop must authenticate as the member's App: the LLM substrate (an
FK fake behind the real registry) only answers a request attributed to the
member key. Keys are minted and revoked in a real admin store.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import proxy
from gateway import accounts, admin_store
from gateway.admin_oidc import AdminSession
from gateway.runtime_auth import hash_api_key
from gateway.user_runtime import mint_user_app_key, revoke_user_app_key
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.support.gateway import gateway_client, register_litellm, use_admin_db
from tests.support.access import service_app, store_plan

LAN_PEER = ("192.168.1.25", 41000)
FRONTENDS = {
    "chat": ("/v1/messages", {"model": "chat-model", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}, "member-chat"),
    "embeddings": ("/api/embed", {"model": "embedding-model", "input": "hi"}, "member-embeddings"),
}


@pytest.fixture
def gateway(fake_upstreams, monkeypatch, tmp_path):
    db = use_admin_db(monkeypatch, tmp_path)
    monkeypatch.setattr(proxy, "AUTH_PUBLIC_PATHS", ["/health"])
    monkeypatch.setattr(proxy, "TRUSTED_PROXIES", [])
    monkeypatch.setattr(proxy, "CLOUDFLARE_MODE", False)
    monkeypatch.setattr(proxy, "ALLOW_QUERY_PARAM_KEY", False)
    fake_upstreams.router.add(f"127.0.0.1:{proxy.PROXY_PORT}", proxy.app)

    def serve(scenario: str, **values):
        fake = fake_upstreams.asgi(load_scenario(f"openai-compatible/{scenario}", **values), host="litellm.fk.test")
        register_litellm(monkeypatch, db, fake.base_url)
        return fake

    return db, serve


def _member(db):
    user = admin_store.upsert_oidc_user("local", "member", email="member@example.test", path=db)
    accounts.change_person(user["id"], "owner", path=db, plan_id=store_plan("Member", path=db)["id"])
    app = admin_store.create_app("Member access", kind="personal", owner_user_id=user["id"], access_plan="person", path=db)
    session = AdminSession(subject=user["id"], email=user["email"], role="none", issued_at=1, expires_at=4_000_000_000)
    return session, mint_user_app_key(session, app["id"], path=db)


def _post(path: str, body: dict, *, headers: list[tuple[str, str]], params: str = "") -> httpx.Response:
    async def call() -> httpx.Response:
        async with gateway_client(client=LAN_PEER) as client:
            return await client.post(path + (f"?{params}" if params else ""), json=body, headers=headers)

    return asyncio.run(call())


@pytest.mark.parametrize("forwarder", ["chat", "embeddings"])
@pytest.mark.parametrize("source", ["bearer", "api-key", "query"])
def test_protocol_loopback_authenticates_as_the_member(gateway, monkeypatch, forwarder, source):
    db, serve = gateway
    _session, credential = _member(db)
    member_key = credential["api_key"]
    path, body, scenario = FRONTENDS[forwarder]
    litellm = serve(scenario, model=body["model"], key_prefix=hash_api_key(member_key)[:18])
    params = ""
    if source == "bearer":
        headers = [("authorization", f"Bearer {member_key}")]
    elif source == "api-key":
        headers = [("api-key", member_key)]
    else:
        monkeypatch.setattr(proxy, "ALLOW_QUERY_PARAM_KEY", True)
        headers = []
        params = f"api_key={member_key}"

    response = _post(path, body, headers=headers, params=params)

    print("REQS", [(r.path, dict(r.headers), r.body[:200]) for r in litellm.requests], hash_api_key(member_key)[:18])
    assert response.status_code == 200, response.text
    if forwarder == "chat":
        assert response.json()["content"] == [{"type": "text", "text": "hello member"}]
    else:
        assert response.json()["embeddings"] == [[0.25, -0.5]]
    [forwarded] = litellm.requests
    assert forwarded.headers["x-azizam-app"] == "Member access"
    assert forwarded.headers["x-azizam-key-prefix"] == hash_api_key(member_key)[:18]
    assert "authorization" not in forwarded.headers and "api-key" not in forwarded.headers
    assert member_key not in forwarded.body.decode()


@pytest.mark.parametrize("forwarder", ["chat", "embeddings"])
def test_a_placeholder_key_beside_the_member_key_is_refused(gateway, forwarder):
    """The LAN placeholder keys are gone: "dummy" is an invalid key, not a skip."""
    db, serve = gateway
    _session, credential = _member(db)
    path, body, _scenario = FRONTENDS[forwarder]
    headers = [("authorization", "Bearer dummy"), ("authorization", f"Bearer {credential['api_key']}")]
    response = _post(path, body, headers=headers)
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("forwarder", ["chat", "embeddings"])
def test_revoked_member_key_gets_401_and_reaches_no_upstream(gateway, forwarder):
    db, serve = gateway
    session, credential = _member(db)
    revoke_user_app_key(session, credential["id"], path=db)
    litellm = serve("no-calls")
    path, body, _scenario = FRONTENDS[forwarder]

    response = _post(path, body, headers=[("authorization", f"Bearer {credential['api_key']}")])

    assert response.status_code == 401, response.text
    assert litellm.requests == []


def test_frontend_chat_reaches_chat_through_the_loopback_once(gateway):
    """The frontend already applied the App's plan; the secret-bearing
    loopback hop must not demand a second permission."""
    db, serve = gateway
    app = service_app("Anthropic app", path=db, caps=("chat",))
    key = "frontend-only-key"
    admin_store.create_credential_hash(
        subject_kind="app", subject_id=app["id"], app_id=app["id"],
        hash=hash_api_key(key), prefix="frontend", path=db,
    )
    path, body, scenario = FRONTENDS["chat"]
    litellm = serve(scenario, model=body["model"], key_prefix=hash_api_key(key)[:18])

    response = _post(path, body, headers=[("authorization", f"Bearer {key}")])

    print("REQS", [(r.path, dict(r.headers), r.body[:200]) for r in litellm.requests], hash_api_key(member_key)[:18])
    assert response.status_code == 200, response.text
    assert response.json()["content"] == [{"type": "text", "text": "hello member"}]
    assert len(litellm.calls("chat")) == 1
