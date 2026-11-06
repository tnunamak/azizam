"""C5: a stored workflow manifest beats the bundled one with its id, and a save is live (docs/design/CONFIG_HOMES.md, 8.4).

The save goes through the real registry route and the live gateway; nothing is faked. The bundled manifest is whatever
``manifests/workflows/`` ships under the id the tests pick, so a release change to it does not break them.
"""
from __future__ import annotations

import copy

import pytest

import proxy
from gateway import admin_store, discovery, service_registry
from gateway.service_registry import ServiceRegistryConflict
from tests.registry_test_helpers import write_service_registry_for_test
from tests.test_frontend_mount_reload import live  # noqa: F401 - fixture
from tests.test_registry_apply_failure import _owner_client

BUNDLED_ID = "image.generate.flux2-klein-4b"
OWNER_NAME = "Owner's own Klein"


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


def _served_name() -> str:
    return proxy.GATEWAY_WORKFLOW_REGISTRY.get(BUNDLED_ID).manifest["display_name"]


def test_a_stored_manifest_with_a_bundled_id_serves_instead_of_the_bundled_one_without_a_restart(live, client):
    bundled_name = _served_name()
    assert bundled_name != OWNER_NAME
    registry = _with_workflow(proxy.ADMIN_STORE.get_service_registry(), _owner_manifest(), overrides=BUNDLED_ID)

    saved = _put(client, live, registry)

    assert saved.status_code == 200, saved.text
    assert _served_name() == OWNER_NAME  # live: no reload call and no new import of proxy
    # A fresh boot reads the same winner.
    assert proxy._load_gateway_workflow_registry().get(BUNDLED_ID).manifest["display_name"] == OWNER_NAME


def test_the_bundled_workflows_the_owner_did_not_touch_keep_serving(live, client):
    ids_before = proxy.GATEWAY_WORKFLOW_REGISTRY.ids()
    registry = _with_workflow(proxy.ADMIN_STORE.get_service_registry(), _owner_manifest(), overrides=BUNDLED_ID)

    assert _put(client, live, registry).status_code == 200

    assert proxy.GATEWAY_WORKFLOW_REGISTRY.ids() == ids_before


def test_a_save_under_a_bundled_id_without_overrides_is_a_409_that_carries_the_bundled_manifest(live, client):
    registry = _with_workflow(proxy.ADMIN_STORE.get_service_registry(), _owner_manifest())
    generation = _generation(live)

    refused = _put(client, live, registry)

    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "id_is_bundled"
    bundled = refused.json()["bundled_workflow"]
    assert bundled == _bundled_manifest()  # a client can copy it, edit the copy and save it with `overrides`
    assert _generation(live) == generation  # nothing stored
    assert _served_name() != OWNER_NAME


def test_overrides_must_name_the_id_it_overrides(live, client):
    registry = _with_workflow(proxy.ADMIN_STORE.get_service_registry(), _owner_manifest(), overrides="some.other.id")

    assert _put(client, live, registry).status_code == 409


def test_the_registry_read_back_saves_again_without_overrides_once_the_override_is_stored(live, client):
    first = _put(client, live, _with_workflow(
        proxy.ADMIN_STORE.get_service_registry(), _owner_manifest(), overrides=BUNDLED_ID))
    assert first.status_code == 200, first.text

    again = _put(client, live, first.json()["service_registry"])  # an unchanged override is not a new claim

    assert again.status_code == 200, again.text
    assert _served_name() == OWNER_NAME


def test_an_edit_to_a_stored_override_asks_for_overrides_again(live, client):
    first = _put(client, live, _with_workflow(
        proxy.ADMIN_STORE.get_service_registry(), _owner_manifest(), overrides=BUNDLED_ID))
    edited = _with_workflow(first.json()["service_registry"], _owner_manifest(display_name="Edited"))

    assert _put(client, live, edited).status_code == 409


def test_a_workflow_with_an_id_the_release_does_not_ship_needs_no_overrides(live, client):
    manifest = _owner_manifest(id="image.generate.owner-own")

    saved = _put(client, live, _with_workflow(proxy.ADMIN_STORE.get_service_registry(), manifest))

    assert saved.status_code == 200, saved.text
    assert proxy.GATEWAY_WORKFLOW_REGISTRY.get("image.generate.owner-own") is not None


def test_reset_to_inherit_brings_the_bundled_manifest_back(live, client):
    first = _put(client, live, _with_workflow(
        proxy.ADMIN_STORE.get_service_registry(), _owner_manifest(), overrides=BUNDLED_ID))
    assert _served_name() == OWNER_NAME
    reset = copy.deepcopy(first.json()["service_registry"])
    del reset["workflows"][BUNDLED_ID]

    assert _put(client, live, reset).status_code == 200

    assert _served_name() == _bundled_manifest()["display_name"]
    assert proxy.GATEWAY_WORKFLOW_REGISTRY.get(BUNDLED_ID).manifest == _bundled_manifest()


def test_the_registry_read_names_each_override_with_the_fields_that_differ_and_no_values(live, client):
    _put(client, live, _with_workflow(
        proxy.ADMIN_STORE.get_service_registry(), _owner_manifest(), overrides=BUNDLED_ID))

    body = client.get("/admin/api/operator/service-registry").json()

    assert body["workflow_overrides"] == {BUNDLED_ID: {"source": "owner", "fields": ["display_name"]}}
    assert OWNER_NAME not in str(body["workflow_overrides"])


def test_a_package_cannot_install_a_workflow_under_a_bundled_id():
    imported = {BUNDLED_ID: {"manifest": _owner_manifest(), "source": "imported", "provenance": {"package_id": "acme"}}}

    with pytest.raises(ServiceRegistryConflict, match="bundled"):
        service_registry.merge_imported_records(service_registry.normalize_service_registry({}), workflows=imported)


def test_a_reload_that_cannot_read_the_registry_keeps_the_workflows_it_serves(live, client, monkeypatch):
    assert _put(client, live, _with_workflow(
        proxy.ADMIN_STORE.get_service_registry(), _owner_manifest(), overrides=BUNDLED_ID)).status_code == 200

    def unreadable(**kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(service_registry, "load_service_registry", unreadable)
    with pytest.raises(RuntimeError):
        proxy.reload_runtime_config()

    assert _served_name() == OWNER_NAME


def test_the_pre_deploy_report_lists_ids_and_field_names_and_never_a_value(live, capsys):
    from scripts import workflow_override_report

    registry = _with_workflow(
        service_registry.normalize_service_registry({}), _owner_manifest(description="sentinel-value-must-never-print"))
    write_service_registry_for_test(registry, path=live)

    status = workflow_override_report.main([str(live)])

    out = capsys.readouterr().out
    assert status == 1  # a stored workflow with a bundled id changes what is served once C5 is live
    assert BUNDLED_ID in out and "description" in out and "display_name" in out
    assert "sentinel-value-must-never-print" not in out and OWNER_NAME not in out


def test_the_pre_deploy_report_is_clean_when_no_stored_workflow_has_a_bundled_id(live, capsys):
    from scripts import workflow_override_report

    write_service_registry_for_test(service_registry.normalize_service_registry({}), path=live)

    assert workflow_override_report.main([str(live)]) == 0
    assert "with a bundled id: 0" in capsys.readouterr().out


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


def test_repairing_a_left_out_workflow_under_a_bundled_id_with_overrides_serves_it(live, client):
    _store_left_out_workflow(live)

    repaired = _repair(client, live, {"manifest": _owner_manifest(), "overrides": BUNDLED_ID})

    assert repaired.status_code == 200, repaired.text
    assert _served_name() == OWNER_NAME
