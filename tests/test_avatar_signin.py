"""The sign-in picture: read from the ID token, kept on the person, served through the gateway."""
from __future__ import annotations

import base64
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from gateway import admin_store, avatars, media_fetch
from gateway.admin_oidc import CookieConfig, OIDCConfig

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PICTURE = "https://lh3.example.test/a/photo.png"
CALLBACK = "https://openai.vivid.fish/admin/oidc/callback"


def _config() -> OIDCConfig:
    return OIDCConfig(
        enabled=True, issuer="https://idp.example", client_id="gateway", client_secret="test-secret",
        redirect_uri=CALLBACK, scopes=("openid", "email", "profile"),
        admin_emails=frozenset({"owner@example.test"}),
        signing_key=b"s" * 32, cookie=CookieConfig(secure=False),
        authorization_endpoint="https://idp.example/authorize", token_endpoint="https://idp.example/token",
        jwks_uri="https://idp.example/jwks",
    )


@pytest.fixture
def gateway(tmp_path, monkeypatch):
    import proxy
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    monkeypatch.setattr(proxy.ADMIN_STORE, "path", path)
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", path)
    monkeypatch.setattr(proxy, "ADMIN_OIDC_CONFIG", _config())
    monkeypatch.setattr(proxy.ADMIN_AUTH, "signing_key", _config().signing_key)
    monkeypatch.setattr(proxy.ADMIN_AUTH, "cookie", _config().cookie)
    admin_store.put_operator_setting("instance_presentation", {"base_url": "https://openai.vivid.fish"}, path=path)
    monkeypatch.setattr(proxy, "ADMIN_OIDC_STATES", {})
    client = TestClient(proxy.app, base_url="https://openai.vivid.fish", client=("127.0.0.1", 50000))
    try:
        yield proxy, client, path
    finally:
        client.close()


def sign_in(proxy, client, monkeypatch, **claims):
    login = client.get("/login", params={"return_to": "/"}, follow_redirects=False)
    if "redirect_uri=" not in login.headers["location"]:
        login = client.get(login.headers["location"], follow_redirects=False)
    assert parse_qs(urlsplit(login.headers["location"]).query)["redirect_uri"] == [CALLBACK]
    state, record = next(iter(proxy.ADMIN_OIDC_STATES.items()))

    async def exchange(code, *, config, pkce):
        return {"id_token": "test.token"}

    async def verify(token, *, config):
        return {"iss": config.issuer, "aud": config.client_id, "sub": "owner", "email": "owner@example.test",
                "email_verified": True, "name": "Olive Owner", "nonce": record.nonce,
                "exp": 4_000_000_000, "iat": 1_700_000_000, **claims}

    monkeypatch.setattr(proxy, "_oidc_exchange_code", exchange)
    monkeypatch.setattr(proxy, "_oidc_verify_id_token", verify)
    callback = client.get(CALLBACK, params={"state": state, "code": "code"}, follow_redirects=False)
    assert callback.status_code == 302, callback.text
    return callback


def person(path):
    return admin_store.find_oidc_user("https://idp.example", "owner", path=path)


def test_the_picture_claim_is_stored_on_the_person_and_not_in_the_session(gateway, monkeypatch):
    proxy, client, path = gateway
    callback = sign_in(proxy, client, monkeypatch, picture=PICTURE)
    stored = avatars.get(person(path)["id"], path=path)
    assert stored["picture_url"] == PICTURE
    assert stored["choice"] == "auto"
    # The cookie is signed, not encrypted: its payload must not carry the URL (or a picture field).
    cookie = callback.headers["set-cookie"]
    value = cookie.split("=", 1)[1].split(";", 1)[0]
    payload = json.loads(base64.urlsafe_b64decode(value.split(".")[1] + "=="))
    assert "picture" not in json.dumps(payload) and "lh3.example.test" not in json.dumps(payload)
    assert "picture" not in payload


@pytest.mark.parametrize("claim", ["http://lh3.example.test/p.png", "//lh3.example.test/p.png",
                                   "https://" + "a" * 2050, "javascript:alert(1)", 7, ""])
def test_an_unsafe_or_long_claim_is_not_kept(gateway, monkeypatch, claim):
    proxy, client, path = gateway
    sign_in(proxy, client, monkeypatch, picture=claim)
    assert avatars.get(person(path)["id"], path=path)["picture_url"] is None


def test_a_provider_that_sends_no_picture_leaves_the_person_without_one(gateway, monkeypatch):
    proxy, client, path = gateway
    sign_in(proxy, client, monkeypatch)
    assert avatars.get(person(path)["id"], path=path)["picture_url"] is None


def test_the_viewer_payload_has_the_person_id_and_the_avatar_with_a_gateway_path(gateway, monkeypatch):
    proxy, client, path = gateway
    sign_in(proxy, client, monkeypatch, picture=PICTURE)
    user = client.get("/me/api/session").json()["user"]
    assert user["person_id"] == person(path)["id"] == user["id"]
    assert user["avatar"]["choice"] == "auto"
    assert user["avatar"]["person_id"] == user["person_id"]
    assert user["avatar"]["picture_url"].startswith("/me/api/profile/avatar/picture?v=")
    assert "lh3.example.test" not in json.dumps(user)


def test_the_whole_journey_choose_the_picture_and_see_it_served_without_the_session(gateway, monkeypatch):
    proxy, client, path = gateway
    sign_in(proxy, client, monkeypatch, picture=PICTURE)
    fetched: list[dict] = []

    async def fake_fetch(url, **kwargs):
        fetched.append({"url": url, **kwargs})
        return media_fetch.FetchedMedia(PNG, "p.png", "image/png")

    monkeypatch.setattr(media_fetch, "safe_fetch_media", fake_fetch)
    csrf = client.get("/me/api/session").json()["csrf_token"]
    write = {"x-azizam-user-action": "1", "x-azizam-csrf": csrf}
    assert client.put("/me/api/profile/avatar", json={"choice": "picture"}).status_code == 403
    saved = client.put("/me/api/profile/avatar", json={"choice": "picture"}, headers=write)
    assert saved.status_code == 200 and saved.json()["choice"] == "picture"
    assert client.get("/me/api/session").json()["user"]["avatar"]["choice"] == "picture"
    picture = client.get(saved.json()["picture_url"])
    assert picture.status_code == 200 and picture.content == PNG
    assert picture.headers["content-type"] == "image/png"
    assert picture.headers["cache-control"] == "no-store"
    assert picture.headers["content-disposition"] == 'inline; filename="avatar"'
    assert fetched[0]["url"] == PICTURE
    assert fetched[0]["schemes"] == frozenset({"https"}) and fetched[0]["limit"] <= 1024 * 1024
    # The browser never sees the provider's address.
    assert PICTURE not in saved.text and PICTURE not in client.get("/me/api/session").text
    # A caller with no session gets nothing.
    anonymous = TestClient(proxy.app, base_url="https://openai.vivid.fish", client=("203.0.113.9", 41000))
    assert anonymous.get(saved.json()["picture_url"]).status_code in {401, 403}


def test_a_failed_fetch_answers_404_and_the_choice_stays(gateway, monkeypatch):
    proxy, client, path = gateway
    sign_in(proxy, client, monkeypatch, picture=PICTURE)
    avatars.set_choice(person(path)["id"], "picture", path=path)

    async def broken(url, **kwargs):
        raise media_fetch.MediaFetchError("media URL resolves to a blocked network address")

    monkeypatch.setattr(media_fetch, "safe_fetch_media", broken)
    assert client.get("/me/api/profile/avatar/picture").status_code == 404
    assert client.get("/me/api/profile/avatar").json()["choice"] == "picture"


def test_every_new_route_answers_no_store_json_or_a_checked_image(gateway, monkeypatch):
    proxy, client, path = gateway
    sign_in(proxy, client, monkeypatch)
    for route in ("/me/api/profile/avatar", "/me/api/profile/avatars"):
        response = client.get(route)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
