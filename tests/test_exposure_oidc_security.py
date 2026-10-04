from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

import proxy
from gateway import admin_store
from gateway.admin_oidc import CookieConfig, OIDCConfig


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
        allowed_email_domains=frozenset({"example.test"}),
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
    proxy.ADMIN_OIDC_STATES.clear()
    try:
        yield path
    finally:
        proxy.ADMIN_OIDC_STATES.clear()


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
    users = admin_store.list_users(path=oidc_gateway)
    assert [user["email"] for user in users] == ["member@example.test"]


def test_oidc_parallel_login_states_are_independent_per_browser_tab(
    oidc_gateway,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser = _client()
    first_state, _first_record = _start_login(browser)
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
