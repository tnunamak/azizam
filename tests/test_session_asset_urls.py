"""Browser media is usable only during its sign-in; App links keep their contract."""
import hashlib
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import pytest
from starlette.requests import Request

import proxy
from gateway import accounts, browser_sessions, thumbnails
from gateway.owner_openapi import walk_routes
from tests.test_library_perf import _png
from tests.test_session_lifecycle import cookie_session, csrf, owner_client, put_cookie
from tests.test_signin_who_can_enter import OWNER, SIGNING_KEY, gateway  # noqa: F401
from gateway.admin_oidc import encode_session_cookie

ASSET = "img_" + "a" * 64


@pytest.fixture
def media(gateway, monkeypatch, tmp_path):
    cache = tmp_path / "images"
    cache.mkdir()
    (cache / ("a" * 64 + ".png")).write_bytes(_png(64, 64))
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", cache)
    monkeypatch.setattr(proxy, "_image_url_signing_secret", b"asset-test-signing-key")
    return gateway


def mint(client, mode="none", *, key=False, image=False):
    headers = [(b"host", b"ai.example")]
    if client.cookies:
        headers.append((b"cookie", ("opx_admin_session=" + client.cookies.get("opx_admin_session")).encode()))
    if key:
        headers.append((b"authorization", b"Bearer test-app-credential"))
    request = Request({"type": "http", "scheme": "https", "server": ("ai.example", 443),
                       "headers": headers, "path": "/me/api/media", "query_string": b""})
    builder = proxy.build_signed_image_url if image else proxy.build_signed_asset_url
    return builder(request, ASSET, mode)


@pytest.mark.parametrize("mode,image", [("none", False), ("none", True), (thumbnails.THUMB_MODE, False)])
def test_signed_asset_url_dies_with_its_session_family(media, mode, image):
    client = owner_client(media)
    url = mint(client, mode, image=image)
    anonymous = media.client()
    response = anonymous.get(url)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.headers["etag"] == '"' + hashlib.sha256(response.content).hexdigest() + '"'
    # A warm verdict and an ETag cannot authorize the first fetch after the end.
    assert client.post("/logout", headers=csrf(client), follow_redirects=False).status_code == 303
    ended = anonymous.get(url, headers={"if-none-match": response.headers["etag"]})
    assert ended.status_code == 403


@pytest.mark.parametrize("mode", ["none", thumbnails.THUMB_MODE])
def test_key_minted_asset_urls_are_unaffected(media, mode):
    client = owner_client(media)
    url = mint(client, mode, key=True)
    query = parse_qs(urlsplit(url).query)
    assert "family" not in query and "subject" not in query
    assert query["sig"][0] == proxy._sign_asset_token(ASSET, int(query["expires"][0]), mode)
    before = media.client().get(url)
    assert before.status_code == 200
    assert before.headers["cache-control"] == "private, max-age=3600"
    browser_sessions.end_everywhere(cookie_session(client), path=media.path)
    after = media.client().get(url)
    assert after.status_code == 200 and after.content == before.content
    if mode == thumbnails.THUMB_MODE:
        etag = before.headers["etag"]
        assert media.client().get(url, headers={"if-none-match": etag}).status_code == 304
        assert media.client().get(url, headers={"if-none-match": "W/" + etag}).status_code == 200


@pytest.mark.parametrize("mode", ["none", thumbnails.THUMB_MODE])
def test_browser_asset_revalidation_and_ranges(media, mode):
    client = owner_client(media)
    url = mint(client, mode)
    response = client.get(url)
    etag = response.headers["etag"]
    for validator in (etag, f'"unrelated", W/{etag}', "*"):
        again = client.get(url, headers={"if-none-match": validator})
        assert again.status_code == 304 and again.content == b""
        assert again.headers["cache-control"] == "private, no-cache"
        assert again.headers["etag"] == etag
    if mode == "none":
        partial = client.get(url, headers={"range": "bytes=0-9"})
        assert partial.status_code == 206 and partial.content == response.content[:10]
        assert partial.headers["etag"] == etag


def test_sign_out_everywhere_from_device_a_kills_device_b_asset_urls(media):
    a, b = owner_client(media), owner_client(media)
    urls = [mint(client, thumbnails.THUMB_MODE) for client in (a, b)]
    assert all(media.client().get(url).status_code == 200 for url in urls)
    ended = a.post("/auth/api/sign-out-everywhere", headers=csrf(a))
    assert ended.status_code == 200
    assert ended.headers["clear-site-data"] == '"cache"'
    assert ended.headers["cache-control"] == "no-store"
    assert all(media.client().get(url).status_code == 403 for url in urls)


def test_inflight_mint_cannot_drop_an_ended_session_binding(media):
    client = owner_client(media)
    session = cookie_session(client)
    browser_sessions.end(session, path=media.path)
    # The request still carries its original cookie after another request ended it.
    url = mint(client)
    assert parse_qs(urlsplit(url).query)["family"] == [session.family_id]
    assert media.client().get(url).status_code == 403


def test_unregistered_service_session_urls_end_with_their_session(media):
    client = media.client()
    session = browser_sessions.issue({"sub": "service-account", "email": "service@example.test"},
                                     role="owner", cookie=media.config.cookie, path=media.path,
                                     register=False, renewable=False)
    put_cookie(client, encode_session_cookie(session, signing_key=SIGNING_KEY))
    url = mint(client)
    assert int(parse_qs(urlsplit(url).query)["expires"][0]) <= session.expires_at
    assert media.client().get(url).status_code == 200
    browser_sessions.end(session, path=media.path)
    assert media.client().get(url).status_code == 403


def test_no_store_includes_replaced_and_unhandled_server_errors():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from gateway.browser_cache import BrowserCacheMiddleware
    from gateway.egress import EgressGate

    app = FastAPI()

    @app.get("/me/api/fail")
    def fail():
        raise RuntimeError("synthetic handler failure")

    app.add_api_route("/admin/api/fail", fail)
    app.add_api_route("/auth/api/fail", fail)
    app.add_middleware(EgressGate)
    app.add_middleware(BrowserCacheMiddleware)
    with TestClient(app, raise_server_exceptions=False) as client:
        for prefix in ("/me/api", "/admin/api", "/auth/api"):
            response = client.get(prefix + "/fail")
            assert response.status_code == 500
            assert response.headers["cache-control"] == "no-store"


def test_rotation_keeps_family_urls_and_invalidates_the_memo(media):
    client = owner_client(media)
    old = cookie_session(client)
    url = mint(client)
    assert client.get(url).status_code == 200
    renewed = browser_sessions.confirm(old, path=media.path)
    # The next request must resolve the new live id, not use the warmed answer.
    assert not accounts._asset_families
    accounts.session_verdict(renewed.subject, renewed.session_id, renewed.family_id,
                             path=media.path, note_use=True)
    # After the retired-cookie grace period, the asset still follows the family.
    with accounts.connection(media.path) as conn:
        conn.execute("UPDATE account_session_lineage SET retired_at=? WHERE session_id=?",
                     (int(time.time()) - 120, old.session_id))
    assert media.client().get(url).status_code == 200
    browser_sessions.end(renewed, path=media.path)
    assert media.client().get(url).status_code == 403


def test_family_memo_uses_the_session_verdict_and_lasts_at_most_five_seconds(media, monkeypatch):
    client = owner_client(media)
    session = cookie_session(client)
    url = mint(client)
    calls = []
    original = accounts.session_verdict_in

    def verdict(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(accounts, "session_verdict_in", verdict)
    clock = [100.0]
    monkeypatch.setattr(accounts, "time", SimpleNamespace(time=time.time, monotonic=lambda: clock[0]))
    assert media.client().get(url).status_code == 200
    count = len(calls)
    clock[0] = 104.99
    assert media.client().get(url).status_code == 200
    assert len(calls) == count
    clock[0] = 105.0
    assert media.client().get(url).status_code == 200
    assert len(calls) > count
    accounts.revoke_person_sessions(session.subject, path=media.path)
    assert media.client().get(url).status_code == 403


@pytest.mark.parametrize("change", ["family", "subject", "remove_binding"])
def test_family_binding_cannot_be_changed_or_removed(media, change):
    client = owner_client(media)
    parts = urlsplit(mint(client))
    query = parse_qs(parts.query)
    if change == "remove_binding":
        query.pop("family")
        query.pop("subject")
    else:
        query[change] = ["another-session"]
    url = urlunsplit(parts._replace(query=urlencode(query, doseq=True)))
    assert media.client().get(url).status_code == 403


@pytest.mark.parametrize("state", ["disabled_at", "removed_at", "expired"])
def test_family_urls_obey_account_state_and_session_expiry(media, state):
    client = owner_client(media)
    url = mint(client)
    session = cookie_session(client)
    assert media.client().get(url).status_code == 200
    with accounts.connection(media.path) as conn:
        accounts.ensure_access_row(conn, session.subject)
        if state == "expired":
            conn.execute("UPDATE account_sessions SET expires_at=0 WHERE session_id=?", (session.session_id,))
        else:
            conn.execute(f"UPDATE account_access SET {state}=1 WHERE user_id=?", (session.subject,))
    assert media.client().get(url).status_code == 403


def test_logout_sends_clear_site_data_cache_and_no_store(media):
    client = owner_client(media)
    for headers, status in (({}, 403), (csrf(client), 303)):
        response = client.post("/logout", headers=headers, follow_redirects=False)
        assert response.status_code == status
        assert response.headers["clear-site-data"] == '"cache"'
        assert response.headers["cache-control"] == "no-store"
    assert client.post("/logout").headers["cache-control"] == "no-store"


def test_every_me_admin_auth_api_response_is_no_store(media):
    client = media.client()
    routes = [(path, route) for path, route in walk_routes(proxy.app.routes)
              if path.startswith(("/me/api/", "/admin/api/", "/auth/api/")) and getattr(route, "methods", None)]
    assert len(routes) > 100
    for path, route in routes:
        for method in route.methods - {"HEAD", "OPTIONS"}:
            response = client.request(method, path, follow_redirects=False)
            assert response.headers.get("cache-control") == "no-store", (method, path, response.status_code)
    for prefix in ("/me/api", "/admin/api", "/auth/api"):
        assert client.get(prefix + "/does-not-exist").headers["cache-control"] == "no-store"


def test_session_view_id_is_public_stable_across_rotation_and_changes_on_new_signin(media):
    client = owner_client(media)
    old = cookie_session(client)
    view = client.get("/me/api/session").json()["session_view_id"]
    assert len(view) == 64 and view not in {old.session_id, old.family_id}
    renewed = browser_sessions.confirm(old, path=media.path)
    put_cookie(client, encode_session_cookie(renewed, signing_key=SIGNING_KEY))
    assert client.get("/me/api/session").json()["session_view_id"] == view
    assert owner_client(media).get("/me/api/session").json()["session_view_id"] != view
    assert "session_view_id" not in media.client().get("/me/api/session").json()
