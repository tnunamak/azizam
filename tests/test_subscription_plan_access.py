"""Plan models: who may use them (the existing access plans), what is sent upstream, and what comes back."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import admin_catalog, subscription_api, subscription_execution as execution
from gateway.admin_api import AdminAuthError, AdminContext
from tests.support import access as acc
from tests.support.subscription_routes import execute
from gateway import receipts
from tests.support.subscription_fakes import (
    KIND, Upstream, body_of, connect, json_route, make_service)

MODEL = "chatgpt-plan/gpt-test"
RESPONSES_PATH = "/v1/responses"


def run(coro):
    return asyncio.run(coro)


def sse(*events: dict) -> bytes:
    return b"".join(b"event: " + e["type"].encode() + b"\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)


COMPLETED = {"type": "response.completed", "response": {
    "id": "resp_1", "status": "completed", "usage": {"input_tokens": 3, "output_tokens": 2},
    "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}]}}


@pytest.fixture(autouse=True)
def _receipts_table():
    from gateway import receipt_store
    from tests.support.adapter_differential_rig import reset_store
    receipt_store.init_db()
    reset_store(receipt_store.DB_PATH)


@pytest.fixture()
def connected(tmp_path):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    run(connect(service, jwks, upstream))
    upstream.routes[("POST", RESPONSES_PATH)] = lambda r: httpx.Response(
        200, content=sse({"type": "response.created", "response": {}}, COMPLETED),
        headers={"content-type": "text/event-stream"})
    return service, upstream


def call(service, ident, access, *, surface="responses", body=None):
    body = body or ({"model": MODEL, "input": [{"role": "user", "content": "hi"}]} if surface == "responses"
                    else {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})
    return run(execute(service, KIND, "gpt-test", surface=surface, body=body, identity=ident, access=access,
                                 app_profile=None, request_path="/v1/" + ("responses" if surface == "responses" else "chat/completions")))


def granted(paid=("chatgpt",)):
    plan = acc.plan_for("plan_granted", connections=[f"{kind}-owner" for kind in paid])
    return acc.identity(plan), acc.access(plan)


# ------------------------------------------------------------------ who may use plan models

def test_default_plan_without_the_provider_is_refused_by_name(connected):
    service, upstream = connected
    plan = acc.plan_for("plan_default")  # no paid providers: nobody holds plan models until a plan lists them
    response = call(service, acc.identity(plan), acc.access(plan))
    assert response.status_code == 403
    error = json.loads(response.body)["error"]
    assert (error["code"], error["details"], error["message"]) == (
        "not_in_access_plan", {"side": "person"}, f"{MODEL} isn't in your access plan.")
    assert upstream.count(RESPONSES_PATH) == 0


def test_an_app_plan_without_the_connection_is_named_as_the_app_s(connected):
    # The person's plan holds the subscription; the app's plan does not. The refusal says which one to change.
    service, upstream = connected
    person = acc.plan_for("plan_person", connections=["chatgpt-owner"])
    app = acc.plan_for("plan_app")
    response = call(service, acc.identity(acc.access(person, app)), acc.access(person, app))
    assert response.status_code == 403
    error = json.loads(response.body)["error"]
    assert (error["code"], error["details"]) == ("not_in_access_plan", {"side": "app"})
    assert "this app's access plan doesn't" in error["message"]
    assert upstream.count(RESPONSES_PATH) == 0
    # The member's own receipt carries the same answer: refused, by the app's plan, nothing run.
    from gateway import receipt_store
    receipt = receipt_store.get(response.headers["x-azizam-request-id"], audience="client")
    assert receipt["status"] == "rejected"
    assert receipt["refusal"] == {"code": "not_in_access_plan", "limit": None, "side": "app"}


def test_a_plan_that_lists_the_provider_may_call(connected):
    service, upstream = connected
    ident, access = granted()
    response = call(service, ident, access)
    assert response.status_code == 200
    assert upstream.count(RESPONSES_PATH) == 1


def test_the_grant_is_a_normal_plan_setting(tmp_path):
    from gateway.access_plans import plan_to_document
    document = plan_to_document(acc.plan_for("plan_x", connections=["chatgpt-owner"]))
    assert document["connections"] == ["chatgpt-owner"]
    assert document["paid_providers"] == []
    choices = admin_catalog.access_plan_editor_contract({"services": {}, "workflows": {}},
                                                        [{"id": "chatgpt-owner", "label": "ChatGPT plan", "available": True}])
    assert {"id": "chatgpt-owner", "label": "ChatGPT plan", "available": True} in choices["connections"]


def test_an_app_plan_that_lacks_the_provider_still_refuses(connected):
    service, _ = connected
    person = acc.plan_for("plan_person", connections=["chatgpt-owner"])
    app_plan = acc.plan_for("plan_app")
    response = call(service, acc.identity(acc.access(person, app_plan)), acc.access(person, app_plan))
    assert response.status_code == 403


# ------------------------------------------------------------------ billing and receipts

def test_plan_billing_has_no_dollar_reservation(connected, monkeypatch):
    service, _ = connected
    seen = {}
    original = receipts.RouteReceipt

    class Spy(original):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen["receipt"] = self

    monkeypatch.setattr(receipts, "RouteReceipt", Spy)
    ident, access = granted()
    assert call(service, ident, access).status_code == 200
    receipt = seen["receipt"]
    assert receipt.cost["usd"] is None and receipt.cost["mode"] == "plan" and receipt.cost["provider"] == "chatgpt"
    assert receipt.usage == {"input_tokens": 3, "output_tokens": 2}  # the fake reports no cached or reasoning count


# ------------------------------------------------------------------ passthrough, translation, errors

def test_responses_passthrough_sends_store_false_and_the_real_slug(connected):
    service, upstream = connected
    ident, access = granted()
    response = call(service, ident, access)
    sent = body_of(next(c for c in upstream.calls if c.url.path == RESPONSES_PATH))
    assert sent["model"] == "gpt-test" and sent["store"] is False and sent["stream"] is True
    assert response.headers["x-azizam-billing"] == "plan"
    assert json.loads(response.body)["id"] == "resp_1"


def test_the_upstream_receives_the_connection_token_not_the_callers(connected):
    service, upstream = connected
    ident, access = granted()
    call(service, ident, access)
    sent = next(c for c in upstream.calls if c.url.path == RESPONSES_PATH)
    assert sent.headers["authorization"] == "Bearer access-1"


def test_chat_is_translated_to_responses_and_back(connected):
    service, upstream = connected
    ident, access = granted()
    response = call(service, ident, access, surface="chat",
                    body={"model": MODEL, "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]})
    out = json.loads(response.body)
    assert out["object"] == "chat.completion" and out["choices"][0]["message"]["content"] == "hi"
    assert out["usage"]["prompt_tokens"] == 3 and out["model"] == MODEL
    sent = body_of(next(c for c in upstream.calls if c.url.path == RESPONSES_PATH))
    assert "messages" not in sent and sent["input"]


def test_unsupported_field_is_a_named_400_before_any_upstream_call(connected):
    service, upstream = connected
    ident, access = granted()
    response = call(service, ident, access, body={"model": MODEL, "input": [{"role": "user", "content": "x"}], "temperature": 1})
    assert response.status_code == 400 and json.loads(response.body)["error"]["code"] == "plan_unsupported_parameter"
    assert upstream.count(RESPONSES_PATH) == 0


@pytest.mark.parametrize("status,code,expected", [
    (429, "subscription_sharing_usage_limit_exceeded", "plan_usage_limit_exceeded"),
    (403, "subscription_sharing_user_not_eligible", "plan_not_eligible"),
    (503, "subscription_sharing_usage_unavailable", "plan_usage_unavailable"),
    (500, None, "plan_upstream_error"),
])
def test_upstream_errors_become_named_refusals(connected, status, code, expected):
    service, upstream = connected
    upstream.routes[("POST", RESPONSES_PATH)] = json_route({"error": {"code": code}} if code else {}, status)
    ident, access = granted()
    response = call(service, ident, access)
    assert json.loads(response.body)["error"]["code"] == expected


def test_error_code_in_a_detail_body_is_mapped_too(connected):
    service, upstream = connected
    upstream.routes[("POST", RESPONSES_PATH)] = json_route({"detail": {"code": "subscription_sharing_usage_limit_exceeded"}}, 429)
    ident, access = granted()
    assert json.loads(call(service, ident, access).body)["error"]["code"] == "plan_usage_limit_exceeded"


def test_streamed_chat_ends_with_done_and_usage(connected):
    service, _ = connected
    ident, access = granted()
    response = call(service, ident, access, surface="chat",
                    body={"model": MODEL, "stream": True, "stream_options": {"include_usage": True},
                          "messages": [{"role": "user", "content": "hi"}]})

    async def read():
        return b"".join([chunk async for chunk in response.body_iterator])

    text = run(read()).decode()
    assert text.rstrip().endswith("data: [DONE]") and '"usage"' in text


# ------------------------------------------------------------------ owner endpoints

def _client(tmp_path, roles):
    upstream = Upstream()
    service, _, _ = make_service(tmp_path, upstream)

    def auth(request):
        if request.headers.get("x-test-who") is None:
            raise AdminAuthError("no", status_code=401, code="unauthorized")
        return AdminContext(subject="p1", actor_id="p1", roles=frozenset(roles), auth_method="key")

    app = FastAPI()
    app.include_router(subscription_api.create_subscription_router(service=service, auth=auth))
    return TestClient(app), service


def test_connect_endpoints_are_owner_only(tmp_path):
    client, _ = _client(tmp_path, {"admin"})
    for method, url in [("get", "/admin/api/subscriptions"), ("post", "/admin/api/subscriptions/chatgpt/connect"),
                        ("delete", "/admin/api/subscriptions/chatgpt")]:
        response = getattr(client, method)(url, headers={"x-test-who": "1"})
        assert response.status_code == 403 and response.json()["error"]["code"] == "owner_required"


def test_owner_connects_and_the_response_has_no_token(tmp_path):
    client, service = _client(tmp_path, {"admin", "owner"})
    started = client.post("/admin/api/subscriptions/chatgpt/connect", headers={"x-test-who": "1"}, json={})
    assert started.status_code == 200
    subscription = started.json()["subscription"]
    assert subscription["authorize_url"].startswith("https://") and subscription["listening"] is False
    assert client.get("/admin/api/subscriptions", headers={"x-test-who": "1"}).json()["subscriptions"][0]["status"] == "not_connected"
    bad = client.post("/admin/api/subscriptions/chatgpt/finish", headers={"x-test-who": "1"}, json={"attempt_id": "nope", "callback": "?code=1&state=2"})
    assert bad.status_code == 404 and bad.json()["error"]["code"] == "attempt_not_found"


def test_import_endpoint_reads_the_named_file_and_can_remove_it(tmp_path):
    from tests.test_subscription_connection import fake_record
    client, service = _client(tmp_path, {"admin", "owner"})
    record = tmp_path / "record.json"
    record.write_text(json.dumps(fake_record()))
    response = client.post("/admin/api/subscriptions/chatgpt/import", headers={"x-test-who": "1"},
                           json={"path": str(record), "remove_source": True})
    assert response.status_code == 200
    assert response.json()["subscription"]["status"] == "connected" and response.json()["subscription"]["source_removed"] is True
    assert "imp-access" not in response.text and not record.exists()


def test_import_endpoint_is_owner_only_and_names_a_bad_file(tmp_path):
    admin, _ = _client(tmp_path, {"admin"})
    assert admin.post("/admin/api/subscriptions/chatgpt/import", headers={"x-test-who": "1"},
                      json={"path": "x"}).status_code == 403
    owner, _ = _client(tmp_path / "o", {"owner"}) if (tmp_path / "o").mkdir() is None else (None, None)
    response = owner.post("/admin/api/subscriptions/chatgpt/import", headers={"x-test-who": "1"},
                          json={"path": str(tmp_path / "missing.json")})
    assert response.status_code == 400 and response.json()["error"]["code"] == "import_unreadable"


def test_editor_model_grant_allows_bare_and_pinned_plan_route_only(connected):
    from gateway import registry_snapshot
    from gateway.access_plans import plan_from_document
    service, upstream = connected
    registry = {"services": {KIND.model_prefix: {"config": {
        "credential_source": {"kind": "connection", "connection_id": "chatgpt-owner"},
        "request_protocol": KIND.protocol, "models": [],
    }}}}
    snapshot = registry_snapshot.build(registry, registry_snapshot.BundledRelease({}), generation=1,
        connection_models={"chatgpt-owner": {"models": {"gpt-test": [], "other-test": []}}})
    contract = admin_catalog.access_plan_editor_contract(registry, snapshot=snapshot)
    item = next(item for cap in contract["capabilities"] if cap["id"] == "chat"
                for item in cap["items"] if item["id"] == "gpt-test")
    plan = plan_from_document({**contract["new_plan"], "name": "Selected model",
        "can_use": {"chat": {"items": [{"model": item["id"]}]}},
        "connections": item["connection_ids"]}, plan_id="selected-model")
    for requested in (item["id"], MODEL):
        response = call(service, acc.identity(plan), acc.access(plan), surface="chat",
            body={"model": requested, "messages": [{"role": "user", "content": "hi"}]})
        assert response.status_code == 200, response.body
    for requested in ("other-test", f"{KIND.model_prefix}/other-test"):
        response = run(execute(service, KIND, "other-test", surface="chat",
            body={"model": requested, "messages": [{"role": "user", "content": "hi"}]},
            identity=acc.identity(plan), access=acc.access(plan)))
        assert response.status_code == 403
        assert json.loads(response.body)["error"]["code"] == "not_in_access_plan"
    assert upstream.count(RESPONSES_PATH) == 2
