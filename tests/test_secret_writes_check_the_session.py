"""A secret is not stored, replaced or deleted by a signed-out session (decision D41).

A provider key, an engine's key and the secret entries of a record are credentials. The round-5 review left
one limit open: ``put_provider_secret`` replaced a stored key behind a recent confirmation but never asked
whether the sign-in was still live, so a request in flight when the owner signed out everywhere could swap
the owner's provider key. The secret store's two writers now take the sign-in and check it in their own
transaction (gateway/session_guard.py).

This file finds every route that reaches a secret writer, requires a probe for each (or a reason it writes no
secret for a browser), and runs the probe with the sign-out committed just before the secret is written.
"""
from __future__ import annotations

import pytest

import proxy
from gateway import accounts, admin_store, secret_store
from tests.support import call_graph
from tests.support.route_discovery import discover
from tests.test_admin_secret_sweep import S, _seed_registry, host_isolated  # noqa: F401  (host_isolated is a fixture)
from tests.test_admin_secret_sweep_writes import BUILT, NEEDS_PROVIDER_PREFIX, Ctx, _create_provider, _engine_body, _etag, _fill
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

SECRET_WRITERS = {"put_provider_secret", "delete_provider_secret", "add_trusted_address"}


def _pasted_key_engine(c):
    """The engine page saves engine-a with a key pasted into it."""
    body = _engine_body(c, disabled=True)
    body["values"] = {**body["values"], "api_key": "pasted-engine-key-4711"}
    return {"json": body}


def _new_engine_with_pasted_key(c):
    return {"json": {**_engine_body(c, disabled=True), "name": "Pasted", "values": {
        "address": "http://pasted.invalid:9", "api_key": "pasted-engine-key-4711"}}}


def _inline_header_registry(c):
    registry = c.registry_echo()
    registry["services"]["engine-d"]["config"].setdefault("headers", []).append(
        {"name": "X-Inline", "value": "inline-header-value-4711"})
    return {"json": registry}


def _inline_header_left_out(c):
    record = c.left_out_fixed("held-url")
    record["config"].setdefault("headers", []).append({"name": "X-Inline", "value": "inline-header-value-4711"})
    return {"json": {"record": record}}


# Route -> a call that stores, replaces or deletes a secret on behalf of the browser sign-in.
PROBES = {
    ("POST", "/admin/api/providers"): BUILT[("POST", "/admin/api/providers")][0][2],
    ("PUT", "/admin/api/providers/{provider_id}/key"): BUILT[("PUT", "/admin/api/providers/{provider_id}/key")][0][2],
    ("DELETE", "/admin/api/providers/{provider_id}/key"): BUILT[("DELETE", "/admin/api/providers/{provider_id}/key")][0][2],
    ("POST", "/admin/api/operator/services"): _new_engine_with_pasted_key,
    ("PATCH", "/admin/api/operator/services/{service_id}"): _pasted_key_engine,
    ("PUT", "/admin/api/operator/service-registry"): _inline_header_registry,
    ("PUT", "/admin/api/operator/service-registry/excluded/{section}/{record_id}"): _inline_header_left_out,
    ("POST", "/admin/api/operator/services/{service_id}/secret-entries"):
        BUILT[("POST", "/admin/api/operator/services/{service_id}/secret-entries")][0][2],
    ("POST", "/admin/api/extensions/enable"): BUILT[("POST", "/admin/api/extensions/enable")][0][2],
}

# Routes that reach a secret writer and store nothing for a browser: what they delete is clean-up of a secret
# the same request stored, or of a record a guarded registry save has just removed. That clean-up must run
# even when the sign-in has ended, so it carries no sign-in. The registry write that goes with each is raced in
# tests/test_registry_writes_check_the_session.py.
CLEAN_UP_ONLY = {
    ("DELETE", "/admin/api/extensions/{package_id}"): "deletes the secrets of records the guarded save removed",
    ("DELETE", "/admin/api/operator/service-registry/excluded/{section}/{record_id}"): "deletes the secrets of the record the guarded save dropped",
    ("DELETE", "/admin/api/operator/services/{service_id}"): "deletes the secrets of the record the guarded save removed",
    ("DELETE", "/admin/api/providers/{provider_id}"): "deletes the removed provider's key after the guarded save",
    ("POST", "/admin/api/operator/services/{service_id}/enabled"): "a switch; it stores no secret",
    ("PUT", "/admin/api/providers/{provider_id}/models"): "a model list; it stores no secret",
}

# Routes with their own race below (not a registry or secret-store probe).
OWN_RACE = {
    ("POST", "/admin/api/apps/{app_id}/trusted-addresses"): "a trusted address lets a device in with no key",
    ("PUT", "/admin/api/signin"): "the identity provider's issuer and client secret (tests/test_signin_settings_api.py)",
}


def routes_reaching_a_secret_writer() -> set[tuple[str, str]]:
    found = set()
    for item in discover(proxy.app):
        if not item.path.startswith(("/admin/api", "/me/api")):
            continue
        endpoint = getattr(item.route, "endpoint", None)
        if endpoint is None:
            continue
        for method in item.methods:
            if method not in {"HEAD", "OPTIONS", "GET"} and {fn.__name__ for fn in call_graph.reach(endpoint, depth=7)} & SECRET_WRITERS:
                found.add((method, item.path))
    return found


def test_every_route_that_reaches_a_secret_writer_has_a_probe_a_reason_or_its_own_race():
    found = routes_reaching_a_secret_writer()
    listed = set(PROBES) | set(CLEAN_UP_ONLY) | set(OWN_RACE)

    assert sorted(found - listed) == [], (
        "a route reaches a secret writer with no probe: add one to PROBES, or list it with the reason it stores "
        "no secret for a browser")
    assert sorted(listed - found) == [], "a listed route no longer reaches a secret writer"


def _secrets(gateway):
    try:
        return gateway.rows("SELECT id, ciphertext_b64 FROM provider_secrets ORDER BY id")
    except Exception:  # noqa: BLE001 - no secret was ever stored
        return []


@pytest.mark.parametrize("method, path", sorted(PROBES), ids=lambda value: str(value))
def test_a_secret_write_after_sign_out_everywhere_stores_nothing(gateway, host_isolated, tmp_path, monkeypatch, method, path):
    owner, owner_id, _, admin = owner_and_admin(gateway)
    ctx = Ctx(gateway, owner, admin, tmp_path, monkeypatch)
    _seed_registry(gateway)
    if path.startswith(NEEDS_PROVIDER_PREFIX):
        assert _create_provider(ctx).status_code < 300
    kwargs = dict(PROBES[(method, path)](ctx))
    secrets_before = _secrets(gateway)
    registry_before = admin_store.get_service_registry_with_generation(path=gateway.path)
    reached: list[str] = []

    def sign_out_first(original):
        def wrapped(*args, **kw):
            if kw.get("session") is not None:       # a write a browser sign-in asked for; clean-up carries none
                reached.append(original.__name__)
                accounts.revoke_person_sessions(owner_id, path=gateway.path)
            return original(*args, **kw)
        wrapped.__name__ = original.__name__
        return wrapped

    for name in ("put_provider_secret", "delete_provider_secret"):
        monkeypatch.setattr(secret_store, name, sign_out_first(getattr(secret_store, name)))

    headers = {"If-Match": _etag(gateway), **kwargs.pop("headers", {}), **csrf_for(owner)}
    response = owner.request(method, _fill(path), headers=headers, **kwargs)

    assert reached, f"{path}: the probe never made a secret write for the sign-in ({response.status_code} {response.text[:300]})"
    assert response.status_code == 401, (method, path, response.status_code, response.text[:300])
    assert response.json()["error"]["code"] == "login_required"
    assert _secrets(gateway) == secrets_before
    after = admin_store.get_service_registry_with_generation(path=gateway.path)
    assert after["operator_settings_generation"] == registry_before["operator_settings_generation"]


def test_a_provider_key_written_before_a_refused_save_is_cleaned_up(gateway, host_isolated, tmp_path, monkeypatch):
    """The sign-out lands after the key is stored and before the registry save: the key must not be left behind."""
    owner, owner_id, _, admin = owner_and_admin(gateway)
    ctx = Ctx(gateway, owner, admin, tmp_path, monkeypatch)
    _seed_registry(gateway)
    before = _secrets(gateway)
    original = admin_store.write_service_registry_with_audit

    def sign_out_then_save(*args, **kw):
        accounts.revoke_person_sessions(owner_id, path=gateway.path)
        return original(*args, **kw)

    monkeypatch.setattr(admin_store, "write_service_registry_with_audit", sign_out_then_save)

    response = _create_provider(ctx)

    assert response.status_code == 401, response.text
    assert _secrets(gateway) == before


def test_a_trusted_address_is_not_added_after_sign_out_everywhere(gateway, monkeypatch):
    owner, owner_id, _, _ = owner_and_admin(gateway)
    app = owner.post("/admin/api/apps", headers=csrf_for(owner), json={"name": "device app", "kind": "service", "access_plan": "person"})
    assert app.status_code == 201, app.text
    app_id = app.json()["app"]["id"]
    original = admin_store.add_trusted_address

    def sign_out_then_add(*args, **kw):
        accounts.revoke_person_sessions(owner_id, path=gateway.path)
        return original(*args, **kw)

    monkeypatch.setattr(admin_store, "add_trusted_address", sign_out_then_add)

    response = owner.post(f"/admin/api/apps/{app_id}/trusted-addresses", headers=csrf_for(owner), json={"address": "10.1.2.3"})

    assert response.status_code == 401, response.text
    assert gateway.rows("SELECT * FROM app_trusted_addresses") == []
