"""A model saved through any service save is checked against its kind, as the engine page checks it.

Add and edit both reach ``service_registry.upsert_service_in_registry``, which runs
``refuse_unoffered_kinds_in_record`` against the stored record. A save checks a record it adds or changes
(its models or capabilities) and leaves a stored record it does not change as it is, so a legacy row never
blocks a save that leaves its kinds alone. A refusal names the same field and the same 400 the engine page gives.
A whole-registry save and a package import check each record they add or change, before anything is written.
A left-out record that a fix brings back is checked as a new record.
"""

from __future__ import annotations

import copy

from gateway import admin_store, extension_packages, service_registry
from gateway.admin_runtime import EXTENSION_PACKAGE_INDEX_SETTING_KEY
from tests.registry_test_helpers import write_service_registry_for_test
from tests.test_signin_who_can_enter import (  # noqa: F401  (gateway is a fixture)
    csrf_for,
    gateway,
    owner_and_admin,
)

SPEECH_ROW = {"public": "voice-1", "upstream": "voice-1", "kind": "speech"}
CHAT_ROW = {"public": "chat-1", "upstream": "chat-1", "kind": "chat"}
LEGACY_ROW = {"public": "legacy-1", "upstream": "legacy-1", "kind": "chat"}


def _write_headers(client):
    # A fresh database has no registry row yet: its generation is 0.
    etag = client.get("/admin/api/operator/service-registry").headers.get("etag", '"os-0"')
    return {**csrf_for(client), "If-Match": etag}


def _add(client, **fields):
    body = {"id": "box", "name": "Box", "host": "10.0.0.9", "port": 8080, **fields}
    return client.post("/admin/api/operator/services", headers=_write_headers(client), json=body)


def _edit(client, **fields):
    return client.patch("/admin/api/operator/services/box", headers=_write_headers(client), json=fields)


def _stored(gateway, service_id="box"):
    record = admin_store.get_service_registry_with_generation(path=gateway.path)
    return (record["value"]["services"] if record else {}).get(service_id)


def _stored_model_kind(gateway, service_id="box"):
    stored = _stored(gateway, service_id)
    assert stored is not None
    return stored["config"]["models"][0]["kind"]


def _stored_models(gateway, service_id="box"):
    stored = _stored(gateway, service_id)
    assert stored is not None
    return stored["config"]["models"]


def _stored_name(gateway, service_id="box"):
    stored = _stored(gateway, service_id)
    assert stored is not None
    return stored["name"]


def test_adding_a_kind_the_record_does_not_offer_is_refused_at_its_row(gateway):
    owner, *_ = owner_and_admin(gateway)

    refused = _add(owner, capabilities=["chat"], models=[SPEECH_ROW])

    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["field"]) == ("invalid_engine", "models.0.kind")
    assert error["message"] == "This engine doesn’t offer Voice."
    assert _stored(gateway) is None


def test_adding_a_kind_the_record_offers_is_saved(gateway):
    owner, *_ = owner_and_admin(gateway)

    created = _add(owner, capabilities=["speech"], models=[SPEECH_ROW])

    assert created.status_code == 201, created.text
    assert _stored_model_kind(gateway) == "speech"


def test_adding_a_model_without_a_kind_is_saved_on_any_record(gateway):
    owner, *_ = owner_and_admin(gateway)

    created = _add(owner, capabilities=["chat"], models=[{"public": "plain-1", "upstream": "plain-1"}])

    assert created.status_code == 201, created.text


def test_adding_a_kind_outside_the_vocabulary_is_refused_at_its_row(gateway):
    owner, *_ = owner_and_admin(gateway)

    refused = _add(owner, capabilities=["chat"], models=[{"public": "odd-1", "upstream": "odd-1", "kind": "video"}])

    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["field"]) == ("invalid_engine", "models.0.kind")
    assert error["message"] == "The capability of “odd-1” is not one of the choices."


def test_editing_a_kind_the_record_does_not_offer_is_refused_and_nothing_changes(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat"], models=[CHAT_ROW]).status_code == 201
    before = _stored(gateway)

    refused = _edit(owner, models=[SPEECH_ROW])

    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["field"] == "models.0.kind"
    assert _stored(gateway) == before


def test_editing_a_record_to_offer_the_kind_is_saved(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat"], models=[CHAT_ROW]).status_code == 201

    edited = _edit(owner, capabilities=["speech"], models=[SPEECH_ROW])

    assert edited.status_code == 200, edited.text
    assert _stored_model_kind(gateway) == "speech"


def test_a_name_edit_that_sends_a_legacy_row_back_as_stored_is_saved(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat"], models=[CHAT_ROW]).status_code == 201
    _seed_stored_kind(gateway, "box", "speech")

    stored_models = _stored_models(gateway)

    edited = _edit(owner, name="Renamed", host="10.0.0.9", port=8080, capabilities=["chat"], models=stored_models)

    assert edited.status_code == 200, edited.text
    assert _stored_name(gateway) == "Renamed"
    assert _stored_model_kind(gateway) == "speech"


def test_a_name_only_patch_of_a_legacy_row_is_saved(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat"], models=[CHAT_ROW]).status_code == 201
    _seed_stored_kind(gateway, "box", "speech")

    edited = _edit(owner, name="Renamed")

    assert edited.status_code == 200, edited.text
    assert _stored_name(gateway) == "Renamed"
    assert _stored_model_kind(gateway) == "speech"


def test_a_models_change_on_a_legacy_row_is_still_checked(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat"], models=[CHAT_ROW]).status_code == 201
    _seed_stored_kind(gateway, "box", "speech")
    before = _durable_state(gateway)

    refused = _edit(owner, host="10.0.0.9", port=8080, capabilities=["chat"], models=[SPEECH_ROW])

    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["field"] == "models.0.kind"
    assert _durable_state(gateway) == before


def test_a_capability_change_that_leaves_a_model_kind_unoffered_is_refused_on_patch(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat", "speech"], models=[SPEECH_ROW]).status_code == 201
    before = _durable_state(gateway)

    refused = _edit(owner, capabilities=["chat"])

    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["field"] == "models.0.kind"
    assert _durable_state(gateway) == before


def _registry(client):
    return client.get("/admin/api/operator/service-registry").json()["service_registry"]


def _put_registry(client, registry):
    return client.put("/admin/api/operator/service-registry", json=registry, headers=_write_headers(client))


def _seed_stored_kind(gateway, service_id, kind):
    """Store a model kind the way a save from before the check did: straight into the registry, unchecked."""
    record = admin_store.get_service_registry_with_generation(path=gateway.path)
    assert record is not None
    record["value"]["services"][service_id]["config"]["models"][0]["kind"] = kind
    write_service_registry_for_test(record["value"], path=gateway.path)


def _durable_state(gateway):
    """What a refused save or import must leave alone: the registry, the package index and the audit log."""
    return (
        admin_store.get_service_registry_with_generation(path=gateway.path),
        admin_store.get_operator_setting(EXTENSION_PACKAGE_INDEX_SETTING_KEY, path=gateway.path),
        admin_store.list_audit(path=gateway.path),
    )


def test_a_whole_registry_save_refuses_a_changed_record_with_a_kind_it_does_not_offer(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat"], models=[CHAT_ROW]).status_code == 201
    registry = _registry(owner)
    registry["services"]["box"]["config"]["models"][0]["kind"] = "speech"
    before = _durable_state(gateway)

    refused = _put_registry(owner, registry)

    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["field"]) == ("invalid_engine", "services.box.config.models.0.kind")
    assert error["message"] == "This engine doesn’t offer Voice."
    assert _durable_state(gateway) == before


def test_a_whole_registry_save_refuses_a_new_record_with_a_kind_it_does_not_offer(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat"], models=[CHAT_ROW]).status_code == 201
    registry = _registry(owner)
    newcomer = copy.deepcopy(registry["services"]["box"])
    newcomer.update(id="voice-box", name="Voice box")
    newcomer["config"]["models"] = [
        {"id": "voice-2", "model_group": "voice-2", "upstream_model": "voice-2", "kind": "speech"},
    ]
    registry["services"]["voice-box"] = newcomer
    before = _durable_state(gateway)

    refused = _put_registry(owner, registry)

    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["field"]) == ("invalid_engine", "services.voice-box.config.models.0.kind")
    assert _durable_state(gateway) == before


def test_a_whole_registry_save_refuses_a_capability_its_model_kind_needs_removed(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat", "speech"], models=[SPEECH_ROW]).status_code == 201
    registry = _registry(owner)
    registry["services"]["box"]["capabilities"] = ["chat"]
    before = _durable_state(gateway)

    refused = _put_registry(owner, registry)

    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["field"]) == ("invalid_engine", "services.box.config.models.0.kind")
    assert _durable_state(gateway) == before


def test_a_whole_registry_save_leaves_an_unchanged_legacy_row_as_it_is(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat"], models=[CHAT_ROW]).status_code == 201
    assert _add(owner, id="legacy", name="Legacy", host="10.0.0.10",
                capabilities=["chat"], models=[LEGACY_ROW]).status_code == 201
    _seed_stored_kind(gateway, "legacy", "speech")
    registry = _registry(owner)
    registry["services"]["box"]["name"] = "Renamed"

    saved = _put_registry(owner, registry)

    assert saved.status_code == 200, saved.text
    stored = _stored(gateway)
    assert stored is not None
    assert stored["name"] == "Renamed"
    assert _stored_model_kind(gateway, "legacy") == "speech"


def _voice_package(tmp_path, capabilities, kind):
    """A local package whose one service serves a model of ``kind`` under ``capabilities``."""
    package_dir = tmp_path / "voice-package"
    (package_dir / "objects").mkdir(parents=True)
    (package_dir / "extension.yml").write_text(
        "schema_version: 1\nkind: extension_package\nid: voice-package\nversion: 0.1.0\n"
        "objects:\n  services:\n    - objects/service.yml\n", encoding="utf-8")
    (package_dir / "objects" / "service.yml").write_text(
        "id: voice-box\nname: Voice box\nsource: provisioned\n"
        f"capabilities: [{capabilities}]\n"
        "config:\n  base_url: http://localhost:9999\n"
        f"  models:\n    - {{id: voice-1, upstream_model: voice-1, kind: {kind}}}\n", encoding="utf-8")
    return {"type": "local_directory", "path": str(package_dir)}


def _enable_package(client, source):
    return client.post("/admin/api/extensions/enable", headers=_write_headers(client), json={"source": source})


def test_an_import_refuses_a_kind_the_package_record_does_not_offer_and_writes_nothing(gateway, tmp_path):
    owner, *_ = owner_and_admin(gateway)
    before = _durable_state(gateway)

    refused = _enable_package(owner, _voice_package(tmp_path, capabilities="chat", kind="speech"))

    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["field"]) == ("invalid_engine", "services.voice-box.config.models.0.kind")
    assert error["message"] == "This engine doesn’t offer Voice."
    assert _durable_state(gateway) == before


def test_an_import_accepts_a_kind_the_package_record_offers(gateway, tmp_path):
    owner, *_ = owner_and_admin(gateway)

    imported = _enable_package(owner, _voice_package(tmp_path, capabilities="speech", kind="speech"))

    assert imported.status_code == 200, imported.text
    assert _stored_model_kind(gateway, "voice-box") == "speech"


def test_reenabling_an_installed_package_whose_kinds_are_unchanged_passes(gateway, tmp_path):
    """A record stored before the check, with the models and capabilities its package still has, is not refused."""
    owner, *_ = owner_and_admin(gateway)
    source = _voice_package(tmp_path, capabilities="chat", kind="speech")
    review = extension_packages.validate_extension_package_source(
        source, install_root=gateway.path.parent / "extensions" / "installed", credentials={})
    installed = extension_packages.imported_registry_records(review)["services"]["voice-box"]
    registry = service_registry.normalize_service_registry({})
    registry["services"]["voice-box"] = installed
    write_service_registry_for_test(registry, path=gateway.path)

    enabled = _enable_package(owner, source)

    assert enabled.status_code == 200, enabled.text
    assert _stored_model_kind(gateway, "voice-box") == "speech"


def _store_left_out(gateway, service_id="held-box"):
    """Store a record the registry leaves out: its models are not model records, so it does not validate."""
    snapshot = admin_store.get_service_registry_with_generation(path=gateway.path)
    assert snapshot is not None
    registry = snapshot["value"]
    registry["services"][service_id] = {
        "id": service_id, "name": "Held box", "kind": "engine", "enabled": True, "capabilities": ["chat"],
        "config": {"host": "10.0.0.11", "port": 8080, "models": ["not a model record"]},
    }
    write_service_registry_for_test(registry, path=gateway.path)


def _left_out_record(client, service_id="held-box"):
    excluded = client.get("/admin/api/operator/service-registry").json()["excluded"]
    return next(item["record"] for item in excluded if item["id"] == service_id)


def _fix_left_out(client, record, service_id="held-box"):
    return client.put(f"/admin/api/operator/service-registry/excluded/services/{service_id}",
                      json={"record": record}, headers=_write_headers(client))


def test_fixing_a_left_out_record_refuses_a_kind_it_does_not_offer(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat"], models=[CHAT_ROW]).status_code == 201
    _store_left_out(gateway)
    record = _left_out_record(owner)
    record["config"]["models"] = [{"id": "voice-2", "model_group": "voice-2", "upstream_model": "voice-2", "kind": "speech"}]
    before = _durable_state(gateway)

    refused = _fix_left_out(owner, record)

    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert (error["code"], error["field"]) == ("invalid_engine", "services.held-box.config.models.0.kind")
    assert error["message"] == "This engine doesn’t offer Voice."
    assert _durable_state(gateway) == before


def test_fixing_a_left_out_record_with_a_kind_it_offers_is_saved(gateway):
    owner, *_ = owner_and_admin(gateway)
    assert _add(owner, capabilities=["chat"], models=[CHAT_ROW]).status_code == 201
    _store_left_out(gateway)
    record = _left_out_record(owner)
    record["config"]["models"] = [{"id": "chat-2", "model_group": "chat-2", "upstream_model": "chat-2", "kind": "chat"}]

    fixed = _fix_left_out(owner, record)

    assert fixed.status_code == 200, fixed.text
    assert _stored_model_kind(gateway, "held-box") == "chat"
