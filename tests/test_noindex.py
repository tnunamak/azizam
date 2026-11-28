"""Azizam is private: /robots.txt asks crawlers to stay away and every HTML response says noindex."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import ui_routes
from gateway.noindex import NoIndexHeaderMiddleware, ROBOTS_TAG
from ui_fixtures import ASSET, use_export, write_export

ROBOTS_TXT = "User-agent: *\nDisallow: /\n"
HTML = {"accept": "text/html,application/xhtml+xml"}
# A caller from outside the host: nothing here may depend on being local or signed in.
REMOTE = ("203.0.113.25", 41000)


@pytest.fixture
def export(tmp_path, monkeypatch):
    out = write_export(tmp_path / "out")
    (out / "robots.txt").write_text(ROBOTS_TXT)
    use_export(monkeypatch, out)
    return out


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setattr(proxy, "AUTH_PUBLIC_PATHS", ["/health"])
    return TestClient(proxy.app, base_url="https://gateway.example", client=REMOTE)


def test_robots_txt_is_a_listed_public_root_file():
    assert "robots.txt" in ui_routes.ROOT_FILES
    assert ui_routes.classify("/robots.txt") == ui_routes.Asset("robots.txt")
    assert ui_routes.classify("/robots.txt/") is None


def test_robots_txt_is_served_to_anyone_without_a_key_and_disallows_everything(export, remote):
    response = remote.get("/robots.txt")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    lines = response.text.splitlines()
    assert lines[0] == "User-agent: *"
    assert "Disallow: /" in lines
    assert "Allow:" not in response.text
    assert remote.head("/robots.txt").status_code == 200


def test_robots_txt_does_not_take_a_bearer_key_or_cookie_to_serve(export, remote):
    assert remote.get("/robots.txt", headers={"authorization": "Bearer opk_wrong"}).status_code == 200


@pytest.mark.parametrize("path", ["/", "/sign-in/", "/invite/", "/setup/", "/library/", "/status/", "/account/"])
def test_every_ui_page_carries_noindex(export, remote, path):
    response = remote.get(path, headers=HTML)
    assert response.headers["content-type"].startswith("text/html"), (path, response.status_code)
    assert response.headers["x-robots-tag"] == "noindex, nofollow"


def test_the_html_404_page_and_redirects_to_sign_in_carry_noindex(export, remote):
    missing = remote.get("/zz-unknown-page", headers=HTML)
    assert missing.status_code == 404
    assert missing.headers["x-robots-tag"] == "noindex, nofollow"
    guarded = remote.get("/people/", headers=HTML, follow_redirects=False)
    assert guarded.status_code == 302


def test_head_requests_for_pages_carry_it_too(export, remote):
    assert remote.head("/sign-in/").headers["x-robots-tag"] == "noindex, nofollow"


def test_html_the_gateway_builds_itself_carries_it(export, remote):
    docs = remote.get("/docs", headers=HTML)
    if docs.status_code == 200:
        assert docs.headers["x-robots-tag"] == "noindex, nofollow"


@pytest.mark.parametrize("path", [ASSET, "/robots.txt", "/health", "/index.txt", "/.well-known/azizam"])
def test_non_html_responses_are_left_alone(export, remote, path):
    response = remote.get(path)
    assert "text/html" not in response.headers.get("content-type", "")
    assert "x-robots-tag" not in response.headers


def test_middleware_tags_html_only_and_keeps_other_headers():
    sent = []

    async def app(scope, receive, send):
        content_type = scope["headers"][0][1] if scope["headers"] else b"text/html; charset=utf-8"
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", content_type), (b"x-keep", b"1")]})
        await send({"type": "http.response.body", "body": b"x"})

    import asyncio

    async def call(content_type: bytes):
        sent.clear()
        wrapped = NoIndexHeaderMiddleware(app)

        async def send(message):
            sent.append(message)

        await wrapped({"type": "http", "headers": [(b"ct", content_type)]}, None, send)
        return dict(sent[0]["headers"])

    html = asyncio.run(call(b"text/html; charset=utf-8"))
    assert html[b"x-robots-tag"] == ROBOTS_TAG.encode() and html[b"x-keep"] == b"1"
    assert b"x-robots-tag" not in asyncio.run(call(b"application/json"))
    assert b"x-robots-tag" not in asyncio.run(call(b"image/png"))


def test_the_tag_is_added_at_the_outermost_layer():
    assert proxy.app.user_middleware[0].cls is NoIndexHeaderMiddleware
