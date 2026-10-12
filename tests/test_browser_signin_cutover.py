"""Sign-in destinations and canonical OIDC origins, with isolated gateway state."""
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
import pytest

from gateway import admin_store
from gateway.admin_oidc import (
    CookieConfig, OIDCConfig, consume_state, oidc_config_for_public_base,
    record_state, safe_browser_return_to,
)


@pytest.mark.parametrize("target", [
    "/", "/models/?capability=chat", "/people/?tab=plans",
    "/usage/?receipt=abc#details", "/invite/#token=opaque", "/setup/", "/status/raw?tab=engines",
])
def test_product_return_paths_keep_queries_and_fragments(target):
    assert safe_browser_return_to(target) == target


@pytest.mark.parametrize("target", [
    None, "", "https://evil.example/library/", "//evil.example/library/",
    "/\\evil.example/library/", "/%5cevil.example/library/", "/%2fevil.example/library/",
    "/library/%0d%0aLocation:evil", "/library/\x00", "/administrator", "/v1/models",
    "/admin/api/services", "/admin/unknown", "/admin/services?service=tts", "/me?tab=keys",
    "/ui/home/", "/ui/usage/?receipt=abc", "/status?engine=tts", "/setup",
])
def test_unsafe_or_removed_return_paths_fall_back_to_home(target):
    assert safe_browser_return_to(target) == "/"


def config():
    return OIDCConfig(
        enabled=True, issuer="https://idp.example", client_id="gateway", client_secret="test-secret",
        redirect_uri="https://openai.vivid.fish/admin/oidc/callback", scopes=("openid", "email"),
        allowed_email_domains=frozenset(), admin_emails=frozenset({"owner@example.test"}),
        signing_key=b"s" * 32, cookie=CookieConfig(secure=False),
        authorization_endpoint="https://idp.example/authorize", token_endpoint="https://idp.example/token",
        jwks_uri="https://idp.example/jwks",
    )


def test_public_origin_changes_only_callback_origin():
    original = config()
    assert oidc_config_for_public_base(original, "") is original
    changed = oidc_config_for_public_base(original, "https://ai.vivid.fish/")
    assert changed.redirect_uri == "https://ai.vivid.fish/admin/oidc/callback"
    assert original.redirect_uri == "https://openai.vivid.fish/admin/oidc/callback"
    store = {}
    record = record_state(store, nonce="nonce", pkce_verifier="verifier", redirect_uri=changed.redirect_uri)
    assert consume_state(store, record.state).redirect_uri == changed.redirect_uri


@pytest.fixture
def browser_gateway(tmp_path, monkeypatch):
    import proxy
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", path)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", path)
    monkeypatch.setattr(proxy, "ADMIN_OIDC_CONFIG", config())
    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", config().signing_key)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "cookie", config().cookie)
    admin_store.put_operator_setting(
        "instance_presentation",
        {"base_url": "https://openai.vivid.fish"},
        path=path,
    )
    proxy.ADMIN_OIDC_STATES.clear()
    # Auth handlers need the initialized temporary store, not engine listeners.
    # Match the route integration fixtures and leave process lifespan to its own tests.
    client = TestClient(
        proxy.app,
        base_url="https://openai.vivid.fish",
        client=("127.0.0.1", 50000),
    )
    try:
        yield proxy, client, path
    finally:
        client.close()
        proxy.ADMIN_OIDC_STATES.clear()


@pytest.mark.parametrize("public_origin", ["", "https://ai.vivid.fish"])
@pytest.mark.parametrize("target,return_page", [
    ("/usage/?receipt=abc#details", "/usage/?receipt=abc#details"),
    ("/ui/usage/?receipt=abc", "/"),
])
def test_oidc_callback_returns_to_product_page_and_pins_origin(browser_gateway, monkeypatch, public_origin, target, return_page):
    proxy, client, path = browser_gateway
    admin_store.put_operator_setting("instance_presentation", {"base_url": public_origin}, path=path)
    login = client.get("/login", params={"return_to": target}, follow_redirects=False)
    assert login.status_code == 302
    if "redirect_uri=" not in login.headers["location"]:
        login = client.get(login.headers["location"], follow_redirects=False)
        assert login.status_code == 302
    expected = (public_origin or "https://openai.vivid.fish") + "/admin/oidc/callback"
    assert parse_qs(urlsplit(login.headers["location"]).query)["redirect_uri"] == [expected]
    state, record = next(iter(proxy.ADMIN_OIDC_STATES.items()))
    assert record.return_to == return_page
    # Mid-login Settings edits must not alter the URI used to exchange this code.
    admin_store.put_operator_setting("instance_presentation", {"base_url": "https://changed.example"}, path=path)

    async def exchange(code, *, config, pkce):
        assert config.redirect_uri == expected
        assert pkce.verifier == record.pkce_verifier
        return {"id_token": "test.token"}

    async def verify(token, *, config):
        return {"iss": config.issuer, "aud": config.client_id, "sub": "owner",
                "email": "owner@example.test", "email_verified": True, "nonce": record.nonce,
                "exp": 4_000_000_000, "iat": 1_700_000_000}

    monkeypatch.setattr(proxy, "_oidc_exchange_code", exchange)
    monkeypatch.setattr(proxy, "_oidc_verify_id_token", verify)
    callback = client.get(expected, params={"state": state, "code": "code"}, follow_redirects=False)
    assert callback.status_code == 302, callback.text
    assert callback.headers["location"] == return_page
    assert "httponly" in callback.headers["set-cookie"].lower()


def test_local_owner_login_returns_to_product_page(browser_gateway, monkeypatch):
    proxy, client, _ = browser_gateway
    monkeypatch.setattr(proxy, "ADMIN_SESSION_SIGNING_KEY", b"s" * 32)
    monkeypatch.setattr(proxy, "ADMIN_OIDC_CONFIG", replace(config(), enabled=False))
    created = client.post("/setup/local-owner", data={"email": "owner@example.test",
                          "password": "correct-horse-battery", "password_confirm": "correct-horse-battery"},
                          follow_redirects=False)
    assert created.status_code == 303, created.text
    client.cookies.clear()
    target = "/settings/?tab=sign-in"
    login = client.post("/setup/local-login", data={"email": "owner@example.test", "password": "correct-horse-battery", "return_to": target}, follow_redirects=False)
    assert login.status_code == 303, login.text
    assert login.headers["location"] == target
