"""Every reader of a workflow reads the schema 2 contract and the record-level facts (CODE_TO_DATA_S3, S3.4).

How the test can fail for real. A consumer is run twice on each bundled workflow: on the ordinary record, and on a
"v2-only" record whose stored document (``manifest``) refuses every access and whose contract is a mapping that records
each key read. The two runs must give equal output, so a consumer that reads the stored document, or needs a v1 shape
inside the contract (the picture type ``image``, a top-level ``requires``, ``transport``), raises or differs. The
recorded reads must hold none of the v1-only keys. An AST scan would see only key spellings; the mesh picture type is a
value, not a key, and only a run sees that.
"""

from __future__ import annotations

import copy
import sys
from collections.abc import Mapping
from typing import Any

import pytest

from gateway import capabilities, extension_catalog, home_api, mesh_api, mesh_manifest, workflow_openapi
from gateway.availability import project_capabilities
from gateway.discovery import BUNDLED_WORKFLOWS_DIR, WorkflowRecord, WorkflowRegistry, summarize_workflow
from gateway.workflow_convert import PolicyFacts, normalize
from gateway.workflow_view import requirements_of
from gateway.workflows_catalog import _input_rows

# What only the old shape had. ``requires`` is v1-only at the top of the document: in v2 it lives in a binding.
V1_KEYS_ANYWHERE = {"trusted", "source", "transport", "modality"}
MESH_ID = "mesh.generate.trellis-2"


class Recording(dict):
    """A dict that notes the keys read through ``get``, ``[]`` and ``in``. Values come back wrapped the same way."""

    def __init__(self, data: Mapping[str, Any], reads: list[tuple[bool, str]], top: bool = True) -> None:
        super().__init__({key: _wrap(value, reads) for key, value in data.items()})
        self._reads = reads
        self._top = top

    def get(self, key: Any, default: Any = None) -> Any:
        self._reads.append((self._top, str(key)))
        return super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        self._reads.append((self._top, str(key)))
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self._reads.append((self._top, str(key)))
        return super().__contains__(key)


def _wrap(value: Any, reads: list[tuple[bool, str]]) -> Any:
    if isinstance(value, Mapping):
        return Recording(value, reads, top=False)
    if isinstance(value, list):
        return [_wrap(item, reads) for item in value]
    return value


class Refusing(dict):
    """The stored document of a v2-only record: any read of it is a reader that has not switched. The one exception is
    named: the discovery summary lists inputs from the stored document, because the mesh picture type is ``image`` on the
    wire (``image`` in the summaries golden) and ``asset`` in the contract."""

    def _refuse(self, *_a: Any, **_k: Any) -> Any:
        raise AssertionError("a reader read the stored document; it must read the contract")

    def get(self, key: Any, default: Any = None) -> Any:
        caller = sys._getframe(1)
        if key == "inputs" and caller.f_globals["__name__"] == "gateway.discovery" and caller.f_code.co_name == "summarize_workflow":
            return {}
        return self._refuse()

    __getitem__ = __contains__ = __iter__ = items = keys = values = __len__ = _refuse


def v2_only(record: WorkflowRecord, reads: list[tuple[bool, str]]) -> WorkflowRecord:
    contract, facts = normalize(record.manifest)
    return WorkflowRecord(
        manifest=Refusing(), identity=record.identity, source_path=record.source_path,
        contract=Recording(contract, reads), facts=facts, stored_schema_version=record.stored_schema_version)


@pytest.fixture(scope="module")
def registry() -> WorkflowRegistry:
    return WorkflowRegistry.from_directory(BUNDLED_WORKFLOWS_DIR)


def read_everything(record: WorkflowRecord) -> dict[str, Any]:
    """The output of every switched consumer for one record."""
    out: dict[str, Any] = {}
    for audience in ("client", "admin", "internal"):
        # The summary's input list is the one reader left on the stored document (see ``Refusing``).
        out[f"summary:{audience}"] = {k: v for k, v in summarize_workflow(record, audience).items() if k != "inputs"}
    for backend in record.supported_backends:
        out[f"capability:{backend}"] = capabilities.capability_from_workflow_record(record, backend=backend)
    out["schema"] = workflow_openapi.workflow_inputs_schema(record)
    out["schema_form"] = workflow_openapi.workflow_inputs_schema(record, form=True)
    out["advertised"] = workflow_openapi.advertised_inputs_schema(record)
    out["extension_schema"] = workflow_openapi.extension_inputs_schema(record)
    out["owned"] = workflow_openapi.gateway_owned_inputs(record)
    out["route_inputs"] = workflow_openapi.route_inputs(record)
    out["mapping"] = {mode: workflow_openapi.request_mapping(record, mode) for mode in ("sync", "async")}
    out["catalog"] = workflow_openapi.workflow_catalog([record])
    out["catalog_inputs"] = _input_rows(record, {})
    out["controls"] = extension_catalog._workflow_extension_controls(record.contract)
    out["requires"] = requirements_of(record.contract)
    out["home_modalities"] = home_api._required_input_modalities(
        capabilities.capability_from_workflow_record(record), [record])
    out["home_seconds"] = home_api._typical_seconds(capabilities.capability_from_workflow_record(record), None, [record])
    out["projected"] = project_capabilities(
        [capabilities.capability_from_workflow_record(record)], workflow_records=[record])
    return out


def test_every_consumer_reads_only_the_v2_contract(registry: WorkflowRegistry) -> None:
    reads: list[tuple[bool, str]] = []
    records = [record for record in registry.records() if record.id != MESH_ID]
    assert len(records) >= 30
    for record in records:
        assert read_everything(v2_only(record, reads)) == read_everything(record), record.id
    seen = {key for _, key in reads}
    assert not seen & V1_KEYS_ANYWHERE, sorted(seen & V1_KEYS_ANYWHERE)
    assert "requires" not in {key for top, key in reads if top}, "a reader read a top-level requires"
    assert {"inputs", "bindings", "intent"} <= seen  # the recording did see the readers work


def test_composed_workflows_still_declare_what_they_need(registry: WorkflowRegistry) -> None:
    composed = [record for record in registry.records() if requirements_of(record.contract)]
    assert composed, "some bundled workflow declares requirements"
    for record in composed:
        assert "requires" not in record.contract  # in the v2 contract it lives in the binding
        assert requirements_of(record.contract) == (record.manifest.get("requires") or None)


def test_mesh_route_reads_its_contract_for_the_picture_input(registry: WorkflowRegistry) -> None:
    reads: list[tuple[bool, str]] = []
    record = v2_only(registry.get(MESH_ID), reads)
    old = mesh_manifest.load_mesh_manifest(BUNDLED_WORKFLOWS_DIR / "mesh.generate.trellis-2.yml")
    workflow = mesh_api.mesh_workflow_from_record(record, checked_against=old)
    assert workflow.image_input == old.image_input == "image"
    assert workflow.inputs[workflow.image_input]["type"] == "asset"  # the contract's name for the old picture type
    picture = mesh_manifest.ImageAsset(b"x", "input.png", "image/png")
    supplied = {"resolution": "512", "seed": "7"}
    request = mesh_manifest.bind_request(workflow, supplied, picture)
    assert request.values == mesh_manifest.bind_request(old, supplied, picture).values
    assert mesh_manifest.downstream_form(workflow, request) == mesh_manifest.downstream_form(old, request)
    with pytest.raises(mesh_manifest.MeshInputError):
        mesh_manifest.bind_request(workflow, supplied, None)
    assert not {key for _, key in reads} & V1_KEYS_ANYWHERE


def test_mesh_profile_refuses_a_workflow_the_wire_cannot_carry(registry: WorkflowRegistry) -> None:
    contract = normalize(registry.get(MESH_ID).manifest)[0]
    assert mesh_api.mesh_profile_errors(contract) == []
    two = copy.deepcopy(contract)
    two["inputs"]["second"] = {"type": "asset", "accepts": ["image/png"], "required": True}
    assert mesh_api.mesh_profile_errors(two) == ["exactly one required picture input"]
    other = copy.deepcopy(contract)
    other["outputs"]["mesh"]["media_type"] = "image/png"
    assert mesh_api.mesh_profile_errors(other) == ["exactly one output, model/gltf-binary"]
    assert mesh_api.mesh_profile_errors({**contract, "bindings": {}}) == ["exactly one binding"]


def test_facts_are_record_data_with_the_old_defaults_filled_in() -> None:
    facts = PolicyFacts(exposure="hidden", trust_tier="experimental", resource_tier="interactive", tags=("a",))
    assert PolicyFacts() == PolicyFacts(None, None, None, ())
    from proxy import _image_policy_metadata  # noqa: PLC0415 - proxy is heavy; this test needs only this function

    assert _image_policy_metadata(PolicyFacts(), trusted=False) == {
        "exposure": "default", "trust_tier": "experimental", "resource_tier": "batch", "tags": ()}
    assert _image_policy_metadata(PolicyFacts(), trusted=True)["trust_tier"] == "stable"
    assert _image_policy_metadata(facts, trusted=True) == {
        "exposure": "hidden", "trust_tier": "experimental", "resource_tier": "interactive", "tags": ("a",)}
