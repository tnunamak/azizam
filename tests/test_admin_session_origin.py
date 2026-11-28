"""A signed-in browser write must say it came from this gateway's own pages; a bearer key need not.

A cookie is an ambient credential: a page on another site can make the browser send it. So a session write is refused
unless Origin (or Referer, when Origin is absent) names this gateway, or the browser vouches with
``Sec-Fetch-Site: same-origin``. An admin key is not ambient, so it is exempt (gateway/admin_api.py ``_require_admin``).
"""
from __future__ import annotations

import pytest

NO_ORIGIN = "x-test-browser-sends-no-origin"   # tests/conftest.py: the test browser sends nothing
from tests.test_signin_who_can_enter import csrf_for, gateway, mint_admin_key, owner_and_admin  # noqa: F401 - gateway is a fixture

FOREIGN = "https://evil.example"


def _create_app(client, plan_id, name, headers):
    return client.post("/admin/api/apps", headers=headers,
                       json={"name": name, "kind": "service", "access_plan": plan_id})


def _apps(gateway):
    return len(gateway.rows("SELECT id FROM apps"))


@pytest.mark.parametrize("extra", [
    {"Origin": FOREIGN},
    {"Referer": f"{FOREIGN}/page"},
    {NO_ORIGIN: "1"},
], ids=["foreign-origin", "foreign-referer", "no-origin-no-referer"])
def test_a_session_write_that_does_not_come_from_this_gateway_is_refused_and_changes_nothing(gateway, extra):
    owner, _, plan_id, _ = owner_and_admin(gateway)
    before = _apps(gateway)
    refused = _create_app(owner, plan_id, "from-elsewhere", {**csrf_for(owner), **extra})
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "admin_origin_forbidden"
    assert _apps(gateway) == before


@pytest.mark.parametrize("extra", [
    {},                                           # the test browser sends its own base URL as Origin
    {NO_ORIGIN: "1", "Sec-Fetch-Site": "same-origin"},
    {NO_ORIGIN: "1", "Referer": "BASE/admin/"},
], ids=["same-origin", "fetch-metadata", "same-origin-referer"])
def test_a_session_write_from_this_gateway_is_allowed(gateway, extra):
    owner, _, plan_id, _ = owner_and_admin(gateway)
    extra = {key: value.replace("BASE", str(owner.base_url).rstrip("/")) for key, value in extra.items()}
    before = _apps(gateway)
    made = _create_app(owner, plan_id, "from-here", {**csrf_for(owner), **extra})
    assert made.status_code in {200, 201}, made.text
    assert _apps(gateway) == before + 1


def test_a_cross_site_fetch_metadata_header_does_not_vouch_for_a_foreign_origin(gateway):
    owner, _, plan_id, _ = owner_and_admin(gateway)
    refused = _create_app(owner, plan_id, "cross-site", {**csrf_for(owner), "Origin": FOREIGN, "Sec-Fetch-Site": "cross-site"})
    assert refused.status_code == 403, refused.text


def test_a_bearer_admin_key_needs_no_origin(gateway):
    owner, _, plan_id, _ = owner_and_admin(gateway)
    key = mint_admin_key(owner, role="owner", name="script")
    before = _apps(gateway)
    made = _create_app(gateway.client(), plan_id, "from-a-script", {"authorization": f"Bearer {key}"})
    assert made.status_code in {200, 201}, made.text
    assert _apps(gateway) == before + 1


def test_a_read_needs_no_origin(gateway):
    owner, _, _, _ = owner_and_admin(gateway)
    assert owner.get("/admin/api/access-plans", headers={NO_ORIGIN: "1"}).status_code == 200
