"""A later 3D stage (texture, retopology, rig, animate) is one data record: a mesh in, a mesh out, over the same
multipart binding the picture-to-3D records use. The stage here is a test stage on a fake engine; the shipped stages are
records of the same shape."""

from __future__ import annotations

import asyncio
import copy

import httpx
import pytest

import proxy
from gateway import registry_snapshot, service_registry
from gateway.discovery import WorkflowRegistry
from gateway.manifests import Provenance, enforce_contract
from tests.media_test_bytes import glb_bytes
from tests.support.gateway import gateway_client
from tests.support.mesh_harness import PNG
from tests.test_adapter_differential_s7_mesh_rows import rig  # noqa: F401

STAGE_ID = "mesh.edit.test-stage"
STAGE_URL = "http://127.0.0.1:8899"
STAGE_GLB = glb_bytes(b"staged-model" * 8)
STAGE_IN = glb_bytes(b"input-model" * 8)


def stage_document() -> dict:
    return {
        "schema_version": 2, "min_gateway_version": "0.1", "version": "0.1.0", "id": STAGE_ID,
        "intent": "mesh.edit", "capability": "mesh", "display_name": "Test stage", "purpose": "A test stage for a 3D model",
        "description": "Takes a 3D model and returns another.", "summary": "Send a model; receive a model.",
        "inputs": {
            "mesh": {"label": "model", "type": "asset", "accepts": ["model/gltf-binary"], "required": True},
            "strength": {"label": "strength", "type": "integer", "minimum": 0, "maximum": 10, "default": 5},
        },
        "outputs": {"mesh": {"media_type": "model/gltf-binary"}},
        "bindings": {"http-multipart": {"service": "mesh-stage", "path": "/stage", "map": {"mesh": "model", "strength": "strength"}}},
    }


def test_the_stage_document_is_valid_to_the_grammar() -> None:
    enforce_contract(stage_document())


@pytest.fixture
def staged(rig, monkeypatch):  # noqa: F811
    """The rig plus a fake engine behind the test stage, on the same card as the other 3D services."""
    harness = rig
    capacity = copy.deepcopy(proxy_config_capacity())
    harness.services["mesh-stage"] = service_registry.service_from_backend_config(
        "mesh-stage", {"base_url": STAGE_URL, "capabilities": ["mesh"], "resource_capacity": capacity}, source="provisioned")
    harness.stage_requests = []
    original = harness.handler

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{STAGE_URL}/health":
            return httpx.Response(200, content=b"ok")
        if str(request.url) == f"{STAGE_URL}/stage":
            harness.stage_requests.append(request)
            return httpx.Response(200, content=STAGE_GLB, headers={"content-type": "model/gltf-binary"})
        return original(request)

    harness.handler = handler
    saved = proxy.GATEWAY_WORKFLOW_REGISTRY.contents()
    held = proxy.GATEWAY_WORKFLOW_REGISTRY.get("mesh.generate.trellis-2")
    changed = WorkflowRegistry()
    for record in proxy.GATEWAY_WORKFLOW_REGISTRY.records():
        changed.add_manifest(copy.deepcopy(record.manifest), source_path=record.source_path,
                             provenance=Provenance(trusted=record.identity.trusted, source=record.identity.source), facts=record.facts)
    changed.add_manifest(stage_document(), source_path=held.source_path,
                         provenance=Provenance(trusted=held.identity.trusted, source=held.identity.source), facts=held.facts)
    proxy.GATEWAY_WORKFLOW_REGISTRY.adopt(changed)
    # The run route reads the registry snapshot, which an adopted registry does not rebuild by itself.
    release = registry_snapshot.BundledRelease({record.id: record for record in changed.records()}, registry_snapshot.load_frontend_rows())
    previous = proxy.GATEWAY_REGISTRY_PROVIDER.swap(registry_snapshot.build({}, release, generation=1))
    yield harness
    proxy.GATEWAY_REGISTRY_PROVIDER.swap(previous)
    proxy.GATEWAY_WORKFLOW_REGISTRY.restore(saved)


def proxy_config_capacity() -> dict:
    from tests.support.mesh_harness import GOOD_CAPACITY
    return dict(GOOD_CAPACITY)


def test_a_stage_is_no_row_of_the_picture_to_3d_routes(staged) -> None:
    assert STAGE_ID not in {workflow.id for workflow in proxy.current_mesh_workflows()}
    assert STAGE_ID not in {capability.workflow for capability in proxy._mesh_facts({})}

    async def scenario():
        async with gateway_client() as client:
            return await client.post("/v1/3d/generations/glb", data={"model": STAGE_ID},
                                     files={"image": ("photo.png", PNG, "image/png")})

    assert asyncio.run(scenario()).status_code == 404


def test_a_stage_runs_a_model_through_its_own_service(staged) -> None:
    async def scenario():
        async with gateway_client() as client:
            return await client.post(f"/gateway/v1/workflows/{STAGE_ID}/run", headers={"Prefer": "wait=10"},
                                     files={"mesh": ("in.glb", STAGE_IN, "model/gltf-binary")}, data={"strength": "3"})

    response = asyncio.run(scenario())
    assert response.status_code == 200, response.text
    result = response.json()["results"][0]
    assert (result["output"], result["media_type"], result["size"]) == ("mesh", "model/gltf-binary", len(STAGE_GLB))
    assert len(staged.stage_requests) == 1 and b"strength" in staged.stage_requests[0].content
    assert STAGE_IN in staged.stage_requests[0].content        # the engine received the model that was sent


def test_a_stage_refuses_a_file_that_is_not_a_model(staged) -> None:
    async def scenario():
        async with gateway_client() as client:
            return await client.post(f"/gateway/v1/workflows/{STAGE_ID}/run", headers={"Prefer": "wait=10"},
                                     files={"mesh": ("in.png", PNG, "image/png")})

    response = asyncio.run(scenario())
    assert response.status_code in (400, 415, 422), response.text
    assert staged.stage_requests == []
