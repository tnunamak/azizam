from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

import proxy
from gateway import admin_store
from gateway.admin_oidc import CookieConfig, OIDCConfig
from tests.support.signin import seed_rules


SIGNING_KEY = b"exposure-oidc-test-key-32-bytes!!"
BASE_URL = "https://gateway.example"


def _oidc_config() -> OIDCConfig:
    return OIDCConfig(
        enabled=True,
        issuer="https://idp.example",
        client_id="gateway",
        client_secret="test-secret",
        redirect_uri=f"{BASE_URL}/admin/oidc/callback",
        scopes=("openid", "email"),
        admin_emails=frozenset({"owner@example.test"}),
        signing_key=SIGNING_KEY,
        cookie=CookieConfig(secure=True),
        authorization_endpoint="https://idp.example/authorize",
        token_endpoint="https://idp.example/token",
        jwks_uri="https://idp.example/jwks",
    )


@pytest.fixture
def oidc_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    cfg = _oidc_config()
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", path)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", path)
    monkeypatch.setattr(proxy, "ADMIN_OIDC_CONFIG", cfg)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "store", proxy.ADMIN_STORE)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", cfg.signing_key)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "cookie", cfg.cookie)
    admin_store.put_operator_setting(
        "instance_presentation",
        {"base_url": BASE_URL},
        path=path,
    )
    monkeypatch.setattr(proxy, "ADMIN_OIDC_STATES", {})
    seed_rules(path, domains=["example.test"])
    yield path


def _client() -> TestClient:
    return TestClient(
        proxy.app,
        base_url=BASE_URL,
        client=("127.0.0.1", 50000),
    )


def _start_login(client: TestClient) -> tuple[str, object]:
    response = client.get("/login", follow_redirects=False)
    assert response.status_code == 302, response.text
    query = parse_qs(urlsplit(response.headers["location"]).query)
    state = query["state"][0]
    record = proxy.ADMIN_OIDC_STATES[state]
    return state, record


def _set_cookie_headers(response) -> list[str]:
    return response.headers.get_list("set-cookie")


def _state_cookie_value(response) -> str:
    for header in _set_cookie_headers(response):
        if header.startswith("__Host-gateway-oidc-"):
            return header.split(";", 1)[0].split("=", 1)[1]
    raise AssertionError(f"OIDC flow cookie missing: {_set_cookie_headers(response)}")


def _flow_cookie_header(response, name: str) -> str:
    for header in _set_cookie_headers(response):
        if header.startswith(f"{name}="):
            return header
    raise AssertionError(f"{name} cookie missing: {_set_cookie_headers(response)}")


def _assert_flow_cookie_deleted(response, name: str) -> None:
    header = _flow_cookie_header(response, name)
    lowered = header.lower()
    assert f"{name}=" in header
    assert "max-age=0" in lowered


def _patch_oidc(monkeypatch: pytest.MonkeyPatch, record, calls: list[str]):
    async def exchange(code, *, config, pkce):
        calls.append(code)
        assert config.redirect_uri == f"{BASE_URL}/admin/oidc/callback"
        assert pkce.verifier == record.pkce_verifier
        return {"id_token": "test.token"}

    async def verify(token, *, config):
        return {
            "iss": config.issuer,
            "aud": config.client_id,
            "sub": "member-subject",
            "email": "member@example.test",
            "email_verified": True,
            "nonce": record.nonce,
            "exp": 4_000_000_000,
            "iat": 1_700_000_000,
        }

    monkeypatch.setattr(proxy, "_oidc_exchange_code", exchange)
    monkeypatch.setattr(proxy, "_oidc_verify_id_token", verify)


def _patch_oidc_by_code(monkeypatch: pytest.MonkeyPatch, records_by_code, calls: list[str]):
    current_record = {"value": None}

    async def exchange(code, *, config, pkce):
        calls.append(code)
        record = records_by_code[code]
        current_record["value"] = record
        assert config.redirect_uri == f"{BASE_URL}/admin/oidc/callback"
        assert pkce.verifier == record.pkce_verifier
        return {"id_token": f"{code}.token"}

    async def verify(token, *, config):
        record = current_record["value"]
        assert record is not None
        subject = str(token).split(".", 1)[0]
        return {
            "iss": config.issuer,
            "aud": config.client_id,
            "sub": f"{subject}-subject",
            "email": f"{subject}@example.test",
            "email_verified": True,
            "nonce": record.nonce,
            "exp": 4_000_000_000,
            "iat": 1_700_000_000,
        }

    monkeypatch.setattr(proxy, "_oidc_exchange_code", exchange)
    monkeypatch.setattr(proxy, "_oidc_verify_id_token", verify)


def test_oidc_login_redirect_sets_browser_bound_state_cookie(
    oidc_gateway,
) -> None:
    browser = _client()

    response = browser.get("/login", follow_redirects=False)

    assert response.status_code == 302, response.text
    state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
    record = proxy.ADMIN_OIDC_STATES[state]
    cookie = _flow_cookie_header(response, record.browser_cookie_name)
    assert record.browser_cookie_name.startswith("__Host-gateway-oidc-")
    assert "domain=" not in cookie.lower()
    assert "Secure" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Max-Age=600" in cookie
    assert _state_cookie_value(response)
    assert state in proxy.ADMIN_OIDC_STATES


def test_oidc_callback_rejects_state_started_by_another_browser_before_exchange(
    oidc_gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_a = _client()
    browser_b = _client()
    state, record = _start_login(browser_a)
    exchange_calls: list[str] = []
    _patch_oidc(monkeypatch, record, exchange_calls)

    stolen = browser_b.get(
        "/admin/oidc/callback",
        params={"state": state, "code": "stolen-code"},
        follow_redirects=False,
    )

    assert stolen.status_code == 400, stolen.text
    assert exchange_calls == []
    assert "set-cookie" not in stolen.headers
    assert admin_store.list_users(path=oidc_gateway) == []


def test_oidc_callback_rejects_wrong_state_cookie_without_consuming_state(
    oidc_gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_a = _client()
    login = browser_a.get("/login", follow_redirects=False)
    state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
    record = proxy.ADMIN_OIDC_STATES[state]
    browser_b = _client()
    browser_b.cookies.set(record.browser_cookie_name, "wrong-state-cookie", path="/")
    exchange_calls: list[str] = []
    _patch_oidc(monkeypatch, record, exchange_calls)

    stolen = browser_b.get(
        "/admin/oidc/callback",
        params={"state": state, "code": "stolen-code"},
        follow_redirects=False,
    )

    assert stolen.status_code == 400, stolen.text
    assert exchange_calls == []
    assert "set-cookie" not in stolen.headers
    assert state in proxy.ADMIN_OIDC_STATES
    assert admin_store.list_users(path=oidc_gateway) == []


def test_oidc_callback_accepts_state_in_initiating_browser(
    oidc_gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_a = _client()
    state, record = _start_login(browser_a)
    exchange_calls: list[str] = []
    _patch_oidc(monkeypatch, record, exchange_calls)

    callback = browser_a.get(
        "/admin/oidc/callback",
        params={"state": state, "code": "valid-code"},
        follow_redirects=False,
    )

    assert callback.status_code == 302, callback.text
    assert exchange_calls == ["valid-code"]
    assert proxy.ADMIN_AUTH.cookie.name in callback.headers.get("set-cookie", "")
    _assert_flow_cookie_deleted(callback, record.browser_cookie_name)
    users = admin_store.list_users(path=oidc_gateway)
    assert [user["email"] for user in users] == ["member@example.test"]


def test_oidc_callback_rejects_replay_and_expired_state(
    oidc_gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = _client()
    state, record = _start_login(browser)
    exchange_calls: list[str] = []
    _patch_oidc(monkeypatch, record, exchange_calls)

    accepted = browser.get(
        "/admin/oidc/callback",
        params={"state": state, "code": "valid-code"},
        follow_redirects=False,
    )
    replay = browser.get(
        "/admin/oidc/callback",
        params={"state": state, "code": "replay-code"},
        follow_redirects=False,
    )

    assert accepted.status_code == 302, accepted.text
    assert replay.status_code == 400, replay.text
    assert exchange_calls == ["valid-code"]

    expired_state, expired_record = _start_login(browser)
    proxy.ADMIN_OIDC_STATES[expired_state] = replace(
        expired_record,
        created_at=expired_record.created_at - 601,
    )
    expired_calls: list[str] = []
    _patch_oidc(monkeypatch, proxy.ADMIN_OIDC_STATES[expired_state], expired_calls)

    expired = browser.get(
        "/admin/oidc/callback",
        params={"state": expired_state, "code": "expired-code"},
        follow_redirects=False,
    )

    assert expired.status_code == 400, expired.text
    assert expired_calls == []
    assert "set-cookie" not in expired.headers
    assert expired_state not in proxy.ADMIN_OIDC_STATES


def test_oidc_parallel_login_states_are_independent_per_browser_tab(
    oidc_gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = _client()
    first_state, first_record = _start_login(browser)
    second_state, second_record = _start_login(browser)
    assert first_state != second_state
    exchange_calls: list[str] = []
    _patch_oidc(monkeypatch, second_record, exchange_calls)

    callback = browser.get(
        "/admin/oidc/callback",
        params={"state": second_state, "code": "second-code"},
        follow_redirects=False,
    )

    assert callback.status_code == 302, callback.text
    assert exchange_calls == ["second-code"]
    assert first_state in proxy.ADMIN_OIDC_STATES
    assert second_state not in proxy.ADMIN_OIDC_STATES
    assert proxy.ADMIN_AUTH.cookie.name in callback.headers.get("set-cookie", "")
    _assert_flow_cookie_deleted(callback, second_record.browser_cookie_name)
    assert browser.cookies.get(first_record.browser_cookie_name) is not None


def test_oidc_parallel_login_states_can_both_redeem_in_same_browser(
    oidc_gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = _client()
    first_state, first_record = _start_login(browser)
    second_state, second_record = _start_login(browser)
    exchange_calls: list[str] = []
    _patch_oidc_by_code(
        monkeypatch,
        {"first": first_record, "second": second_record},
        exchange_calls,
    )

    first = browser.get(
        "/admin/oidc/callback",
        params={"state": first_state, "code": "first"},
        follow_redirects=False,
    )
    second = browser.get(
        "/admin/oidc/callback",
        params={"state": second_state, "code": "second"},
        follow_redirects=False,
    )

    assert first.status_code == 302, first.text
    assert second.status_code == 302, second.text
    assert exchange_calls == ["first", "second"]
    assert first_state not in proxy.ADMIN_OIDC_STATES
    assert second_state not in proxy.ADMIN_OIDC_STATES
    assert sorted(user["email"] for user in admin_store.list_users(path=oidc_gateway)) == [
        "first@example.test",
        "second@example.test",
    ]
    _assert_flow_cookie_deleted(first, first_record.browser_cookie_name)
    _assert_flow_cookie_deleted(second, second_record.browser_cookie_name)


def test_oidc_login_on_noncanonical_host_redirects_before_minting_state(
    oidc_gateway,
) -> None:
    client = TestClient(
        proxy.app,
        base_url="http://evil.example",
        client=("127.0.0.1", 50000),
    )

    response = client.get("/login", follow_redirects=False)

    assert response.status_code == 302, response.text
    target = urlsplit(response.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == f"{BASE_URL}/login"
    assert parse_qs(target.query)["return_to"] == ["/"]
    assert proxy.ADMIN_OIDC_STATES == {}


def test_oidc_login_correct_host_with_http_asgi_scheme_starts_without_loop(
    oidc_gateway,
) -> None:
    client = TestClient(
        proxy.app,
        base_url="http://gateway.example",
        client=("127.0.0.1", 50000),
    )

    response = client.get("/login", follow_redirects=False)

    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert "redirect_uri=" in location
    assert parse_qs(urlsplit(location).query)["redirect_uri"] == [
        f"{BASE_URL}/admin/oidc/callback"
    ]
    assert len(proxy.ADMIN_OIDC_STATES) == 1


def test_oidc_login_explicit_default_https_port_matches_host_without_port(
    oidc_gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = replace(
        _oidc_config(),
        redirect_uri="https://gateway.example:443/admin/oidc/callback",
    )
    monkeypatch.setattr(proxy, "ADMIN_OIDC_CONFIG", cfg)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", cfg.signing_key)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "cookie", cfg.cookie)
    admin_store.put_operator_setting("instance_presentation", {"base_url": ""}, path=oidc_gateway)
    client = TestClient(
        proxy.app,
        base_url="http://gateway.example",
        client=("127.0.0.1", 50000),
    )

    response = client.get("/login", follow_redirects=False)

    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert "redirect_uri=" in location
    assert parse_qs(urlsplit(location).query)["redirect_uri"] == [
        "https://gateway.example:443/admin/oidc/callback"
    ]
    assert len(proxy.ADMIN_OIDC_STATES) == 1


def test_browser_provider_decline_returns_to_signin_without_a_session(oidc_gateway):
    with closing(_client()) as client:
        response = client.get("/login?return_to=/usage/?receipt=one", follow_redirects=False)
        state_value = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        record = proxy.ADMIN_OIDC_STATES[state_value]
        failure = client.get(
            "/admin/oidc/callback", params={"state": state_value, "error": "access_denied", "error_description": "untrusted"},
            headers={"accept": "text/html"}, follow_redirects=False,
        )
        assert failure.status_code == 303
        target = urlsplit(failure.headers["location"])
        assert target.path == "/sign-in/"
        assert parse_qs(target.query) == {"error": ["provider"], "return_to": ["/usage/?receipt=one"]}
        assert "untrusted" not in failure.headers["location"]
        assert state_value not in proxy.ADMIN_OIDC_STATES
        _assert_flow_cookie_deleted(failure, record.browser_cookie_name)
        assert not client.cookies.get(_oidc_config().cookie.name)
        assert failure.headers["cache-control"] == "no-store"
        assert failure.headers["referrer-policy"] == "no-referrer"


def test_browser_invalid_state_cannot_supply_a_return_target(oidc_gateway):
    with closing(_client()) as client:
        failure = client.get(
            "/admin/oidc/callback?state=unknown&error=denied&return_to=//evil.example", headers={"accept": "text/html"}, follow_redirects=False,
        )
        assert failure.status_code == 303
        assert parse_qs(urlsplit(failure.headers["location"]).query) == {"error": ["provider"], "return_to": ["/"]}
        assert not client.cookies.get(_oidc_config().cookie.name)


def test_browser_provider_start_failure_keeps_safe_return_target(oidc_gateway, monkeypatch):
    async def unavailable():
        raise ValueError("private provider detail")
    monkeypatch.setattr(proxy, "_admin_oidc_config_with_discovery", unavailable)
    with closing(_client()) as client:
        failure = client.get("/login?return_to=//evil.example", headers={"accept": "text/html"}, follow_redirects=False)
        assert failure.status_code == 303
        assert parse_qs(urlsplit(failure.headers["location"]).query)["return_to"] == ["/"]
        assert "private" not in failure.text
        assert not client.cookies.get(_oidc_config().cookie.name)
