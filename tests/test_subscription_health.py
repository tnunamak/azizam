"""Owner-facing health of a subscription plan: when it last worked, why it stopped, a manual model check."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import subscription_api
from gateway.admin_api import AdminAuthError, AdminContext
from gateway.subscription_oauth import SubscriptionError
from tests.support import access as acc
from tests.support.subscription_fakes import KIND, Upstream, connect, json_route, make_service, token_body, urlparts
from tests.test_subscription_connection import fake_record
from tests.test_subscription_plan_access import COMPLETED, MODEL, RESPONSES_PATH, _receipts_table, call, granted, sse  # noqa: F401

TOKEN_PATH = urlparts(KIND.oauth["token_url"])
START = 1_000_000.0


def run(coro):
    return asyncio.run(coro)


def clocked(tmp_path):
    now = [START]
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream, now=lambda: now[0])
    run(connect(service, jwks, upstream))
    upstream.routes[("POST", RESPONSES_PATH)] = lambda r: httpx.Response(
        200, content=sse({"type": "response.created", "response": {}}, COMPLETED), headers={"content-type": "text/event-stream"})
    return service, store, upstream, now


def view(service):
    return run(service.status_one("chatgpt"))


# ------------------------------------------------------------------ 1. the status view

def test_status_view_carries_check_times_and_model_efforts(tmp_path):
    service, store, upstream, now = clocked(tmp_path)
    now[0] += 120
    run(service.refresh_models("chatgpt"))
    shown = view(service)
    row = store.connection("chatgpt")
    assert shown["models_checked_at"] == row["models_at"] == int(START) + 120
    assert shown["status_changed_at"] == row["updated_at"]
    assert shown["models"] == ["gpt-test"]
    assert shown["model_details"] == [{"slug": "gpt-test", "efforts": ["low", "medium", "high"]}]
    assert shown["last_ok_at"] == int(START) + 120
    assert "last_error" not in shown and "reconnect_reason" not in shown


def test_status_view_has_no_secret_value(tmp_path):
    service, store, upstream, now = clocked(tmp_path)
    tokens = store.load_tokens("chatgpt", origin=KIND.upstream_origin)
    secrets = {tokens["access_token"], tokens["refresh_token"], "dynamic-issued"}

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from walk(item)
        elif isinstance(value, list):
            for item in value:
                yield from walk(item)
        else:
            yield value

    shown = view(service)
    assert shown["model_details"] and not (set(walk(shown)) & secrets)
    assert not any(secret in json.dumps(shown) for secret in secrets if secret != "dynamic-issued")


# ------------------------------------------------------------------ 2. what is stored

def test_a_failed_model_refresh_is_recorded_with_its_code_and_time(tmp_path):
    service, store, upstream, now = clocked(tmp_path)
    now[0] += 30
    upstream.routes[("GET", "/v1/models")] = json_route({}, 500)
    with pytest.raises(SubscriptionError) as error:
        run(service.refresh_models("chatgpt"))
    assert view(service)["last_error"] == {"code": error.value.code, "at": int(START) + 30}


def test_an_upstream_refusal_of_a_plan_request_is_recorded(tmp_path):
    service, store, upstream, now = clocked(tmp_path)
    upstream.routes[("POST", RESPONSES_PATH)] = json_route({"error": {"message": "x"}}, 429)
    now[0] += 5
    ident, access = granted()
    assert call(service, ident, access).status_code == 429
    assert view(service)["last_error"] == {"code": "plan_usage_limit_exceeded", "at": int(START) + 5}


def test_a_plan_request_that_fails_before_sending_is_recorded(tmp_path):
    service, store, upstream, now = clocked(tmp_path)
    now[0] += 3600  # the access token ran out and the refresh is refused: the request fails inside the credential step
    upstream.routes[("POST", TOKEN_PATH)] = json_route({"error": "invalid_grant"}, 400)
    ident, access = granted()
    assert call(service, ident, access).status_code == 503
    shown = view(service)
    assert shown["last_error"]["code"] == "plan_connection_invalid" and shown["reconnect_reason"] == "refresh_refused"


def test_a_good_plan_request_stamps_last_ok_at_most_once_a_minute(tmp_path):
    service, store, upstream, now = clocked(tmp_path)
    ident, access = granted()
    now[0] += 100  # connecting stamped last_ok_at at START
    assert call(service, ident, access).status_code == 200
    assert view(service)["last_ok_at"] == int(START) + 100
    now[0] += 20
    assert call(service, ident, access).status_code == 200
    assert view(service)["last_ok_at"] == int(START) + 100  # inside the minute: no second write
    now[0] += 60
    assert call(service, ident, access).status_code == 200
    assert view(service)["last_ok_at"] == int(START) + 180


def test_a_store_that_cannot_record_never_fails_the_request(tmp_path, monkeypatch):
    service, store, upstream, now = clocked(tmp_path)

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    tried = []

    def broken(*args, **kwargs):
        tried.append(args[0])
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "record_error", broken, raising=False)
    monkeypatch.setattr(store, "record_ok", broken, raising=False)
    ident, access = granted()
    assert call(service, ident, access).status_code == 200
    upstream.routes[("POST", RESPONSES_PATH)] = json_route({"error": {}}, 429)
    assert call(service, ident, access).status_code == 429
    assert len(tried) == 2  # both notes were attempted; their failure reached neither answer


def test_a_slow_health_write_does_not_hold_the_request(tmp_path, monkeypatch):
    from gateway import subscription_oauth
    service, store, upstream, now = clocked(tmp_path)
    monkeypatch.setattr(subscription_oauth, "HEALTH_WRITE_WAIT_SECONDS", 0.05)
    real = store.record_ok

    def slow(*args, **kwargs):
        import time as _time
        _time.sleep(0.5)
        return real(*args, **kwargs)

    monkeypatch.setattr(store, "record_ok", slow, raising=False)
    started = asyncio.run(_timed(service.note_ok("chatgpt")))
    assert started < 0.3


async def _timed(coro):
    loop = asyncio.get_running_loop()
    begin = loop.time()
    await coro
    took = loop.time() - begin
    await asyncio.sleep(0.6)  # let the write finish before the loop closes
    return took


def test_a_later_success_clears_the_last_error(tmp_path):
    service, store, upstream, now = clocked(tmp_path)
    ident, access = granted()
    upstream.routes[("POST", RESPONSES_PATH)] = json_route({"error": {"message": "x"}}, 429)
    now[0] += 5
    assert call(service, ident, access).status_code == 429
    assert view(service)["last_error"]["code"] == "plan_usage_limit_exceeded"
    upstream.routes[("POST", RESPONSES_PATH)] = lambda r: httpx.Response(
        200, content=sse({"type": "response.created", "response": {}}, COMPLETED), headers={"content-type": "text/event-stream"})
    now[0] += 10  # inside the once-a-minute stamp window: the error still clears
    assert call(service, ident, access).status_code == 200
    assert "last_error" not in view(service)


def test_a_request_the_client_got_wrong_is_not_the_subscriptions_error(tmp_path):
    service, store, upstream, now = clocked(tmp_path)
    ident, access = granted()
    upstream.routes[("POST", RESPONSES_PATH)] = json_route({"error": {"message": "bad"}}, 400)
    assert call(service, ident, access).status_code == 400
    assert "last_error" not in view(service)


def test_a_refused_refresh_says_why_the_plan_needs_the_owner(tmp_path):
    service, store, upstream, now = clocked(tmp_path)
    now[0] += 3600
    upstream.routes[("POST", TOKEN_PATH)] = json_route({"error": "invalid_grant"}, 400)
    with pytest.raises(SubscriptionError):
        run(service.acquire("chatgpt"))
    shown = view(service)
    assert shown["status"] == "needs_reconnect" and shown["reconnect_reason"] == "refresh_refused"
    assert shown["status_changed_at"] == int(START) + 3600
    # connecting again clears the reason and the old error
    upstream.routes[("POST", TOKEN_PATH)] = json_route(token_body("access-9", "refresh-9"))
    run(service.import_record("owner_1", "chatgpt", fake_record(saved_at=float(START) + 3600), session=None))
    again = view(service)
    assert again["status"] == "connected" and "reconnect_reason" not in again


def test_a_store_made_before_these_columns_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old" / "subscriptions.db"
    path.parent.mkdir()
    conn = sqlite3.connect(path)
    conn.executescript("""CREATE TABLE subscription_connections (kind TEXT PRIMARY KEY, connected_by TEXT NOT NULL,
      client_id TEXT NOT NULL, host_id TEXT NOT NULL, status TEXT NOT NULL, scopes TEXT NOT NULL DEFAULT '',
      account_email TEXT, account_subject TEXT, access_expires_at INTEGER, earliest_refresh_at INTEGER,
      models_json TEXT NOT NULL DEFAULT '{}', models_at INTEGER, connected_at INTEGER, updated_at INTEGER NOT NULL);
      INSERT INTO subscription_connections(kind, connected_by, client_id, host_id, status, models_json, updated_at)
      VALUES ('chatgpt', 'o', 'c', 'h', 'needs_reconnect', '{"m": ["low"]}', 5);""")
    conn.commit()
    conn.close()
    from gateway import subscription_store
    store = subscription_store.SubscriptionStore(lambda: path)
    row = store.connection("chatgpt")
    assert row["last_ok_at"] is None and row["last_error_code"] is None and row["reconnect_reason"] is None
    store.record_error("chatgpt", "plan_usage_limit_exceeded")
    assert store.connection("chatgpt")["last_error_code"] == "plan_usage_limit_exceeded"


# ------------------------------------------------------------------ 3. the refresh route

def _client(tmp_path, roles=frozenset({"owner"})):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)

    def auth(request):
        if request.headers.get("x-test-who") is None:
            raise AdminAuthError("no", status_code=401, code="unauthorized")
        return AdminContext(subject="p1", actor_id="p1", roles=frozenset(roles), auth_method="key")

    app = FastAPI()
    app.include_router(subscription_api.create_subscription_router(service=service, auth=auth))
    return TestClient(app), service, upstream, jwks


URL = "/admin/api/subscriptions/chatgpt/refresh-models"
H = {"x-test-who": "1"}


def test_refresh_route_is_owner_only(tmp_path):
    client, *_ = _client(tmp_path, {"admin"})
    response = client.post(URL, headers=H)
    assert response.status_code == 403 and response.json()["error"]["code"] == "owner_required"


def test_refresh_route_returns_the_fresh_view(tmp_path):
    client, service, upstream, jwks = _client(tmp_path)
    run(connect(service, jwks, upstream))
    upstream.routes[("GET", "/v1/models")] = json_route({"models": [{"slug": "gpt-new", "visibility": "list"}]})
    response = client.post(URL, headers=H)
    assert response.status_code == 200, response.text
    shown = response.json()["subscription"]
    assert shown["models"] == ["gpt-new"] and shown["model_details"] == [{"slug": "gpt-new", "efforts": []}]
    assert isinstance(shown["models_checked_at"], int)


def test_refresh_route_names_its_error(tmp_path):
    client, *_ = _client(tmp_path)
    response = client.post(URL, headers=H)  # nothing connected
    assert response.status_code == 503 and response.json()["error"]["code"] == "plan_not_connected"


# ------------------------------------------------------------------ 4. a list read makes no network call

def test_the_list_read_does_not_wait_for_the_network_and_refreshes_in_the_background(tmp_path):
    service, store, upstream, now = clocked(tmp_path)
    now[0] += 7200  # the model list is stale
    upstream.routes[("GET", "/v1/models")] = json_route({"models": [{"slug": "gpt-later", "visibility": "list"}]})

    async def scenario():
        before = upstream.count("/v1/models")
        first = await service.status()
        assert upstream.count("/v1/models") == before  # no call inside the read
        assert first[0]["models"] == ["gpt-test"]  # the stored list answers at once
        await service.status()  # a second read while one is in flight starts no second refresh
        await asyncio.gather(*service._refreshing.values())
        assert upstream.count("/v1/models") == before + 1
        return (await service.status())[0]

    assert run(scenario())["models"] == ["gpt-later"]


def test_a_failing_background_refresh_is_swallowed(tmp_path):
    service, store, upstream, now = clocked(tmp_path)
    now[0] += 7200
    upstream.routes[("GET", "/v1/models")] = json_route({}, 500)

    async def scenario():
        await service.status()
        await asyncio.gather(*service._refreshing.values())
        return (await service.status())[0]

    shown = run(scenario())
    assert shown["models"] == ["gpt-test"] and shown["last_error"]["at"] == int(START) + 7200


def test_a_failed_background_refresh_is_not_retried_by_every_read(tmp_path):
    service, store, upstream, now = clocked(tmp_path)
    now[0] += 7200
    upstream.routes[("GET", "/v1/models")] = json_route({}, 500)

    async def reads(count):
        for _ in range(count):
            await service.status()
            await asyncio.gather(*service._refreshing.values())

    before = upstream.count("/v1/models")
    run(reads(3))
    assert upstream.count("/v1/models") == before + 1
    now[0] += 300  # the back-off is over: the next read tries again
    run(reads(1))
    assert upstream.count("/v1/models") == before + 2


# ------------------------------------------------------------------ 5. import after the connection ended

def test_import_is_allowed_when_the_connection_needs_reconnect_but_not_when_connected(tmp_path):
    service, store, upstream, now = clocked(tmp_path)
    with pytest.raises(SubscriptionError) as refused:
        run(service.import_record("owner_1", "chatgpt", fake_record(), session=None))
    assert refused.value.code == "already_connected"
    store.invalidate("chatgpt", status="needs_reconnect")
    shown = run(service.import_record("owner_1", "chatgpt", fake_record(), session=None))
    assert shown["status"] == "connected"
    assert store.load_tokens("chatgpt", origin=KIND.upstream_origin)["refresh_token"] == "imp-refresh"
