"""A second mesh-shaped workflow written only as data (CODE_TO_DATA_S3, S3.9, D9).

Nothing here is code for the second workflow: a contract document and a service record. The control is a mesh-shaped
workflow with two required pictures: the grammar accepts it, the profile refuses it, and the save refuses it.

S7 (builder D4): the tests that posted the second workflow to ``POST /v1/meshes/generate`` through the old route harness left
with that route (the route and its harness are gone). The 3D rows select the workflow by registry record and run it through
the generic multipart runner, which names no workflow, field or option (tests/test_adapter_differential_s7_mesh_rows.py
runs the bundled workflow through them end to end). What stays here needs no route.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import proxy
from gateway import frontend_profiles, mesh_api, service_registry
from gateway.discovery import WorkflowRegistry
from gateway.manifests import ContractErrors, Provenance, enforce_contract
from gateway.mesh_manifest import MeshManifestError
from tests.mesh_workflows import SECOND_ID, SECOND_SERVICE, bundled_document, second_document, workflow_of


# --- another contract under the bundled id runs through the live registry reader ------------------------------------------

def test_a_different_contract_under_the_bundled_id_is_what_the_live_reader_serves() -> None:
    document = second_document()
    document["id"] = bundled_document()["id"]
    registry = proxy.GATEWAY_WORKFLOW_REGISTRY
    saved = registry.contents()
    held = registry.get(document["id"])
    changed = WorkflowRegistry()
    changed.add_manifest(document, source_path=held.source_path, provenance=Provenance(trusted=held.identity.trusted, source=held.identity.source), facts=held.facts)
    registry.adopt(changed)
    try:
        workflow = proxy.current_mesh_workflow(document["id"])
    finally:
        registry.restore(saved)
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


# --- the seam and the source -------------------------------------------------------------------------------------------------

def test_the_profile_seam_names_no_intent_and_core_modules_import_no_mesh_module() -> None:
    import re
    from pathlib import Path

    seam = Path(frontend_profiles.__file__).read_text(encoding="utf-8")
    assert "mesh" not in seam.lower()
    root = Path(frontend_profiles.__file__).parent
    for name in ("manifests.py", "service_registry.py", "extension_packages.py", "binding_descriptors.py", "workflow_contract.py"):
        assert not re.search(r"^\s*(from|import)\s+gateway(\.| import )\s*mesh", (root / name).read_text(encoding="utf-8"), re.M), name
