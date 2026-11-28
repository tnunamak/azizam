"""Connecting the gateway to a subscription plan: tokens at rest, pasted callback, serialized refresh, disconnect."""

from __future__ import annotations

import asyncio
import sqlite3

import httpx
import pytest

from gateway.subscription_oauth import SubscriptionError, parse_callback
from tests.support.subscription_fakes import (
    KIND, Upstream, callback_for, connect, json_route, make_service, token_body, urlparts)

TOKEN_PATH = urlparts(KIND.oauth["token_url"])


def run(coro):
    return asyncio.run(coro)


def test_pasted_callback_connects_the_gateway_and_records_the_owner(tmp_path):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    view = run(connect(service, jwks, upstream, owner="owner_1"))
    assert view["status"] == "connected" and view["connected_by"] == "owner_1"
    assert view["models"] == ["gpt-test"]
    assert store.connection("chatgpt")["connected_by"] == "owner_1"
    assert "access-1" not in repr(view) and "refresh-1" not in repr(view)


def test_tokens_are_encrypted_at_rest(tmp_path):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    run(connect(service, jwks, upstream))
    raw = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert b"access-1" not in raw and b"refresh-1" not in raw
    assert store.load_tokens("chatgpt", origin=KIND.upstream_origin)["refresh_token"] == "refresh-1"
    with pytest.raises(Exception):
        store.load_tokens("chatgpt", origin="https://elsewhere.test")


def test_state_mismatch_is_refused_and_spends_the_attempt(tmp_path):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    upstream.routes[("POST", TOKEN_PATH)] = json_route(token_body())
    started = run(service.start("owner_1", "chatgpt", session=None))
    with pytest.raises(SubscriptionError) as error:
        run(service.finish("owner_1", started["attempt_id"], "http://127.0.0.1:1455/auth/callback?code=c&state=wrong", session=None))
    assert error.value.code == "state_mismatch"
    assert upstream.count(TOKEN_PATH) == 0
    with pytest.raises(SubscriptionError) as again:
        run(service.finish("owner_1", started["attempt_id"], callback_for(started), session=None))
    assert again.value.code == "attempt_not_found"


def test_another_owner_cannot_finish_the_attempt(tmp_path):
    upstream = Upstream()
    service, _, _ = make_service(tmp_path, upstream)
    started = run(service.start("owner_1", "chatgpt", session=None))
    with pytest.raises(SubscriptionError) as error:
        run(service.finish("someone_else", started["attempt_id"], callback_for(started), session=None))
    assert error.value.code == "attempt_not_found"


def test_callback_without_a_result_is_refused():
    with pytest.raises(SubscriptionError):
        parse_callback("http://127.0.0.1:1455/auth/callback")


def test_a_second_connect_reuses_the_issued_client_id_and_drops_the_name_hint(tmp_path):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    first = run(service.start("owner_1", "chatgpt", session=None))
    assert httpx.URL(first["authorize_url"]).params["client_id"] == KIND.oauth["dynamic_client_id"]
    assert "agent_name_hint" in httpx.URL(first["authorize_url"]).params
    run(connect(service, jwks, upstream))
    second = run(service.start("owner_1", "chatgpt", session=None))
    params = httpx.URL(second["authorize_url"]).params
    assert params["client_id"] == "dynamic-issued" and "agent_name_hint" not in params


def test_a_different_account_is_refused_until_disconnect(tmp_path):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    run(connect(service, jwks, upstream))
    jwks.claims["sub"] = "acct-2"
    with pytest.raises(SubscriptionError) as error:
        run(connect(service, jwks, upstream))
    assert error.value.code == "account_mismatch"


def test_concurrent_callers_cause_one_refresh_and_the_rotated_token_is_saved(tmp_path):
    upstream = Upstream()
    now = [1_000_000.0]
    service, store, jwks = make_service(tmp_path, upstream, now=lambda: now[0])
    run(connect(service, jwks, upstream))
    now[0] += 3600  # the access token is past its life

    def refresh(request):
        assert b"refresh-1" in request.content  # the saved token, not a stale copy
        return httpx.Response(200, json=token_body("access-2", "refresh-2"))

    upstream.routes[("POST", TOKEN_PATH)] = refresh
    before = upstream.count(TOKEN_PATH)

    async def many():
        return await asyncio.gather(*(service.acquire("chatgpt") for _ in range(8)))

    assert {credential.value for credential in run(many())} == {"access-2"}
    assert upstream.count(TOKEN_PATH) - before == 1
    assert store.load_tokens("chatgpt", origin=KIND.upstream_origin)["refresh_token"] == "refresh-2"


def test_a_terminal_refresh_error_asks_the_owner_to_connect_again(tmp_path):
    upstream = Upstream()
    now = [1_000_000.0]
    service, store, jwks = make_service(tmp_path, upstream, now=lambda: now[0])
    run(connect(service, jwks, upstream))
    now[0] += 3600
    upstream.routes[("POST", TOKEN_PATH)] = json_route({"error": "refresh_token_reused"}, 400)
    with pytest.raises(SubscriptionError) as error:
        run(service.acquire("chatgpt"))
    assert error.value.code == "plan_connection_invalid"
    assert store.connection("chatgpt")["status"] == "needs_reconnect"
    assert store.load_tokens("chatgpt", origin=KIND.upstream_origin) is None


def test_disconnect_revokes_and_deletes_tokens(tmp_path):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    run(connect(service, jwks, upstream))
    view = run(service.disconnect("chatgpt", session=None))
    assert view["status"] == "disconnected" and view["revoked_upstream"] is True
    assert upstream.count("/revoke") == 1
    assert store.load_tokens("chatgpt", origin=KIND.upstream_origin) is None
    with pytest.raises(SubscriptionError) as error:
        run(service.acquire("chatgpt"))
    assert error.value.code == "plan_not_connected"


def test_one_connection_per_kind_for_the_whole_instance(tmp_path):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    run(connect(service, jwks, upstream, owner="owner_1"))
    with sqlite3.connect(tmp_path / "subscriptions.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM subscription_connections").fetchone()[0] == 1
    assert service.store.connections()[0]["kind"] == "chatgpt"
    assert service.provider_choices()[0]["id"] == "chatgpt-owner"


# ------------------------------------------------------------------ import of an earlier registration (fake record only)

def fake_record(**overrides):
    record = {"client_id": "dynamic-issued", "ext_agent_host_id": "urn:uuid:11111111-1111-1111-1111-111111111111",
              "access_token": "imp-access", "refresh_token": "imp-refresh", "expires_in": 3600, "saved_at": 1_000_000.0,
              "scopes": ["chatgpt.tokens.use.direct", "openid"], "email": "owner@example.test", "subject": "acct-1",
              "issuer": KIND.oauth["issuer"], "token_type": "Bearer"}
    return {**record, **overrides}


def test_import_makes_the_record_the_instance_connection(tmp_path):
    upstream = Upstream()
    service, store, _ = make_service(tmp_path, upstream)
    view = run(service.import_record("owner_1", "chatgpt", fake_record(), session=None))
    assert view["status"] == "connected" and view["connected_by"] == "owner_1"
    assert store.host_id("chatgpt", "urn:uuid") == "urn:uuid:11111111-1111-1111-1111-111111111111"
    assert store.connection("chatgpt")["client_id"] == "dynamic-issued"
    assert store.load_tokens("chatgpt", origin=KIND.upstream_origin)["refresh_token"] == "imp-refresh"
    raw = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert b"imp-access" not in raw and b"imp-refresh" not in raw
    assert "imp-access" not in repr(view)
    assert view["models"] == ["gpt-test"]  # the access token is still fresh: no refresh was needed


def test_an_imported_set_is_refreshed_by_the_gateway_with_the_imported_client_and_token(tmp_path):
    upstream = Upstream()
    now = [1_000_000.0 + 7200]  # the record's access token ran out
    service, store, _ = make_service(tmp_path, upstream, now=lambda: now[0])
    seen = {}

    def refresh(request):
        seen["body"] = request.content
        return httpx.Response(200, json=token_body("access-new", "refresh-new"))

    upstream.routes[("POST", TOKEN_PATH)] = refresh
    run(service.import_record("owner_1", "chatgpt", fake_record(), session=None))
    assert b"imp-refresh" in seen["body"] and b"client_id=dynamic-issued" in seen["body"]
    assert store.load_tokens("chatgpt", origin=KIND.upstream_origin)["refresh_token"] == "refresh-new"


@pytest.mark.parametrize("change", [{"issuer": "https://other.test"}, {"scopes": ["openid"]}, {"refresh_token": ""},
                                    {"saved_at": "yesterday"}])
def test_a_bad_record_is_refused_and_stores_nothing(tmp_path, change):
    upstream = Upstream()
    service, store, _ = make_service(tmp_path, upstream)
    with pytest.raises(SubscriptionError):
        run(service.import_record("owner_1", "chatgpt", fake_record(**change), session=None))
    assert store.connection("chatgpt") is None


def test_import_does_not_replace_a_live_connection(tmp_path):
    upstream = Upstream()
    service, _, jwks = make_service(tmp_path, upstream)
    run(connect(service, jwks, upstream))
    with pytest.raises(SubscriptionError) as error:
        run(service.import_record("owner_1", "chatgpt", fake_record(), session=None))
    assert error.value.code == "already_connected"
