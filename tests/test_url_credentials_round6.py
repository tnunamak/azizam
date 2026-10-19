"""Round 6 of the secret handling: what the review of phase1-r5 (2daae6d8) found (decision D42).

* HIGH: ``confirm_address`` bound a legacy credential to whatever address its field had, which the owner had not seen.
  The engine view now shows the destination, the command names it, and the binding refuses if it is not the one it
  would bind to where it commits.
* MED: two credential readers did not register the value they read with the log scrub (a structural test now finds them).
* LOW: the scrub registry was bounded by sources only.
"""

from __future__ import annotations

import copy
import sqlite3
import types

import pytest

import proxy
from gateway import admin_store, engine_credentials, log_scrub, record_carriers, runtime_secrets, secret_refs, secret_store, service_registry
from tests.test_admin_secret_sweep import host_isolated  # noqa: F401
from tests.test_secret_canary_matrix import REGISTRY, _etag
from tests.test_secret_review_r3 import change, error_code, secret_rows, stored, write_raw  # noqa: F401
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401
from tests.test_url_credentials_round5 import (  # noqa: F401
    _confirm, _engine_view, _reviewed, _unbind, _unbound_plant,
)
from tests.test_url_origin_binding import CANARY, GOOD, SERVICE, _plant, store  # noqa: F401

ATTACKER = "http://attacker.invalid"


def _document(gateway) -> dict:
    return admin_store.get_service_registry_with_generation(path=gateway.path)["value"]


def _generation(gateway) -> int:
    return int(admin_store.get_service_registry_with_generation(path=gateway.path)["operator_settings_generation"])


def _reaim(gateway, public: str, key: str = "health_endpoint") -> None:
    """What an administrator could do under an older version: change only the public address of a field."""
    document = _document(gateway)
    document["services"]["d39"]["config"][key] = public
    admin_store.write_service_registry_with_audit(
        document, expected_generation=_generation(gateway), updated_by="test", audit_action="test",
        audit_actor_kind=None, audit_actor_id=None, audit_actor_label=None, audit_target_kind=None, audit_target_id=None,
        path=gateway.path)


def _bound_rows(gateway, refs):
    return {ref: secret_rows(gateway)[ref] for ref in refs}


# ---------------------------------------------------------------------------
# HIGH: the owner confirms the address they were shown
# ---------------------------------------------------------------------------

def test_the_engine_view_shows_the_destination_each_confirmation_would_bind_to_and_no_value(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    _unbound_plant(gateway, owner)
    for client in (owner, admin):
        view = _engine_view(client)
        rows = {row["carrier"]: row for row in view["url_credentials"]}
        assert rows["config.health_endpoint"]["destination"] == f"{GOOD}/ready"
        assert rows["config.context_window_probe.endpoint"]["destination"] == f"{GOOD}/props"
        assert all(row["state"] == "needs_confirmation" and row["reenter"] is False for row in rows.values())
        shown = [item for item in view["attention"] if item["code"] == "engine_address_needs_confirmation"]
        assert {item["destination"] for item in shown} == {row["destination"] for row in rows.values()}
        assert all(row["destination"] in item["message"] for row, item in zip(rows.values(), shown)) or shown
        assert CANARY not in str(view)


@pytest.mark.parametrize("public,shown", [
    ("HTTP://Attacker.INVALID/collect", "http://attacker.invalid:80/collect"),
    ("https://attacker.invalid/collect", "https://attacker.invalid:443/collect"),
    ("http://[::1]:9/collect", "http://[::1]:9/collect"),
    ("/elsewhere", f"{GOOD}/elsewhere"),
    ("//attacker.invalid/collect", None),
    ("@attacker.invalid/collect", None),
])
def test_the_destination_is_the_origin_and_the_path_with_no_credential_in_it(gateway, host_isolated, public, shown):
    owner, *_ = owner_and_admin(gateway)
    _unbound_plant(gateway, owner)
    _reaim(gateway, public)
    rows = {row["carrier"]: row for row in _engine_view(owner)["url_credentials"]}
    assert rows["config.health_endpoint"]["destination"] == shown
    assert CANARY not in str(rows)


def test_the_reviewers_repro_an_address_the_owner_never_reviewed_is_not_approved(gateway, host_isolated):
    """A legacy ``/ready?token=CANARY``; an administrator set the public field to an attacker URL before the migration."""
    owner, *_ = owner_and_admin(gateway)
    held, refs = _unbound_plant(gateway, owner)
    health = record_carriers.refs_in(held["config"]["health_endpoint_entries"])
    reviewed_before = _reviewed(owner)
    assert reviewed_before == f"{GOOD}/ready"
    _reaim(gateway, f"{ATTACKER}/collect")
    # the command as r5 took it (a carrier only) is no longer a command
    bare = owner.post(f"{SERVICE}/secret-entries", headers=_etag(owner), json={"commands": [
        {"carrier": "config.health_endpoint", "operation": "confirm_address"}]})
    assert bare.status_code == 400 and error_code(bare) == "invalid_command", bare.text
    # what the owner reviewed before is not what the field says now
    stale = _confirm(owner, destination=reviewed_before)
    assert stale.status_code == 400 and error_code(stale) == "destination_changed", stale.text
    assert {secret_rows(gateway)[ref] for ref in health} == {None}
    runtime_secrets.configure(gateway.path)
    try:
        reread = service_registry.load_service_registry(path=gateway.path)["services"]["d39"]
        assert proxy._runtime_health_endpoint(reread) is None, "nothing reaches the attacker"
    finally:
        runtime_secrets.configure(None)
    # the view now shows the attacker address, so an owner who confirms it does so having seen it
    assert _reviewed(owner) == "http://attacker.invalid:80/collect"


def test_a_destination_that_is_not_the_one_the_field_has_is_refused_and_binds_nothing(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    held, refs = _unbound_plant(gateway, owner)
    for wrong in (f"{GOOD}/other", f"{GOOD}", f"{GOOD}/ready?token={CANARY}", "http://good.invalid:80/ready", "HTTP://GOOD.INVALID:9/ready",
                  f"{ATTACKER}/ready"):
        refused = _confirm(owner, destination=wrong)
        assert refused.status_code == 400 and error_code(refused) == "destination_changed", (wrong, refused.text)
        assert CANARY not in refused.text
    assert set(_bound_rows(gateway, refs).values()) == {None}


def test_confirming_with_the_destination_the_view_shows_binds_the_credentials(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    held, refs = _unbound_plant(gateway, owner)
    health = record_carriers.refs_in(held["config"]["health_endpoint_entries"])
    done = _confirm(owner)
    assert done.status_code == 200, done.text
    assert {secret_rows(gateway)[ref] for ref in health} == {GOOD}
    audit = gateway.rows("SELECT metadata_json FROM audit_events WHERE action='operator.service.address_confirmed'")
    assert len(audit) == 1 and CANARY not in audit[0]["metadata_json"]


def test_a_confirmation_read_against_an_older_registry_generation_is_refused(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    held, refs = _unbound_plant(gateway, owner)
    health = record_carriers.refs_in(held["config"]["health_endpoint_entries"])
    destination = _reviewed(owner)
    headers = _etag(owner)  # the generation the owner's view came from
    changed = change(admin, lambda services: services["d39"]["config"].update(note="another save"))
    assert changed.status_code == 200, changed.text
    refused = owner.post(f"{SERVICE}/secret-entries", headers=headers, json={"commands": [
        {"carrier": "config.health_endpoint", "operation": "confirm_address", "expected_destination": destination}]})
    assert refused.status_code == 412 and error_code(refused) == "generation_mismatch", refused.text
    assert {secret_rows(gateway)[ref] for ref in health} == {None}


def _owner_actor():
    return types.SimpleNamespace(actor_kind="owner", audit_actor_id="owner", actor_label="owner", roles={"owner"})


def test_the_binding_transaction_refuses_a_registry_that_changed_after_the_check(gateway, host_isolated):
    """The race: the record is read under the lock, then written by another route before the binding commits."""
    owner, *_ = owner_and_admin(gateway)
    held, refs = _unbound_plant(gateway, owner)
    health = record_carriers.refs_in(held["config"]["health_endpoint_entries"])
    store = proxy.ADMIN_STORE
    reviewed = _reviewed(owner)
    _reaim(gateway, f"{ATTACKER}/collect")  # after the owner looked
    with pytest.raises(Exception) as refused:
        store._bind_confirmed("d39", [(ref, GOOD) for ref in health], [("config.health_endpoint", reviewed)],
                              [{"carrier": "config.health_endpoint", "operation": "confirm_address"}], _owner_actor(), None)
    assert getattr(refused.value, "code", "") == "destination_changed"
    assert {secret_rows(gateway)[ref] for ref in health} == {None}
    # and the generation the view came from is checked in the same transaction
    with pytest.raises(admin_store.OperatorSettingsGenerationConflict):
        store._bind_confirmed("d39", [(ref, GOOD) for ref in health], [], [], _owner_actor(), _generation(gateway) - 1)
    assert {secret_rows(gateway)[ref] for ref in health} == {None}


def test_a_url_kept_whole_has_nothing_to_review_and_is_entered_again(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    _plant(gateway, owner)
    kept = owner.post(f"{SERVICE}/secret-entries", headers=_etag(owner), json={"commands": [
        {"carrier": "config.key_url", "operation": "set_credentials", "url": f"https://hooks.invalid/in/{CANARY}", "whole": True, "label": "hook"}]})
    assert kept.status_code == 200, kept.text
    whole = record_carriers.refs_in(stored(gateway)["d39"]["config"]["key_url_entries"])
    _unbind(gateway.path, whole)
    row = {row["carrier"]: row for row in _engine_view(owner)["url_credentials"]}["config.key_url"]
    assert row["state"] == "needs_confirmation" and row["destination"] is None and row["reenter"] is True
    assert CANARY not in str(row)
    refused = _confirm(owner, "config.key_url", destination="anything")
    assert refused.status_code == 400 and error_code(refused) == "reenter_required", refused.text
    assert {secret_rows(gateway)[ref] for ref in whole} == {None}
    again = owner.post(f"{SERVICE}/secret-entries", headers=_etag(owner), json={"commands": [
        {"carrier": "config.key_url", "operation": "set_credentials", "url": f"https://hooks.invalid/in/{CANARY}", "whole": True, "label": "hook"}]})
    assert again.status_code == 200, again.text
    fresh = record_carriers.refs_in(stored(gateway)["d39"]["config"]["key_url_entries"])
    assert {secret_rows(gateway)[ref] for ref in fresh} == {"https://hooks.invalid:443"}


def test_an_administrator_still_cannot_confirm(gateway, host_isolated):
    owner, _, _, admin = owner_and_admin(gateway)
    held, refs = _unbound_plant(gateway, owner)
    refused = _confirm(admin, destination=_reviewed(admin))
    assert refused.status_code == 403 and error_code(refused) == "owner_required", refused.text
    assert set(_bound_rows(gateway, refs).values()) == {None}


# ---------------------------------------------------------------------------
# Item 4: where a session check goes
# ---------------------------------------------------------------------------

def test_the_binding_transaction_runs_its_guard_first_inside_the_transaction_and_a_refusal_binds_nothing(store):
    ref = secret_refs.store("R6-GUARDED-VALUE", db_path=store)
    seen = []

    def guard(conn: sqlite3.Connection) -> None:
        seen.append(conn.in_transaction)
        raise PermissionError("the session is no longer live")

    with pytest.raises(PermissionError):
        secret_store.bind_provider_secrets([(ref, GOOD)], db_path=store, guard=guard)
    assert seen == [True]
    with sqlite3.connect(store) as conn:
        assert conn.execute("SELECT approved_origin FROM provider_secrets WHERE id=?", (ref,)).fetchone()[0] is None
    assert secret_store.bind_provider_secrets([(ref, GOOD)], db_path=store, guard=lambda conn: None) == 1


# ---------------------------------------------------------------------------
# MED: the readers register what they read
# ---------------------------------------------------------------------------

def test_the_engine_credential_a_probe_sends_is_registered(store, monkeypatch):
    monkeypatch.setenv("R6_CONTROL_KEY", "R6-ENV-CONTROL-KEY-VALUE")
    log_scrub.clear()
    service = {"connection": {"address": GOOD, "credentials": {"control": {
        "secret_ref": "os.environ/R6_CONTROL_KEY", "origin": GOOD, "header": "X-Control", "scheme": ""}}}}
    assert engine_credentials.credential_headers(service, "control") == {"X-Control": "R6-ENV-CONTROL-KEY-VALUE"}
    assert "R6-ENV-CONTROL-KEY-VALUE" not in runtime_secrets.scrub("the server said R6-ENV-CONTROL-KEY-VALUE")
    log_scrub.clear()
    ref = secret_refs.store("R6-STORED-CONTROL-KEY-VALUE", db_path=store, origin=GOOD)
    monkeypatch.setattr(admin_store, "DB_PATH", store)
    service["connection"]["credentials"]["control"]["secret_ref"] = ref
    assert engine_credentials.credential_headers(service, "control") == {"X-Control": "R6-STORED-CONTROL-KEY-VALUE"}
    assert "R6-STORED-CONTROL-KEY-VALUE" not in runtime_secrets.scrub("the server said R6-STORED-CONTROL-KEY-VALUE")


def test_the_native_lane_credential_is_registered(store, monkeypatch):
    import asyncio

    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", store)
    for source, secret in (("env", "R6-NATIVE-ENV-KEY-VALUE"), ("stored", "R6-NATIVE-STORED-KEY-VALUE")):
        log_scrub.clear()
        monkeypatch.setenv("R6_NATIVE_KEY", "R6-NATIVE-ENV-KEY-VALUE")
        ref = secret_refs.store("R6-NATIVE-STORED-KEY-VALUE", db_path=store, origin=GOOD)
        credential = {"header": "X-Native", "scheme": "", "origin": GOOD,
                      **({"secret_env": "R6_NATIVE_KEY"} if source == "env" else {"secret_ref": ref})}
        target = {"host": GOOD, "record": {"connection": {"credentials": {"control": credential, "inference": credential}}}}
        headers: dict[str, str] = {}
        assert asyncio.run(proxy._native_credential_headers(headers, target, "control", "e")) is None
        assert headers == {"X-Native": secret}
        assert secret not in runtime_secrets.scrub(f"the server said {secret}")


# ---------------------------------------------------------------------------
# LOW: the registry is bounded in bytes, not only in sources
# ---------------------------------------------------------------------------

def test_a_long_value_is_registered_by_its_first_8_kib_only():
    log_scrub.clear()
    runtime_secrets.remember("A" * (1 << 20), "big")
    held = [value for values in runtime_secrets._RESOLVED.values() for value in values]
    assert held and max(len(value) for value in held) == log_scrub._MAX_VALUE == 8192
    echoed = "echo " + "A" * 9000 + " end"
    cleaned = runtime_secrets.scrub(echoed, limit=100000)
    assert "A" * 8192 not in cleaned and cleaned.startswith("echo [removed]")


def test_the_registry_never_holds_more_than_its_byte_bound(monkeypatch):
    monkeypatch.setattr(log_scrub, "_TOTAL_BYTES", 40000)
    log_scrub.clear()
    for index in range(30):
        log_scrub.remember(f"{index:03d}" + "x" * 7000, f"source-{index}")
    total = sum(len(value) for values in log_scrub._RESOLVED.values() for value in values)
    assert total <= 40000 and total == log_scrub._held_bytes
    assert "source-29" in log_scrub._RESOLVED and "source-0" not in log_scrub._RESOLVED, "the oldest-used goes, the newest stays"


def test_a_source_that_is_in_use_survives_the_byte_bound(monkeypatch):
    monkeypatch.setattr(log_scrub, "_TOTAL_BYTES", 30000)
    log_scrub.clear()
    for index in range(3):
        log_scrub.remember(f"{index}" + "y" * 7000, f"source-{index}")
    log_scrub.remember("0" + "y" * 7000, "source-0")  # used again
    log_scrub.remember("3" + "y" * 7000, "source-3")
    log_scrub.remember("4" + "y" * 7000, "source-4")
    assert "source-0" in log_scrub._RESOLVED and "source-1" not in log_scrub._RESOLVED


def test_the_byte_count_follows_a_value_that_changes_under_one_source():
    log_scrub.clear()
    for value in ("a" * 100, "b" * 100, "c" * 100, "d" * 100, "d" * 100):
        log_scrub.remember(value, "rotating")
    assert log_scrub._held_bytes == 300 == sum(len(v) for v in log_scrub._RESOLVED["rotating"])
