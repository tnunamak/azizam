"""A second mesh-shaped workflow written only as data runs through the mesh route (CODE_TO_DATA_S3, S3.9, D9).

Nothing here is code for the second workflow: a contract document, a service record and the frontend row's workflow name.
A route that named the bundled workflow, its service, its path, its picture field or its option names in code would fail
these tests. The control is a mesh-shaped workflow with two required pictures: the grammar accepts it, the profile refuses it,
and the route never calls an engine for it.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import httpx
import pytest

import proxy
from gateway import frontend_profiles, mesh_api, service_registry
from gateway.manifests import ContractErrors, enforce_contract
from gateway.mesh_manifest import MeshManifestError
from tests.mesh_workflows import (
    SECOND_ID, SECOND_PATH, SECOND_SERVICE, SECOND_URL, bundled_document, second_document, workflow_of,
)
from tests.test_mesh_route import GLB, PNG, Harness, _client, _frontend, _multipart, _post


class SecondHarness(Harness):
    """The route harness with the second engine's service record and URL and the frontend row pointing at the second workflow."""

    def __init__(self, document: dict | None = None) -> None:
        super().__init__()
        self.document = document or second_document()
        raw = copy.deepcopy(self.services["trellis"]["config"]) if "config" in self.services["trellis"] else {}
        raw["base_url"] = SECOND_URL
        self.services[SECOND_SERVICE] = service_registry.service_from_backend_config(SECOND_SERVICE, raw, source="provisioned")
        self.current_manifest = lambda: workflow_of(self.document)
        _frontend(self, lambda row: [route.update(workflow=self.document["id"]) for route in row["registered_routes"]])

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{SECOND_URL}/health":
            return httpx.Response(200, content=b"ok")
        if url == f"{SECOND_URL}{SECOND_PATH}":
            self.downstream.append(request)
            return httpx.Response(200, content=GLB, headers={"content-type": "model/gltf-binary"})
        return super().handler(request)


def second_multipart(fields: dict[str, str] | None = None) -> dict:
    return {"data": fields or {}, "files": {"photo": ("p.png", PNG, "image/png")}}


def test_the_second_workflow_passes_the_grammar_and_the_profile() -> None:
    enforce_contract(second_document())
    assert mesh_api.mesh_profile_errors(second_document()) == []
    workflow = workflow_of(second_document())
    assert (workflow.id, workflow.service_id, workflow.transport_path, workflow.image_input) == (SECOND_ID, SECOND_SERVICE, SECOND_PATH, "photo")


def test_the_second_workflow_runs_through_the_route_with_its_own_names() -> None:
    harness = SecondHarness()
    response = _post(_client(harness), **second_multipart({"detail": "512", "quality": "best", "salt": "-3"}))
    assert response.status_code == 200 and response.content == GLB
    assert response.headers["content-type"] == "model/gltf-binary"
    [sent] = harness.downstream
    body = sent.content
    for expected in (b'name="source_image"', b'name="detail_level"', b'name="mode"', b'name="salt_value"', b"512", b"best", b"-3"):
        assert expected in body, expected
    for absent in (b'name="image"', b'name="resolution"', b'name="seed"'):
        assert absent not in body, absent


def test_the_second_workflow_fills_its_own_defaults_and_not_the_bundled_ones() -> None:
    harness = SecondHarness()
    assert _post(_client(harness), **second_multipart()).status_code == 200
    body = harness.downstream[0].content
    assert b'name="detail_level"' in body and b"256" in body and b'name="mode"' in body and b"fast" in body
    assert b'name="salt_value"' not in body, "an option with no default is left out"


def test_the_second_workflow_refuses_by_its_own_contract() -> None:
    harness = SecondHarness()
    client = _client(harness)
    assert _post(client, **second_multipart({"detail": "300"})).status_code == 400
    assert _post(client, **second_multipart({"salt": "9"})).status_code == 400
    assert _post(client, **second_multipart({"resolution": "512"})).status_code == 400, "a bundled option is unknown here"
    wrong_part = _post(client, **_multipart())
    assert wrong_part.status_code == 400 and wrong_part.json()["error"]["code"] == "unknown_field", "the part is named by the contract"
    assert harness.downstream == []


def test_the_bundled_route_does_not_serve_the_second_workflows_names() -> None:
    harness = Harness()
    response = _post(_client(harness), **second_multipart())
    assert response.status_code == 400 and harness.downstream == []


# --- another contract under the bundled id runs through the live registry reader ------------------------------------------

def test_a_different_contract_under_the_bundled_id_is_what_the_live_reader_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    document = second_document()
    document["id"] = bundled_document()["id"]
    record = SimpleNamespace(contract=document, identity=SimpleNamespace(manifest_hash="h"))
    registry = proxy.GATEWAY_WORKFLOW_REGISTRY
    original = registry.get(document["id"])
    monkeypatch.setattr(registry, "get", lambda key, *a, **k: record if key == document["id"] else original)
    workflow = proxy.current_mesh_workflow()
    assert (workflow.image_input, workflow.service_id) == ("photo", SECOND_SERVICE)


# --- the control: two required pictures --------------------------------------------------------------------------------------

def two_picture_document() -> dict:
    doc = second_document()
    doc["inputs"]["second_photo"] = copy.deepcopy(doc["inputs"]["photo"])
    doc["bindings"]["http-multipart"]["map"]["second_photo"] = "other_image"
    return doc


def test_the_two_picture_control_is_valid_to_the_grammar_and_refused_by_the_profile() -> None:
    doc = two_picture_document()
    enforce_contract(doc)
    assert "exactly one required picture input" in mesh_api.mesh_profile_errors(doc)
    with pytest.raises(MeshManifestError, match="exactly one required picture input"):
        workflow_of(doc)
    with pytest.raises(ContractErrors) as caught:
        frontend_profiles.check(doc)
    assert caught.value.primary.code == frontend_profiles.PROFILE_REFUSED and caught.value.primary.path == ("intent",)


def test_the_two_picture_control_is_refused_at_save() -> None:
    with pytest.raises(ContractErrors) as caught:
        service_registry.normalize_workflow_record(SECOND_ID, two_picture_document())
    assert caught.value.primary.code == frontend_profiles.PROFILE_REFUSED
    service_registry.normalize_workflow_record(SECOND_ID, second_document())  # the valid neighbour saves


def test_a_record_that_fails_the_profile_gets_a_503_and_no_engine_call() -> None:
    harness = SecondHarness(two_picture_document())
    response = _post(_client(harness), **second_multipart())
    assert response.status_code == 503 and response.json()["error"]["code"] == "service_unavailable"
    assert harness.downstream == []


# --- the seam and the source -------------------------------------------------------------------------------------------------

def test_the_profile_seam_names_no_intent_and_core_modules_import_no_mesh_module() -> None:
    import re
    from pathlib import Path

    seam = Path(frontend_profiles.__file__).read_text(encoding="utf-8")
    assert "mesh" not in seam.lower()
    root = Path(frontend_profiles.__file__).parent
    for name in ("manifests.py", "service_registry.py", "extension_packages.py", "binding_descriptors.py", "workflow_contract.py"):
        assert not re.search(r"^\s*(from|import)\s+gateway(\.| import )\s*mesh", (root / name).read_text(encoding="utf-8"), re.M), name
