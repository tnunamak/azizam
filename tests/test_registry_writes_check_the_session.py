"""A registry save by a signed-out session writes nothing (decision D41).

The round-4 review paused a registry save after its credential-binding check, committed "Sign out
everywhere", and let it resume: the write succeeded and a stored key was bound to a new host. The
registry writers now check the browser sign-in inside their own transaction (gateway/session_guard.py).

This file finds every route that reaches a registry writer in the running app, requires a probe for
each, and runs the probe with the sign-out committed just before the writer. The probes are the calls
of the write sweep (tests/test_admin_secret_sweep_writes.py).
"""
from __future__ import annotations

import pytest

import proxy
from gateway import accounts, admin_store
from tests.support import call_graph
from tests.support.route_discovery import discover
from tests.test_admin_secret_sweep import _seed_registry, host_isolated  # noqa: F401  (host_isolated is a fixture)
from tests.test_admin_secret_sweep_writes import BUILT, NEEDS_PROVIDER_PREFIX, Ctx, _create_provider, _etag, _fill
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

REGISTRY_WRITERS = {
    "write_service_registry", "write_service_registry_with_audit", "write_service_registry_and_setting_with_audits",
}

# Routes that reach a registry writer and no browser sign-in asks for the write.
NOT_STARTED_BY_A_BROWSER = {
    ("POST", "/admin/api/config/reload"): "re-reads the registry and seeds a missing one at start; no actor, session=None",
}


def routes_reaching_a_registry_writer() -> set[tuple[str, str]]:
    found = set()
    for item in discover(proxy.app):
        if not item.path.startswith(("/admin/api", "/me/api")):
            continue
        for method in item.methods:
            if method in {"HEAD", "OPTIONS", "GET"}:
                continue
            endpoint = getattr(item.route, "endpoint", None)
            if endpoint is not None and {fn.__name__ for fn in call_graph.reach(endpoint, depth=6)} & REGISTRY_WRITERS:
                found.add((method, item.path))
    return found - set(NOT_STARTED_BY_A_BROWSER)


ROUTES = sorted(routes_reaching_a_registry_writer())


def test_the_routes_that_reach_a_registry_writer_are_found_and_each_has_a_probe():
    assert {("PUT", "/admin/api/operator/service-registry"), ("PATCH", "/admin/api/operator/services/{service_id}"),
            ("POST", "/admin/api/operator/services"), ("POST", "/admin/api/providers"),
            ("POST", "/admin/api/extensions/enable"),
            ("POST", "/admin/api/operator/services/{service_id}/secret-entries")} <= set(ROUTES)
    assert [route for route in ROUTES if route not in BUILT] == [], (
        "a route reaches a registry writer and the sweep builds no call for it; add one to BUILT in "
        "tests/test_admin_secret_sweep_writes.py, or list the route in NOT_STARTED_BY_A_BROWSER with the reason")


@pytest.mark.parametrize("method, path", ROUTES, ids=lambda value: str(value))
def test_a_registry_save_after_sign_out_everywhere_writes_nothing(gateway, host_isolated, tmp_path, monkeypatch, method, path):
    owner, owner_id, _, admin = owner_and_admin(gateway)
    ctx = Ctx(gateway, owner, admin, tmp_path, monkeypatch)
    label, _, factory = next(call for call in BUILT[(method, path)] if call[1])
    _seed_registry(gateway)
    if path.startswith(NEEDS_PROVIDER_PREFIX):
        assert _create_provider(ctx).status_code < 300
    kwargs = dict(factory(ctx))
    before = admin_store.get_service_registry_with_generation(path=gateway.path)
    reached = []

    def sign_out_first(original):
        def wrapped(*args, **kw):
            reached.append(original.__name__)
            accounts.revoke_person_sessions(owner_id, path=gateway.path)
            return original(*args, **kw)
        wrapped.__name__ = original.__name__
        return wrapped

    for name in ("write_service_registry_with_audit", "write_service_registry_and_setting_with_audits"):
        monkeypatch.setattr(admin_store, name, sign_out_first(getattr(admin_store, name)))

    headers = {"If-Match": _etag(gateway), **kwargs.pop("headers", {}), **csrf_for(owner)}
    response = owner.request(method, _fill(path), headers=headers, **kwargs)

    assert reached, f"{label}: the probe never reached a registry writer ({response.status_code} {response.text[:200]})"
    assert response.status_code == 401, (label, response.status_code, response.text[:300])
    assert response.json()["error"]["code"] == "login_required"
    after = admin_store.get_service_registry_with_generation(path=gateway.path)
    assert after["operator_settings_generation"] == before["operator_settings_generation"]
    assert after["value"] == before["value"]
