"""Real gateway redirect boundaries, independent of the optional web export."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.responses import Response
from fastapi.testclient import TestClient

import proxy
from gateway.browser_routes import ADMIN_SECTIONS, OWNER_UI_PAGES, accepts_html, admin_destination


@pytest.mark.parametrize("mounted", [False, True])
@pytest.mark.parametrize("prefix", ["/admin", "/ui/admin"])
@pytest.mark.parametrize("section,target", list(ADMIN_SECTIONS.items()))
@pytest.mark.parametrize("slash", ["", "/"])
def test_known_admin_aliases_redirect_directly_with_query(mounted, prefix, section, target, slash):
    path = prefix + (f"/{section}" if section else "") + slash
    query = "service=a%2Fb&source=attention&source=receipt"
    with patch.object(proxy, "WEB_UI_MOUNTED", mounted), patch.object(
        type(proxy.ADMIN_AUTH), "__call__", return_value=SimpleNamespace()
    ):
        response = TestClient(proxy.app).get(f"{path}?{query}", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == target + ("&" if "?" in target else "?") + query
    assert response.headers["location"].startswith("/ui/")


@pytest.mark.parametrize("path", ["/", "/me", "/me/"])
def test_browser_home_entry_preserves_query(path):
    response = TestClient(proxy.app).get(
        path + "?connect=phone%2Fapp", headers={"accept": "text/html"}, follow_redirects=False
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/ui/home/?connect=phone%2Fapp"


@pytest.mark.parametrize("accept", ["application/json", "*/*", "", "text/html;q=0, application/json", "text/html;q=invalid"])
def test_api_root_keeps_existing_handler(accept, monkeypatch):
    monkeypatch.setattr(proxy, "_render_user_portal_response", lambda *args, **kwargs: Response("legacy"))
    response = TestClient(proxy.app).get("/", headers={"accept": accept}, follow_redirects=False)
    assert response.status_code == 200
    assert response.text == "legacy"


@pytest.mark.parametrize("path", ["/status", "/status/"])
def test_browser_status_entry_redirects_to_product(path):
    response = TestClient(proxy.app).get(path + "?service=x", headers={"accept": "text/html"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/ui/status/?service=x"


@pytest.mark.parametrize("path", ["/status/raw", "/status/raw/", "/status"])
def test_technical_status_retains_guarded_renderer(path, monkeypatch):
    async def render(request):
        return Response("owner-guarded-renderer")
    monkeypatch.setattr(proxy, "_render_status_html", render)
    response = TestClient(proxy.app).get(path, headers={"authorization": "Bearer test", "accept": "text/html"})
    assert response.status_code == 200
    assert response.text == "owner-guarded-renderer"


def test_firehose_preserves_query():
    client = TestClient(proxy.app)
    response = client.get("/firehose?service=tts", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/status/raw?service=tts"


@pytest.mark.parametrize("path", ["/admin/api/status", "/me/api/session", "/ui/admin/unknown", "/admin/unknown"])
def test_api_and_unknown_paths_are_outside_alias_map(path):
    assert admin_destination(path) is None


def test_unknown_legacy_ui_path_stays_404():
    with patch.object(type(proxy.ADMIN_AUTH), "__call__", return_value=SimpleNamespace()):
        response = TestClient(proxy.app).get("/ui/admin/unknown/", follow_redirects=False)
    assert response.status_code == 404


@pytest.mark.parametrize("accept,expected", [
    ("text/html", True), ("text/html; charset=utf-8; q=0.8", True),
    ("TEXT/HTML;Q=0.2,application/json", True),
    ("text/html;q=0, */*", False), ("text/html;q=bad", False),
    ("application/json", False), ("*/*", False),
])
def test_html_accept_quality(accept, expected):
    assert accepts_html(accept) is expected


@pytest.mark.parametrize("page", sorted(OWNER_UI_PAGES))
@pytest.mark.parametrize("entry", ["/", "/index.html"])
def test_owner_static_entries_require_authorization(page, entry):
    from gateway.admin_api import AdminAuthError
    error = AdminAuthError("Owner role required", status_code=403, code="admin_forbidden")
    with patch.object(type(proxy.ADMIN_AUTH), "__call__", side_effect=error):
        response = TestClient(proxy.app).get(
            f"/ui/{page}{entry}", headers={"authorization": "Bearer member"}, follow_redirects=False
        )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "admin_forbidden"


@pytest.mark.parametrize("path,endpoint", [
    ("/me/api/session", proxy.user_portal_session),
    ("/admin/api/drain", proxy.admin_drain_status),
])
def test_api_routes_retain_registered_handlers(path, endpoint):
    assert any(route.path == path and route.endpoint is endpoint for route in proxy.app.routes if hasattr(route, "endpoint"))
