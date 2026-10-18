"""A credential goes to every address its record names, so a change to any of them is an owner change.

Review of the native lane found that a non-owner administrator could keep an owner's env-var engine key
and point it elsewhere: the binding check read only the record's base URL, while the lane dials the
record's ``url`` and sends a key to the origin stored beside the key.
"""
import copy

import pytest

from gateway import admin_store, native_lane, service_registry
from gateway.admin_api import AdminContext
from gateway.admin_runtime import AdminSQLiteStore

OLD_HOST = "http://198.51.100.10:8080"
NEW_HOST = "http://203.0.113.7:9000"


def _stored():
    return {"services": {"eng": {"name": "eng", "config": {
        "native_passthrough": True,
        "base_url": f"{OLD_HOST}/v1",
        "connection": {"credentials": {"inference": {"secret_env": "PROBE_ENGINE_KEY", "origin": OLD_HOST}}},
    }}}}


def _with(registry, path, value):
    out = copy.deepcopy(registry)
    node = out["services"]["eng"]["config"]
    *parents, leaf = path.split(".")
    for key in parents:
        node = node.setdefault(key, {})
    node[leaf] = value
    return out


def _credential_origin(registry):
    return registry["services"]["eng"]["config"]["connection"]["credentials"]["inference"]["origin"]


ATTACKS = {
    "the reviewer's input: url and credential origin together": [
        ("url", NEW_HOST), ("connection.credentials.inference.origin", NEW_HOST)],
    "config.url alone": [("url", NEW_HOST)],
    "the credential origin alone": [("connection.credentials.inference.origin", NEW_HOST)],
    "config.address": [("address", NEW_HOST)],
    "connection.address": [("connection.address", NEW_HOST)],
    "base_url alone": [("base_url", f"{NEW_HOST}/v1")],
}


def _attacked(name):
    registry = _stored()
    for path, value in ATTACKS[name]:
        registry = _with(registry, path, value)
    return registry


@pytest.mark.parametrize("name", sorted(ATTACKS))
def test_a_change_to_any_address_of_a_record_with_a_key_is_a_binding_change(name):
    changed = service_registry.credential_binding_changes(_stored(), _attacked(name))
    assert "host" in changed.get("eng", []), changed


@pytest.mark.parametrize("name", sorted(ATTACKS))
def test_a_non_owner_administrator_cannot_redirect_the_key_through_the_admin_save(tmp_path, name):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.write_service_registry_with_audit(
        _stored(), expected_generation=0, updated_by="seed", audit_action="seed",
        audit_actor_kind="user", audit_actor_id="seed", audit_actor_label="seed",
        audit_target_kind="operator_setting", audit_target_id="service_registry", path=path,
    )
    store = AdminSQLiteStore(path)
    admin = AdminContext(subject="admin-user", roles=frozenset({"admin"}), actor_kind="user")
    with pytest.raises(PermissionError):
        store._settle_credential_bindings(_attacked(name), admin)


def test_the_owner_moving_the_engine_rebinds_the_key_to_the_new_host(tmp_path):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.write_service_registry_with_audit(
        _stored(), expected_generation=0, updated_by="seed", audit_action="seed",
        audit_actor_kind="user", audit_actor_id="seed", audit_actor_label="seed",
        audit_target_kind="operator_setting", audit_target_id="service_registry", path=path,
    )
    store = AdminSQLiteStore(path)
    owner = AdminContext(subject="owner-user", roles=frozenset({"owner"}), actor_kind="user")
    settled = store._settle_credential_bindings(_attacked("config.url alone"), owner)
    cfg = settled["services"]["eng"]["config"]
    credential = native_lane.credential_for(cfg, "generate")
    assert credential is not None and credential.bound_to(service_registry.native_host_url(cfg))
    assert service_registry.origin_of(_credential_origin(settled)) == NEW_HOST


def test_an_edit_that_leaves_every_address_alone_is_not_a_binding_change():
    renamed = copy.deepcopy(_stored())
    renamed["services"]["eng"]["name"] = "renamed"
    renamed["services"]["eng"]["config"]["timeout"] = 30
    assert service_registry.credential_binding_changes(_stored(), renamed) == {}


def test_naming_the_address_the_record_already_uses_is_not_a_binding_change():
    same = _with(_stored(), "url", OLD_HOST)
    assert service_registry.credential_binding_changes(_stored(), same) == {}
