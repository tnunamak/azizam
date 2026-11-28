"""Bundled workflow ids serve the release contract and accept run control.

These tests use the real registry route and gateway reload against a test database.
"""
from __future__ import annotations

import copy

import pytest

import proxy
from gateway import admin_store, discovery, registry_layers, service_registry
from gateway.service_registry import ServiceRegistryConflict
from tests.registry_test_helpers import write_legacy_registry_for_test, write_service_registry_for_test
from tests.test_frontend_mount_reload import live  # noqa: F401 - fixture
from tests.test_registry_apply_failure import _owner_client

BUNDLED_ID = "image.generate.krea2-turbo"
OWNER_NAME = "Owner's own image workflow"


def _bundled_manifest() -> dict:
    return copy.deepcopy(discovery.bundled_workflow_manifests()[BUNDLED_ID])


def _owner_manifest(**changes) -> dict:
    return {**_bundled_manifest(), "display_name": OWNER_NAME, **changes}


def _generation(db) -> int:
    return admin_store.get_service_registry_with_generation(path=db)["operator_settings_generation"]


def _put(client, db, registry):
    return client.put("/admin/api/operator/service-registry", json=registry, headers={"If-Match": f'"os-{_generation(db)}"'})


def _with_workflow(registry: dict, manifest: dict, **record) -> dict:
    registry = copy.deepcopy(registry)
    registry["workflows"] = {**registry.get("workflows", {}), manifest["id"]: {"manifest": manifest, **record}}
    return registry


@pytest.fixture
def client(live, monkeypatch):  # noqa: F811 - the fixture it asks for
    # A reload swaps the contents of the one registry object, so a test puts the bundled ones back itself.
    saved = proxy.GATEWAY_WORKFLOW_REGISTRY.contents()
    write_service_registry_for_test(service_registry.normalize_service_registry({}), path=live)
    yield _owner_client(monkeypatch)
    proxy.GATEWAY_WORKFLOW_REGISTRY.restore(saved)


def _served_name(workflow_id: str = BUNDLED_ID) -> str:
    return proxy.GATEWAY_WORKFLOW_REGISTRY.get(workflow_id).manifest["display_name"]


def _seed_legacy_contract(db) -> None:
    """Seed the replacement record migration leaves when the legacy contract differs."""
    write_legacy_registry_for_test({
        "workflows": {
            BUNDLED_ID: {registry_layers.REPLACE_KEY: {
                "manifest": _owner_manifest(), "source": "ui", "overrides": BUNDLED_ID,
            }},
        },
    }, path=db)


def test_reload_serves_the_bundled_contract_over_a_retained_legacy_contract(live, client):
    _seed_legacy_contract(live)

    proxy.reload_runtime_config()

    release = _bundled_manifest()
    assert proxy.GATEWAY_WORKFLOW_REGISTRY.get(BUNDLED_ID).manifest == release
    assert proxy._load_gateway_workflow_registry().get(BUNDLED_ID).manifest == release
    assert admin_store.get_service_registry_with_generation(path=live)["value"]["workflows"][BUNDLED_ID]["manifest"] == _owner_manifest()


def test_the_bundled_workflows_the_owner_did_not_touch_keep_serving(live, client):
    proxy.reload_runtime_config()
    ids_before = proxy.GATEWAY_WORKFLOW_REGISTRY.ids()
    registry = copy.deepcopy(proxy.ADMIN_STORE.get_service_registry())
    registry.setdefault("workflows", {})[BUNDLED_ID] = {"patch_of": BUNDLED_ID, "published": False}

    saved = _put(client, live, registry)

    assert saved.status_code == 200, saved.text
    assert saved.json()["service_registry"]["workflows"][BUNDLED_ID]["published"] is False
    assert proxy.GATEWAY_WORKFLOW_REGISTRY.ids() == ids_before


def test_a_save_under_a_bundled_id_without_overrides_is_a_409_that_carries_the_bundled_manifest(live, client):
    registry = _with_workflow(proxy.ADMIN_STORE.get_service_registry(), _owner_manifest())
    generation = _generation(live)

    refused = _put(client, live, registry)

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "id_is_bundled"
    bundled = refused.json()["bundled_workflow"]
    assert bundled == _bundled_manifest()
    assert "save it under a new id, turn this one off, and point the stable name at the new id" in (
        refused.json()["error"]["message"]
    )
    assert _generation(live) == generation  # nothing stored
    assert _served_name() != OWNER_NAME


@pytest.mark.parametrize("marker", [BUNDLED_ID, "owner.test.other"], ids=["matching", "different"])
def test_a_legacy_marker_cannot_claim_a_bundled_id(live, client, marker):
    registry = _with_workflow(proxy.ADMIN_STORE.get_service_registry(), _owner_manifest(), overrides=marker)
    before = _generation(live)

    refused = _put(client, live, registry)

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "id_is_bundled"
    assert _generation(live) == before


def test_the_registry_saves_an_unchanged_read_back_of_a_legacy_contract(live, client):
    _seed_legacy_contract(live)
    proxy.reload_runtime_config()
    assert proxy.GATEWAY_WORKFLOW_REGISTRY.get(BUNDLED_ID).manifest == _bundled_manifest()
    read_back = client.get("/admin/api/operator/service-registry")
    assert read_back.status_code == 200, read_back.text
    registry = read_back.json()["service_registry"]
    assert registry["workflows"][BUNDLED_ID]["manifest"] == _owner_manifest()
    assert "overrides" not in registry["workflows"][BUNDLED_ID]

    again = _put(client, live, registry)

    assert again.status_code == 200, again.text
    stored = admin_store.get_service_registry_with_generation(path=live)["value"]
    assert stored["workflows"][BUNDLED_ID]["manifest"] == _owner_manifest()
    assert proxy.GATEWAY_WORKFLOW_REGISTRY.get(BUNDLED_ID).manifest == _bundled_manifest()


@pytest.mark.parametrize("legacy_marker", [{}, {"overrides": BUNDLED_ID}], ids=["without-marker", "with-marker"])
def test_an_edit_to_a_legacy_contract_under_a_bundled_id_is_refused(live, client, legacy_marker):
    _seed_legacy_contract(live)
    proxy.reload_runtime_config()
    before = _generation(live)
    edited = _with_workflow(proxy.ADMIN_STORE.get_service_registry(), _owner_manifest(display_name="Edited"),
                            **legacy_marker)

    refused = _put(client, live, edited)

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "id_is_bundled"
    assert _generation(live) == before
    assert proxy.GATEWAY_WORKFLOW_REGISTRY.get(BUNDLED_ID).manifest == _bundled_manifest()


def test_a_workflow_with_an_id_the_release_does_not_ship_needs_no_overrides(live, client):
    manifest = _owner_manifest(id="image.generate.owner-own")

    saved = _put(client, live, _with_workflow(proxy.ADMIN_STORE.get_service_registry(), manifest))

    assert saved.status_code == 200, saved.text
    assert proxy.GATEWAY_WORKFLOW_REGISTRY.get("image.generate.owner-own") is not None


def test_registry_reset_removes_the_legacy_row_and_keeps_serving_the_release(live, client):
    _seed_legacy_contract(live)
    proxy.reload_runtime_config()
    assert proxy.GATEWAY_WORKFLOW_REGISTRY.get(BUNDLED_ID).manifest == _bundled_manifest()
    reset = copy.deepcopy(proxy.ADMIN_STORE.get_service_registry())
    del reset["workflows"][BUNDLED_ID]

    assert _put(client, live, reset).status_code == 200

    stored = admin_store.get_service_registry_with_generation(path=live)["value"]
    assert BUNDLED_ID not in stored["workflows"]
    assert proxy.GATEWAY_WORKFLOW_REGISTRY.get(BUNDLED_ID).manifest == _bundled_manifest()


def test_the_registry_read_names_each_override_with_the_fields_that_differ_and_no_values(live, client):
    _seed_legacy_contract(live)

    body = client.get("/admin/api/operator/service-registry").json()

    assert body["workflow_overrides"] == {BUNDLED_ID: {"source": "owner", "fields": ["display_name"]}}
    assert OWNER_NAME not in str(body["workflow_overrides"])


def test_a_package_cannot_install_a_workflow_under_a_bundled_id():
    imported = {BUNDLED_ID: {"manifest": _owner_manifest(), "source": "imported", "provenance": {"package_id": "acme"}}}

    with pytest.raises(ServiceRegistryConflict, match="bundled"):
        service_registry.merge_imported_records(service_registry.normalize_service_registry({}), workflows=imported)


def test_a_reload_that_cannot_read_the_registry_keeps_the_workflows_it_serves(live, client, monkeypatch):
    owner_id = "image.generate.owner-own"
    assert _put(client, live, _with_workflow(
        proxy.ADMIN_STORE.get_service_registry(), _owner_manifest(id=owner_id))).status_code == 200
    assert _served_name(owner_id) == OWNER_NAME

    def unreadable(**kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(service_registry, "load_service_registry", unreadable)
    with pytest.raises(RuntimeError):
        proxy.reload_runtime_config()

    assert _served_name(owner_id) == OWNER_NAME


def _store_left_out_workflow(db) -> None:
    """Store a workflow under the bundled id that the registry leaves out: its manifest does not validate."""
    registry = admin_store.get_service_registry_with_generation(path=db)["value"]
    registry["workflows"] = {**(registry.get("workflows") or {}),
                             BUNDLED_ID: {"manifest": {**_owner_manifest(), "inputs": "not a list"}}}
    write_service_registry_for_test(registry, path=db)


def _repair(client, db, record):
    return client.put(f"/admin/api/operator/service-registry/excluded/workflows/{BUNDLED_ID}", json={"record": record},
                      headers={"If-Match": f'"os-{_generation(db)}"'})


def test_repairing_a_left_out_workflow_under_a_bundled_id_without_overrides_is_a_409(live, client):
    _store_left_out_workflow(live)
    excluded = client.get("/admin/api/operator/service-registry").json()["excluded"]
    assert any(item["id"] == BUNDLED_ID for item in excluded), excluded
    before = admin_store.get_service_registry_with_generation(path=live)

    refused = _repair(client, live, {"manifest": _owner_manifest()})

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "id_is_bundled"
    assert admin_store.get_service_registry_with_generation(path=live) == before
    assert _served_name() != OWNER_NAME


def test_repairing_a_left_out_bundled_workflow_as_a_run_control_patch_serves_the_release(live, client):
    _store_left_out_workflow(live)
    excluded = client.get("/admin/api/operator/service-registry").json()["excluded"]
    assert any(item["id"] == BUNDLED_ID for item in excluded), excluded

    repaired = _repair(client, live, {"patch_of": BUNDLED_ID, "state": "active", "published": False})

    assert repaired.status_code == 200, repaired.text
    body = repaired.json()
    assert all(item["id"] != BUNDLED_ID for item in body["excluded"])
    record = body["service_registry"]["workflows"][BUNDLED_ID]
    assert record["patch_of"] == BUNDLED_ID and record["published"] is False
    assert "manifest" not in record
    assert proxy.GATEWAY_WORKFLOW_REGISTRY.get(BUNDLED_ID).manifest == _bundled_manifest()


def test_a_whole_registry_save_with_a_run_control_patch_of_a_bundled_id_is_a_claim(live, client):
    # ``patch_of: <id>`` names the bundled id it changes, so it needs no ``overrides``; the bundled contract still serves.
    registry = copy.deepcopy(proxy.ADMIN_STORE.get_service_registry())
    registry.setdefault("workflows", {})[BUNDLED_ID] = {"patch_of": BUNDLED_ID, "state": "active", "published": False}

    saved = _put(client, live, registry)

    assert saved.status_code == 200, saved.text
    assert _served_name() == _bundled_manifest()["display_name"]
