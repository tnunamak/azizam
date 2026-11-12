"""Cookie-authenticated member writes must come from the gateway's own pages."""
from __future__ import annotations

import pytest
from gateway import admin_store, config_fold

from tests.test_headless_access import principals  # noqa: F401 - fixture

NO_ORIGIN = "x-test-browser-sends-no-origin"


@pytest.mark.parametrize("extra, allowed", [
    ({"Origin": "https://evil.example"}, False),
    ({NO_ORIGIN: "1"}, False),
    ({}, True),
    ({NO_ORIGIN: "1", "Sec-Fetch-Site": "same-origin"}, True),
    ({NO_ORIGIN: "1", "Referer": "BASE/me/"}, True),
])
def test_member_app_creation_checks_browser_origin(principals, extra, allowed):
    case = principals
    case.client.cookies.clear()
    case.client.cookies.set(*case.member_cookie)
    extra = {key: value.replace("BASE", str(case.client.base_url).rstrip("/")) for key, value in extra.items()}
    before = len(admin_store.list_apps(path=case.db))
    response = case.client.post("/me/api/apps", json={"name": "origin-proof"},
                                headers={**case.member_headers, **extra})
    if allowed:
        assert response.status_code == 201, response.text
    else:
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "member_origin_forbidden"
        assert response.json()["error"]["message"] == "Use your account page on this gateway to make changes."
    assert len(admin_store.list_apps(path=case.db)) == before + int(allowed)


def test_member_key_write_needs_no_origin(principals):
    case = principals
    case.client.cookies.clear()
    response = case.client.delete("/gateway/v1/requests", headers={
        "Authorization": f"Bearer {case.app_key}", NO_ORIGIN: "1",
    })
    assert response.status_code == 200, response.text


def test_member_write_accepts_the_saved_public_address(principals, monkeypatch):
    case = principals
    original = config_fold.instance

    def instance(path):
        assert path == case.db
        return {**original(path), "base_url": "https://account.example"}

    monkeypatch.setattr(config_fold, "instance", instance)
    case.client.cookies.clear()
    case.client.cookies.set(*case.member_cookie)
    response = case.client.post("/me/api/apps", json={"name": "saved-address"},
                                headers={**case.member_headers, "Origin": "https://account.example"})
    assert response.status_code == 201, response.text


def test_member_read_needs_no_origin(principals):
    case = principals
    case.client.cookies.clear()
    case.client.cookies.set(*case.member_cookie)
    assert case.client.get("/me/api/session", headers={NO_ORIGIN: "1"}).status_code == 200
