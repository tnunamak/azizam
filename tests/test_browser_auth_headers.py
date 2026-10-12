"""Every browser sign-in and sign-out response is no-store and no-referrer."""
from __future__ import annotations

import pytest

import proxy
from test_exposure_oidc_security import _client, oidc_gateway  # noqa: F401  (fixture)
from test_sec_auth_login import login_claims


def _private(response):
    assert response.headers["cache-control"] == "no-store", response.request.url
    assert response.headers["referrer-policy"] == "no-referrer", response.request.url


def test_oidc_login_callback_and_logout_are_private(oidc_gateway, monkeypatch):  # noqa: F811
    client = _client()
    try:
        _private(client.get("/login?return_to=%2Flibrary%2F", follow_redirects=False))
        failure = client.get("/admin/oidc/callback", headers={"accept": "text/html"}, follow_redirects=False)
        assert failure.headers["location"].startswith("/sign-in/?error=provider")
        _private(failure)
        captured = {}
        original = client.get

        def get(url, **kwargs):
            response = original(url, **kwargs)
            if url == "/admin/oidc/callback":
                captured["callback"] = response
            return response

        client.get = get
        login_claims(monkeypatch, client, subject="headers", email="headers@example.test")
        _private(captured["callback"])
        _private(client.get("/auth/logout-confirm", follow_redirects=False))
        _private(client.post("/logout", follow_redirects=False))
        _private(client.get("/auth/logout-confirm", follow_redirects=False))
    finally:
        client.close()


@pytest.mark.parametrize("path", ["/setup/claim", "/setup/local-owner", "/setup/local-login", "/setup/first-app"])
def test_setup_form_posts_are_private_and_keep_html(path, oidc_gateway):  # noqa: F811
    client = _client()
    try:
        response = client.post(path, data={}, follow_redirects=False)
        _private(response)
        if response.status_code != 303:
            assert response.headers["content-type"].startswith("text/html"), (path, response.status_code)
    finally:
        client.close()


def test_logout_answers_root_paths(oidc_gateway, monkeypatch):  # noqa: F811
    client = _client()
    try:
        assert client.get("/auth/logout-confirm", follow_redirects=False).headers["location"] == "/sign-in/"
        login_claims(monkeypatch, client, subject="logout", email="logout@example.test")
        csrf = proxy._csrf_token_for_oidc_session(
            proxy.decode_session_cookie(client.cookies.get(proxy._browser_admin_cookie_config().name),
                                        signing_key=proxy._browser_admin_session_signing_key()))
        response = client.post("/logout", headers={"accept": "application/json", "x-azizam-csrf": csrf})
        assert response.json() == {"next": "/sign-in/"}
    finally:
        client.close()
