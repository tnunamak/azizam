"""Every reader of a workflow reads the schema 2 contract and the record-level facts (CODE_TO_DATA_S3, S3.4).

The wire stays as it was. ``tests/fixtures/workflow_reader_outputs.json.gz`` holds the output of every record-consuming
entry point (``tests/workflow_reader_outputs.py``) for every bundled workflow, captured with the code of the commit before
the readers switched. The new output must equal it, so a change that both sides of a comparison share (the old test's
flaw) is seen.

How the test can fail for real. Each consumer also runs on a "v2-only" record whose stored document (``manifest``)
refuses every access and whose contract is a mapping that records each key read. A consumer that reads the stored
document, or needs a v1 shape inside the contract (the picture type ``image``, a top-level ``requires``, ``transport``),
raises or differs. The recorded reads are kept per consumer: none holds a v1-only key, and each reads what it must.
"""

from __future__ import annotations

import copy
import dataclasses
import gzip
import json
import sys
import types
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from gateway import capabilities, mesh_api, mesh_manifest
from gateway.availability import project_capabilities
from gateway.discovery import BUNDLED_WORKFLOWS_DIR, WorkflowRecord, WorkflowRegistry
from gateway.workflow_convert import PolicyFacts, normalize
from gateway.workflow_view import requirements_of
from tests.workflow_reader_outputs import CONSUMERS, plain, read_one

OLD_OUTPUT = Path(__file__).parent / "fixtures" / "workflow_reader_outputs.json.gz"
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


def _old_output() -> dict[str, dict[str, Any]]:
    with gzip.open(OLD_OUTPUT, "rt", encoding="utf-8") as handle:
        return json.load(handle)


def _without_stored_inputs(name: str, output: Any) -> Any:
    """The summaries list inputs from the stored document (see ``Refusing``); the v2-only run cannot, so both drop them."""
    if name.startswith("summary:"):
        return {key: value for key, value in output.items() if key != "inputs"}
    return output


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


def test_every_consumer_reads_only_the_v2_contract(registry: WorkflowRegistry) -> None:
    per_consumer: dict[str, list[tuple[bool, str]]] = {name: [] for name in CONSUMERS}
    old = _old_output()
    for record in registry.records():
        for name, consumer in CONSUMERS.items():
            reads = per_consumer[name]
            probe = v2_only(record, reads)
            got = _without_stored_inputs(name, plain(consumer(probe)))
            assert got == _without_stored_inputs(name, old[record.id][name]), (record.id, name)
    for name, reads in per_consumer.items():
        seen = {key for _, key in reads}
        assert not seen & V1_KEYS_ANYWHERE, (name, sorted(seen & V1_KEYS_ANYWHERE))
        assert "requires" not in {key for top, key in reads if top}, f"{name} read a top-level requires"
        assert MUST_READ.get(name, set()) <= seen, (name, sorted(MUST_READ.get(name, set()) - seen))


def test_composed_workflows_still_declare_what_they_need(registry: WorkflowRegistry) -> None:
    # Enumerated from what the stored documents say, not from the reader under test: a record the reader drops is seen.
    composed = [record for record in registry.records() if record.manifest.get("requires")]
    assert len(composed) >= 20
    for record in composed:
        assert "requires" not in record.contract  # in the v2 contract it lives in a binding
        assert requirements_of(record.contract) == record.manifest["requires"], record.id


def _with_stored_document(record: WorkflowRecord, manifest: dict[str, Any]) -> WorkflowRecord:
    return dataclasses.replace(record, manifest=manifest, contract=None, facts=None, stored_schema_version=None)


@pytest.mark.parametrize("bindings", [("gateway", "comfyui"), ("comfyui", "gateway"), ("comfyui", "other"), ("gateway", "comfyui", "other")])
def test_requirements_survive_every_binding_shape_the_validator_accepts(registry: WorkflowRegistry, bindings: tuple[str, ...]) -> None:
    base = registry.get("image.analyze.face-likeness")
    manifest = copy.deepcopy(base.manifest)
    assert manifest["requires"] == {"services": ["faces"]}
    only = next(iter(manifest["bindings"].values()))
    manifest["bindings"] = {key: copy.deepcopy(only) for key in bindings}
    record = _with_stored_document(base, manifest)
    assert requirements_of(record.contract) == manifest["requires"]
    assert "requires" not in record.contract
    if "gateway" not in bindings:
        return  # the gateway's requirements apply to a workflow the gateway runs; the others are shown by the reader above
    projected = project_capabilities([capabilities.capability_from_workflow_record(record, backend="gateway")], workflow_records=[record])
    single = project_capabilities([capabilities.capability_from_workflow_record(base)], workflow_records=[base])
    assert projected[0].availability["evidence"]["source"] == "dependencies"  # chosen by the requirements
    assert projected[0].availability == single[0].availability


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


def _mesh_contract(registry: WorkflowRegistry) -> dict[str, Any]:
    return copy.deepcopy(normalize(registry.get(MESH_ID).manifest)[0])


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
    record = dataclasses.replace(registry.get(MESH_ID), contract=contract)
    with pytest.raises(mesh_manifest.MeshManifestError):
        mesh_api.mesh_workflow_from_record(record)


# A change the profile accepts (the contract is still a valid mesh contract) that the route would act on differently from
# the old validator's reading of the same file: the cross-check refuses it.
DRIFT = {
    "default changed": lambda c: c["inputs"]["resolution"].__setitem__("default", 512),
    "default removed": lambda c: c["inputs"]["resolution"].pop("default"),
    "maximum changed": lambda c: c["inputs"]["seed"].__setitem__("maximum", 256),
    "minimum changed": lambda c: c["inputs"]["seed"].__setitem__("minimum", 5),
    "enum narrowed": lambda c: c["inputs"]["resolution"].__setitem__("enum", [512, 1024]),
    "type changed": lambda c: c["inputs"]["bg_removal"].__setitem__("type", "integer"),
    "input added": lambda c: c["inputs"].__setitem__("extra", {"type": "string"}),
    "input removed": lambda c: c["inputs"].pop("extensions"),
    "default null added": lambda c: c["inputs"]["bg_removal"].__setitem__("default", None),
}


@pytest.mark.parametrize("name", sorted(DRIFT))
def test_mesh_cross_check_compares_the_scalars_the_route_binds(registry: WorkflowRegistry, name: str) -> None:
    old = mesh_manifest.load_mesh_manifest(BUNDLED_WORKFLOWS_DIR / "mesh.generate.trellis-2.yml")
    record = registry.get(MESH_ID)
    mesh_api.mesh_workflow_from_record(record, checked_against=old)  # the untouched record agrees
    contract = _mesh_contract(registry)
    DRIFT[name](contract)
    assert mesh_api.mesh_profile_errors(contract) == [], "the profile alone accepts this; only the cross-check can refuse it"
    with pytest.raises(mesh_manifest.MeshManifestError, match="disagree"):
        mesh_api.mesh_workflow_from_record(dataclasses.replace(record, contract=contract), checked_against=old)


def test_facts_are_record_data_with_the_old_defaults_filled_in() -> None:
    facts = PolicyFacts(exposure="hidden", trust_tier="experimental", resource_tier="interactive", tags=("a",))
    assert PolicyFacts() == PolicyFacts(None, None, None, ())
    from proxy import _image_policy_metadata  # noqa: PLC0415 - proxy is heavy; this test needs only this function

    assert _image_policy_metadata(PolicyFacts(), trusted=False) == {
        "exposure": "default", "trust_tier": "experimental", "resource_tier": "batch", "tags": ()}
    assert _image_policy_metadata(PolicyFacts(), trusted=True)["trust_tier"] == "stable"
    assert _image_policy_metadata(facts, trusted=True) == {
        "exposure": "hidden", "trust_tier": "experimental", "resource_tier": "interactive", "tags": ("a",)}


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


def _explicit_capability_manifest(registry: WorkflowRegistry, annotations: Any) -> dict[str, Any]:
    manifest = copy.deepcopy(registry.get(MESH_ID).manifest)
    manifest["capability"] = "declared-capability"
    manifest["annotations"] = {"azizam.dev/backfill": annotations}
    return manifest


@pytest.mark.parametrize("annotation", [None, 7, {"capability": "x"}, {"other": 1}, "text", []])
def test_a_declared_capability_is_shown_whatever_the_annotations_hold(registry: WorkflowRegistry, annotation: Any) -> None:
    base = registry.get(MESH_ID)
    record = _with_stored_document(base, _explicit_capability_manifest(registry, annotation))
    assert not record.facts.capability_backfilled
    (cap,) = _caps_of(record)
    assert cap.capability == "declared-capability"


def test_a_capability_the_converter_inferred_is_not_shown_as_declared(registry: WorkflowRegistry) -> None:
    base = registry.get(MESH_ID)
    manifest = copy.deepcopy(base.manifest)
    manifest.pop("capability", None)
    record = _with_stored_document(base, manifest)
    assert record.facts.capability_backfilled
    (cap,) = _caps_of(record)
    assert cap.capability is None


def _caps_of(record: WorkflowRecord) -> list[Any]:
    return [capabilities.capability_from_workflow_record(record, backend=b) for b in record.supported_backends]


def _image_row(record: WorkflowRecord) -> dict[str, Any]:
    out = read_one(record, "member_catalog")
    rows = [row for row in _walk_rows(out) if row.get("name") == "image" and row.get("asset")]
    assert len(rows) == 1
    return rows[0]


def _walk_rows(value: Any) -> Any:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_rows(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_rows(item)


def test_a_picture_input_keeps_the_media_type_it_declared(registry: WorkflowRegistry) -> None:
    base = registry.get(MESH_ID)
    manifest = copy.deepcopy(base.manifest)
    assert manifest["inputs"]["image"]["type"] == "image"
    manifest["inputs"]["image"]["media_type"] = "image/png"
    record = _with_stored_document(base, manifest)
    # What the old helper answered for the stored spec: the declared type, else the intent's default.
    assert str(manifest["inputs"]["image"]["media_type"]).strip() == "image/png"
    assert record.facts.declared_media("image") == "image/png"
    assert _image_row(record)["media_type"] == "image/png"
    undeclared = _with_stored_document(base, base.manifest)
    assert undeclared.facts.declared_media("image") == ""
    assert "media_type" not in _image_row(undeclared)  # the intent has no default picture type: the old output left it out
