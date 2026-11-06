"""A registry read, changed and written back, keeps every connection setting it stored: the control address is shown
and comes back; a key no type names is hidden and kept, never dropped."""
from __future__ import annotations

import json
import sqlite3

from gateway import admin_store, service_registry
from tests.test_admin_secret_sweep import host_isolated  # noqa: F401
from tests.support.fake_upstream import fake_upstreams  # noqa: F401 - fixture
from tests.support.engine_dns import use_fake_engine_dns
from tests.test_signin_who_can_enter import csrf_for, gateway, owner_and_admin  # noqa: F401

REGISTRY = "/admin/api/operator/service-registry"
CONNECTION = {"transport": "http", "address": "http://127.0.0.1:5053", "control_address": "http://127.0.0.1:5056",
              "protocol": "openai-compatible", "timeouts": {"connect_s": 5, "later_s": 9}, "later_field": "kept"}


def _plant(gateway) -> None:
    record = {"kind": "llm", "label": "Engine", "config": {"address": "http://127.0.0.1:5053", "connection": dict(CONNECTION),
                                                         "offerings": [{"id": "m1", "kind": "chat", "later_key": 1},
                                                                       {"id": "m2", "kind": "chat", "later_key": 2}]}}
    admin_store.write_service_registry_with_audit(
        service_registry.normalize_service_registry({"schema_version": 2, "services": {"eng": record}}),
        expected_generation=0, updated_by="test", audit_action="test", audit_actor_kind=None,
        audit_actor_id=None, audit_actor_label=None, audit_target_kind=None, audit_target_id=None, path=gateway.path)


def _stored_connection(gateway) -> dict:
    with sqlite3.connect(gateway.path) as c:
        rows = [json.loads(value) for key, value in c.execute("SELECT key, value_json FROM operator_settings")
                if "eng" in value]
    return rows[0]["services"]["eng"]["config"]["connection"]


def test_the_registry_read_shows_the_control_address(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    _plant(gateway)
    shown = owner.get(REGISTRY).json()["service_registry"]["services"]["eng"]["config"]["connection"]
    assert shown["control_address"] == "http://127.0.0.1:5056"
    assert "later_field" not in shown and shown["hidden_settings"] == 1
    assert "later_s" not in shown["timeouts"] and shown["timeouts"]["hidden_settings"] == 1


def test_a_record_edited_from_the_read_keeps_its_control_address_and_hidden_keys(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    _plant(gateway)
    read = owner.get(REGISTRY)
    body = read.json()["service_registry"]
    body["services"]["eng"]["label"] = "Engine, renamed"
    wrote = owner.put(REGISTRY, headers={**csrf_for(owner), "If-Match": read.headers["etag"]}, json=body)
    assert wrote.status_code == 200, wrote.text
    stored = _stored_connection(gateway)
    assert stored["control_address"] == "http://127.0.0.1:5056"
    assert stored["later_field"] == "kept" and stored["timeouts"]["later_s"] == 9
    assert "hidden_settings" not in stored and "hidden_settings" not in stored["timeouts"]


def test_a_list_item_keeps_its_hidden_settings_through_a_read_and_write(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    _plant(gateway)
    read = owner.get(REGISTRY)
    body = read.json()["service_registry"]
    offerings = body["services"]["eng"]["config"]["offerings"]
    assert all("later_key" not in row and row["hidden_settings"] == 1 for row in offerings)
    offerings.reverse()  # matched by id, never by position
    wrote = owner.put(REGISTRY, headers={**csrf_for(owner), "If-Match": read.headers["etag"]}, json=body)
    assert wrote.status_code == 200, wrote.text
    with sqlite3.connect(gateway.path) as c:
        stored = [json.loads(value) for _key, value in c.execute("SELECT key, value_json FROM operator_settings") if "eng" in value][0]
    rows = {row["id"]: row for row in stored["services"]["eng"]["config"]["offerings"]}
    assert rows["m1"]["later_key"] == 1 and rows["m2"]["later_key"] == 2


def test_an_engine_page_edit_keeps_the_connection_and_residency_the_page_does_not_show(gateway, fake_upstreams, monkeypatch):
    from tests.test_engine_admin_routes import _edit, _put_registry, _record, _registry, _silent

    use_fake_engine_dns(monkeypatch)
    owner, *_ = owner_and_admin(gateway)
    _silent(fake_upstreams, "ctl.fk.test")
    registry = _registry(gateway)
    record = service_registry.service_from_backend_config("ctl", {
        "name": "Control engine", "host": "ctl.fk.test", "port": 80, "base_path": "/v1", "protocol": "openai-compatible",
        "capabilities": ["chat"], "models": [{"model_group": "m", "upstream_model": "m"}]}, source="ui")
    record["config"]["connection"] = {"transport": "http", "address": "http://ctl.fk.test:80",
                                      "control_address": "http://127.0.0.1:5056", "later_field": "kept"}
    record["config"]["later_config_key"] = "kept"
    record["residency"] = {"domain": "gpu9", "transition": {"enabled": False, "profile": "declared-ops-v1", "admission_wait_s": 60}}
    registry.setdefault("services", {})["ctl"] = record
    _put_registry(gateway, registry)
    reply = owner.get("/admin/api/engines/ctl")
    assert reply.status_code == 200, owner.get(REGISTRY).json().get("excluded")
    view = reply.json()["engine"]
    saved = _edit(owner, "ctl", {"definition": "openai-compatible", "name": "Control engine, renamed",
                                 "models": view["models"], "values": {}})
    assert saved.status_code == 200, saved.text
    after = _record(gateway, "ctl")
    assert after["config"]["connection"]["control_address"] == "http://127.0.0.1:5056"
    assert after["config"]["connection"]["later_field"] == "kept" and after["config"]["later_config_key"] == "kept"
    assert after["residency"]["transition"] == record["residency"]["transition"]


def test_no_part_of_a_written_record_drops_a_key_its_type_does_not_name():
    """A registry write is built from a read; a key the read drops (rather than hides) is lost on the write. Only the
    record's top level may drop: it names every stored record field."""
    from gateway import projection as p, projection_types as t

    assert set(service_registry.SERVICE_RECORD_FIELDS) <= set(t.SERVICE_RECORD.fields)
    dropping, seen = [], set()

    def walk(policy, path):
        if id(policy) in seen:
            return
        seen.add(id(policy))
        if isinstance(policy, p.Obj):
            if policy.extras is None and policy not in (t.SERVICE_RECORD, t.REGISTRY_DOC):
                dropping.append(f"{path} ({policy.name})")
            for key, inner in policy.fields.items():
                walk(inner, f"{path}.{key}")
            if policy.extras is not None:
                walk(policy.extras, f"{path}.*")
        elif isinstance(policy, p.Seq):
            walk(policy.item, f"{path}[]")
        elif isinstance(policy, p.Dict):
            walk(policy.value, f"{path}{{}}")
        elif isinstance(policy, p.Alt):
            for option in policy.options:
                walk(option, path)

    walk(t.REGISTRY_DOC, "registry")
    assert dropping == []


def test_a_list_item_without_an_id_is_matched_by_what_it_shows_and_never_by_position():
    from gateway import projection, projection_types

    stored = {"config": {"native": {"allow": [{"method": "GET", "path": "/a", "later": "A"}, {"method": "GET", "path": "/b", "later": "B"}]}}}
    incoming = {"config": {"native": {"allow": [{"method": "GET", "path": "/b", "hidden_settings": 1},
                                     {"method": "GET", "path": "/a", "hidden_settings": 1},
                                     {"method": "POST", "path": "/new"}]}}}
    projection.retain_hidden(projection_types.SERVICE_RECORD, incoming, stored)
    assert incoming["config"]["native"]["allow"] == [{"method": "GET", "path": "/b", "later": "B"}, {"method": "GET", "path": "/a", "later": "A"},
                                           {"method": "POST", "path": "/new"}]


def test_a_list_item_that_matches_two_stored_items_keeps_neither():
    from gateway import projection, projection_types

    stored = {"config": {"native": {"allow": [{"method": "GET", "path": "/a", "later": "A"}, {"method": "GET", "path": "/a", "later": "B"}]}}}
    incoming = {"config": {"native": {"allow": [{"method": "GET", "path": "/a", "hidden_settings": 1}]}}}
    projection.retain_hidden(projection_types.SERVICE_RECORD, incoming, stored)
    assert incoming["config"]["native"]["allow"] == [{"method": "GET", "path": "/a"}]


def test_a_connection_credential_with_a_key_it_cannot_take_is_refused_without_its_value():
    import pytest

    from gateway import native_lane

    block = {"credentials": {"control": {"secret_env": "CONTROL_KEY", "api_key": "raw-secret-value"}}}
    with pytest.raises(ValueError) as refused:
        native_lane.validate_connection(block)
    assert "api_key" in str(refused.value) and "raw-secret-value" not in str(refused.value)


def test_a_list_item_that_shows_nothing_keeps_nothing():
    from gateway import projection, projection_types

    stored = {"config": {"native": {"allow": [{"method": "GET", "path": "/a", "later": "A"}]}}}
    incoming = {"config": {"native": {"allow": [{"hidden_settings": 1}]}}}
    projection.retain_hidden(projection_types.SERVICE_RECORD, incoming, stored)
    assert incoming["config"]["native"]["allow"] == [{}]


def test_a_registry_write_refuses_a_credential_key_it_cannot_take_without_its_value(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    _plant(gateway)
    read = owner.get(REGISTRY)
    body = read.json()["service_registry"]
    body["services"]["eng"]["config"]["connection"]["credentials"] = {
        "control": {"secret_env": "CONTROL_KEY", "api_key": "raw-secret-value"}}
    wrote = owner.put(REGISTRY, headers={**csrf_for(owner), "If-Match": read.headers["etag"]}, json=body)
    assert wrote.status_code == 400, wrote.text
    assert "api_key" in wrote.text and "raw-secret-value" not in wrote.text
    assert "api_key" not in json.dumps(_stored_connection(gateway))


def test_a_registry_write_refuses_a_connection_address_that_holds_a_credential_without_its_value(gateway, host_isolated):
    owner, *_ = owner_and_admin(gateway)
    _plant(gateway)
    before = _stored_connection(gateway)
    read = owner.get(REGISTRY)
    body = read.json()["service_registry"]
    body["services"]["eng"]["config"]["connection"]["address"] = "http://user:raw-secret-value@127.0.0.1:5053"
    wrote = owner.put(REGISTRY, headers={**csrf_for(owner), "If-Match": read.headers["etag"]}, json=body)
    assert wrote.status_code == 400, wrote.text
    assert "connection.address" in wrote.text and "raw-secret-value" not in wrote.text
    assert _stored_connection(gateway) == before


def test_a_registry_refusal_never_quotes_a_secret_the_body_holds(gateway, host_isolated, monkeypatch):
    from gateway import service_registry

    owner, *_ = owner_and_admin(gateway)
    _plant(gateway)
    read = owner.get(REGISTRY)
    body = read.json()["service_registry"]
    address = "http://127.0.0.1:5053/v1?token=query-secret-value"
    body["services"]["eng"]["config"]["address"] = address

    def quoting(payload, before=None):
        raise ValueError(f"cannot reach {address}")

    monkeypatch.setattr(service_registry, "validate_protocol_dispatch_registry", quoting)
    wrote = owner.put(REGISTRY, headers={**csrf_for(owner), "If-Match": read.headers["etag"]}, json=body)
    assert wrote.status_code == 400, wrote.text
    assert "cannot reach" in wrote.text and "query-secret-value" not in wrote.text
