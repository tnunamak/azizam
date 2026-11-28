"""The access plan editor reads its choices from the service registry (data, not code)."""

from __future__ import annotations

from gateway.access_plans import plan_from_document
from gateway.admin_catalog import access_plan_editor_contract

from tests.support.mesh_harness import OWNER_MESH_MANIFEST


def _registry() -> dict[str, object]:
    return {
        "services": {
            "extension-engine": {"enabled": True, "name": "Extension engine", "capabilities": ["chat"],
                                 "config": {"models": [{"id": "extension-public-model"}]}},
            "parked-engine": {"enabled": False, "capabilities": ["image"],
                              "config": {"models": [{"id": "parked-image-model"}]}},
            "extension-cloud": {"enabled": True, "config": {"provider_type": "paid-remote", "api_key_env": "EXTENSION_CLOUD_KEY"}},
        },
        "workflows": {},
    }


def _items(contract, capability):
    return {item["id"]: item for entry in contract["capabilities"] if entry["id"] == capability
            for item in entry["items"]}


def test_data_only_services_become_choices_without_code_ids():
    contract = access_plan_editor_contract(_registry())
    assert _items(contract, "chat")["extension-public-model"] == {
        "kind": "model", "id": "extension-public-model", "available": True}
    assert _items(contract, "image")["parked-image-model"]["available"] is False
    assert {engine["id"] for engine in contract["engines"]} == {"extension-engine", "parked-engine"}
    assert [provider["id"] for provider in contract["paid_providers"]] == ["extension-cloud"]


def test_new_plan_document_is_a_valid_local_only_plan():
    plan = plan_from_document(access_plan_editor_contract(_registry())["new_plan"] | {"name": "New"}, plan_id="new")
    assert plan.paid_providers == frozenset()
    assert not plan.direct_engine_access.granted
    assert all(grant.all_items for grant in plan.can_use.values())
    assert plan.categories == frozenset()


def test_a_mesh_workflow_is_offered_under_3d_models_and_nothing_else():
    registry = _registry()
    registry["workflows"] = {"mesh.generate.owner-demo": {
        "id": "mesh.generate.owner-demo", "enabled": True, "manifest": OWNER_MESH_MANIFEST,
    }}
    contract = access_plan_editor_contract(registry)
    labels = {entry["id"]: entry["label"] for entry in contract["capabilities"]}
    assert labels["mesh"] == "3D models"
    assert _items(contract, "mesh") == {
        "mesh.generate.owner-demo": {"kind": "workflow", "id": "mesh.generate.owner-demo", "available": True}}
    assert "mesh.generate.owner-demo" not in _items(contract, "image")


def test_discovered_plan_models_use_snapshot_names_and_connection_metadata():
    from gateway import registry_snapshot
    registry = {"services": {"member-plan": {"name": "Member plan", "config": {
        "credential_source": {"kind": "cli-session", "connection_id": "member-owner"},
        "models": [],
    }}}}
    snapshot = registry_snapshot.build(registry, registry_snapshot.BundledRelease({}), generation=1,
        connection_models={"member-owner": {"models": {"model-one": [], "model-two": []}}})
    contract = access_plan_editor_contract(registry, snapshot=snapshot)
    assert _items(contract, "chat") == {
        mid: {"kind": "model", "id": mid, "available": True, "connection_ids": ["member-owner"]}
        for mid in ("model-one", "model-two")}
    assert not any(_items(contract, cap) for cap in ("image", "video", "speech"))
    assert registry["services"]["member-plan"]["config"]["models"] == []

    # The HTTP editor must use the snapshot attached by the gateway middleware,
    # not the stored registry whose discovery rows are empty.
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from gateway.admin_api import AdminContext, create_admin_router
    app = FastAPI()
    @app.middleware("http")
    async def held_snapshot(request, call_next):
        request.state.registry_snapshot = snapshot
        return await call_next(request)
    app.include_router(create_admin_router(
        SimpleNamespace(get_service_registry=lambda: registry),
        auth=lambda request: AdminContext(subject="owner", roles=frozenset({"owner"}))))
    response = TestClient(app).get("/admin/api/access-plans/editor")
    assert response.status_code == 200, response.text
    assert response.json() == contract
