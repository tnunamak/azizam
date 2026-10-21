"""Round 5 of the secret handling: what the independent review of phase1-r4 (ce54285b) found (decision D42).

* HIGH 1: schema 13 bound a legacy credential to whatever address its field had at migration time, which an
  administrator could have set under an older version. Now nothing binds it but the owner's own write.
* HIGH 2: the context-window probe checked one base and sent to another that it read again. Every reader now builds the
  request once and sends exactly what was checked.
* MED: environment-sourced keys were not registered with the log scrubber, and the scrubber forgot a value after 512
  newer ones.

Each test fails on ce54285b.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

import proxy
from gateway import log_scrub
from gateway import (
    admin_store, engine_connection, lifecycle_drivers, record_carriers, runtime_secrets, secret_refs, secret_store, service_registry,
    url_binding,
)
from tests.support.fake_upstream import fake_upstreams, scenario_from_data  # noqa: F401
from tests.test_admin_secret_sweep import host_isolated  # noqa: F401
from tests.test_secret_canary_matrix import REGISTRY, _etag
from tests.test_secret_review_r3 import change, empty_registry, error_code, secret_rows, stored  # noqa: F401
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401
from tests.test_url_origin_binding import (  # noqa: F401
    CANARY, GOOD, SERVICE, _bound, _plant, _record, _store_as_v12, store, wire,
)

ATTACKER = "http://attacker.invalid"
LEGACY_PUBLICS = {
    "absolute attacker URL": f"{ATTACKER}/collect",
    "absolute attacker URL, upper case": "HTTP://ATTACKER.INVALID/collect",
    "protocol-relative": "//attacker.invalid/collect",
    "the path the owner wrote": "/ready",
}


def _legacy_database(path: Path, public: str) -> dict:
    """A schema 12 database whose health path was written by the owner as ``/ready?token=CANARY`` (stored bound to nothing, as
    schema 12 stored a path) and then changed by an administrator to ``public`` (the owner-only rule did not see it)."""
    admin_store.init_db(path)
    runtime_secrets.configure(path)
    put = lambda value, origin=None: secret_refs.store(value, db_path=path, origin=None)  # noqa: E731
    moved, _ = record_carriers.externalize_service(_record(), put)
    moved["config"]["health_endpoint"] = public
    document = service_registry.normalize_service_registry({"schema_version": 2, "services": {"d39": moved}})
    _store_as_v12(path, document)
    return moved


# ---------------------------------------------------------------------------
# HIGH 1: the migration does not approve what an administrator set
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(LEGACY_PUBLICS))
def test_the_migration_does_not_bind_a_legacy_credential_to_the_address_its_field_has_now(tmp_path, wire, name):
    path = tmp_path / "admin.db"
    try:
        moved = _legacy_database(path, LEGACY_PUBLICS[name])
        refs = record_carriers.refs_in(moved)
        admin_store.init_db(path)
        assert {_bound(path)[ref] for ref in refs} == {None}, "no credential is bound by the migration"
        reread = service_registry.load_service_registry(path=path)["services"]["d39"]
        assert proxy._runtime_health_endpoint(reread) is None
        assert CANARY not in proxy._service_resource_probe_url(reread["config"])
        backend = service_registry.runtime_backend_from_service(reread)
        asyncio.run(lifecycle_drivers.ExternalDriver().ensure_ready({**backend, "health_endpoint": backend["health_endpoint"]}, 0.2))
        asyncio.run(lifecycle_drivers.HttpLoadUnloadDriver().ensure_ready(backend, 0.2))
        assert not [request for request in wire if CANARY in str(request.url)], [str(request.url) for request in wire]
    finally:
        runtime_secrets.configure(None)


def test_a_secret_bound_to_nothing_is_never_put_back_for_any_address(store):
    moved = _owner_write(_record(), store)
    ref = record_carriers.refs_in(moved["config"]["health_endpoint_entries"])[0]
    _unbind(store, [ref])
    for address in (f"{GOOD}/ready", f"{ATTACKER}/ready", "http://127.0.0.1:1/ready"):
        record = copy.deepcopy(moved)
        with pytest.raises(runtime_secrets.OriginRefused):
            runtime_secrets.url(record["config"], "health_endpoint", dial=lambda text, address=address: address)


def test_a_record_that_exists_already_keeps_the_credentials_of_a_path_bound_to_nothing_and_those_of_a_url_to_its_origin(store):
    """What a migration of an older database does (schema 11, and the inline secrets of a record): only a URL is self-contained."""
    record = {"id": "e", "name": "e", "config": {"host": "good.invalid", "port": 9, "health_endpoint": f"/ready?token={CANARY}",
                                                 "key_url": f"http://good.invalid:9/keys?token={CANARY}-abs"}}
    moved, _ = record_carriers.externalize_service(record, secret_refs.putter(store))  # not a write by the owner
    health = record_carriers.refs_in(moved["config"]["health_endpoint_entries"])
    absolute = record_carriers.refs_in(moved["config"]["key_url_entries"])
    rows = _bound(store)
    assert {rows[ref] for ref in health} == {None}
    assert {rows[ref] for ref in absolute} == {GOOD}


def _owner_write(record, db):
    moved, _ = record_carriers.externalize_service(copy.deepcopy(record), secret_refs.putter(db), reject_ambiguous=True)
    return moved


def _unbind(db: Path, refs) -> None:
    """Make stored secrets what an older version left them: bound to no origin."""
    with sqlite3.connect(db) as conn:
        for ref in refs:
            value, _ = secret_store.read_provider_secret_on(conn, ref, db_path=db)
            secret_store.put_provider_secret(ref, value, db_path=db, approved_origin=None, conn=conn, allow_short=True, keep_suffix=False)


def _engine_view(client):
    shown = client.get("/admin/api/engines/d39")
    assert shown.status_code == 200, shown.text
    return shown.json()["engine"]


def _unbound_plant(gateway, owner):
    held = _plant(gateway, owner)
    refs = record_carriers.refs_in(held)
    _unbind(gateway.path, refs)
    return held, refs


def test_the_engine_view_says_the_owner_must_confirm_the_address_and_shows_no_value(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    _plant(gateway, owner)
    before = _engine_view(owner)
    assert before["url_credentials"] and {row["state"] for row in before["url_credentials"]} == {"confirmed"}
    _unbound_plant(gateway, owner)
    for client in (owner, admin):
        view = _engine_view(client)
        states = {row["carrier"]: row["state"] for row in view["url_credentials"]}
        assert states and set(states.values()) == {"needs_confirmation"}
        codes = [row for row in view["attention"] if row.get("code") == "engine_address_needs_confirmation"]
        assert {row["carrier"] for row in codes} == set(states)
        assert CANARY not in json.dumps(view)
        assert all(ref not in json.dumps(view["attention"]) for ref in secret_rows(gateway))


def test_the_engine_view_flags_a_field_that_was_moved_after_its_credentials_were_bound(gateway, host_isolated):
    owner, _, _, _ = owner_and_admin(gateway)
    _plant(gateway, owner)
    moved = change(owner, lambda services: services["d39"]["config"].update(health_endpoint="http://elsewhere.invalid:9/ready"))
    assert moved.status_code == 200, moved.text
    states = {row["carrier"]: row["state"] for row in _engine_view(owner)["url_credentials"]}
    assert states["config.health_endpoint"] == "needs_confirmation"
    assert states["config.context_window_probe.endpoint"] == "confirmed"


def _reviewed(client, carrier="config.health_endpoint"):
    """What the owner reads in the engine view for a carrier: the destination a confirmation would bind to."""
    rows = {row["carrier"]: row for row in _engine_view(client)["url_credentials"]}
    return rows[carrier]["destination"]


def _confirm(client, carrier="config.health_endpoint", destination="reviewed"):
    if destination == "reviewed":
        destination = _reviewed(client, carrier)
    return client.post(f"{SERVICE}/secret-entries", headers=_etag(client), json={"commands": [
        {"carrier": carrier, "operation": "confirm_address", "expected_destination": destination or "none"}]})


def test_an_administrator_cannot_confirm_an_address(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    held, refs = _unbound_plant(gateway, owner)
    refused = _confirm(admin)
    assert refused.status_code == 403 and error_code(refused) == "owner_required", refused.text
    assert {secret_rows(gateway)[ref] for ref in refs} == {None}


def test_the_owner_confirms_the_address_a_field_has_now_and_the_credentials_follow_it(gateway, host_isolated):
    owner, _, _, _ = owner_and_admin(gateway)
    held, refs = _unbound_plant(gateway, owner)
    health = record_carriers.refs_in(held["config"]["health_endpoint_entries"])
    done = _confirm(owner)
    assert done.status_code == 200, done.text
    rows = secret_rows(gateway)
    assert {rows[ref] for ref in health} == {GOOD}
    assert {rows[ref] for ref in refs if ref not in health} == {None}, "only the named field is confirmed"
    reread = service_registry.load_service_registry(path=gateway.path)["services"]["d39"]
    runtime_secrets.configure(gateway.path)
    try:
        assert proxy._runtime_health_endpoint(reread) == f"{GOOD}/ready?token={CANARY}-health"
    finally:
        runtime_secrets.configure(None)
    audit = gateway.rows("SELECT metadata_json FROM audit_events WHERE action='operator.service.address_confirmed'")
    assert len(audit) == 1 and "config.health_endpoint" in audit[0]["metadata_json"] and CANARY not in audit[0]["metadata_json"]
    assert {row["carrier"]: row["state"] for row in _engine_view(owner)["url_credentials"]}["config.health_endpoint"] == "confirmed"


def test_confirming_a_field_that_a_reader_would_send_somewhere_else_is_refused(gateway, host_isolated):
    owner, _, _, _ = owner_and_admin(gateway)
    held, refs = _unbound_plant(gateway, owner)
    gateway_write = change(owner, lambda services: services["d39"]["config"].update(health_endpoint="//attacker.invalid/collect"))
    assert gateway_write.status_code in (200, 400)
    refused = _confirm(owner, destination="none")
    assert refused.status_code == 400 and error_code(refused) == "url_unbound", refused.text
    assert CANARY not in refused.text
    assert {secret_rows(gateway)[ref] for ref in record_carriers.refs_in(held["config"]["health_endpoint_entries"])} == {None}


def test_confirming_a_field_with_no_stored_credentials_is_refused(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    _plant(gateway, owner)
    refused = _confirm(owner, "config.key_url", destination="none")
    assert refused.status_code == 400, refused.text


def test_the_owner_entering_the_credentials_again_binds_them_to_the_address_of_the_field(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    held, refs = _unbound_plant(gateway, owner)
    again = owner.post(f"{SERVICE}/secret-entries", headers=_etag(owner), json={"commands": [
        {"carrier": "config.health_endpoint", "operation": "set_credentials", "url": f"/ready?token={CANARY}-again"}]})
    assert again.status_code == 200, again.text
    fresh = record_carriers.refs_in(stored(gateway)["d39"]["config"]["health_endpoint_entries"])
    assert fresh and {secret_rows(gateway)[ref] for ref in fresh} == {GOOD}


def test_an_unbound_path_does_not_become_bound_by_a_save_that_does_not_touch_it(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    held, refs = _unbound_plant(gateway, owner)
    done = change(admin, lambda services: services["d39"]["config"].update(note="a note"))
    assert done.status_code == 200, done.text
    assert {secret_rows(gateway)[ref] for ref in refs} == {None}


# ---------------------------------------------------------------------------
# HIGH 2: a request is built once, and sent as built
# ---------------------------------------------------------------------------

def test_the_context_window_probe_sends_the_url_it_checked_even_if_the_configuration_changes_between_the_two_reads(store, wire, monkeypatch):
    moved = _owner_write(_record(), store)
    monkeypatch.setitem(proxy.config, "backends", {"d39": copy.deepcopy(moved["config"])})
    reads = []

    def base(backend):
        # a hot reload moved the service after the first read
        reads.append(backend)
        return GOOD if len(reads) == 1 else ATTACKER

    monkeypatch.setattr(proxy, "_backend_base_url", base)
    proxy._MODEL_CONTEXT_CACHE.clear()
    asyncio.run(proxy._probed_context_window("d39"))
    assert [str(request.url) for request in wire] == [f"{GOOD}/props?token={CANARY}-context"]
    assert len(reads) == 1, "the base is read once for the whole probe"


def test_the_context_window_probe_request_is_one_value(store, monkeypatch):
    moved = _owner_write(_record(), store)
    monkeypatch.setitem(proxy.config, "backends", {"d39": copy.deepcopy(moved["config"])})
    url, headers = proxy._context_window_probe_request("d39")
    assert url == f"{GOOD}/props?token={CANARY}-context" and isinstance(headers, dict)
    monkeypatch.setattr(proxy, "_backend_base_url", lambda backend: ATTACKER)
    assert proxy._context_window_probe_request("d39") is None, "credentials stored for one origin are not put on another"


def test_the_health_poll_of_a_connection_probe_sends_the_credentials_to_the_address_it_dials_only(store, wire):
    record = _record()
    record["config"].update(probe_with_inference_connection=True, base_url=f"{GOOD}/v1")
    moved = _owner_write(record, store)
    backend = service_registry.runtime_backend_from_service(moved)
    asyncio.run(lifecycle_drivers._poll_health(backend, 0.2))
    assert [str(request.url) for request in wire][:1] == [f"{GOOD}/v1/ready?token={CANARY}-health"] or \
        [str(request.url) for request in wire][:1] == [f"{GOOD}/ready?token={CANARY}-health"]
    wire.clear()
    for public in ("//attacker.invalid/x", f"{ATTACKER}/x", "/ready"):
        backend["health_endpoint"] = public
        backend["base_url"] = "http://attacker.invalid:9/v1" if public == "/ready" else f"{GOOD}/v1"
        asyncio.run(lifecycle_drivers._poll_health(backend, 0.2))
    assert not [request for request in wire if CANARY in str(request.url)], [str(request.url) for request in wire]


def test_the_native_call_goes_to_the_url_its_headers_were_resolved_for(store):
    from gateway import secret_commands

    record = {"id": "n", "name": "n", "config": {"host": "good.invalid", "port": 9}}
    new, _ = secret_commands.apply_commands(
        {**record, "config": {**record["config"], "url": f"{ATTACKER}/native"}},
        [{"carrier": "config.url", "operation": "set_credentials", "url": f"{ATTACKER}/native?token={CANARY}"}], secret_refs.putter(store))
    target = {"host": GOOD, "record": new["config"]}
    assert proxy._native_engine_base(target) == GOOD, "a URL of another origin than the host the headers were resolved for is not used"
    same = {"host": ATTACKER, "record": new["config"]}
    assert proxy._native_engine_base(same) == f"{ATTACKER}/native?token={CANARY}"


# ---------------------------------------------------------------------------
# MED: every value a driver resolves is registered, and a registered value stays
# ---------------------------------------------------------------------------

def test_an_environment_key_a_driver_reads_never_reaches_a_log_line(store, fake_upstreams, capsys, monkeypatch):
    monkeypatch.setenv("R5_ADMIN_KEY", "R5-ENV-ADMIN-KEY-VALUE")
    monkeypatch.setenv("R5_API_KEY", "R5-ENV-API-KEY-VALUE")
    log_scrub.clear()
    record = {"id": "lu", "name": "lu", "kind": "engine", "enabled": True, "capabilities": ["chat"],
              "config": {"base_url": f"{GOOD}/v1", "host": "good.invalid", "port": 9, "protocol": "openai-compatible",
                         "models": [{"id": "m", "upstream_model": "m"}], "driver_credential_origin": GOOD},
              "lifecycle": {"type": "http", "driver": "http-load-unload", "driver_config": {
                  "load_url": "/v1/model/load", "unload_url": "/v1/model/unload", "verify_endpoint": "/v1/model",
                  "admin_key_env": "R5_ADMIN_KEY", "api_key_env": "R5_API_KEY"}}}
    echoed = "echo R5-ENV-ADMIN-KEY-VALUE and R5-ENV-API-KEY-VALUE"
    scenario = scenario_from_data({
        "protocol": "http-load-unload", "name": "r5-echo",
        "provenance": {"source": "hand-written", "reference": "gateway/lifecycle_drivers.py HttpLoadUnloadDriver"},
        "exchanges": [
            {"required": False, "id": "unload", "match": {"method": "POST", "path": "/v1/model/unload"}, "response": {"status": 500, "body": [{"text": echoed}]}},
            {"required": False, "id": "load", "match": {"method": "POST", "path": "/v1/model/load"}, "response": {"body": [{"text": '{"error": "' + echoed + '"}'}]}},
            {"required": False, "id": "verify", "match": {"method": "GET", "path": "/v1/model"}, "response": {"body": [{"text": echoed}]}}]})
    fake_upstreams.asgi(scenario, host="good.invalid:9")
    backend = service_registry.runtime_backend_from_service(record)
    ctx = lifecycle_drivers.SwapContext(loaded_model="old", target_model="wanted")
    assert asyncio.run(lifecycle_drivers.HttpLoadUnloadDriver().swap(backend, {"model_name": "wanted"}, ctx=ctx)) is False
    out = capsys.readouterr().out
    assert "HttpLoadUnloadDriver.swap" in out
    assert "R5-ENV-ADMIN-KEY-VALUE" not in out and "R5-ENV-API-KEY-VALUE" not in out


def test_the_inference_key_a_probe_sends_is_registered(store, monkeypatch):
    monkeypatch.setenv("R5_INFERENCE_KEY", "R5-INFERENCE-KEY-VALUE")
    log_scrub.clear()
    record = {"config": {"base_url": f"{GOOD}/v1", "api_key_env": "R5_INFERENCE_KEY", "credential_origin": GOOD}, "secret_refs": {}}
    connection = engine_connection.inference_connection(record, "", fallback_address="", fallback_timeout=0.0, db_path=store, env=lambda name, default=None: __import__("os").environ.get(name, default))
    assert connection.headers["authorization"].endswith("R5-INFERENCE-KEY-VALUE")
    assert "R5-INFERENCE-KEY-VALUE" not in runtime_secrets.scrub(f"the server said {connection.headers['authorization']}")


def test_a_value_is_not_forgotten_after_many_newer_ones(store):
    log_scrub.clear()
    refs = [secret_refs.store(f"R5-VALUE-{index:05d}-SECRET", db_path=store) for index in range(700)]
    for ref in refs:
        runtime_secrets.resolve(ref)
    assert "R5-VALUE-00000-SECRET" not in runtime_secrets.scrub("echo R5-VALUE-00000-SECRET")
    assert "R5-VALUE-00699-SECRET" not in runtime_secrets.scrub("echo R5-VALUE-00699-SECRET")



def test_a_value_replaced_under_the_same_source_is_still_scrubbed_until_newer_ones_push_it_out(store, monkeypatch):
    log_scrub.clear()
    for value in ("R5-ROTATED-ONE-VALUE", "R5-ROTATED-TWO-VALUE"):
        monkeypatch.setenv("R5_ROTATING", value)
        runtime_secrets.env_secret("R5_ROTATING")
    scrubbed = runtime_secrets.scrub("R5-ROTATED-ONE-VALUE R5-ROTATED-TWO-VALUE")
    assert "R5-ROTATED" not in scrubbed
