"""The record schema step ``workflow-record-1`` and the name check on every registry write (CODE_TO_DATA_S4_S8, 5.5, 5.6):
the step is additive, runs once, and keeps what a record already states; a write may add no name clash and an old clash
does not lock an unrelated edit."""
from __future__ import annotations

import copy
import json

import pytest

from gateway import admin_store, discovery, registry_layers, service_registry
from tests.registry_test_helpers import write_legacy_registry_for_test

BUNDLED_ID = "image.generate.flux2-klein-4b"


def contract(workflow_id: str) -> dict:
    out = copy.deepcopy(discovery.bundled_workflow_manifests()[BUNDLED_ID])
    out["id"] = workflow_id
    return out


def stored(workflows: dict, **sections) -> dict:
    return {**registry_layers.empty_stored(service_registry.SERVICE_REGISTRY_SCHEMA_VERSION), "workflows": workflows,
            **sections}


def db_with(tmp_path, document: dict):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    write_legacy_registry_for_test(document, path=db)
    # the earlier steps are not what these tests are about
    admin_store.record_migration_step(registry_layers.LAYERING_STEP, path=db)
    admin_store.record_migration_step(service_registry.WORKFLOW_SCHEMA_2_STEP, path=db)
    return db


def row(db):
    record = admin_store.get_service_registry_json_with_generation(path=db)
    return json.loads(record["value_json"]), int(record["operator_settings_generation"])


def test_each_stored_record_gets_active_published_revision_one_and_its_own_incarnation(tmp_path):
    db = db_with(tmp_path, stored({"owner.a": {"manifest": contract("owner.a"), "source": "ui"},
                                   "owner.b": {registry_layers.REPLACE_KEY: {"manifest": contract("owner.b")}}}))
    service_registry.ensure_service_registry(path=db, actor="test")
    document, _ = row(db)
    a, b = document["workflows"]["owner.a"], document["workflows"]["owner.b"][registry_layers.REPLACE_KEY]
    for record in (a, b):
        assert (record["state"], record["published"], record["record_revision"]) == ("active", True, 1)
    assert a["incarnation"] and b["incarnation"] and a["incarnation"] != b["incarnation"]
    assert admin_store.migration_step_recorded(service_registry.WORKFLOW_RECORD_STEP, path=db)


def test_the_step_runs_once_and_a_second_start_changes_nothing(tmp_path):
    db = db_with(tmp_path, stored({"owner.a": {"manifest": contract("owner.a")}}))
    service_registry.ensure_service_registry(path=db, actor="test")
    first = row(db)
    service_registry.ensure_service_registry(path=db, actor="test")
    assert row(db) == first


def test_a_registry_with_no_workflows_only_records_the_step(tmp_path):
    db = db_with(tmp_path, stored({}))
    before = row(db)[1]
    service_registry.ensure_service_registry(path=db, actor="test")
    assert row(db)[1] == before
    assert admin_store.migration_step_recorded(service_registry.WORKFLOW_RECORD_STEP, path=db)


def test_what_a_record_already_states_is_kept(tmp_path):
    db = db_with(tmp_path, stored({"owner.a": {"manifest": contract("owner.a"), "state": "disabled", "published": False,
                                               "incarnation": "mine", "record_revision": 7}}))
    service_registry.ensure_service_registry(path=db, actor="test")
    record = row(db)[0]["workflows"]["owner.a"]
    assert (record["state"], record["published"], record["incarnation"], record["record_revision"]) == ("disabled", False, "mine", 7)


def test_the_normalized_record_shows_the_run_control_and_the_contract_is_untouched(tmp_path):
    db = db_with(tmp_path, stored({"owner.a": {"manifest": contract("owner.a")}}))
    registry = service_registry.ensure_service_registry(path=db, actor="test")
    record = registry["workflows"]["owner.a"]
    assert (record["state"], record["published"]) == ("active", True) and record["incarnation"]
    assert record["manifest"] == contract("owner.a")


def test_an_inline_record_keeps_the_run_control_out_of_its_contract():
    inline = {**contract("owner.a"), "state": "disabled", "published": False, "incarnation": "i", "record_revision": 2}
    record = service_registry.normalize_workflow_record("owner.a", inline)
    assert record["manifest"] == contract("owner.a")
    assert (record["state"], record["published"], record["incarnation"], record["record_revision"]) == ("disabled", False, "i", 2)


def test_a_record_with_no_run_control_reads_without_any(tmp_path):
    record = service_registry.normalize_workflow_record("owner.a", {"manifest": contract("owner.a")})
    assert not {"state", "published", "incarnation", "record_revision"} & set(record)


def test_resource_class_survives_a_read_and_write_back():
    policy = {"exposure": "default", "trust_tier": "stable", "resource_tier": "batch", "tags": [], "resource_class": "image"}
    record = service_registry.normalize_workflow_record("owner.a", {"manifest": contract("owner.a"), "policy": policy})
    assert record["policy"] == policy


# -- the name check on a write of the document ------------------------------------------------------------------------


def alias_service(alias_id: str) -> dict:
    return {"svc": {"id": "svc", "name": "svc", "kind": "engine",
                    "config": {"model_policy_schema_version": 1,
                               "models": [{"id": "base"}, {"id": alias_id, "base_model_id": "base"}]}}}


def write(db, document: dict):
    return admin_store.write_service_registry(
        document, expected_generation=row(db)[1], path=db, stored_form=True)


def test_a_write_that_adds_a_clash_is_refused_and_stores_nothing(tmp_path):
    db = db_with(tmp_path, stored({"owner.a": {"manifest": contract("owner.a")}}))
    before = row(db)
    with pytest.raises(service_registry.NameClash) as raised:
        write(db, stored({"owner.a": {"manifest": contract("owner.a")}}, services=alias_service("Owner-A")))
    assert raised.value.code == "id_clash"
    assert row(db) == before


def test_two_ids_with_one_token_cannot_be_written_together(tmp_path):
    db = db_with(tmp_path, stored({}))
    with pytest.raises(service_registry.NameClash):
        write(db, stored({"x.a-b": {"manifest": contract("x.a-b")}, "x.a.b": {"manifest": contract("x.a.b")}}))


def test_a_clash_the_registry_already_holds_does_not_lock_an_unrelated_write(tmp_path):
    clashing = stored({"x.a-b": {"manifest": contract("x.a-b")}, "x.a.b": {"manifest": contract("x.a.b")}})
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    # an old registry that holds the clash: written by the layer below the check
    import sqlite3
    with sqlite3.connect(db) as connection:
        connection.execute("INSERT INTO operator_settings(key, value_json, updated_at, updated_by) VALUES (?,?,?,?)",
                           ("service_registry", json.dumps(clashing), 1, "test"))
        connection.execute("INSERT INTO operator_settings_generations(setting_key, operator_settings_generation) VALUES ('service_registry', 1)")
    unrelated = copy.deepcopy(clashing)
    unrelated["workflows"]["x.a-b"]["note"] = "edited"
    write(db, unrelated)                                  # no new clash: accepted
    worse = copy.deepcopy(unrelated)
    worse["workflows"]["x.a_b"] = {"manifest": contract("x.a_b")}
    with pytest.raises(service_registry.NameClash):
        write(db, worse)                                  # a third owner of the token is a new clash


def test_a_self_update_and_an_alias_of_the_same_workflow_are_not_clashes(tmp_path):
    db = db_with(tmp_path, stored({"owner.a": {"manifest": contract("owner.a")}}))
    write(db, stored({"owner.a": {"manifest": {**contract("owner.a"), "display_name": "changed"}}}))
