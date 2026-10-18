"""Round 3 of the secret handling: what the independent re-review of round 2 found.

The cases come from ``Documents/azizam/p1r2-rv-probes`` (``test_rv5.py`` and ``test_rv6.py``). Each section names the
finding it covers. The HTTP cases plant a record as the owner, change it as an administrator, and read the stored
registry back, so they exercise the same routes the page uses.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3

import pytest

from gateway import admin_store, lifecycle_drivers, runtime_secrets, secret_refs, service_registry
from tests.support import canary
from tests.test_admin_secret_sweep import host_isolated  # noqa: F401
from tests.test_secret_canary_matrix import REGISTRY, _etag
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generation(gateway) -> int:
    current = admin_store.get_service_registry_with_generation(path=gateway.path)
    return int(current["operator_settings_generation"]) if current else 0


def write_raw(gateway, document) -> None:
    """Store a registry as it stands, past every route (a record written by hand, or by an older version)."""
    admin_store.write_service_registry_with_audit(
        document, expected_generation=_generation(gateway), updated_by="test", audit_action="test",
        audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None, audit_target_kind=None, audit_target_id=None,
        path=gateway.path)


def empty_registry(gateway) -> None:
    write_raw(gateway, service_registry.normalize_service_registry({"schema_version": 2, "services": {}}))


def stored(gateway) -> dict:
    return admin_store.get_service_registry_with_generation(path=gateway.path)["value"]["services"]


def secret_rows(gateway) -> dict:
    with sqlite3.connect(gateway.path) as conn:
        return {row[0]: row[1] for row in conn.execute("SELECT id, approved_origin FROM provider_secrets")}


def change(client, mutate):
    """Read the registry as ``client`` sees it, let ``mutate`` edit its services, write it back."""
    read = client.get(REGISTRY)
    document = read.json()["service_registry"]
    mutate(document["services"])
    return client.put(REGISTRY, headers={**csrf_for(client), "If-Match": read.headers["etag"]}, json=document)


def error_code(response) -> str:
    return (response.json().get("error") or {}).get("code", "") if response.status_code >= 400 else "OK"


# ---------------------------------------------------------------------------
# M-A: an administrator moves the host a lifecycle driver sends its keys to
# ---------------------------------------------------------------------------

GOOD = "http://good.invalid:9"


def _driver_record(**config):
    return {
        "id": "lu", "name": "lu", "kind": "engine", "enabled": True, "capabilities": ["chat"],
        "config": {"base_url": f"{GOOD}/v1", "host": "good.invalid", "port": 9, "protocol": "openai-compatible",
                   "models": [{"id": "m", "upstream_model": "m"}], **config},
        "lifecycle": {"type": "http", "driver": "http-load-unload",
                      "driver_config": {"load_url": "/load", "unload_url": "/unload", "verify_endpoint": "/v1/model",
                                        "admin_key_env": "R3_DRIVER_ADMIN_KEY", "api_key_env": "R3_DRIVER_API_KEY"}},
    }


def _owner_creates_driver_record(owner):
    read = owner.get(REGISTRY)
    document = read.json()["service_registry"]
    document["services"]["lu"] = _driver_record()
    return owner.put(REGISTRY, headers={**csrf_for(owner), "If-Match": read.headers["etag"]}, json=document)


MOVES = {
    "host": lambda config: config.update(host="evil.invalid"),
    "port": lambda config: config.update(port=4444),
    "scheme": lambda config: config.update(scheme="https"),
    "base_url": lambda config: config.update(base_url="http://evil.invalid:4444/v1"),
    "api_base": lambda config: config.update(api_base="http://evil.invalid:4444/v1"),
    "default_base_url": lambda config: config.update(default_base_url="http://evil.invalid:4444/v1"),
    "url": lambda config: config.update(url="http://evil.invalid:4444"),
    "address": lambda config: config.update(address="http://evil.invalid:4444"),
}


@pytest.mark.parametrize("move", sorted(MOVES))
def test_an_administrator_cannot_move_the_host_a_driver_key_goes_to(gateway, host_isolated, move):
    """Review M-A: the host and port were not in the fingerprint, so an administrator re-aimed the driver's keys."""
    owner, _, _, admin = owner_and_admin(gateway)
    empty_registry(gateway)
    assert _owner_creates_driver_record(owner).status_code == 200
    before = copy.deepcopy(stored(gateway)["lu"]["config"])
    refused = change(admin, lambda services: MOVES[move](services["lu"]["config"]))
    assert refused.status_code == 403 and error_code(refused) == "owner_required", refused.text
    assert stored(gateway)["lu"]["config"] == before


def test_the_owner_can_move_the_driver_host_and_the_key_follows_it(gateway, host_isolated):
    owner, _, _, _ = owner_and_admin(gateway)
    empty_registry(gateway)
    assert _owner_creates_driver_record(owner).status_code == 200
    assert stored(gateway)["lu"]["config"]["driver_credential_origin"] == GOOD
    moved = change(owner, lambda services: services["lu"]["config"].update(host="new.invalid", port=4444))
    assert moved.status_code == 200, moved.text
    assert stored(gateway)["lu"]["config"]["driver_credential_origin"] == "http://new.invalid:4444"


def test_an_administrator_can_still_edit_a_driver_record_without_moving_it(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    empty_registry(gateway)
    assert _owner_creates_driver_record(owner).status_code == 200
    renamed = change(admin, lambda services: services["lu"]["config"].update(note="a note"))
    assert renamed.status_code == 200, renamed.text


def _fingerprint_moves(base: dict, mutate) -> bool:
    other = copy.deepcopy(base)
    mutate(other)
    return service_registry._endpoint_fingerprint(base) != service_registry._endpoint_fingerprint(other)


def test_the_fingerprint_sees_host_port_and_scheme_even_when_a_base_url_is_set():
    base = _driver_record()["config"]
    assert base["base_url"]
    for name in ("host", "port", "scheme"):
        assert _fingerprint_moves(base, MOVES[name]), name


SHAPES = [
    {"host": "good.invalid", "port": 9},
    {"host": "good.invalid", "port": 9, "scheme": "https"},
    {"host": "::1", "port": 9},
    {"base_url": f"{GOOD}/v1"},
    {"base_url": f"{GOOD}/v1", "host": "good.invalid", "port": 9},
    {"api_base": f"{GOOD}/v1"},
    {"host": "good.invalid", "port": 9, "base_url": "http://other.invalid:5/v1"},
    {},
]


@pytest.mark.parametrize("shape", SHAPES, ids=lambda shape: json.dumps(shape, sort_keys=True)[:60])
def test_the_driver_dials_an_address_the_fingerprint_covers(shape):
    """What ``HttpLoadUnloadDriver`` dials moves only when the credential fingerprint moves."""
    dialed = lifecycle_drivers.HttpLoadUnloadDriver._base_url(shape)
    for target in ("http://elsewhere.invalid:7", "https://elsewhere.invalid"):
        for field in ("host", "port", "scheme", "base_url", "api_base", "default_base_url"):
            moved = copy.deepcopy(shape)
            moved[field] = {"host": "elsewhere.invalid", "port": 7, "scheme": "https"}.get(field, f"{target}/v1")
            if lifecycle_drivers.HttpLoadUnloadDriver._base_url(moved) != dialed:
                assert service_registry._endpoint_fingerprint(moved) != service_registry._endpoint_fingerprint(shape), field


class _Recorder:
    """A stand-in for ``httpx.AsyncClient`` that records each request and answers 200."""

    calls: list = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _answer(self, method, url, **kwargs):
        type(self).calls.append((method, url, dict(kwargs.get("headers") or {})))

        class Response:
            status_code = 200
            text = '{"status":"finished"}'

            def json(self):
                return {"id": "wanted"}

            async def aiter_bytes(self):
                yield b'{"status":"finished"}'

        return Response()

    async def post(self, url, **kwargs):
        return await self._answer("POST", url, **kwargs)

    async def get(self, url, **kwargs):
        return await self._answer("GET", url, **kwargs)


def _swap_backend(**config):
    backend = service_registry.runtime_backend_from_service(_driver_record(**config))
    return backend


def _run_swap(monkeypatch, backend):
    import httpx

    _Recorder.calls = []
    monkeypatch.setattr(httpx, "AsyncClient", _Recorder)
    monkeypatch.setenv("R3_DRIVER_ADMIN_KEY", "R3-ADMIN-KEY-VALUE")
    monkeypatch.setenv("R3_DRIVER_API_KEY", "R3-API-KEY-VALUE")
    result = asyncio.run(lifecycle_drivers.HttpLoadUnloadDriver().swap(
        backend, {"model_name": "wanted"}, ctx=lifecycle_drivers.SwapContext(loaded_model="old", target_model="wanted")))
    return result, list(_Recorder.calls)


def test_the_driver_sends_its_keys_to_the_origin_they_are_bound_to(monkeypatch):
    backend = _swap_backend(**{"driver_credential_origin": GOOD})
    result, calls = _run_swap(monkeypatch, backend)
    assert result is True
    assert calls and all(url.startswith(GOOD) for _, url, _ in calls)
    assert any(headers.get("x-admin-key") == "R3-ADMIN-KEY-VALUE" for _, _, headers in calls)


@pytest.mark.parametrize("config", [
    {"host": "evil.invalid", "port": 4444, "driver_credential_origin": GOOD},
    {"host": "evil.invalid", "port": 4444},
    {"driver_credential_origin": GOOD, "port": 4444},
    {"driver_credential_origin": "not a url"},
], ids=["host moved", "never bound, host moved", "port moved", "unreadable origin"])
def test_the_driver_sends_nothing_to_an_origin_its_keys_are_not_bound_to(monkeypatch, config):
    result, calls = _run_swap(monkeypatch, _swap_backend(**config))
    assert result is False
    assert calls == []


def test_a_driver_with_no_key_needs_no_binding(monkeypatch):
    backend = _swap_backend()
    backend["lifecycle"]["driver_config"].pop("admin_key_env")
    backend["lifecycle"]["driver_config"].pop("api_key_env")
    result, calls = _run_swap(monkeypatch, backend)
    assert result is True and calls


def _v11_database(path, registry) -> None:
    admin_store.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version=11")
        conn.execute("DELETE FROM operator_settings WHERE key='service_registry'")
        conn.execute("INSERT INTO operator_settings(key,value_json,updated_at) VALUES ('service_registry',?,0)", (json.dumps(registry),))
        conn.execute("INSERT OR REPLACE INTO operator_settings_generations(setting_key, operator_settings_generation) VALUES ('service_registry', 1)")


def test_the_schema_12_migration_binds_the_driver_keys_of_an_existing_record_once(tmp_path):
    path = tmp_path / "admin.db"
    keyless = _driver_record()
    keyless["lifecycle"]["driver_config"].pop("admin_key_env")
    keyless["lifecycle"]["driver_config"].pop("api_key_env")
    keyless["id"] = keyless["name"] = "keyless"
    _v11_database(path, {"schema_version": 2, "services": {"lu": _driver_record(), "keyless": keyless}})

    admin_store.init_db(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 12
        stored = json.loads(conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0])
        audit = conn.execute("SELECT metadata_json FROM audit_events WHERE action='operator.driver_key_origin.migrate'").fetchall()
    assert stored["services"]["lu"]["config"]["driver_credential_origin"] == GOOD
    assert "driver_credential_origin" not in stored["services"]["keyless"]["config"]
    assert len(audit) == 1 and json.loads(audit[0][0]) == {"services": ["lu"]}
    admin_store.init_db(path)  # a second start changes nothing
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key='service_registry'").fetchone()[0] == 2


# ---------------------------------------------------------------------------
# L1: an administrator detaches a credential and leaves its secret in the store
# ---------------------------------------------------------------------------

ENGINE_ORIGIN = "http://ek.invalid:9"


def _engine_with_stored_key(gateway, owner):
    """An engine record whose key sits in the store, bound to its host (as the owner's save leaves it)."""
    empty_registry(gateway)
    made = owner.post("/admin/api/operator/services", headers=_etag(owner), json={"name": "ek", "base_url": f"{ENGINE_ORIGIN}/v1"})
    assert made.status_code in (200, 201), made.text
    document = admin_store.get_service_registry_with_generation(path=gateway.path)["value"]
    ref = secret_refs.store("R3-ENGINE-KEY-0001", db_path=gateway.path, origin=ENGINE_ORIGIN)
    document["services"]["ek"]["config"]["api_key_secret_id"] = ref
    document["services"]["ek"]["config"]["credential_origin"] = ENGINE_ORIGIN
    document["services"]["ek"]["secret_refs"] = {"api_key": ref}
    write_raw(gateway, document)
    return ref


DETACHES = {
    "pop api_key_secret_id": lambda record: record["config"].pop("api_key_secret_id", None),
    "empty api_key_secret_id": lambda record: record["config"].update(api_key_secret_id=""),
    "null api_key_secret_id": lambda record: record["config"].update(api_key_secret_id=None),
    "pop credential_origin": lambda record: record["config"].pop("credential_origin", None),
    "empty secret_refs": lambda record: record.update(secret_refs={}),
}


@pytest.mark.parametrize("detach", sorted(DETACHES))
def test_an_administrator_cannot_detach_a_credential_from_a_record(gateway, host_isolated, detach):
    owner, _, _, admin = owner_and_admin(gateway)
    ref = _engine_with_stored_key(gateway, owner)
    held = copy.deepcopy(stored(gateway)["ek"])
    refused = change(admin, lambda services: DETACHES[detach](services["ek"]))
    assert refused.status_code == 403 and error_code(refused) == "owner_required", refused.text
    assert stored(gateway)["ek"] == held
    assert ref in secret_rows(gateway)


def test_the_owner_can_detach_a_credential(gateway, host_isolated):
    owner, _, _, _ = owner_and_admin(gateway)
    _engine_with_stored_key(gateway, owner)
    done = change(owner, lambda services: DETACHES["pop api_key_secret_id"](services["ek"]))
    assert done.status_code == 200, done.text
    assert "api_key_secret_id" not in stored(gateway)["ek"]["config"]


def test_a_removed_credential_reference_is_a_binding_change():
    before = {"services": {"ek": {"name": "ek", "config": {"base_url": f"{ENGINE_ORIGIN}/v1", "api_key_secret_id": "sref_" + "1" * 24,
                                                           "credential_origin": ENGINE_ORIGIN}}}}
    after = copy.deepcopy(before)
    del after["services"]["ek"]["config"]["api_key_secret_id"]
    assert "config.api_key_secret_id" in service_registry.credential_binding_changes(before, after)["ek"]
    assert service_registry.credential_binding_changes(before, before) == {}
