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


# --- auth_inject: the record's own key rules --------------------------------------------------------
# A rule's ``secret`` names a downstream secret the gateway holds, and ``secret_env`` an environment
# variable. Neither ends in a suffix the credential scan knows, so the first version of the scan missed
# ``secret`` and a non-owner could add a rule and move the host in one save.

def _plain():
    """A record with no credential of any kind, so only the new fields can make the change an owner's."""
    return {"services": {"eng": {"name": "eng", "config": {"native_passthrough": True, "base_url": f"{OLD_HOST}/v1"}}}}


INJECT_BY_SECRET = [{"header": "x-api-key", "secret": "api_key"}]
INJECT_BY_ENV = [{"header": "x-api-key", "secret_env": "PROBE_ENGINE_KEY"}]


@pytest.mark.parametrize("rules", [INJECT_BY_SECRET, INJECT_BY_ENV], ids=["secret", "secret_env"])
def test_a_rule_names_a_credential_by_secret_or_secret_env(rules):
    refs = service_registry.credential_references(_with(_plain(), "auth_inject", rules)["services"]["eng"])
    assert any("auth_inject" in path for path in refs), refs


@pytest.mark.parametrize("rules", [INJECT_BY_SECRET, INJECT_BY_ENV], ids=["secret", "secret_env"])
def test_adding_an_auth_inject_rule_is_a_binding_change(rules):
    changed = service_registry.credential_binding_changes(_plain(), _with(_plain(), "auth_inject", rules))
    assert any("auth_inject" in reason for reason in changed.get("eng", [])), changed


def test_adding_a_rule_and_moving_the_host_in_one_save_is_a_binding_change():
    attacked = _with(_with(_plain(), "auth_inject", INJECT_BY_SECRET), "base_url", f"{NEW_HOST}/v1")
    assert service_registry.credential_binding_changes(_plain(), attacked).get("eng")


def test_changing_or_removing_a_rule_is_a_binding_change():
    stored = _with(_plain(), "auth_inject", INJECT_BY_ENV)
    changed_rule = _with(stored, "auth_inject", [{"header": "x-api-key", "secret_env": "OTHER_KEY"}])
    removed = _with(stored, "auth_inject", [])
    assert service_registry.credential_binding_changes(stored, changed_rule).get("eng")
    assert service_registry.credential_binding_changes(stored, removed).get("eng")


def test_a_rule_that_is_not_changed_is_not_a_binding_change():
    stored = _with(_plain(), "auth_inject", INJECT_BY_ENV)
    renamed = copy.deepcopy(stored)
    renamed["services"]["eng"]["name"] = "renamed"
    assert service_registry.credential_binding_changes(stored, renamed) == {}


def _store_with(tmp_path, registry):
    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    admin_store.write_service_registry_with_audit(
        registry, expected_generation=0, updated_by="seed", audit_action="seed",
        audit_actor_kind="user", audit_actor_id="seed", audit_actor_label="seed",
        audit_target_kind="operator_setting", audit_target_id="service_registry", path=path,
    )
    return AdminSQLiteStore(path)


@pytest.mark.parametrize("rules", [INJECT_BY_SECRET, INJECT_BY_ENV], ids=["secret", "secret_env"])
def test_a_non_owner_administrator_cannot_add_a_rule_and_move_the_host_in_one_save(tmp_path, rules):
    store = _store_with(tmp_path, _plain())
    attacked = _with(_with(_plain(), "auth_inject", rules), "base_url", f"{NEW_HOST}/v1")
    admin = AdminContext(subject="admin-user", roles=frozenset({"admin"}), actor_kind="user")
    with pytest.raises(PermissionError):
        store._settle_credential_bindings(attacked, admin)


def test_the_owner_can_write_an_auth_inject_rule(tmp_path):
    store = _store_with(tmp_path, _plain())
    owner = AdminContext(subject="owner-user", roles=frozenset({"owner"}), actor_kind="user")
    settled = store._settle_credential_bindings(_with(_plain(), "auth_inject", INJECT_BY_SECRET), owner)
    assert settled["services"]["eng"]["config"]["auth_inject"] == INJECT_BY_SECRET


def test_a_non_owner_administrator_can_still_edit_a_service_that_carries_auth_inject_rules(tmp_path):
    """A partial edit carries every config key over, so keeping the rules is not a change to them."""
    store = _store_with(tmp_path, _with(_plain(), "auth_inject", INJECT_BY_ENV))
    admin = AdminContext(subject="admin-user", roles=frozenset({"admin"}), actor_kind="user")
    generation = admin_store.get_service_registry_with_generation(path=store.path)["operator_settings_generation"]

    store.update_service(service_id="eng", data={"name": "renamed"}, actor=admin, expected_generation=generation)

    saved = store.get_service_registry()["services"]["eng"]
    assert saved["name"] == "renamed"
    assert saved["config"]["auth_inject"] == INJECT_BY_ENV
