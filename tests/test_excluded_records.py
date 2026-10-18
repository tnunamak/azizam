"""A record the registry leaves out (W3 round 5, rebuilt on decisions D26 to D28).

It is listed with its error. A record that parses into the service type is shown through it, with each secret as a
configured entry, so it can be edited; one that does not is shown only as its id, its error and the fields that failed.
Two routes fix or remove it. Editing never infers anything about a secret: an entry the stored record holds comes back
as the read showed it, or is left out; a new entry comes from an inline value (the owner's) or from the secret-entries
commands once the record validates. A service the schema 2 migration refused is such a record, with the migration's
error rebuilt from its blocks.
"""

from __future__ import annotations

import copy
import json

import pytest

import proxy
from gateway import admin_store, secret_refs, service_registry
from tests.registry_test_helpers import write_service_registry_for_test
from tests.test_frontend_mount_reload import live  # noqa: F401 - fixture
from tests.test_registry_apply_failure import GOOD, _boot, _owner_client, _service
from tests.test_registry_partial_operator import _if_match, _running_partial, _stored

URL = "/admin/api/operator/service-registry/excluded"
HEADER = {"id": "ent_" + "a" * 16, "name": "X-Vendor"}


def _held(**fields):
    return {**_service("held", models=["not a model record"], **fields)}


def _shown(client, section="services", record_id="held"):
    excluded = client.get("/admin/api/operator/service-registry").json()["excluded"]
    return next(item for item in excluded if item["section"] == section and item["id"] == record_id)


def _with_entry(db, **extra):
    """A left-out record that holds one stored secret entry, written raw."""
    ref = secret_refs.store("SEED-held-secret-1", db_path=db)
    return _held(headers=[{**HEADER, "secret_ref": ref}], **extra), ref


def test_the_registry_read_lists_a_left_out_record_with_its_error_and_its_secrets_as_entries(live, monkeypatch):
    record, ref = _with_entry(live)
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "held": record})

    response = client.get("/admin/api/operator/service-registry")

    item = _shown(client)
    assert item["parsed"] is True and item["error"] and item["secret_slots"] == ["config.headers[0]"]
    assert item["record"]["config"]["headers"] == [{**HEADER, "configured": True}]
    assert "SEED-held-secret-1" not in response.text and ref not in response.text


def test_a_record_that_does_not_parse_is_listed_with_only_its_error_and_the_fields_that_failed(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "held": {"id": "held", "config": "password=SEED-x"}})
    item = _shown(client)
    assert item["parsed"] is False and "record" not in item and item["failed_fields"] == ["config"]
    assert "SEED-x" not in json.dumps(item)


def test_editing_a_left_out_service_fixes_it_and_keeps_its_secret_entries(live, monkeypatch):
    record, ref = _with_entry(live)
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "held": record})
    edited = _shown(client)["record"]
    edited["config"]["models"] = []  # the fix; the header entry comes back as read

    response = client.put(f"{URL}/services/held", json={"record": edited}, headers=_if_match(live))

    assert response.status_code == 200, response.text
    assert response.json()["excluded"] == [] and "held" in response.json()["service_registry"]["services"]
    stored = _stored(live)["services"]["held"]
    assert stored["config"]["headers"] == [{**HEADER, "secret_ref": ref}]  # the same entry, the same secret
    assert "held" in proxy.config["backends"]


def test_an_edit_cannot_change_an_entry_the_record_holds(live, monkeypatch):
    record, ref = _with_entry(live)
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "held": record})
    edited = _shown(client)["record"]
    edited["config"]["models"] = []
    edited["config"]["headers"] = [{"name": "X-Vendor", "value": "SEED-new-value-2"}]  # not what the read showed

    response = client.put(f"{URL}/services/held", json={"record": edited}, headers=_if_match(live))

    assert response.status_code == 400 and "secret-entries" in response.json()["error"]["message"]
    assert "SEED-new-value-2" not in response.text
    assert _stored(live)["services"]["held"]["config"]["headers"] == [{**HEADER, "secret_ref": ref}]


def test_inline_input_in_an_edit_becomes_a_new_entry_and_is_the_owners(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "held": _held()})
    edited = _shown(client)["record"]
    edited["config"]["models"] = []
    edited["config"]["headers"] = [{"name": "X-New", "value": "SEED-inline-3"}]

    response = client.put(f"{URL}/services/held", json={"record": edited}, headers=_if_match(live))

    assert response.status_code == 200 and "SEED-inline-3" not in response.text
    [row] = _stored(live)["services"]["held"]["config"]["headers"]
    assert secret_refs.is_secret_entry(row) and row["name"] == "X-New"


def test_an_edit_that_still_does_not_validate_is_refused_with_the_reason_and_stores_nothing(live, monkeypatch):
    record, ref = _with_entry(live)
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "held": record})
    before = _stored(live)

    response = client.put(f"{URL}/services/held", json={"record": _shown(client)["record"]}, headers=_if_match(live))

    assert response.status_code == 400 and "models" in response.json()["error"]["message"]
    assert _stored(live) == before and secret_refs.configured(ref, db_path=live)


def test_a_record_that_could_not_be_parsed_is_replaced_whole_and_its_entries_are_deleted(live, monkeypatch):
    ref = secret_refs.store("SEED-held-secret-4", db_path=live)
    broken = {"id": "held", "config": "text", "process": {"driver": "process", "start": ["r", {"id": "ent_" + "d" * 16, "secret_ref": ref}]}}
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "held": broken})
    assert _shown(client)["parsed"] is False

    response = client.put(f"{URL}/services/held", json={"record": _service("held")}, headers=_if_match(live))

    assert response.status_code == 200, response.text
    assert not secret_refs.configured(ref, db_path=live)


def test_an_edit_of_a_record_that_is_not_left_out_or_of_an_unknown_section_is_not_found(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "held": _held()})
    assert client.put(f"{URL}/services/good-engine", json={"record": GOOD}, headers=_if_match(live)).status_code == 404
    assert client.put(f"{URL}/services/nothing", json={"record": GOOD}, headers=_if_match(live)).status_code == 404
    assert client.put(f"{URL}/widgets/held", json={"record": GOOD}, headers=_if_match(live)).status_code == 404
    assert client.delete(f"{URL}/widgets/held", headers=_if_match(live)).status_code == 404


def test_an_edit_needs_the_current_generation_and_a_record(live, monkeypatch):
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "held": _held()})
    edited = _shown(client)["record"]
    edited["config"]["models"] = []
    assert client.put(f"{URL}/services/held", json={"record": edited}, headers={"If-Match": '"os-99"'}).status_code == 412
    assert client.put(f"{URL}/services/held", json={"record": edited}).status_code == 412
    assert client.put(f"{URL}/services/held", json={"service": {}}, headers=_if_match(live)).status_code == 400


def test_removing_a_left_out_record_drops_only_that_record_and_deletes_its_entries(live, monkeypatch):
    record, ref = _with_entry(live)
    client = _running_partial(live, monkeypatch, {"good-engine": GOOD, "held": record, "other": {**_held(), "id": "other", "name": "other"}})

    response = client.delete(f"{URL}/services/held", headers=_if_match(live))

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["excluded"]] == ["other"]
    stored = _stored(live)["services"]
    assert "held" not in stored and "other" in stored and "good-engine" in stored
    assert not secret_refs.configured(ref, db_path=live)


def test_removing_a_left_out_record_of_another_section(live, monkeypatch):
    write_service_registry_for_test({"schema_version": 1, "services": {"good-engine": GOOD}, "protocol_frontends": {"held": "junk"},
                                     "workflows": {}, "capabilities": {}}, path=live)
    config, problem = _boot(live)
    client = _owner_client(monkeypatch)
    monkeypatch.setattr(proxy, "config", config)
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", problem, raising=False)

    response = client.delete(f"{URL}/protocol_frontends/held", headers=_if_match(live))

    assert response.status_code == 200 and response.json()["excluded"] == []
    assert "held" not in _stored(live)["protocol_frontends"]


def test_a_left_out_record_refused_by_the_migration_is_one_concept_and_fixed_by_keeping_one_block(live, monkeypatch):
    mixed = _service("mixed-engine", lifecycle={"driver": "command", "driver_config": {"start": ["legacy-start"]}})
    mixed["process"] = {"driver": "process", "start": ["runner"], "port": {"fixed": 8080}}
    registry = {"schema_version": 1, "services": {"good-engine": GOOD, "mixed-engine": mixed}, "protocol_frontends": {}, "workflows": {}, "capabilities": {}}
    service_registry.migrate_engine_records_v2(registry, refused={})
    registry["services"]["mixed-engine"]["migration_refused"] = "password=SEED-planted-5"  # a hand edit over the stored text
    write_service_registry_for_test(registry, path=live)
    config, problem = _boot(live)
    client = _owner_client(monkeypatch)
    monkeypatch.setattr(proxy, "config", config)
    monkeypatch.setattr(proxy, "REGISTRY_APPLY_PROBLEM", problem, raising=False)
    item = _shown(client, record_id="mixed-engine")
    assert "keep one" in item["error"].lower() and "SEED-planted-5" not in json.dumps(item) and "migration_refused" not in item["record"]
    edited = copy.deepcopy(item["record"])
    edited["config"].pop("lifecycle")  # keep the process block

    response = client.put(f"{URL}/services/mixed-engine", json={"record": edited}, headers=_if_match(live))

    assert response.status_code == 200, response.text
    stored = _stored(live)["services"]["mixed-engine"]
    assert stored["process"]["start"] == ["runner"] and "migration_refused" not in stored


def test_an_alias_clash_is_fixed_by_editing_the_later_claimant(live, monkeypatch):
    def claiming(service_id):
        return _service(service_id, models=[{"id": "base"}, {"id": "shared", "base_model_id": "base"}])

    client = _running_partial(live, monkeypatch, {"one": claiming("one"), "two": claiming("two")})
    edited = _shown(client, record_id="two")["record"]
    edited["config"]["models"] = [{"id": "base2"}, {"id": "other", "base_model_id": "base2"}]

    response = client.put(f"{URL}/services/two", json={"record": edited}, headers=_if_match(live))

    assert response.status_code == 200, response.text
    assert response.json()["excluded"] == []
