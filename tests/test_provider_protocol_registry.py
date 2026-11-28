"""Frozen wire behavior captured on 9755b8c4, before registry extraction."""
import asyncio
import json

import httpx
import pytest

from gateway import receipt_store, subscription_execution as execution
from gateway.provider_protocols import REQUEST_PROTOCOLS
from gateway.provider_connections import CONNECT_MECHANISMS
from gateway.subscription_kinds import normalize_kind
from tests.support import access as acc
from tests.support.subscription_routes import execute
from gateway import receipts
from tests.support.subscription_fakes import KIND, Upstream, callback_for, connect, make_service

protocol = REQUEST_PROTOCOLS[KIND.protocol]


@pytest.mark.parametrize("surface", ["responses", "chat"])
@pytest.mark.parametrize("stream,status", [(False, 200), (True, 200), (False, 429)])
def test_parent_wire_bytes(tmp_path, monkeypatch, surface, stream, status):
    receipt_store.init_db()
    monkeypatch.setattr(protocol.time, "time", lambda: 1_000_000)
    monkeypatch.setattr(protocol.secrets, "token_hex", lambda n: "a" * (2 * n))
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    store.set_host_id(KIND.id, "fixture-host")
    asyncio.run(connect(service, jwks, upstream))
    event = {"type": "response.completed", "response": {"id": "resp_fixed", "status": "completed", "output": [],
             "usage": {"input_tokens": 3, "output_tokens": 2}}}
    wire = b'data: ' + json.dumps(event).encode() + b'\n\n'
    upstream.routes[("POST", "/v1/responses")] = lambda request: httpx.Response(
        status, content=wire if status == 200 else b'{"error":{"code":"usage_limit_exceeded"}}',
        headers={"retry-after": "12"})
    body = {"model": KIND.pin_id("gpt-test"), "stream": stream}
    if surface == "chat":
        body["messages"] = [{"role": "user", "content": "hi"}]
    else:
        body["input"] = [{"role": "user", "content": "hi"}]
    plan = acc.plan_for("plan_granted", connections=[f"{KIND.id}-owner"])

    async def call():
        response = await execute(service, KIND, "gpt-test", surface=surface, body=body,
            identity=acc.identity(plan), access=acc.access(plan), app_profile=None, request_path="/v1/responses")
        output = b"".join([part async for part in response.body_iterator]) if stream else response.body
        return response.status_code, output

    code, output = asyncio.run(call())
    sent = next(request for request in upstream.calls if request.url.path == "/v1/responses")
    expected_sent, expected_code, expected_output = PARENT_WIRE[(surface, stream, status)]
    assert (sent.content, code, output) == (expected_sent, expected_code, expected_output)
    assert sent.headers["authorization"] == "Bearer access-1"


# Captured using the injected client and fixed clock before moving production code.
PARENT_WIRE = {
    ('responses', False, 200): (b'{"model":"gpt-test","stream":true,"input":[{"role":"user","content":"hi"}],"store":false,"include":["reasoning.encrypted_content"],"prompt_cache_key":"azizam-plan-9a2534483c7cb7ebf80b0ff1ae907fe0"}', 200, b'{"id":"resp_fixed","status":"completed","output":[],"usage":{"input_tokens":3,"output_tokens":2}}'),
    ('chat', False, 200): (b'{"model":"gpt-test","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}],"store":false,"stream":true,"include":["reasoning.encrypted_content"],"prompt_cache_key":"azizam-plan-94d83c01b5122fb38d76ef1e9e0c3909"}', 200, b'{"id":"chatcmpl-aaaaaaaaaaaaaaaaaaaaaaaa","object":"chat.completion","created":1000000,"model":"chatgpt-plan/gpt-test","choices":[{"index":0,"message":{"role":"assistant","content":""},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5,"prompt_tokens_details":{"cached_tokens":0},"completion_tokens_details":{"reasoning_tokens":0}}}'),
    ('responses', True, 200): (b'{"model":"gpt-test","stream":true,"input":[{"role":"user","content":"hi"}],"store":false,"include":["reasoning.encrypted_content"],"prompt_cache_key":"azizam-plan-9a2534483c7cb7ebf80b0ff1ae907fe0"}', 200, b'data: {"type": "response.completed", "response": {"id": "resp_fixed", "status": "completed", "output": [], "usage": {"input_tokens": 3, "output_tokens": 2}}}\n\n'),
    ('chat', True, 200): (b'{"model":"gpt-test","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}],"store":false,"stream":true,"include":["reasoning.encrypted_content"],"prompt_cache_key":"azizam-plan-94d83c01b5122fb38d76ef1e9e0c3909"}', 200, b'data: {"id":"chatcmpl-aaaaaaaaaaaaaaaaaaaaaaaa","object":"chat.completion.chunk","created":1000000,"model":"chatgpt-plan/gpt-test","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\ndata: {"id":"chatcmpl-aaaaaaaaaaaaaaaaaaaaaaaa","object":"chat.completion.chunk","created":1000000,"model":"chatgpt-plan/gpt-test","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'),
    ('responses', False, 429): (b'{"model":"gpt-test","stream":true,"input":[{"role":"user","content":"hi"}],"store":false,"include":["reasoning.encrypted_content"],"prompt_cache_key":"azizam-plan-9a2534483c7cb7ebf80b0ff1ae907fe0"}', 429, b'{"error":{"code":"plan_usage_limit_exceeded","message":"The usage limit of the connected plan was reached. Try again later.","type":"plan_refusal"}}'),
    ('chat', False, 429): (b'{"model":"gpt-test","input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"hi"}]}],"store":false,"stream":true,"include":["reasoning.encrypted_content"],"prompt_cache_key":"azizam-plan-94d83c01b5122fb38d76ef1e9e0c3909"}', 429, b'{"error":{"code":"plan_usage_limit_exceeded","message":"The usage limit of the connected plan was reached. Try again later.","type":"plan_refusal"}}'),
}


def provider_row():
    import yaml
    from gateway.subscription_kinds import KINDS_DIR
    return yaml.safe_load((KINDS_DIR / "chatgpt.yml").read_text())


@pytest.mark.parametrize("field,value,message", [
    ("mechanism", "missing-mechanism", "unknown connection mechanism 'missing-mechanism'"),
    ("protocol", "missing-protocol", "unknown request protocol 'missing-protocol'"),
    ("billing_mode", "missing-billing", "unsupported billing mode 'missing-billing'"),
])
def test_unknown_registry_ids_are_named(field, value, message):
    row = provider_row()
    row[field] = value
    with pytest.raises(ValueError, match=message):
        normalize_kind(row)


@pytest.mark.parametrize("through_router", [False, True])
def test_cli_mechanism_has_no_oauth_or_http_requirements(tmp_path, monkeypatch, through_router):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from gateway import subscription_api
    from gateway.admin_api import AdminContext
    from gateway.subscription_oauth import SubscriptionService
    from gateway.subscription_store import SubscriptionStore
    from tests.support.fake_upstream.cli_connection import CliConnection, CliProtocol

    mechanism = CliConnection()
    monkeypatch.setitem(CONNECT_MECHANISMS, "cli-sign-in-fake", mechanism)
    monkeypatch.setitem(REQUEST_PROTOCOLS, "cli-runner-fake", CliProtocol())
    row = provider_row()
    row.update(id="cli-test", mechanism="cli-sign-in-fake", protocol="cli-runner-fake")
    del row["oauth"], row["upstream"]
    kind = normalize_kind(row)
    assert kind.oauth == kind.upstream == {}
    store = SubscriptionStore(lambda: tmp_path / "cli.db")
    # An attempted HTTP operation would fail: this mechanism has no network dependency.
    kinds = {KIND.id: KIND, kind.id: kind} if through_router else {kind.id: kind}
    service = SubscriptionService(store, kinds, http=lambda **kwargs: pytest.fail("CLI used HTTP"))
    if through_router:
        app = FastAPI()
        auth = lambda request: AdminContext(subject="owner", actor_id="owner", roles=frozenset({"owner"}), auth_method="key")
        app.include_router(subscription_api.create_subscription_router(service=service, auth=auth))
        with TestClient(app) as client:
            base = "/admin/api/subscriptions/cli-test"
            started = client.post(base + "/connect", json={"listen": True}).json()["subscription"]
            assert started["listening"] is False
            assert started["command"] == "fake-cli sign-in"
            assert {item["kind"]: item["status"] for item in client.get("/admin/api/subscriptions").json()["subscriptions"]}[kind.id] == "not_connected"
            done = client.post(base + "/finish", json={"attempt_id": started["attempt_id"], "callback": "state=cli-state&done=yes"})
            assert done.status_code == 200 and done.json()["subscription"]["runner_status"] == "ready"
            assert {item["kind"]: item["status"] for item in client.get("/admin/api/subscriptions").json()["subscriptions"]}[kind.id] == "connected"
            ended = client.delete(base).json()["subscription"]
    else:
        started = asyncio.run(service.start("owner", kind.id, session=None))
        assert started["command"] == "fake-cli sign-in"
        assert asyncio.run(service.status_one(kind.id))["status"] == "not_connected"
        done = asyncio.run(service.finish("owner", started["attempt_id"], "state=cli-state&done=yes", session=None))
        assert done["runner_status"] == "ready"
        assert asyncio.run(service.acquire(kind.id)).kind == "runner"
        ended = asyncio.run(service.disconnect(kind.id, session=None))
    assert ended["status"] == "disconnected" and ended["signed_out"] is True
    assert {name for name, _ in mechanism.calls} >= {"start", "finish", "status", "disconnect"}
    assert b"opaque-cli-ticket" not in (tmp_path / "cli.db").read_bytes()


def test_renamed_provider_uses_the_same_wire_bytes(tmp_path):
    receipt_store.init_db()
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    store.set_host_id(KIND.id, "fixture-host")
    asyncio.run(connect(service, jwks, upstream))
    row = provider_row()
    row.update(id="renamed-plan", display_name="Renamed plan")
    renamed = normalize_kind(row)
    service.kinds = {renamed.id: renamed}
    store.set_host_id(renamed.id, "fixture-host")

    async def connect_renamed():
        started = await service.start("owner_1", renamed.id, session=None)
        jwks.claims["nonce"] = httpx.URL(started["authorize_url"]).params["nonce"]
        return await service.finish("owner_1", started["attempt_id"], callback_for(started), session=None)

    asyncio.run(connect_renamed())
    upstream.routes[("POST", "/v1/responses")] = lambda request: httpx.Response(
        429, content=b'{"error":{"code":"usage_limit_exceeded"}}')
    plan = acc.plan_for("plan_renamed", connections=[f"{renamed.id}-owner"])
    response = asyncio.run(execute(service, renamed, "gpt-test", surface="responses",
        body={"model": renamed.pin_id("gpt-test"), "stream": False, "input": [{"role": "user", "content": "hi"}]},
        identity=acc.identity(plan), access=acc.access(plan), app_profile=None, request_path="/v1/responses"))
    sent = next(request for request in upstream.calls if request.url.path == "/v1/responses")
    assert sent.content == PARENT_WIRE[("responses", False, 429)][0]
    assert response.body == PARENT_WIRE[("responses", False, 429)][2]


def test_attempt_state_is_opaque_encrypted_and_spent_once(tmp_path):
    from gateway.subscription_store import SubscriptionStore
    store = SubscriptionStore(lambda: tmp_path / "attempts.db", clock=lambda: 1_000_000)
    state = {"runner_ticket": "sensitive-ticket", "nested": {"round": 2}}
    attempt_id = store.begin_attempt(owner_id="owner", kind="cli-test", state="correlation", mechanism_state=state,
                                     ttl=600, session=None)
    assert b"sensitive-ticket" not in (tmp_path / "attempts.db").read_bytes()
    attempt = store.take_attempt(attempt_id=attempt_id, owner_id="owner")
    assert attempt.mechanism_state == state
    assert "sensitive-ticket" not in repr(attempt)
    assert store.take_attempt(attempt_id=attempt_id, owner_id="owner") is None


def test_pending_legacy_attempt_decodes_into_mechanism_state(tmp_path):
    import sqlite3
    from gateway import secret_store
    from gateway.subscription_store import SubscriptionStore
    path = tmp_path / "legacy.db"
    store = SubscriptionStore(lambda: path, clock=lambda: 1_000_000)
    attempt_id = store.begin_attempt(owner_id="owner", kind=KIND.id, state="correlation", mechanism_state={},
                                     ttl=600, session=None)
    secret_store.put_provider_secret("subscription-attempt/" + attempt_id, "legacy-verifier", db_path=path,
                                     allow_short=True, keep_suffix=False, session=None)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE subscription_attempts SET nonce=?, client_id=?, host_id=?, redirect_uri=? WHERE attempt_id=?",
                   ("legacy-nonce", "legacy-client", "legacy-host", "http://127.0.0.1/cb", attempt_id))
    attempt = store.take_attempt(attempt_id=attempt_id, owner_id="owner")
    assert attempt.mechanism_state == {"nonce": "legacy-nonce", "client_id": "legacy-client", "host_id": "legacy-host",
                                        "redirect_uri": "http://127.0.0.1/cb", "verifier": "legacy-verifier"}


def test_registries_import_independently_of_implementation_order():
    import subprocess
    import sys
    for module in ("gateway.connections.oauth_pkce", "gateway.provider_connections",
                   "gateway.protocols.responses_plan", "gateway.provider_protocols"):
        program = (
            "import importlib, inspect; importlib.import_module(" + repr(module) + "); "
            "from gateway.provider_connections import CONNECT_MECHANISMS; "
            "from gateway.provider_protocols import REQUEST_PROTOCOLS; "
            "assert not inspect.isclass(CONNECT_MECHANISMS['oauth-pkce-dynamic']); "
            "assert REQUEST_PROTOCOLS['openai-responses-plan'].check_request"
        )
        result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr


def test_optional_callback_listener_finishes_through_generic_service(tmp_path):
    from tests.support.subscription_fakes import json_route, token_body, urlparts
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    upstream.routes[("POST", urlparts(KIND.oauth["token_url"]))] = json_route(token_body())

    async def round_trip():
        started = await service.start("owner", KIND.id, session=None)
        jwks.claims["nonce"] = httpx.URL(started["authorize_url"]).params["nonce"]
        listener = await service.listen_for_callback(port=0)
        assert listener is not None
        writer = None
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
            target = httpx.URL(callback_for(started)).raw_path
            writer.write(b"GET " + target + b" HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            received = await asyncio.wait_for(reader.read(), 5)
            assert b"Sign-in received." in received
            assert listener.result["status"] == "connected"
        finally:
            listener.close()
            if writer is not None:
                writer.close()
                await writer.wait_closed()

    asyncio.run(round_trip())
    assert store.connection(KIND.id)["connected_by"] == "owner"
