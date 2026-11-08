"""Every reader of a workflow reads the schema 2 contract and the record-level facts (CODE_TO_DATA_S3, S3.4).

The wire stays as it was. ``tests/fixtures/workflow_reader_outputs.json.gz`` holds the output of every record-consuming
entry point (``tests/workflow_reader_outputs.py``) for every bundled workflow, captured with the code of the commit before
the readers switched. The new output must equal it, so a change that both sides of a comparison share (the old test's
flaw) is seen.

How the test can fail for real. Each consumer also runs on a record whose document is a mapping that records each key read.
The recorded reads are kept per consumer: none holds a key the schema 2 document does not have (the picture type, a
top-level ``requires``, ``transport``), none holds a policy fact (exposure, trust_tier, resource_tier, tags, trusted; those
are record data, read from ``record.facts`` and ``record.identity``), and each reads what it must.

The golden moved at S3.5a, when the bundled files became schema 2. Only the entries below moved; every other entry is
byte for byte what the readers gave before the switch:

- ``schema_version`` 1 to 2 and the new ``manifest_hash`` in the three summaries of all 38 workflows (the file changed);
- the capability of the 33 workflows that did not declare one (``capabilities`` and ``projected``): the converter wrote
  the inferred capability into the file, so it is declared now;
- the mesh workflow: its picture input is an ``asset`` accepting three types (design open question 6), so its summary
  input type, input modalities (``text`` to ``asset`` and ``text``), home modalities and catalog media type changed;
- ace-step: its ``prompt`` input is gone (it and ``tags`` wrote the same node input); the route sends the prompt to
  ``tags`` as before, so only the catalog, schema, mapping and example entries lose ``prompt``.
"""

from __future__ import annotations

import copy
import dataclasses
import gzip
import json
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from gateway import capabilities, mesh_api, mesh_manifest
from gateway.availability import project_capabilities
from gateway.discovery import BUNDLED_WORKFLOWS_DIR, WorkflowRecord, WorkflowRegistry
from gateway.workflow_view import requirements_of
from tests.workflow_reader_outputs import CONSUMERS, plain, read_one

OLD_OUTPUT = Path(__file__).parent / "fixtures" / "workflow_reader_outputs.json.gz"
# What only the old shape had. ``requires`` is v1-only at the top of the document: in v2 it lives in a binding.
V1_KEYS_ANYWHERE = {"trusted", "source", "transport", "modality"}
# Record data, not document data: a reader that reads one of these at the top of the document reads the wrong place.
POLICY_KEYS_AT_TOP = {"exposure", "trust_tier", "resource_tier", "tags", "trusted", "source"}
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


def recorded(record: WorkflowRecord, reads: list[tuple[bool, str]]) -> WorkflowRecord:
    """The record with its document replaced by one that notes every key read."""
    return dataclasses.replace(record, manifest=Recording(record.manifest, reads))


@pytest.fixture(scope="module")
def registry() -> WorkflowRegistry:
    return WorkflowRegistry.from_directory(BUNDLED_WORKFLOWS_DIR)


def _old_output() -> dict[str, dict[str, Any]]:
    with gzip.open(OLD_OUTPUT, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def test_every_consumer_gives_the_output_it_gave_before_the_switch(registry: WorkflowRegistry) -> None:
    old = _old_output()
    records = registry.records()
    assert {record.id for record in records} == set(old)
    assert len(records) >= 30 and MESH_ID in old
    for record in records:
        for name in CONSUMERS:
            assert read_one(record, name) == old[record.id][name], (record.id, name)


# What each consumer must read of the contract, over all bundled workflows: a revert to the stored document is seen by the
# refusing manifest, and a consumer that stops reading what it must (a hidden fallback) is seen here.
MUST_READ = {
    "summary:client": {"display_name", "version", "bindings", "outputs", "intent"},
    "capabilities": {"inputs", "outputs", "display_name", "capability", "action_label", "bindings"},
    "schema": {"inputs", "display_name", "summary"},
    "advertised": {"inputs"},
    "route_inputs": {"inputs", "bindings"},
    "mapping": {"inputs", "bindings"},
    "openapi_catalog": {"outputs", "typical_seconds", "display_name"},
    "member_catalog": {"inputs", "license", "summary", "capability"},
    "extension_targets": {"inputs", "bindings"},
    "home": {"inputs", "typical_seconds"},
    "projected": {"bindings"},
}


def test_every_consumer_reads_only_the_v2_contract_and_no_policy_fact(registry: WorkflowRegistry) -> None:
    per_consumer: dict[str, list[tuple[bool, str]]] = {name: [] for name in CONSUMERS}
    old = _old_output()
    for record in registry.records():
        for name, consumer in CONSUMERS.items():
            reads = per_consumer[name]
            got = plain(consumer(recorded(record, reads)))
            assert got == old[record.id][name], (record.id, name)
    for name, reads in per_consumer.items():
        seen = {key for _, key in reads}
        assert not seen & V1_KEYS_ANYWHERE, (name, sorted(seen & V1_KEYS_ANYWHERE))
        assert not {key for top, key in reads if top} & POLICY_KEYS_AT_TOP, (name, "read a policy fact from the document")
        assert "requires" not in {key for top, key in reads if top}, f"{name} read a top-level requires"
        assert MUST_READ.get(name, set()) <= seen, (name, sorted(MUST_READ.get(name, set()) - seen))


def test_composed_workflows_still_declare_what_they_need(registry: WorkflowRegistry) -> None:
    # Enumerated from what the stored documents say, not from the reader under test: a record the reader drops is seen.
    composed = [record for record in registry.records() if any(b.get("requires") for b in record.manifest["bindings"].values())]
    assert len(composed) >= 20
    for record in composed:
        assert "requires" not in record.contract  # in the v2 contract it lives in a binding
        (binding,) = record.contract["bindings"].values()
        assert requirements_of(record.contract) == binding["requires"], record.id


@pytest.mark.parametrize("bindings", [("gateway", "comfyui"), ("comfyui", "gateway"), ("comfyui", "other"), ("gateway", "comfyui", "other")])
def test_requirements_survive_every_binding_shape_the_validator_accepts(registry: WorkflowRegistry, bindings: tuple[str, ...]) -> None:
    base = registry.get("image.analyze.face-likeness")
    manifest = copy.deepcopy(base.manifest)
    only = next(iter(manifest["bindings"].values()))
    assert only["requires"] == {"services": ["faces"]}
    manifest["bindings"] = {key: copy.deepcopy(only) for key in bindings}
    record = dataclasses.replace(base, manifest=manifest)
    assert requirements_of(record.contract) == only["requires"]
    assert "requires" not in record.contract
    if "gateway" not in bindings:
        return  # the gateway's requirements apply to a workflow the gateway runs; the others are shown by the reader above
    projected = project_capabilities([capabilities.capability_from_workflow_record(record, backend="gateway")], workflow_records=[record])
    single = project_capabilities([capabilities.capability_from_workflow_record(base)], workflow_records=[base])
    assert projected[0].availability["evidence"]["source"] == "dependencies"  # chosen by the requirements
    assert projected[0].availability == single[0].availability


def test_mesh_route_reads_its_contract_for_the_picture_input(registry: WorkflowRegistry) -> None:
    reads: list[tuple[bool, str]] = []
    record = recorded(registry.get(MESH_ID), reads)
    workflow = mesh_api.mesh_workflow_from_record(record)
    assert workflow.image_input == "image"
    assert workflow.inputs[workflow.image_input]["type"] == "asset"  # the contract's name for the picture
    assert not {key for _, key in reads} & V1_KEYS_ANYWHERE


def test_mesh_profile_refuses_a_workflow_the_wire_cannot_carry(registry: WorkflowRegistry) -> None:
    contract = registry.get(MESH_ID).contract
    assert mesh_api.mesh_profile_errors(contract) == []
    two = copy.deepcopy(contract)
    two["inputs"]["second"] = {"type": "asset", "accepts": ["image/png"], "required": True}
    assert mesh_api.mesh_profile_errors(two) == ["exactly one required picture input"]
    other = copy.deepcopy(contract)
    other["outputs"]["mesh"]["media_type"] = "image/png"
    assert mesh_api.mesh_profile_errors(other) == ["exactly one output, model/gltf-binary"]
    assert mesh_api.mesh_profile_errors({**contract, "bindings": {}}) == ["exactly one binding"]


def _mesh_contract(registry: WorkflowRegistry) -> dict[str, Any]:
    return copy.deepcopy(registry.get(MESH_ID).contract)


def _drop(key: str):
    return lambda contract: contract.pop(key)


MALFORMED = {
    "missing id": _drop("id"),
    "missing version": _drop("version"),
    "id not text": lambda c: c.__setitem__("id", 7),
    "version blank": lambda c: c.__setitem__("version", " "),
    "inputs not a mapping": lambda c: c.__setitem__("inputs", ["image"]),
    "no inputs": lambda c: c.__setitem__("inputs", {}),
    "scalar spec None": lambda c: c["inputs"].__setitem__("resolution", None),
    "scalar spec a string": lambda c: c["inputs"].__setitem__("resolution", "integer"),
    "scalar type a list": lambda c: c["inputs"]["resolution"].__setitem__("type", []),
    "scalar type a dict": lambda c: c["inputs"]["resolution"].__setitem__("type", {}),
    "scalar type unknown": lambda c: c["inputs"]["resolution"].__setitem__("type", "float"),
    "scalar default of the wrong type": lambda c: c["inputs"]["resolution"].__setitem__("default", "big"),
    "range of the wrong type": lambda c: c["inputs"]["seed"].__setitem__("maximum", "high"),
    "enum empty": lambda c: c["inputs"]["resolution"].__setitem__("enum", []),
    "picture takes only png": lambda c: c["inputs"]["image"].__setitem__("accepts", ["image/png"]),
    "picture takes nothing listed": lambda c: c["inputs"]["image"].pop("accepts"),
    "map names an unknown input": lambda c: next(iter(c["bindings"].values()))["map"].__setitem__("ghost", "ghost"),
    "picture not mapped": lambda c: next(iter(c["bindings"].values()))["map"].pop("image"),
    "an object input mapped": lambda c: next(iter(c["bindings"].values()))["map"].__setitem__("extensions", "extensions"),
}


@pytest.mark.parametrize("name", sorted(MALFORMED))
def test_mesh_refuses_a_malformed_contract_as_a_manifest_error(registry: WorkflowRegistry, name: str) -> None:
    contract = _mesh_contract(registry)
    MALFORMED[name](contract)
    assert mesh_api.mesh_profile_errors(contract)  # an answer, never an exception
    with pytest.raises(mesh_manifest.MeshManifestError):
        mesh_api.mesh_workflow(contract, "hash")
    record = dataclasses.replace(registry.get(MESH_ID), manifest=contract)
    with pytest.raises(mesh_manifest.MeshManifestError):
        mesh_api.mesh_workflow_from_record(record)


# --- second fix round ---------------------------------------------------------------------------------------------------

NOT_A_MAPPING_ROOTS = {"a list": [], "a string": "mesh", "None": None}


@pytest.mark.parametrize("root", sorted(NOT_A_MAPPING_ROOTS))
def test_mesh_refuses_a_contract_root_that_is_not_a_mapping(registry: WorkflowRegistry, root: str) -> None:
    contract = NOT_A_MAPPING_ROOTS[root]
    assert mesh_api.mesh_profile_errors(contract)  # an answer, never an exception
    with pytest.raises(mesh_manifest.MeshManifestError):
        mesh_api.mesh_workflow(contract, "hash")
    # A record whose contract is None would be re-derived from its stored document, so the record is a plain stand-in.
    record = types.SimpleNamespace(contract=contract, identity=registry.get(MESH_ID).identity)
    with pytest.raises(mesh_manifest.MeshManifestError):
        mesh_api.mesh_workflow_from_record(record)


@pytest.mark.parametrize("key", sorted(POLICY_KEYS_AT_TOP))
def test_control_a_policy_fact_read_from_the_document_is_seen(registry: WorkflowRegistry, key: str) -> None:
    """The key-read check above fails for a reader that takes one of the record's facts from the document."""
    reads: list[tuple[bool, str]] = []
    record = recorded(registry.get(MESH_ID), reads)
    record.manifest.get(key)
    assert {name for top, name in reads if top} & POLICY_KEYS_AT_TOP == {key}
