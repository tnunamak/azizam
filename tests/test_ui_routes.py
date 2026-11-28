"""The web app at the root: classifier, route placement, pages, cache, return paths."""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Match, Mount

import proxy
import route_matrix
from gateway import ui_routes
from gateway.admin_oidc import safe_browser_return_to
from ui_fixtures import ASSET, use_export, write_export

ROOT = Path(__file__).resolve().parents[1]
METHODS = route_matrix.METHODS
UI_SHAPES = sorted({
    *(f"/{name}/" if name else "/" for name in ui_routes.PAGES),
    *(f"/{name}" for name in ui_routes.PAGES if name),
    *(f"/{name}/index.txt" if name else "/index.txt" for name in ui_routes.PAGES),
    "/_next/static/x", ASSET,
    *(f"/{name}" for name in ui_routes.ROOT_FILES),
})
HTML = {"accept": "text/html,application/xhtml+xml"}


@pytest.fixture
def export(tmp_path, monkeypatch):
    out = write_export(tmp_path / "out")
    use_export(monkeypatch, out)
    return out


@pytest.fixture
def client():
    return TestClient(proxy.app, client=("127.0.0.1", 50000))


# -- classifier ---------------------------------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("/", ui_routes.Page("")),
    ("/library/", ui_routes.Page("library")),
    ("/categories/", ui_routes.Page("categories")),
    ("/library", ui_routes.PageSlashless("library")),
    ("/index.txt", ui_routes.PagePayload("")),
    ("/people/index.txt", ui_routes.PagePayload("people")),
    (ASSET, ui_routes.Asset(ASSET[1:])),
    ("/_next/static/css/a.css", ui_routes.Asset("_next/static/css/a.css")),
    ("/favicon.ico", ui_routes.Asset("favicon.ico")),
    ("/icon.svg", ui_routes.Asset("icon.svg")),
    ("/opengraph-image.png", ui_routes.Asset("opengraph-image.png")),
    ("/manifest.webmanifest", ui_routes.Asset("manifest.webmanifest")),
])
def test_classify_exact_export_shapes(path, expected):
    assert ui_routes.classify(path) == expected


@pytest.mark.parametrize("path", [
    "", "/home/", "/gallery/", "/services/", "/ui/", "/ui/home/", "/llms.txt", "/v1/models", "/library/x",
    "/library/index.html", "/library//", "//library/", "/_next/static/", "/_next/other/x.js", "/index.html",
    "/_next/static/../../people/index.txt", "/library/../people/", "/_next/static/./x.js", "/_next/static//x.js",
    "/_next/static/a\\b.js", "/404.html", "/404/",
    "/favicon.ico/", "/library/favicon.ico", "/_next/favicon.ico", "/icon.png", "/apple-touch-icon.png",
])
def test_classify_rejects_everything_else(path):
    assert ui_routes.classify(path) is None


def test_root_files_are_bare_names_that_no_page_or_route_owns():
    assert {"favicon.ico", "icon.svg", "apple-icon.png", "manifest.webmanifest",
            "opengraph-image.png"} <= ui_routes.ROOT_FILES
    assert not ui_routes.ROOT_FILES & set(ui_routes.PAGES)
    assert all("/" not in name for name in ui_routes.ROOT_FILES)
    assert not {name.split(".")[0] for name in ui_routes.ROOT_FILES} & proxy.WEB_UI_ROUTE.registered_segments


def test_registry_access_classes_and_admin_pages():
    assert set(ui_routes.PAGES.values()) == {"public", "member", "admin"}
    assert {name for name, access in ui_routes.PAGES.items() if access == "admin"} == {
        "activity", "categories", "engines", "people", "providers", "settings"}
    assert ui_routes.PAGES[""] == "public"


# -- placement (design test 2) --------------------------------------------------

def test_ui_route_is_last_before_the_catch_all_and_nothing_is_mounted():
    routes = proxy.app.router.routes
    assert routes[-2] is proxy.WEB_UI_ROUTE
    assert routes[-1].path == "/{path:path}" and routes[-1].endpoint is proxy.unmatched_route
    assert not [route for route in routes if isinstance(route, Mount)]
    assert "_next" not in proxy.WEB_UI_ROUTE.registered_segments


@pytest.mark.parametrize("path", UI_SHAPES)
def test_no_registered_route_matches_a_ui_shape_for_any_method(path):
    for method in METHODS:
        scope = {"type": "http", "method": method, "path": path, "root_path": "", "headers": [], "query_string": b""}
        for route in proxy.app.router.routes[:-2]:
            assert route.matches(scope)[0] == Match.NONE, (method, path, route_matrix.route_label(route))


def test_ui_route_never_matches_partially_or_websockets():
    route = proxy.WEB_UI_ROUTE
    for path in ["/v1/models", "/mcp", "/admin", "/ui/home/", "/zz-unknown"]:
        for method in METHODS:
            for headers in ([], [(b"accept", b"text/html")]):
                match, _ = route.matches({"type": "http", "method": method, "path": path, "headers": headers})
                assert match in {Match.NONE, Match.FULL}
    assert route.matches({"type": "websocket", "path": "/", "headers": []})[0] == Match.NONE


def test_registered_segments_come_from_the_app_and_must_be_literal():
    assert {"v1", "mcp", "admin", "me", "auth", "setup", "status", "api", "ollama"} <= (
        proxy.WEB_UI_ROUTE.registered_segments)
    assert not {"ui", "r", "firehose"} & proxy.WEB_UI_ROUTE.registered_segments

    class Fake:
        path = "/{token}/v1/models"

    with pytest.raises(ValueError, match="literal"):
        ui_routes.registered_first_segments([Fake()])


# -- registry parity (design test 3) ---------------------------------------------

def test_registry_matches_the_web_app_page_directories():
    app_dir = ROOT / "web" / "src" / "app"
    pages = set()
    for page in app_dir.rglob("page.tsx"):
        relative = page.parent.relative_to(app_dir)
        if any(part.startswith(("_", "(")) for part in relative.parts):
            continue
        pages.add(str(relative) if str(relative) != "." else "")
    assert pages == set(ui_routes.PAGES)


def test_web_build_inputs_include_the_registry():
    assert "gateway/ui_pages.json" in (ROOT / "scripts" / "safe-restart").read_text()
    assert re.search(r"^COPY [^\n]*gateway/ui_pages\.json", (ROOT / "Dockerfile").read_text(), re.M)


# -- pages (design test 6) ---------------------------------------------------------

@pytest.mark.parametrize("name", [name for name, access in ui_routes.PAGES.items() if access != "admin"])
def test_public_and_member_pages_serve_get_and_bodyless_head(name, export, client):
    path = f"/{name}/" if name else "/"
    get = client.get(path)
    head = client.head(path)
    assert get.status_code == 200 and head.status_code == 200
    assert get.text == (export / (name or ".") / "index.html").read_text()
    assert head.content == b""
    assert head.headers["content-length"] == get.headers["content-length"]
    assert get.headers["cache-control"] == "no-cache"
    assert get.headers["etag"]
    payload = client.get(f"/{name}/index.txt?_rsc=x" if name else "/index.txt?_rsc=x")
    assert payload.status_code == 200 and payload.text == f"payload {name or 'home'}"
    assert payload.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path", ["/library/", "/library", "/library/index.txt", ASSET, "/"])
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_other_methods_on_ui_shapes_are_405_json(path, method, export, client):
    response = client.request(method, path)
    assert response.status_code == 405
    assert response.headers["allow"] == "GET, HEAD"
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_slashless_page_redirects_with_query_to_a_path(export, client):
    response = client.get("/library?tab=images&q=a%2Fb", follow_redirects=False)
    assert response.status_code == 308
    assert response.headers["location"] == "/library/?tab=images&q=a%2Fb"
    assert client.head("/try", follow_redirects=False).headers["location"] == "/try/"


# httpx removes literal dot segments before sending; classify() covers those.
@pytest.mark.parametrize("path", [
    "/_next/static/..%2f..%2fproxy.py", "/library/..%2findex.html",
    "/_next/static/%2e%2e/%2e%2e/people/index.txt", "/_next/static/%2e%2e/%2e%2e/proxy.py",
])
def test_traversal_never_leaves_the_export_or_skips_the_guard(path, export, client):
    (export.parent / "proxy.py").write_text("secret")
    response = client.get(path, headers=HTML)
    assert "secret" not in response.text
    assert "payload people" not in response.text
    assert response.status_code in {401, 404}


def test_anonymous_admin_payload_is_guarded(export, client):
    response = client.get("/people/index.txt", headers=HTML, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login?return_to=%2Fpeople%2F"


@pytest.mark.parametrize("name,media", [
    ("favicon.ico", "image/"), ("icon.svg", "image/svg+xml"), ("apple-icon.png", "image/png"),
    ("icon-maskable-512.png", "image/png"), ("opengraph-image.png", "image/png"),
    ("manifest.webmanifest", "application/manifest+json"),
])
def test_root_files_are_served_to_anyone_and_revalidate(name, media, export, client):
    # Browsers fetch icons without credentials and link previews fetch from outside.
    (export / name).write_bytes(b"icon bytes " + name.encode())
    response = client.get(f"/{name}")
    assert response.status_code == 200
    assert response.content == b"icon bytes " + name.encode()
    assert response.headers["content-type"].startswith(media)
    assert response.headers["cache-control"] == "no-cache"
    head = client.head(f"/{name}")
    assert head.status_code == 200 and head.content == b""


def test_missing_root_file_is_a_bodyless_404(export, client):
    response = client.get("/favicon.ico", headers=HTML)
    assert response.status_code == 404
    assert response.content == b""


def test_missing_asset_is_a_bodyless_404(export, client):
    response = client.get("/_next/static/chunks/missing-0123456789abcdef.js", headers=HTML)
    assert response.status_code == 404
    assert response.content == b""
    assert "text/html" not in response.headers.get("content-type", "")


def test_unknown_page_with_html_accept_gets_the_app_404(export, client):
    for path in ["/ui/home/", "/ui/", "/home/", "/r/abc", "/firehose", "/zz-unknown?x=1"]:
        response = client.get(path, headers=HTML, follow_redirects=False)
        assert response.status_code == 404, path
        assert "Not found" in response.text
        assert response.headers["content-type"].startswith("text/html")
        assert response.headers["cache-control"] == "no-cache"
        head = client.head(path, headers=HTML)
        assert head.status_code == 404 and head.content == b""


def test_api_clients_keep_json_on_unknown_paths(export, client):
    response = client.get("/ui/home/", headers={"accept": "*/*"})
    assert response.status_code in {401, 404}  # the catch-all: auth first, then not_found
    assert response.headers["content-type"].startswith("application/json")


# -- missing export (design test 11) ---------------------------------------------------

def test_missing_export_gives_503_pages_and_404_files(tmp_path, monkeypatch, client):
    use_export(monkeypatch, tmp_path / "never-built")
    for path in ["/", "/library/", "/library", "/people/", "/ui/home/"]:
        response = client.get(path, headers=HTML, follow_redirects=False)
        if path == "/people/":
            assert response.status_code == 302  # the guard runs first
            continue
        assert response.status_code == 503, path
        assert response.headers["content-type"].startswith("text/plain")
        assert response.text == ui_routes.NOT_BUILT
    for path in ["/index.txt", "/library/index.txt", ASSET]:
        assert client.get(path).status_code == 404, path
    assert client.get("/health").status_code == 200


# -- cache (design test 9) ----------------------------------------------------------------

def test_hashed_assets_are_immutable_and_html_revalidates(export, client):
    asset = client.get(ASSET, headers={"accept-encoding": "br"})
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert asset.headers["content-encoding"] == "br"
    assert "Accept-Encoding" in asset.headers["vary"]
    page = client.get("/library/")
    assert page.headers["cache-control"] == "no-cache"
    cached = client.get("/library/", headers={"if-none-match": page.headers["etag"]})
    assert cached.status_code == 304


# -- API errors stay API errors (design test 5) ---------------------------------------------

HTML_ALLOWED = {"/docs", "/redoc", "/status/raw", "/status/raw/", "/auth/logout-confirm", "/setup/console",
                "/setup/claim", "/setup/local-owner", "/setup/local-login", "/setup/first-app"}


def _known_path(segment):
    for route in proxy.app.router.routes[:-2]:
        path = getattr(route, "path", "")
        if ui_routes.first_segment(path) == segment and "{" not in path:
            return path
    return None


@pytest.mark.parametrize("segment", sorted(proxy.WEB_UI_ROUTE.registered_segments))
def test_registered_namespaces_never_answer_html_outside_the_allowlist(segment, export, monkeypatch):
    # Bound side effects: an unauthenticated remote caller, so no handler
    # below reaches a backend.
    monkeypatch.setattr(proxy, "AUTH_PUBLIC_PATHS", ["/health"])
    monkeypatch.setattr(proxy, "_setup_page_response", _fake_setup_page)
    client = TestClient(proxy.app, base_url="https://gateway.example", client=("203.0.113.25", 41000))
    paths = {f"/{segment}", f"/{segment}/zz-unknown-probe", _known_path(segment)} - {None}
    for path in sorted(paths):
        for method in METHODS:
            if method not in {"GET", "HEAD"} and path in HTML_ALLOWED:
                continue
            for accept in ("text/html", "application/json", "*/*"):
                response = client.request(method, path, headers={"accept": accept}, follow_redirects=False)
                if response.headers.get("content-type", "").startswith("text/html"):
                    assert path in HTML_ALLOWED, (method, path, accept, response.status_code)


async def _fake_setup_page(request, **kwargs):
    from fastapi.responses import HTMLResponse
    return HTMLResponse("<p>setup</p>", status_code=kwargs.get("status_code", 200))


# -- return paths (design test 10) ------------------------------------------------------------

CASES = json.loads((ROOT / "tests" / "fixtures" / "return_paths.json").read_text())["cases"]


@pytest.mark.parametrize("value,expected", CASES)
def test_return_path_sanitizer_follows_the_shared_table(value, expected):
    assert safe_browser_return_to(value) == expected


def test_login_defaults_to_root_and_keeps_the_callback_path():
    assert proxy._safe_admin_return_to(None) == "/"
    assert proxy._login_url(return_to="/ui/home/") == "/login?return_to=%2F"
    assert proxy._login_url(return_to="/people/?tab=plans") == "/login?return_to=%2Fpeople%2F%3Ftab%3Dplans"
    assert any(getattr(route, "path", "") == "/admin/oidc/callback" for route in proxy.app.router.routes)


@pytest.mark.parametrize("accept,expected", [
    ("text/html", True), ("text/html; charset=utf-8; q=0.8", True),
    ("TEXT/HTML;Q=0.2,application/json", True),
    ("text/html;q=0, */*", False), ("text/html;q=bad", False),
    ("application/json", False), ("*/*", False),
])
def test_html_accept_quality(accept, expected):
    from gateway.browser_routes import accepts_html
    assert accepts_html(accept) is expected
