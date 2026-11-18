"""A stored retired owner workflow gets 409 workflow_disabled at the image job route."""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path
from unittest.mock import AsyncMock

import proxy
from gateway import admin_store, discovery, registry_layers, service_registry
from gateway.registry_snapshot import build, bundled_release
from tests.registry_test_helpers import write_legacy_registry_for_test
from tests.support.access import authenticated, everything, identity
from tests.support.gateway import gateway_client, use_admin_db


def test_retired_stored_rows_do_not_reappear_or_run(monkeypatch, tmp_path):
    db = use_admin_db(monkeypatch, tmp_path)
    run_control_ids = {f"image.generate.retired-control-{index}" for index in range(6)}
    owner_id = "image.generate.retired-example"
    manifest = copy.deepcopy(discovery.bundled_workflow_manifests()["image.generate.krea2-turbo"])
    manifest["id"] = owner_id
    manifest["bindings"]["comfyui"]["workflow"] = "workflows/images/retired-example.json"
    assert not (Path(__file__).resolve().parents[1] / manifest["bindings"]["comfyui"]["workflow"]).exists()
    workflows = {
        wid: {"id": wid, "patch_of": wid, "state": "disabled", "enabled": True,
              "published": True, "record_revision": 1}
        for wid in run_control_ids
    }
    workflows[owner_id] = {
        "id": owner_id, "source": "ui", "overrides": owner_id, "manifest": manifest,
        "policy": {"exposure": "disabled", "trust_tier": "stable", "resource_tier": "batch",
                   "resource_class": "image", "tags": []},
        "state": "disabled", "enabled": True, "published": True, "record_revision": 1,
    }
    stored = {**registry_layers.empty_stored(service_registry.SERVICE_REGISTRY_SCHEMA_VERSION), "workflows": workflows}
    write_legacy_registry_for_test(stored, path=db)
    admin_store.record_migration_step(registry_layers.LAYERING_STEP, path=db)
    admin_store.record_migration_step(service_registry.WORKFLOW_SCHEMA_2_STEP, path=db)

    effective = service_registry.ensure_service_registry(path=db, actor="test")
    assert admin_store.migration_step_recorded(service_registry.WORKFLOW_RECORD_STEP, path=db)
    assert service_registry.load_service_registry_exclusions(path=db) == []
    snapshot = build(effective, bundled_release(), generation=1)
    assert not run_control_ids & snapshot.workflows.keys()
    assert snapshot.workflows[owner_id].state == "disabled"
    workflow_registry = proxy._load_gateway_workflow_registry(strict=True)
    assert workflow_registry.get(owner_id) is not None
    monkeypatch.setattr(proxy, "GATEWAY_WORKFLOW_REGISTRY", workflow_registry)
    monkeypatch.setattr(type(proxy.GATEWAY_REGISTRY_PROVIDER), "current", lambda self: snapshot)
    monkeypatch.setattr(proxy, "WORKFLOW_SNAPSHOTS", proxy.GATEWAY_REGISTRY_PROVIDER)
    monkeypatch.setattr(proxy, "_litellm_models_payload", AsyncMock(return_value={"object": "list", "data": []}))

    async def exercise():
        with authenticated(identity(everything())):
            async with gateway_client(client=("203.0.113.28", 1234)) as client:
                listing = await client.get("/v1/models")
                job = await client.post("/gateway/v1/media/jobs", json={
                    "intent": "image.generate", "request": {"model": "retired-example", "prompt": "a tree"},
                })
        return listing, job

    listing, job = asyncio.run(exercise())
    assert listing.status_code == 200, listing.text
    ids = {row["id"] for row in listing.json()["data"]}
    assert not ids & (run_control_ids | {wid.rsplit(".", 1)[-1] for wid in run_control_ids} | {owner_id, "retired-example"})
    assert job.status_code == 409, job.text
    assert job.json()["error"]["code"] == "workflow_disabled"


def test_orphan_merge_patch_is_excluded_at_startup_and_in_owner_api(monkeypatch, tmp_path):
    from tests.test_registry_apply_failure import _owner_client

    db = use_admin_db(monkeypatch, tmp_path)
    wid = "image.generate.retired-patch"
    row = {"id": wid, "state": "disabled", "policy": {"exposure": "disabled"}}
    stored = {**registry_layers.empty_stored(service_registry.SERVICE_REGISTRY_SCHEMA_VERSION), "workflows": {wid: row}}
    write_legacy_registry_for_test(stored, path=db)
    admin_store.record_migration_step(registry_layers.LAYERING_STEP, path=db)
    admin_store.record_migration_step(service_registry.WORKFLOW_SCHEMA_2_STEP, path=db)
    effective = service_registry.ensure_service_registry(path=db)
    assert wid not in effective["workflows"]
    snapshot = build(effective, bundled_release(), generation=1)
    assert wid not in snapshot.workflows
    assert admin_store.get_service_registry_with_generation(path=db)["value"]["workflows"][wid] == row
    excluded = service_registry.load_service_registry_exclusions(path=db)
    assert any(item.id == wid and "base" in item.error.lower() for item in excluded)
    monkeypatch.setattr(type(proxy.GATEWAY_REGISTRY_PROVIDER), "current", lambda self: snapshot)
    monkeypatch.setattr(proxy, "WORKFLOW_SNAPSHOTS", proxy.GATEWAY_REGISTRY_PROVIDER)
    monkeypatch.setattr(proxy, "_litellm_models_payload", AsyncMock(return_value={"object": "list", "data": []}))

    async def models():
        with authenticated(identity(everything())):
            async with gateway_client(client=("203.0.113.28", 1234)) as client:
                return await client.get("/v1/models")

    response = asyncio.run(models())
    assert response.status_code == 200
    assert wid not in {item["id"] for item in response.json()["data"]}
    response = _owner_client(monkeypatch).get("/admin/api/operator/service-registry")
    assert response.status_code == 200, response.text
    assert any(item["id"] == wid for item in response.json()["excluded"])


def test_startup_rejects_malformed_stored_section_and_mismatched_id(tmp_path):
    import pytest

    for workflows in ([], {"one": {"id": "two", "state": "disabled"}}):
        db = tmp_path / ("section.db" if isinstance(workflows, list) else "id.db")
        admin_store.init_db(db)
        stored = {**registry_layers.empty_stored(service_registry.SERVICE_REGISTRY_SCHEMA_VERSION), "workflows": workflows}
        write_legacy_registry_for_test(stored, path=db)
        admin_store.record_migration_step(registry_layers.LAYERING_STEP, path=db)
        admin_store.record_migration_step(service_registry.WORKFLOW_SCHEMA_2_STEP, path=db)
        with pytest.raises(ValueError):
            service_registry.ensure_service_registry(path=db)
