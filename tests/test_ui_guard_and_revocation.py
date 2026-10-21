"""Admin page guard and session revocation for the web app at the root."""
from __future__ import annotations

import pytest

import proxy
import route_matrix
import test_proxy_admin_integration as admin_integration
from gateway import accounts, admin_store, ui_routes
from gateway.admin_oidc import CookieConfig, decode_session_cookie
from ui_fixtures import ASSET, SIGNING_KEY, Principals, use_export, write_export
from tests.support.access import store_plan

ADMIN_PAGES = sorted(name for name, access in ui_routes.PAGES.items() if access == "admin")
PASSWORD = "a sufficiently long password"


@pytest.fixture(scope="module")
def principals():
    case = admin_integration.ProxyAdminIntegrationTests(methodName="runTest")
    case.setUp()
    try:
        accounts.initialize(case.db)
        proxy.ADMIN_AUTH.signing_key = SIGNING_KEY
        proxy.ADMIN_AUTH.cookie = CookieConfig(secure=False)
        yield Principals(case, route_matrix.mint_app_key(case))
    finally:
        case.tearDown()


@pytest.fixture
def export(tmp_path, monkeypatch):
    out = write_export(tmp_path / "out")
    use_export(monkeypatch, out)
    return out


@pytest.fixture
def client(principals):
    principals.client.cookies.clear()
    yield principals.client
    principals.client.cookies.clear()


def _cookie_name(principals):
    return principals.cookie.name


def _session_cookies(response, name):
    return [value for value in response.headers.get_list("set-cookie") if value.startswith(f"{name}=")]


def _deletes(response, name):
    return any(value.startswith(f'{name}="";') or value.startswith(f"{name}=;") or "max-age=0" in value.lower()
               for value in _session_cookies(response, name))


# -- admin page guard (design test 7) ------------------------------------------------

@pytest.mark.parametrize("name", ADMIN_PAGES)
@pytest.mark.parametrize("payload", [False, True])
def test_admin_page_guard_table(name, payload, principals, export, client):
    path = f"/{name}/index.txt" if payload else f"/{name}/"
    cookie = _cookie_name(principals)

    anonymous = client.get(path, headers={"accept": "text/html"}, follow_redirects=False)
    assert anonymous.status_code == 302
    assert anonymous.headers["location"] == f"/login?return_to=%2F{name}%2F"

    owner_etag = client.get(path, headers={"authorization": f"Bearer {principals.admin_key}"}).headers["etag"]
    client.cookies.set(cookie, principals.session_cookie("none"))
    member = client.get(path, follow_redirects=False)
    assert (member.status_code, member.headers["location"]) == (302, "/")
    cached = client.get(path, headers={"if-none-match": owner_etag}, follow_redirects=False)
    assert cached.status_code == 302, "the guard runs before any 304"
    client.cookies.clear()

    for role in ("owner", "admin", "superadmin"):
        client.cookies.set(cookie, principals.session_cookie(role))
        allowed = client.get(path)
        assert allowed.status_code == 200, role
        etag = allowed.headers["etag"]
        assert client.get(path, headers={"if-none-match": etag}).status_code == 304
        client.cookies.clear()

    client.cookies.set(cookie, principals.session_cookie("owner", revoked=True))
    revoked = client.get(path, follow_redirects=False)
    assert revoked.status_code == 302 and revoked.headers["location"].startswith("/login?")
    assert _deletes(revoked, cookie)
    client.cookies.clear()

    bearer = client.get(path, headers={"authorization": f"Bearer {principals.admin_key}"})
    assert bearer.status_code == 200
    app_key = client.get(path, headers={"authorization": f"Bearer {principals.app_key}"})
    assert app_key.status_code == 403
    assert app_key.json()["error"]["code"] == "admin_forbidden"


@pytest.mark.parametrize("name", [name for name, access in ui_routes.PAGES.items() if access != "admin"])
def test_public_and_member_pages_never_call_the_guard(name, export, client, monkeypatch):
    async def guard(request, page):
        raise AssertionError(f"guard called for {page}")
    monkeypatch.setattr(proxy.WEB_UI_ROUTE, "admin_guard", guard)
    assert client.get(f"/{name}/" if name else "/").status_code == 200


# -- revocation (design test 8) ----------------------------------------------------------

def test_revoked_cookie_never_hides_an_app_key(principals, client):
    cookie = _cookie_name(principals)
    client.cookies.set(cookie, principals.session_cookie("none", revoked=True))
    response = client.get("/v1/models", headers={"authorization": f"Bearer {principals.app_key}"})
    assert response.status_code == 200, response.text
    assert _deletes(response, cookie)


def test_revoked_cookie_never_hides_an_admin_key(principals, client):
    client.cookies.set(_cookie_name(principals), principals.session_cookie("owner", revoked=True))
    response = client.get("/admin/api/session", headers={"authorization": f"Bearer {principals.admin_key}"})
    assert response.status_code == 200, response.text
    without_key = client.get("/admin/api/session")
    assert without_key.status_code == 401


def test_revoked_cookie_never_breaks_a_signed_media_url(principals, client, tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace
    from PIL import Image
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path)
    monkeypatch.setattr(proxy, "_image_url_signing_secret", b"test-media-url-secret")
    image = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(image, format="PNG")
    asset_id = proxy.store_image_bytes(image.getvalue(), owner_key_hash="owner")
    request = SimpleNamespace(headers={"host": "testserver"}, url=SimpleNamespace(scheme="http", netloc="testserver"))
    path = proxy.build_signed_image_url(request, asset_id, "none").split("testserver", 1)[1]
    client.cookies.set(_cookie_name(principals), principals.session_cookie("none", revoked=True))
    response = client.get(path, headers={"range": "bytes=0-9"})
    assert response.status_code == 206, response.text
    assert response.content == image.getvalue()[:10]


def test_revoked_cookie_on_session_routes_gets_their_normal_denial(principals, client):
    client.cookies.set(_cookie_name(principals), principals.session_cookie("none", revoked=True))
    for path in ("/admin/api/session", "/me/api/home", "/me/api/usage/summary"):
        response = client.get(path)
        assert response.status_code in {401, 403}, path
    view = client.get("/me/api/session")
    assert view.status_code == 200 and view.json()["authenticated"] is False


def test_revoked_cookie_on_pages_is_cleared_but_never_on_assets(principals, export, client):
    cookie = _cookie_name(principals)
    for path, status in (("/", 200), ("/sign-in/", 200), ("/invite/", 200), ("/index.txt", 200),
                         ("/library/", 200), ("/ui/home/", 404)):
        client.cookies.set(cookie, principals.session_cookie("none", revoked=True))
        response = client.get(path, headers={"accept": "text/html"})
        assert response.status_code == status, path
        assert len(_session_cookies(response, cookie)) == 1 and _deletes(response, cookie), path
        client.cookies.clear()
    client.cookies.set(cookie, principals.session_cookie("none", revoked=True))
    asset = client.get(ASSET)
    assert asset.status_code == 200
    assert "set-cookie" not in asset.headers


def test_undecodable_cookie_is_never_deleted(principals, export, client):
    cookie = _cookie_name(principals)
    client.cookies.set(cookie, "another-gateway-owns-this")
    for path in ("/", "/v1/models", "/sign-in/"):
        response = client.get(path, headers={"authorization": f"Bearer {principals.app_key}"})
        assert "set-cookie" not in response.headers, path


def test_valid_cookie_is_never_cleared_and_an_old_one_is_upgraded_in_place(principals, export, client):
    name = _cookie_name(principals)
    old = principals.session_cookie("none")
    client.cookies.set(name, old)

    first = client.get("/")

    # A cookie from before sliding sessions is renewed once, into the same sign-in (family); none is ever deleted.
    [upgraded] = _session_cookies(first, name)
    assert "Max-Age=0" not in upgraded
    value = upgraded.split(";", 1)[0].split("=", 1)[1].strip('"')
    assert decode_session_cookie(value, signing_key=SIGNING_KEY).family_id == \
        decode_session_cookie(old, signing_key=SIGNING_KEY).session_id
    assert "set-cookie" not in client.get("/").headers      # the renewed cookie is not touched again


def _assert_one_new_session(response, cookie, old_value):
    cookies = _session_cookies(response, cookie)
    assert len(cookies) == 1, cookies
    value = cookies[0].split(";", 1)[0].split("=", 1)[1].strip('"')
    assert value and value != old_value
    session = decode_session_cookie(value, signing_key=SIGNING_KEY)
    assert accounts.session_allowed(session.subject, session.session_id, path=proxy.ADMIN_STORE.path)
    return value


def test_password_sign_in_with_a_revoked_cookie_keeps_the_new_session(principals, client, monkeypatch):
    monkeypatch.setattr(accounts, "_SCRYPT_N", 2**10)
    plan = store_plan("UI root plan", path=principals.case.db, models=["m"])
    _, token = accounts.create_link("invite", "owner", plan_id=plan["id"], email="revoked@example.test",
                                    path=principals.case.db)
    accounts.accept_invite(token, "revoked@example.test", "Revoked", PASSWORD, path=principals.case.db)
    cookie = _cookie_name(principals)
    stale = principals.session_cookie("none", revoked=True)
    client.cookies.set(cookie, stale)
    response = client.post("/auth/api/login", json={"email": "revoked@example.test", "password": PASSWORD})
    assert response.status_code == 200, response.text
    _assert_one_new_session(response, cookie, stale)
    assert client.get("/me/api/session").json().get("authenticated") is True


def test_admin_key_claim_with_a_revoked_cookie_keeps_the_new_session(principals, client):
    cookie = _cookie_name(principals)
    stale = principals.session_cookie("owner", revoked=True)
    client.cookies.set(cookie, stale)
    response = client.post("/setup/claim", data={"admin_key": principals.admin_key}, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/"
    _assert_one_new_session(response, cookie, stale)
    assert client.get("/admin/api/session").status_code == 200
