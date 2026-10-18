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
        try:
            return {row[0]: row[1] for row in conn.execute("SELECT id, approved_origin FROM provider_secrets")}
        except sqlite3.OperationalError:  # no secret was ever stored here
            return {}


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


# ---------------------------------------------------------------------------
# L4: the credentials a migration moved out of a URL are put back where the URL is used
# ---------------------------------------------------------------------------

import httpx  # noqa: E402

import proxy  # noqa: E402
from gateway import decision_routing, record_carriers  # noqa: E402


@pytest.fixture
def store(tmp_path):
    """A real secret store in a temporary database, which the runtime resolves from."""
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    runtime_secrets.configure(path)
    yield path
    runtime_secrets.configure(None)


def _moved(record, db):
    moved, paths = record_carriers.externalize_service(record, secret_refs.putter(db))
    assert paths
    return moved


def test_the_swap_requests_carry_the_query_credentials_stored_for_load_unload_and_verify(store, monkeypatch):
    record = _driver_record()
    record["lifecycle"]["driver_config"].update(
        load_url="/load?token=R3LOADTOKEN", unload_url="/unload?token=R3UNLOADTOKEN", verify_endpoint="/v1/model?sig=R3VERIFYSIG")
    for name in ("admin_key_env", "api_key_env"):
        record["lifecycle"]["driver_config"].pop(name)
    moved = _moved(record, store)
    assert not [seed for seed in ("R3LOADTOKEN", "R3UNLOADTOKEN", "R3VERIFYSIG") if seed in json.dumps(moved)]
    result, calls = _run_swap(monkeypatch, service_registry.runtime_backend_from_service(moved))
    assert result is True
    urls = [url for _, url, _ in calls]
    assert any(url.endswith("/load?token=R3LOADTOKEN") for url in urls), urls
    assert any(url.endswith("/unload?token=R3UNLOADTOKEN") for url in urls), urls


def test_an_unreadable_stored_part_sends_nothing_and_never_falls_back_to_the_default_path(store, monkeypatch):
    record = _driver_record()
    record["lifecycle"]["driver_config"].update(load_url="/load?token=R3LOADTOKEN")
    for name in ("admin_key_env", "api_key_env"):
        record["lifecycle"]["driver_config"].pop(name)
    moved = _moved(record, store)
    secret_refs.delete(record_carriers.refs_in(moved["lifecycle"]["driver_config"]["load_url_entries"])[0], db_path=store)
    result, calls = _run_swap(monkeypatch, service_registry.runtime_backend_from_service(moved))
    assert result is False and calls == []


def _answering(monkeypatch, *, client="AsyncClient"):
    seen: list[str] = []

    class Client(_Recorder):
        async def _answer(self, method, url, **kwargs):
            seen.append(str(url))
            return await super()._answer(method, url, **kwargs)

    monkeypatch.setattr(httpx, client, Client)
    return seen


def test_the_readiness_poll_carries_the_query_credentials_stored_for_the_health_endpoint(store, monkeypatch):
    record = {"id": "e", "config": {"host": "good.invalid", "port": 9, "health_endpoint": "/ready?token=R3HEALTHTOKEN"},
              "lifecycle": {"driver": "external"}}
    backend = service_registry.runtime_backend_from_service(_moved(record, store))
    assert "R3HEALTHTOKEN" not in json.dumps(backend)
    seen = _answering(monkeypatch)
    assert asyncio.run(lifecycle_drivers.ExternalDriver().ensure_ready(backend, 1.0)) is True
    assert seen == ["http://good.invalid:9/ready?token=R3HEALTHTOKEN"]


def test_the_resource_probe_url_carries_the_user_info_and_query_stored_for_the_health_endpoint(store):
    record = {"id": "e", "config": {"host": "good.invalid", "port": 9, "health_endpoint": "http://user:R3PASS@good.invalid:9/health?k=R3QUERY"}}
    moved = _moved(record, store)
    assert "R3PASS" not in json.dumps(moved) and "R3QUERY" not in json.dumps(moved)
    assert proxy._service_resource_probe_url(moved["config"]) == "http://user:R3PASS@good.invalid:9/health?k=R3QUERY"
    relative = _moved({"id": "e", "config": {"host": "good.invalid", "port": 9, "health_endpoint": "/h?k=R3REL"}}, store)
    assert proxy._service_resource_probe_url(relative["config"]) == "http://good.invalid:9/h?k=R3REL"
    based = _moved({"id": "e", "config": {"base_url": "http://good.invalid:9/v1?key=R3BASE", "health_endpoint": "/health"}}, store)
    assert "R3BASE" in proxy._service_resource_probe_url(based["config"])


def test_the_resource_probe_url_is_empty_when_a_stored_part_cannot_be_read(store):
    moved = _moved({"id": "e", "config": {"host": "good.invalid", "port": 9, "health_endpoint": "/h?k=R3GONE"}}, store)
    secret_refs.delete(record_carriers.refs_in(moved["config"]["health_endpoint_entries"])[0], db_path=store)
    assert proxy._service_resource_probe_url(moved["config"]) == ""


def test_the_models_catalog_probe_and_the_context_window_probe_carry_their_stored_credentials(store, monkeypatch):
    record = {"id": "e", "config": {"host": "good.invalid", "port": 9, "scheme": "http", "health_endpoint": "/ready?token=R3CATALOG",
                                    "context_window_probe": {"endpoint": "/props?token=R3CONTEXT"}}}
    moved = _moved(record, store)
    assert proxy._runtime_health_endpoint(moved) == "http://good.invalid:9/ready?token=R3CATALOG"
    monkeypatch.setitem(proxy.config, "backends", {"e": moved["config"]})
    assert proxy._context_window_probe_endpoint("e") == "/props?token=R3CONTEXT"
    secret_refs.delete(record_carriers.refs_in(moved["config"]["health_endpoint_entries"])[0], db_path=store)
    assert proxy._runtime_health_endpoint(moved) is None


def test_the_placement_probe_carries_the_query_credentials_stored_for_the_health_endpoint(store, monkeypatch):
    moved = _moved({"id": "e", "config": {"host": "good.invalid", "port": 9, "health_endpoint": "/placement?token=R3PLACE"}}, store)
    seen: list[str] = []

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, **kwargs):
            seen.append(url)
            raise httpx.ConnectError("no engine runs in this test")

    monkeypatch.setattr(httpx, "Client", Client)
    runtime = decision_routing.DecisionRuntime(lambda: {"backends": {"e": moved["config"]}}, lambda *a, **k: None, lambda *a, **k: None)
    runtime.observed_engine("e", fresh=True)
    assert seen == ["http://good.invalid:9/placement?token=R3PLACE"]


def test_the_observation_a_driver_health_endpoint_becomes_keeps_its_stored_query(store):
    record = {"id": "e", "config": {"host": "good.invalid", "port": 9}, "lifecycle": {
        "type": "http", "driver": "http-load-unload", "driver_config": {"load_url": "/load", "health_endpoint": "/ready?token=R3OBSERVED"}}}
    moved = _moved(record, store)
    v2 = service_registry.service_record_v2(moved, public=False)
    http = v2["observations"]["process_ready"]["http"]
    assert http["path"] == "/ready" and runtime_secrets.url(http, "path") == "/ready?token=R3OBSERVED"
    assert "path_entries" not in service_registry.service_record_v2(moved)["observations"]["process_ready"]["http"]


@pytest.mark.parametrize("value", [{"a": 1}, [1, 2], {"value": {"a": "R3SECRET"}}, ["R3SECRET"]])
def test_a_write_refuses_a_mapping_or_a_list_as_an_environment_value(store, value):
    record = {"id": "e", "config": {"host": "good.invalid", "port": 9}, "process": {"driver": "command", "env": {"MODE": value}}}
    put = lambda text, origin=None: pytest.fail("a refused value must not be stored")  # noqa: E731
    with pytest.raises(record_carriers.ValueRefused) as refused:
        record_carriers.externalize_service(record, put, reject_ambiguous=True)
    assert "R3SECRET" not in str(refused.value)


def test_a_migration_still_keeps_a_stored_structured_environment_value_as_a_secret_never_inline(store):
    record = {"id": "e", "config": {"host": "good.invalid", "port": 9}, "process": {"driver": "command", "env": {"MODE": {"a": "R3SECRET"}}}}
    moved = _moved(record, store)
    assert "R3SECRET" not in json.dumps(moved)


# ---------------------------------------------------------------------------
# L5: a read's view of an entry written back, and runtime lookups that fail without a KeyError
# ---------------------------------------------------------------------------

VIEW = {"id": "ent_" + "1" * 16, "configured": True}


def _never_put(value, origin=None):
    pytest.fail("a refused write must not store anything")


VIEW_WRITES = {
    "env": {"id": "e", "config": {"host": "h.invalid", "port": 9, "env": {"TOKEN": VIEW}}},
    "header row": {"id": "e", "config": {"host": "h.invalid", "port": 9, "headers": [{**VIEW, "name": "X-Key"}]}},
    "argv item": {"id": "e", "config": {"host": "h.invalid", "port": 9}, "process": {"driver": "command", "start": ["run", VIEW]}},
    "scalar": {"id": "e", "config": {"host": "h.invalid", "port": 9}, "lifecycle": {"driver": "http-load-unload", "admin_key": VIEW}},
    "userinfo": {"id": "e", "config": {"host": "h.invalid", "port": 9, "base_url": "http://h.invalid:9/v1", "base_url_entries": {"userinfo": VIEW}}},
    "whole": {"id": "e", "config": {"host": "h.invalid", "port": 9, "base_url": "", "base_url_entries": {"whole": VIEW}}},
    "fragment": {"id": "e", "config": {"host": "h.invalid", "port": 9, "base_url": "http://h.invalid:9/v1", "base_url_entries": {"fragment": VIEW}}},
    "query chunk": {"id": "e", "config": {"host": "h.invalid", "port": 9, "base_url": "http://h.invalid:9/v1",
                                          "base_url_entries": {"query": [{**VIEW, "name": "key"}]}}},
    "unknown part": {"id": "e", "config": {"host": "h.invalid", "port": 9, "base_url": "http://h.invalid:9/v1", "base_url_entries": {"extra": 1}}},
    "entries not a mapping": {"id": "e", "config": {"host": "h.invalid", "port": 9, "base_url": "http://h.invalid:9/v1", "base_url_entries": ["x"]}},
}


@pytest.mark.parametrize("name", sorted(VIEW_WRITES))
def test_a_write_refuses_a_read_s_view_of_an_entry_and_stores_nothing(name):
    with pytest.raises(record_carriers.EntryRefused) as refused:
        record_carriers.ingest_service(copy.deepcopy(VIEW_WRITES[name]), None, _never_put)
    assert VIEW["id"] not in str(refused.value)


def test_a_write_accepts_the_entry_shapes_the_gateway_itself_stores():
    ref = "sref_" + "a" * 24
    record = {"id": "e", "config": {"host": "h.invalid", "port": 9, "base_url": "http://h.invalid:9/v1", "base_url_entries": {
        "userinfo": {"id": "ent_" + "2" * 16, "secret_ref": ref},
        "query": [{"id": "ent_" + "3" * 16, "name": "key", "secret_ref": ref}, {"id": "ent_" + "4" * 16, "name": "x", "empty": True},
                  {"id": "ent_" + "5" * 16, "gap": True}],
        "fragment": {"id": "ent_" + "6" * 16, "empty": True}}}}
    written, _ = record_carriers.ingest_service(copy.deepcopy(record), None, _never_put)
    assert written["config"]["base_url_entries"] == record["config"]["base_url_entries"]


def test_an_administrator_cannot_save_a_view_as_the_url_credentials_of_a_record(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    empty_registry(gateway)
    made = owner.post("/admin/api/operator/services", headers=_etag(owner), json={"name": "nb", "base_url": "http://nb.invalid:9/v1"})
    assert made.status_code == 201, made.text
    held = copy.deepcopy(stored(gateway)["nb"])
    for entries in ({"userinfo": VIEW}, {"whole": VIEW}):
        refused = change(admin, lambda services, entries=entries: services["nb"]["config"].update(base_url_entries=entries))
        assert refused.status_code == 400, refused.text
        assert VIEW["id"] not in refused.text
        assert stored(gateway)["nb"] == held


def test_a_view_written_as_an_environment_value_does_not_become_a_secret(gateway, host_isolated):
    owner, _, _, _ = owner_and_admin(gateway)
    empty_registry(gateway)
    made = owner.post("/admin/api/operator/services", headers=_etag(owner), json={"name": "ev", "base_url": "http://ev.invalid:9/v1"})
    assert made.status_code == 201, made.text
    before = secret_rows(gateway)
    refused = change(owner, lambda services: services["ev"]["config"].update(env={"TOKEN": VIEW}))
    assert refused.status_code == 400, refused.text
    assert secret_rows(gateway) == before


MALFORMED_STORED = {
    "userinfo view": {"userinfo": VIEW},
    "whole view": {"whole": VIEW},
    "whole not a mapping": {"whole": True},
    "userinfo not a mapping": {"userinfo": "x"},
    "fragment view": {"fragment": VIEW},
    "query chunk view": {"query": [{**VIEW, "name": "key"}]},
    "query chunk not a mapping": {"query": ["x"]},
}


@pytest.mark.parametrize("name", sorted(MALFORMED_STORED))
def test_a_stored_entry_that_cannot_be_resolved_raises_secret_unavailable_and_never_a_key_error(store, name):
    container = {"base_url": "http://h.invalid:9/v1", "base_url_entries": MALFORMED_STORED[name]}
    with pytest.raises(runtime_secrets.SecretUnavailable):
        runtime_secrets.url(container, "base_url")
    with pytest.raises(runtime_secrets.SecretUnavailable):
        runtime_secrets.service_url({"base_url": "http://h.invalid:9/v1", "base_url_entries": MALFORMED_STORED[name]})


def test_a_header_row_without_a_reference_is_left_out_by_the_router_config(store):
    from gateway import litellm_runtime

    assert litellm_runtime._header({"id": "ent_" + "1" * 16, "name": "X"}, None) is None


def test_a_secret_whose_stored_bytes_are_damaged_is_unavailable_and_not_a_crypto_error(store):
    ref = secret_refs.store("R3-DAMAGED", db_path=store)
    with sqlite3.connect(store) as conn:
        conn.execute("UPDATE provider_secrets SET ciphertext_b64 = ? WHERE id = ?", ("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", ref))
    with pytest.raises(runtime_secrets.SecretUnavailable) as raised:
        runtime_secrets.resolve(ref)
    assert "R3-DAMAGED" not in str(raised.value) and ref not in str(raised.value)
    with pytest.raises(runtime_secrets.SecretUnavailable):
        runtime_secrets.resolve("sref_" + "0" * 24)


# ---------------------------------------------------------------------------
# L6: package install proofs on a live-shaped registry that holds no secret
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

from gateway import extension_packages, record_integrity  # noqa: E402
from gateway.admin_api import AdminContext  # noqa: E402
from gateway.admin_runtime import AdminSQLiteStore  # noqa: E402
from tests.test_secret_externalize import FIXTURE, _v10_database  # noqa: E402

PACKAGE_PROVENANCE = {"package_id": "pk", "package_hash": "h1", "package_version": "1"}


def _live_shaped_package_database(path: Path):
    """The live registry with two records a package installed (one since changed by the operator) and a bare-digest proof."""
    registry = json.loads(FIXTURE.read_text())
    unchanged, changed = list(registry["services"])[:2]
    for service_id in (unchanged, changed):
        registry["services"][service_id]["provenance"] = dict(PACKAGE_PROVENANCE)
    installed = {"services": {service_id: copy.deepcopy(registry["services"][service_id]) for service_id in (unchanged, changed)}}
    package = {"id": "pk", "name": "pk", "version": "1", "enabled": True, "source": {"type": "local_directory", "path": "/x"},
               "services": [unchanged, changed], "package_hash": "h1", **extension_packages._legacy_digest_proof(installed)}
    registry["services"][changed]["name"] = "changed by the operator"
    _v10_database(path, registry, {"schema_version": 1, "sources": [], "packages": {"pk": package}, "updated_at": 1})
    return registry, unchanged, changed


def _package(path: Path) -> dict:
    with sqlite3.connect(path) as conn:
        return json.loads(conn.execute("SELECT value_json FROM operator_settings WHERE key='extension_package_index'").fetchone()[0])["packages"]["pk"]


def test_a_live_shaped_registry_keeps_its_records_and_gets_keyed_proofs_that_still_tell_changed_from_unchanged(tmp_path):
    path = tmp_path / "admin.db"
    registry, unchanged, changed = _live_shaped_package_database(path)
    before = json.dumps(registry)

    admin_store.init_db(path)

    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0] == before, "the registry is not touched"
        audit = conn.execute("SELECT metadata_json FROM audit_events WHERE action='operator.secrets.externalize'").fetchall()
    assert [json.loads(row[0]) for row in audit] == [{"package_proofs": 1}]
    package = _package(path)
    assert package["installed_records_version"] == extension_packages.INSTALLED_RECORDS_VERSION
    entries = package["installed_records"]["services"]
    assert all("sha256" not in entry and set(entry) == {"kind", "id", "mac"} for entry in entries.values())
    key = record_integrity.install_key(path)
    stored = json.loads(before)["services"]
    assert entries[unchanged]["mac"] and extension_packages._matches_install(stored[unchanged], entries[unchanged], package, None, key)
    assert entries[changed]["mac"] is None and not extension_packages._matches_install(stored[changed], entries[changed], package, None, key)


def test_the_proof_migration_is_idempotent_on_a_live_shaped_registry(tmp_path):
    path = tmp_path / "admin.db"
    _live_shaped_package_database(path)
    admin_store.init_db(path)
    with sqlite3.connect(path) as conn:
        first = [conn.execute("SELECT value_json FROM operator_settings WHERE key=?", (key,)).fetchone()[0]
                 for key in ("service_registry", "extension_package_index")]
        generation = conn.execute("SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key='extension_package_index'").fetchone()[0]

    admin_store.init_db(path)

    with sqlite3.connect(path) as conn:
        again = [conn.execute("SELECT value_json FROM operator_settings WHERE key=?", (key,)).fetchone()[0]
                 for key in ("service_registry", "extension_package_index")]
        assert conn.execute("SELECT operator_settings_generation FROM operator_settings_generations WHERE setting_key='extension_package_index'").fetchone()[0] == generation
        assert conn.execute("SELECT count(*) FROM audit_events WHERE action='operator.secrets.externalize'").fetchone()[0] == 1
    assert again == first


def test_removing_the_package_removes_the_unchanged_record_and_detaches_the_changed_one(tmp_path):
    path = tmp_path / "admin.db"
    _, unchanged, changed = _live_shaped_package_database(path)
    admin_store.init_db(path)

    removed = AdminSQLiteStore(path).remove_extension_package(
        package_id="pk", actor=AdminContext(subject="owner", roles=frozenset({"owner"}), actor_kind="user"))

    assert {item["id"]: item["action"] for item in removed["affected_records"]} == {unchanged: "removed", changed: "detached"}


# ---------------------------------------------------------------------------
# L2: the driver's API key is a declared carrier like its admin key
# ---------------------------------------------------------------------------


def _stored_api_key_record(db, key="R3-LIFECYCLE-API-KEY"):
    record = _driver_record(driver_credential_origin=GOOD)
    record["lifecycle"]["driver_config"].pop("admin_key_env")
    record["lifecycle"]["driver_config"].pop("api_key_env")
    record["lifecycle"]["api_key"] = key
    return _moved(record, db)


def test_a_driver_api_key_is_moved_into_the_store_and_shown_as_configured(store):
    from gateway import projection, secret_commands

    moved = _stored_api_key_record(store)
    assert secret_refs.is_secret_entry(moved["lifecycle"]["api_key"])
    assert "R3-LIFECYCLE-API-KEY" not in json.dumps(moved)
    assert secret_commands.carrier_kind("lifecycle.api_key") == "secret"
    shown = projection.project("ServiceRecord", moved).tree
    assert shown["lifecycle"]["api_key"]["configured"] is True and "R3-LIFECYCLE-API-KEY" not in json.dumps(shown)
    assert "api_key" not in (shown["lifecycle"].get("hidden") or [])


def test_the_driver_sends_its_stored_api_key_with_the_verify_request_to_its_bound_origin(store, monkeypatch):
    backend = service_registry.runtime_backend_from_service(_stored_api_key_record(store))
    seen = _answering(monkeypatch)
    calls = _Recorder.calls = []
    result = asyncio.run(lifecycle_drivers.HttpLoadUnloadDriver().swap(backend, {"model_name": "wanted"}))
    assert result is True and seen and seen[0].startswith(GOOD)
    assert calls and calls[0][2].get("x-api-key") == "R3-LIFECYCLE-API-KEY"


def test_the_driver_sends_no_stored_api_key_to_an_origin_it_is_not_bound_to(store, monkeypatch):
    moved = _stored_api_key_record(store)
    moved["config"]["host"] = "evil.invalid"
    backend = service_registry.runtime_backend_from_service(moved)
    seen = _answering(monkeypatch)
    assert asyncio.run(lifecycle_drivers.HttpLoadUnloadDriver().swap(backend, {"model_name": "wanted"})) is False
    assert seen == []


def test_a_number_written_as_a_driver_key_is_moved_like_text(store):
    record = _driver_record()
    record["lifecycle"]["admin_key"] = 123456
    moved = _moved(record, store)
    assert secret_refs.is_secret_entry(moved["lifecycle"]["admin_key"]) and "123456" not in json.dumps(moved)


# ---------------------------------------------------------------------------
# L3: ;name=value path parameters are credentials like query values; a whole URL can be kept as one secret
# ---------------------------------------------------------------------------

from gateway import projection, secret_commands  # noqa: E402

PARAM_URLS = [
    "http://h.invalid:9/v1;token=R3PARAM", "http://h.invalid:9/a;x=R3ONE;y=R3TWO/b;z=R3THREE", "http://h.invalid:9/v1;",
    "http://h.invalid:9/v1;;a=R3GAP", "http://h.invalid:9/v1;R3BARE", "http://h.invalid:9/v1;token=", "http://h.invalid:9/v1;a=R3A?c=d#e",
    "/health;k=R3REL", "http://u:R3PW@h.invalid:9/v1;k=R3V?q=R3Q", "http://h.invalid:9/v1;=R3NONAME", "http://h.invalid:9/v1;a=R3B=R3C",
    "h.invalid:9/v1;k=R3SCHEMELESS", "http://h.invalid:9/v1;k=R3V#frag;x=R3FRAG", "http://[::1]:9/v1;k=R3V6",
]


class _MemoryStore:
    def __init__(self, monkeypatch):
        self.values: dict[str, str] = {}
        monkeypatch.setattr(runtime_secrets, "resolve", lambda ref, *, origin=None: self.values[ref])

    def put(self, value, origin=None):
        ref = secret_refs.new_ref()
        self.values[ref] = value
        return ref


@pytest.mark.parametrize("url", PARAM_URLS)
def test_a_url_with_path_parameters_is_stored_without_them_and_read_back_byte_for_byte(monkeypatch, url):
    memory = _MemoryStore(monkeypatch)
    container = {"base_url": url}
    assert record_carriers.externalize_url(container, "base_url", memory.put) is True
    seeds = [text for text in ("R3PARAM", "R3ONE", "R3TWO", "R3GAP", "R3BARE", "R3A", "R3REL", "R3V", "R3NONAME", "R3B", "R3C", "R3SCHEMELESS", "R3FRAG", "R3V6") if text in url]
    assert not [seed for seed in seeds if seed in json.dumps(container)], container
    assert runtime_secrets.url(container, "base_url") == url
    again = copy.deepcopy(container)
    assert record_carriers.externalize_url(again, "base_url", memory.put) is False and again == container
    assert not [seed for seed in seeds if seed in projection.public_url(container["base_url"])]


def test_a_legacy_inline_path_parameter_is_cut_from_the_public_form_of_a_url():
    assert ";R3SHOWN" not in projection.public_url("http://h.invalid:9/v1;token=R3SHOWN")
    assert "R3SHOWN" not in projection.public_url("/health;k=R3SHOWN?x=1")


def test_new_path_parameters_replace_the_old_ones_when_the_url_changes(monkeypatch):
    memory = _MemoryStore(monkeypatch)
    container = {"base_url": "http://h.invalid:9/v1;token=R3OLD"}
    record_carriers.externalize_url(container, "base_url", memory.put)
    container["base_url"] = "http://h.invalid:9/v2;token=R3NEW;other=R3OTHER"
    record_carriers.externalize_url(container, "base_url", memory.put)
    assert "R3NEW" not in json.dumps(container) and "R3OTHER" not in json.dumps(container)
    assert runtime_secrets.url(container, "base_url") == "http://h.invalid:9/v2;token=R3NEW;other=R3OTHER"


def test_a_path_parameter_never_reaches_a_response_or_the_store_in_clear(gateway, host_isolated):
    owner, _, _, _ = owner_and_admin(gateway)
    empty_registry(gateway)
    made = owner.post("/admin/api/operator/services", headers=_etag(owner),
                      json={"name": "pp", "base_url": "http://pp.invalid:9/v1;token=R3ROUTE"})
    assert made.status_code == 201, made.text
    assert "R3ROUTE" not in made.text and "R3ROUTE" not in owner.get(REGISTRY).text
    assert "R3ROUTE" not in json.dumps(stored(gateway))


WHOLE = "https://hooks.invalid/services/T0/B0/R3WEBHOOKSECRET"


def _whole(db, **command):
    record = {"id": "e", "name": "e", "kind": "engine", "enabled": True, "capabilities": ["chat"],
              "config": {"host": "hooks.invalid", "port": 443, "protocol": "openai-compatible", "base_url": "https://hooks.invalid/v1"}}
    put = secret_refs.putter(db)
    commands = secret_commands.parse_commands({"commands": [{"carrier": "config.base_url", "operation": "set_credentials", **command}]})
    return secret_commands.apply_commands(record, commands, put)


def test_a_whole_url_is_kept_as_one_secret_with_a_public_label(store):
    record, outcome = _whole(store, url=WHOLE, whole=True, label="Alert webhook")
    config = record["config"]
    assert config["base_url"] == "" and len(outcome.created) == 1
    assert "R3WEBHOOKSECRET" not in json.dumps(record)
    assert config["base_url_entries"]["whole"]["label"] == "Alert webhook"
    assert runtime_secrets.url(config, "base_url") == WHOLE
    shown = projection.project("ServiceRecord", record).tree["config"]["base_url_entries"]["whole"]
    assert shown["label"] == "Alert webhook" and shown["configured"] is True and "R3WEBHOOKSECRET" not in json.dumps(shown)


def test_a_replaced_whole_url_retires_the_secret_it_replaces(store):
    first, _ = _whole(store, url=WHOLE, whole=True, label="Alert webhook")
    put = secret_refs.putter(store)
    commands = secret_commands.parse_commands({"commands": [
        {"carrier": "config.base_url", "operation": "set_credentials", "url": WHOLE + "-2", "whole": True, "label": "Alert webhook"}]})
    second, outcome = secret_commands.apply_commands(first, commands, put)
    assert outcome.retired == record_carriers.refs_in(first["config"]["base_url_entries"]) and len(outcome.created) == 1
    assert second["config"]["base_url_entries"]["whole"]["secret_ref"] != first["config"]["base_url_entries"]["whole"]["secret_ref"]


@pytest.mark.parametrize("command", [
    {"url": WHOLE, "whole": True},
    {"url": WHOLE, "whole": True, "label": ""},
    {"url": WHOLE, "whole": True, "label": "x" * 200},
    {"url": WHOLE, "whole": True, "label": "R3WEBHOOKSECRET"},
    {"url": WHOLE, "whole": True, "label": "see https://elsewhere.invalid"},
    {"url": WHOLE, "whole": True, "label": "tab\there"},
    {"url": WHOLE, "label": "Alert webhook"},
    {"url": WHOLE, "whole": "yes", "label": "Alert webhook"},
])
def test_a_whole_url_needs_a_label_that_does_not_repeat_the_url_and_a_label_needs_whole(store, command):
    with pytest.raises(secret_commands.CommandError) as refused:
        _whole(store, **command)
    assert "R3WEBHOOKSECRET" not in refused.value.message
