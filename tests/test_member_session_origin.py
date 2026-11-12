"""Cookie-authenticated member writes must come from the gateway's own pages."""
from __future__ import annotations

import pytest
from gateway import admin_store

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
    assert len(admin_store.list_apps(path=case.db)) == before + int(allowed)


def test_member_key_write_needs_no_origin(principals):
    case = principals
    case.client.cookies.clear()
    response = case.client.delete("/gateway/v1/requests", headers={
        "Authorization": f"Bearer {case.app_key}", NO_ORIGIN: "1",
    })
    assert response.status_code == 200, response.text
