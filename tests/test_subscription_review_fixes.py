"""Review fixes for plan models: timeouts, streamed items, 401 handling, admission, token checks, route coverage."""

from __future__ import annotations

import asyncio
import json
import os
import time

import httpx
import pytest
from authlib.jose import JsonWebKey, jwt
from fastapi.testclient import TestClient

import proxy
from gateway import subscription_api, subscription_kinds, subscription_oauth
from gateway.oidc_jwks import JWKSCache
from gateway.subscription_oauth import SubscriptionError
from tests.support import access as acc
from tests.support.subscription_fakes import (
    KIND, Upstream, body_of, callback_for, connect, json_route, make_service, token_body, urlparts)
from tests.test_subscription_plan_access import (  # noqa: F401  (fixtures)
    COMPLETED, MODEL, RESPONSES_PATH, _client, _receipts_table, call, connected, granted, run, sse)

TOKEN_PATH = urlparts(KIND.oauth["token_url"])
AUTH = {"authorization": "Bearer k", "x-api-key": "k"}


def ok_stream(*events: dict):
    return lambda request: httpx.Response(200, content=sse(*events), headers={"content-type": "text/event-stream"})


def code_of(response) -> str:
    body = response.body if hasattr(response, "body") else response.content
    return json.loads(body)["error"]["code"]


# ------------------------------------------------------------------ 1. the plan call's own read limit

def test_the_plan_call_waits_for_a_slow_first_byte(connected):
    service, upstream = connected

    def slow_then_ok(request):
        if request.extensions["timeout"]["read"] < 120:  # an upstream that pauses longer than the sign-in limit
            raise httpx.ReadTimeout("paused", request=request)
        return httpx.Response(200, content=sse(COMPLETED), headers={"content-type": "text/event-stream"})

    upstream.routes[("POST", RESPONSES_PATH)] = slow_then_ok
    ident, access = granted()
    assert call(service, ident, access).status_code == 200


def test_the_sign_in_calls_keep_the_short_limit_and_the_plan_call_does_not(tmp_path):
    assert subscription_oauth._default_http().timeout.read == 30
    service = subscription_oauth.SubscriptionService(
        subscription_oauth.subscription_store.SubscriptionStore(lambda: tmp_path / "s.db"), {"chatgpt": KIND})
    assert service.new_client(KIND).timeout.read >= 300
    assert service.new_client(KIND).timeout.connect == 30


# ------------------------------------------------------------------ 2. items done before an empty completed.output

ITEM = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "from items"}]}
EMPTY_COMPLETED = {"type": "response.completed", "response": {"id": "r", "status": "completed", "output": [],
                                                               "usage": {"input_tokens": 1, "output_tokens": 1}}}


def test_a_non_streamed_response_uses_the_streamed_items_when_completed_has_none(connected):
    service, upstream = connected
    upstream.routes[("POST", RESPONSES_PATH)] = ok_stream(
        {"type": "response.output_item.done", "output_index": 0, "item": ITEM}, EMPTY_COMPLETED)
    ident, access = granted()
    out = json.loads(call(service, ident, access).body)
    assert out["output"] == [ITEM]


def test_a_non_streamed_chat_reply_uses_the_streamed_items_when_completed_has_none(connected):
    service, upstream = connected
    upstream.routes[("POST", RESPONSES_PATH)] = ok_stream(
        {"type": "response.output_item.done", "output_index": 0, "item": ITEM}, EMPTY_COMPLETED)
    ident, access = granted()
    out = json.loads(call(service, ident, access, surface="chat").body)
    assert out["choices"][0]["message"]["content"] == "from items"


def test_a_filled_completed_output_is_kept_as_it_is(connected):
    service, upstream = connected
    upstream.routes[("POST", RESPONSES_PATH)] = ok_stream(
        {"type": "response.output_item.done", "output_index": 0, "item": ITEM}, COMPLETED)
    ident, access = granted()
    assert json.loads(call(service, ident, access).body)["output"] == COMPLETED["response"]["output"]


# ------------------------------------------------------------------ 3. a 401 forces one refresh and one retry

def _first_401_then_ok(upstream, seen):
    ok = ok_stream(COMPLETED)

    def handler(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(401, json={}) if len(seen) == 1 else ok(request)

    upstream.routes[("POST", RESPONSES_PATH)] = handler


def test_a_401_then_a_good_refresh_retries_once_with_the_new_token(connected):
    service, upstream = connected
    seen: list[str] = []
    _first_401_then_ok(upstream, seen)
    upstream.routes[("POST", TOKEN_PATH)] = json_route(token_body("access-2", "refresh-2"))
    ident, access = granted()
    assert call(service, ident, access).status_code == 200
    assert seen == ["Bearer access-1", "Bearer access-2"]
    assert service.store.connection("chatgpt")["status"] == "connected"


def test_a_401_then_a_terminal_refresh_failure_marks_the_connection_lost_as_503(connected):
    service, upstream = connected
    upstream.routes[("POST", RESPONSES_PATH)] = json_route({}, 401)
    upstream.routes[("POST", TOKEN_PATH)] = json_route({"error": "invalid_grant"}, 400)
    ident, access = granted()
    response = call(service, ident, access)
    assert response.status_code == 503 and code_of(response) == "plan_connection_invalid"
    assert service.store.connection("chatgpt")["status"] == "needs_reconnect"


def test_a_401_then_a_refresh_that_could_not_run_does_not_end_the_connection(connected):
    service, upstream = connected
    upstream.routes[("POST", RESPONSES_PATH)] = json_route({}, 401)
    upstream.routes[("POST", TOKEN_PATH)] = json_route({}, 500)
    ident, access = granted()
    response = call(service, ident, access)
    assert response.status_code == 503 and code_of(response) == "plan_refresh_unavailable"
    assert service.store.connection("chatgpt")["status"] == "connected"


def test_a_401_after_a_good_refresh_is_a_503_and_asks_for_one_retry_only(connected):
    service, upstream = connected
    upstream.routes[("POST", RESPONSES_PATH)] = json_route({}, 401)
    upstream.routes[("POST", TOKEN_PATH)] = json_route(token_body("access-2", "refresh-2"))
    ident, access = granted()
    response = call(service, ident, access)
    assert response.status_code == 503 and code_of(response) == "plan_connection_invalid"
    assert upstream.count(RESPONSES_PATH) == 2 and upstream.count(TOKEN_PATH) == 2  # the sign-in exchange and one refresh
    assert service.store.connection("chatgpt")["status"] == "connected"


def test_a_missing_token_is_never_a_401_to_the_client(connected):
    service, _ = connected
    service.store.drop_tokens("chatgpt", session=None)
    ident, access = granted()
    assert call(service, ident, access).status_code == 503


# ------------------------------------------------------------------ 5. the identity proof, with a real signature check

class _Keys(JWKSCache):
    def __init__(self, key, *, fail=False):
        super().__init__()
        self._key, self._fail = key, fail

    async def _fetch(self, uri):
        if self._fail:
            raise httpx.ConnectError("down")
        return {"keys": [self._key.as_dict(is_private=False)]}


def _proof(key, claims: dict) -> str:
    return jwt.encode({"alg": "RS256", "kid": "k1"}, claims, key).decode()


async def _sign_in_with(tmp_path, *, change=None, fail_fetch=False):
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "k1"})
    upstream = Upstream()
    service, _, _ = make_service(tmp_path, upstream)
    service.mechanisms[KIND.mechanism].proof_verifier = _Keys(key, fail=fail_fetch)
    started = await service.start("owner_1", "chatgpt", session=None)
    nonce = httpx.URL(started["authorize_url"]).params["nonce"]
    claims = {"iss": KIND.oauth["issuer"], "aud": ["dynamic-issued"], "sub": "acct-1", "email": "o@example.test",
              "exp": int(time.time()) + 600, "nonce": nonce}
    claims.update(change or {})
    upstream.routes[("POST", TOKEN_PATH)] = json_route(token_body(id_token=_proof(key, claims)))
    return await service.finish("owner_1", started["attempt_id"], callback_for(started), session=None)


def test_a_good_signed_proof_signs_in(tmp_path):
    # The service's clock is a fixed 1,000,000 s in these fakes, so give it the real time for this check.
    assert run(_sign_in_with(tmp_path, change={"exp": 2_000_000_000}))["status"] == "connected"


@pytest.mark.parametrize("change", [
    {"iss": "https://evil.example"}, {"aud": ["someone-else"]}, {"exp": 1}, {"nonce": "not-the-nonce"}, {"aud": "someone-else"},
])
def test_a_proof_with_a_wrong_claim_is_refused_by_name(tmp_path, change):
    with pytest.raises(SubscriptionError) as error:
        run(_sign_in_with(tmp_path / "x", change=change)) if (tmp_path / "x").mkdir() is None else None
    assert error.value.code == "id_token_invalid"


def test_a_signing_key_fetch_that_fails_is_a_named_refusal_not_a_500(tmp_path):
    with pytest.raises(SubscriptionError) as error:
        run(_sign_in_with(tmp_path, fail_fetch=True))
    assert error.value.code == "id_token_invalid"


# ------------------------------------------------------------------ 6. the record file is opened once and bounded

def test_a_record_file_that_is_a_symlink_is_refused(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}")
    link = tmp_path / "link.json"
    os.symlink(real, link)
    with pytest.raises(SubscriptionError) as error:
        subscription_api.read_record_file(str(link))
    assert error.value.code == "import_unreadable"


def test_a_deeply_nested_record_is_refused_not_a_crash(tmp_path):
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 40000)
    with pytest.raises(SubscriptionError) as error:
        subscription_api.read_record_file(str(deep))
    assert error.value.code == "import_unreadable"


def test_a_large_or_a_non_regular_record_is_refused(tmp_path):
    big = tmp_path / "big.json"
    big.write_text(" " * (subscription_api._MAX_RECORD_BYTES + 10) + "{}")
    for target in (big, tmp_path):
        with pytest.raises(SubscriptionError):
            subscription_api.read_record_file(str(target))


def test_a_plain_record_is_read(tmp_path):
    record = tmp_path / "ok.json"
    record.write_text('{"a": 1}')
    assert subscription_api.read_record_file(str(record)) == {"a": 1}


# ------------------------------------------------------------------ 7. a 400 from the plan is a named 400

def test_an_upstream_400_is_a_named_400_in_the_gateways_own_words(connected):
    service, upstream = connected
    upstream.routes[("POST", RESPONSES_PATH)] = json_route(
        {"error": {"message": "Bad key sk-secret-1234 in 'x'.", "type": "invalid_request_error"}}, 400)
    ident, access = granted()
    response = call(service, ident, access)
    assert response.status_code == 400
    error = json.loads(response.body)["error"]
    assert error["code"] == "plan_request_rejected"
    assert "sk-secret" not in response.body.decode() and "'x'" not in error["message"]


def test_an_upstream_400_detail_text_never_reaches_the_client(connected):
    service, upstream = connected
    upstream.routes[("POST", RESPONSES_PATH)] = json_route({"detail": "zzz-upstream-words"}, 400)
    ident, access = granted()
    assert "zzz-upstream-words" not in call(service, ident, access).body.decode()


FAILED = {"type": "response.failed", "response": {"id": "r", "status": "failed",
                                                   "error": {"code": "invalid_prompt", "message": "That prompt was refused."}}}


def test_a_mid_stream_failed_event_for_a_bad_request_is_a_named_400_without_its_words(connected):
    service, upstream = connected
    upstream.routes[("POST", RESPONSES_PATH)] = ok_stream(FAILED)
    ident, access = granted()
    response = call(service, ident, access)
    assert response.status_code == 400
    error = json.loads(response.body)["error"]
    assert error["code"] == "plan_request_rejected" and "refused" not in error["message"]


def test_a_mid_stream_failure_with_a_server_code_stays_a_502(connected):
    service, upstream = connected
    upstream.routes[("POST", RESPONSES_PATH)] = ok_stream(
        {"type": "response.failed", "response": {"error": {"code": "server_error", "message": "boom"}}})
    ident, access = granted()
    response = call(service, ident, access)
    assert response.status_code == 502 and "boom" not in response.body.decode()


# ------------------------------------------------------------------ 8. chat callers' limits and sampling

def test_a_chat_request_with_caps_and_sampling_is_dropped_and_disclosed(connected):
    service, upstream = connected
    ident, access = granted()
    body = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 64, "max_completion_tokens": 64,
            "temperature": 0.2, "top_p": 0.9, "stop": ["x"], "user": "u1"}
    response = call(service, ident, access, surface="chat", body=body)
    assert response.status_code == 200
    sent = body_of(next(c for c in upstream.calls if c.url.path == RESPONSES_PATH))
    assert not KIND.dropped_fields & sent.keys()
    assert response.headers["x-azizam-dropped-params"] == ",".join(sorted(KIND.dropped_fields & body.keys()))


def test_the_responses_surface_still_names_a_sampling_field_it_cannot_send(connected):
    service, upstream = connected
    ident, access = granted()
    response = call(service, ident, access, body={"model": MODEL, "input": [{"role": "user", "content": "x"}], "max_output_tokens": 5})
    assert response.status_code == 400 and code_of(response) == "plan_unsupported_parameter"


# ------------------------------------------------------------------ 4 and 9. through the real app

@pytest.fixture()
def world(connected, monkeypatch):
    service, upstream = connected
    from gateway import registry_snapshot
    sid = KIND.model_prefix
    services = {sid: {"id": sid, "enabled": True, "config": {
        "credential_source": {"kind": "connection", "connection_id": "chatgpt-owner"},
        "request_protocol": KIND.protocol, "models": []}}}
    snapshot = registry_snapshot.build({"services": services}, registry_snapshot.BundledRelease({}), generation=1,
        model_aliases={"chat": {"stable": {"plan-alias": MODEL}}},
        connection_models={"chatgpt-owner": {"models": {"gpt-test": []}}})
    monkeypatch.setattr(proxy, "GATEWAY_REGISTRY_PROVIDER", registry_snapshot.Provider(snapshot))
    monkeypatch.setattr(proxy, "SUBSCRIPTIONS", service)
    return service, upstream


def post(path, body, paid, *, headers=AUTH):
    plan = acc.plan_for("plan_app", connections=[f"{kind}-owner" for kind in paid])
    with acc.authenticated(acc.identity(plan, person_id="usr_app")):
        return TestClient(proxy.app).post(path, json=body, headers=headers)


CHAT_BODY = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
RESPONSES_BODY = {"model": MODEL, "input": [{"role": "user", "content": "hi"}]}


def _alias(monkeypatch):
    from gateway import stable_aliases
    monkeypatch.setattr(proxy, "resolve_stable_alias", lambda operation, model: stable_aliases.AliasResolution(
        requested="plan-alias", operation="chat", layer="stable", chain=(), target=MODEL) if model == "plan-alias" else None)


def _in_process_loopback(monkeypatch):
    async def forward(request, auth_context, cc_body, *, stream):
        assert not stream
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://gateway.test") as client:
            return await client.post("/v1/chat/completions", json=cc_body, headers={"authorization": "Bearer k"})

    monkeypatch.setattr(proxy, "_forward_chat_completions", forward)


ROUTES = {
    "chat": ("/v1/chat/completions", lambda: CHAT_BODY),
    "responses": ("/v1/responses", lambda: RESPONSES_BODY),
    "alias": ("/v1/chat/completions", lambda: {**CHAT_BODY, "model": "plan-alias"}),
    "messages": ("/v1/messages", lambda: {"model": MODEL, "max_tokens": 64, "temperature": 0.3,
                                          "messages": [{"role": "user", "content": "hi"}]}),
}


@pytest.mark.parametrize("route", list(ROUTES))
def test_each_route_refuses_a_plan_without_the_provider_and_serves_one_with_it(world, monkeypatch, route):
    service, upstream = world
    _alias(monkeypatch)
    _in_process_loopback(monkeypatch)
    path, make_body = ROUTES[route]
    refused = post(path, make_body(), paid=())
    assert refused.status_code == 403, refused.text
    assert upstream.count(RESPONSES_PATH) == 0
    served = post(path, make_body(), paid=("chatgpt",))
    assert served.status_code == 200, served.text
    assert upstream.count(RESPONSES_PATH) == 1
    if route == "messages":
        assert served.headers["x-azizam-dropped-params"] == "max_tokens,reasoning_effort,temperature"
    sent = body_of(next(c for c in upstream.calls if c.url.path == RESPONSES_PATH))
    assert sent["model"] == "gpt-test" and sent["store"] is False


OPENAI_NAME_PATTERNS = {"chat": {"stable": {"default": "local-default"},
                                  "openai": [{"match": "chatgpt-*", "to": "default"}, {"match": "gpt-*", "to": "default"}]}}


def test_a_failed_plan_chat_never_falls_back_to_an_alias_engine(world, monkeypatch):
    # The chat path never asks the alias resolver about a plan id, so an alias's fallback engines cannot take over
    # when the plan answers 503.
    service, upstream = world
    monkeypatch.setitem(proxy.config, "model_aliases", OPENAI_NAME_PATTERNS)
    asked = []
    real = proxy.resolve_stable_alias
    monkeypatch.setattr(proxy, "resolve_stable_alias", lambda operation, model: asked.append(model) or real(operation, model))
    upstream.routes[("POST", RESPONSES_PATH)] = lambda request: httpx.Response(503, json={"error": {"message": "down"}})
    response = post("/v1/chat/completions", CHAT_BODY, paid=("chatgpt",))
    assert response.status_code >= 500 and response.headers.get("x-azizam-provider") == "chatgpt", response.text
    assert MODEL not in asked
    assert upstream.count(RESPONSES_PATH) >= 1


def test_plan_chat_reaches_the_plan_when_a_chatgpt_pattern_is_configured(world, monkeypatch):
    # Live bug 11-23: the "chatgpt-*" OpenAI-name pattern caught "chatgpt-plan/..." on the chat path, so a local
    # model answered a plan request.
    service, upstream = world
    monkeypatch.setitem(proxy.config, "model_aliases", OPENAI_NAME_PATTERNS)
    served = post("/v1/chat/completions", CHAT_BODY, paid=("chatgpt",))
    assert served.status_code == 200, served.text
    assert upstream.count(RESPONSES_PATH) == 1


@pytest.mark.parametrize("route", ["chat", "responses"])
def test_an_operator_pause_of_the_plan_service_refuses_before_the_upstream_call(world, monkeypatch, route):
    service, upstream = world
    monkeypatch.setattr(proxy, "_operator_service_availability_overrides",
                        lambda: {KIND.model_prefix: {"state": "maintenance", "reason": "paused for now"}})
    path, make_body = ROUTES[route]
    response = post(path, make_body(), paid=("chatgpt",))
    assert response.status_code in (429, 503), response.text
    assert upstream.count(RESPONSES_PATH) == 0


def test_a_disabled_intent_refuses_plan_chat_too(world, monkeypatch):
    service, upstream = world
    monkeypatch.setattr(proxy, "_effective_media_job_admission_cfg", lambda: {"disabled_resource_classes": ["chat"]})
    response = post("/v1/chat/completions", CHAT_BODY, paid=("chatgpt",))
    assert response.status_code in (429, 503), response.text
    assert upstream.count(RESPONSES_PATH) == 0


# ------------------------------------------------------------------ 10. the finish round trip

def test_the_finish_endpoint_completes_an_attempt_and_the_response_has_no_token(tmp_path):
    client, service = _client(tmp_path, {"owner"})
    started = client.post("/admin/api/subscriptions/chatgpt/connect", headers={"x-test-who": "1"}, json={}).json()["subscription"]
    upstream_routes = service._http.__self__  # the fake upstream behind the service
    nonce = httpx.URL(started["authorize_url"]).params["nonce"]
    service.mechanisms[KIND.mechanism].proof_verifier.claims["nonce"] = nonce
    upstream_routes.routes[("POST", TOKEN_PATH)] = json_route(token_body("secret-access", "secret-refresh"))
    callback = callback_for(started)
    done = client.post("/admin/api/subscriptions/chatgpt/finish", headers={"x-test-who": "1"},
                       json={"attempt_id": started["attempt_id"], "callback": callback})
    assert done.status_code == 200, done.text
    assert done.json()["subscription"]["status"] == "connected"
    assert "secret-access" not in done.text and "secret-refresh" not in done.text
    assert service.store.connection("chatgpt")["connected_by"] == "p1"


# ------------------------------------------------------------------ the Anthropic route's "minimal" effort

def test_a_chat_minimal_effort_becomes_the_lowest_advertised_effort_and_is_disclosed(connected):
    service, upstream = connected
    ident, access = granted()
    body = {**CHAT_BODY, "reasoning_effort": "minimal"}
    response = call(service, ident, access, surface="chat", body=body)
    assert response.status_code == 200
    sent = body_of(next(c for c in upstream.calls if c.url.path == RESPONSES_PATH))
    assert sent["reasoning"]["effort"] == "low"
    assert response.headers["x-azizam-dropped-params"] == "reasoning_effort"


def test_any_other_unsupported_chat_effort_is_still_a_named_400(connected):
    service, upstream = connected
    ident, access = granted()
    response = call(service, ident, access, surface="chat", body={**CHAT_BODY, "reasoning_effort": "xhigh"})
    assert response.status_code == 400 and code_of(response) == "plan_unsupported_reasoning_effort"
    assert upstream.count(RESPONSES_PATH) == 0
