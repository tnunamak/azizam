"""Sign-in settings change only through Settings > Sign-in's save.

The older owner routes wrote the provider connection and the sign-in switch on
their own. They skipped the test sign-in and the cut-off for people who lose
access, so they are gone. Azizam is unreleased: no redirect and no shim stand
in for them.
"""
import pytest

from gateway import accounts, admin_store
from tests.support.access import store_plan
from tests.test_signin_settings_api import csrf_for, get_snapshot, owner_client, sg  # noqa: F401  (sg is a fixture)
from tests.test_signin_who_can_enter import gateway  # noqa: F401  (a fixture sg builds on)

LEGACY_PATHS = ("/admin/api/oidc-config", "/admin/api/operator/sign-in")
WRITE_METHODS = ("put", "post", "patch", "delete")


def _ask(client, method, path, headers=None):
    kwargs = {"headers": headers or {}}
    if method in WRITE_METHODS:
        kwargs["json"] = {"enabled": False, "mode": "built-in"}
    return client.request(method.upper(), path, **kwargs)


def _app_key(sg, owner):
    """A key for the data plane, whose catch-all answers an unserved path 404."""
    person = admin_store.upsert_oidc_user("https://id.example/realms/home", "app-person", path=sg.path)
    plan = store_plan("Legacy routes", path=sg.path, engines="all", operations=["*"])
    accounts.change_person(person["id"], "owner", path=sg.path, plan_id=plan["id"])
    headers = csrf_for(owner)
    app = owner.post("/admin/api/apps", headers=headers, json={
        "name": "legacy-routes", "owner_user_id": person["id"], "access_plan": plan["id"]})
    assert app.status_code == 201, app.text
    minted = owner.post("/admin/api/apps/" + app.json()["app"]["id"] + "/keys", headers=headers, json={"label": "k"})
    assert minted.status_code == 201, minted.text
    return minted.json()["credential"]["api_key"]


@pytest.mark.parametrize("path", LEGACY_PATHS)
@pytest.mark.parametrize("method", ("get", *WRITE_METHODS))
def test_the_old_owner_routes_answer_404_or_405(sg, path, method):
    key = _app_key(sg, owner_client(sg))

    response = _ask(sg.client(), method, path, {"authorization": "Bearer " + key})

    assert response.status_code in {404, 405}, (method, path, response.status_code, response.text)


@pytest.mark.parametrize("path", LEGACY_PATHS)
@pytest.mark.parametrize("method", ("get", *WRITE_METHODS))
def test_the_owner_gets_the_answer_a_path_that_never_existed_gets(sg, path, method):
    owner = owner_client(sg)
    headers = {**csrf_for(owner), "If-Match": "0"}

    old = _ask(owner, method, path, headers)
    never = _ask(owner, method, "/admin/api/never-existed-route", headers)

    assert (old.status_code, old.json()) == (never.status_code, never.json())
    assert old.status_code >= 400


def test_a_refused_old_write_changes_nothing(sg):
    owner = owner_client(sg)
    before = get_snapshot(owner)

    for path in LEGACY_PATHS:
        owner.put(path, json={"enabled": False, "mode": "built-in", "client_id": "other"},
                  headers={**csrf_for(owner), "If-Match": before["revision"]})

    assert get_snapshot(owner) == before


def test_no_registered_route_serves_the_old_paths(sg):
    served = {(method, route.path) for route in sg.proxy.app.routes
              for method in (getattr(route, "methods", None) or ()) if route.path in LEGACY_PATHS}
    assert served == set()
