"""Data-owned identity regressions; fixtures never write shipped registry files."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from gateway import mesh_api
from gateway.capabilities import Capability, ResourceEstimate
from gateway.mesh_manifest import MeshManifestError

ROOT = Path(__file__).resolve().parents[1]

# Captured by source inspection at f8e56732469d179adeb2840dbd0177e137fa68a7:
# gateway/mesh_api.py:150-175 returns this row from
# registry/protocol_frontends.yml:953-957 without changing it.
BASE_ROUTE = {
    "methods": ["POST"],
    "canonical": "/v1/3d/generations",
    # Train 54 gave every route row its MCP exposure fact; the row still comes back unchanged.
    "mcp_not_exposed": "Use the workflow catalog or HTTP; no standalone MCP target is declared.",
    "intent": "mesh.generate",
    "capability": "mesh",
    "runtime_policy": {
        "protocol": "openai.mesh",
        "intent": "mesh.generate",
        "capability_exposure": "default",
        "capability_trust_tier": "stable",
        "capability_resource_tier": "batch",
    },
}
# gateway/mesh_api.py:186-207 and Capability defaults at
# gateway/capabilities.py:107-153 capture the entire base value, not a subset.
BASE_FACTS = Capability(
    intent="mesh.generate", backend="trellis", workflow="mesh.generate.trellis-2",
    capability="mesh", title="TRELLIS.2 image to 3D", input_modalities=("image",),
    output_modalities=("mesh",), execution="sync", protocol="openai.mesh",
    output_media_types=("model/gltf-binary",), exposure="default",
    trust_tier="stable", resource_tier="batch",
    resources=ResourceEstimate(gpu_required=True),
)


def _document(relative):
    return yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))


@pytest.fixture
def record():
    records = _document("registry/protocol_frontends.yml")["records"]
    return deepcopy(next(item for item in records if item["id"] == "mesh"))


@pytest.fixture
def contract():
    return deepcopy(_document("manifests/workflows/mesh.generate.trellis-2.yml"))


def _row(record):
    return next(row for row in record["registered_routes"]
                if row["canonical"] == mesh_api.LISTING_PATH)


def _metadata(record):
    # Existing-base fixtures remain useful while the separate shipped-data test is red.
    return _row(record).setdefault("discovery_metadata", {
        "input_modalities": ["image"], "output_modalities": ["mesh"],
    })


def _workflow(contract):
    return mesh_api.mesh_workflow(contract, "fixture-hash")


def _deps(manifest, records):
    def no_client():
        raise AssertionError("No network client should be needed in this test")
    return mesh_api.MeshApiDeps(
        current_manifest=lambda: manifest, frontend_records=lambda: records,
        services=lambda: {}, client_factory=no_client,
    )


def _evidence(manifest, records):
    return asyncio.run(mesh_api.pool_evidence(_deps(manifest, records)))[manifest.service_id]


def test_shipped_row_declares_modalities(record):
    assert _row(record).get("discovery_metadata") == {
        "input_modalities": ["image"], "output_modalities": ["mesh"],
    }


def test_renamed_owner_drives_tiers_and_availability(record, contract):
    record["id"] = "atelier-3d"
    _metadata(record)
    _row(record)["runtime_policy"].update(
        capability_exposure="opt_in", capability_trust_tier="private",
        capability_resource_tier="expensive",
    )
    row, reason = mesh_api.route_row([record])
    assert row is _row(record) and reason is None
    facts = mesh_api.discovery_facts(_workflow(contract), [record])
    assert (facts.exposure, facts.trust_tier, facts.resource_tier) == (
        "opt_in", "private", "expensive",
    )
    assert _evidence(_workflow(contract), [record])["reason"] != mesh_api.FRONTEND_ABSENT


@pytest.mark.parametrize("enabled, reason", [
    (None, mesh_api.FRONTEND_ABSENT), (False, mesh_api.FRONTEND_DISABLED),
])
def test_setup_states_keep_their_reasons_and_disabled_facts(record, contract, enabled, reason):
    records = [] if enabled is None else [record]
    record["enabled"] = enabled
    assert mesh_api.route_row(records) == (None, reason)
    facts = mesh_api.discovery_facts(_workflow(contract), records)
    assert facts == replace(BASE_FACTS, exposure="disabled", trust_tier="experimental")
    assert _evidence(_workflow(contract), records)["reason"] == reason


@pytest.mark.parametrize("second_enabled", [True, False])
def test_duplicate_owners_are_refused_even_if_one_is_disabled(record, second_enabled):
    second = deepcopy(record)
    second.update(id="atelier-3d", enabled=second_enabled)
    row, reason = mesh_api.route_row([record, second])
    assert row is None
    assert "multiple" in reason and "owner" in reason


def test_get_only_row_is_not_an_owner(record):
    _row(record)["methods"] = ["GET"]
    assert mesh_api.route_row([record]) == (None, mesh_api.FRONTEND_ABSENT)


def test_get_only_record_does_not_shadow_post_owner(record):
    get_record = deepcopy(record)
    _row(get_record)["methods"] = ["GET"]
    record["id"] = "atelier-3d"
    assert mesh_api.route_row([get_record, record]) == (_row(record), None)


def test_two_listing_rows_in_one_record_are_refused(record):
    record["registered_routes"].append(deepcopy(_row(record)))
    row, reason = mesh_api.route_row([record])
    assert row is None and "exactly one" in reason


def test_mixed_methods_are_refused(record):
    _row(record)["methods"] = ["GET", "POST"]
    row, reason = mesh_api.route_row([record])
    assert row is None and "only POST" in reason


def test_custom_identity_drives_workflow_facts_title_and_service_check(record, contract, monkeypatch):
    monkeypatch.setattr(mesh_api, "CAPABILITY_LABELS", {
        **mesh_api.CAPABILITY_LABELS, "sculpture": "Sculptures",
    })
    record["id"] = "atelier-3d"
    _metadata(record)
    _row(record).update(capability="sculpture", intent="sculpture.create")
    _row(record)["runtime_policy"]["intent"] = "sculpture.create"
    contract.update(capability="sculpture", intent="sculpture.create")
    # A workflow that states no display name is titled by its capability's label (here the custom one).
    contract.pop("display_name", None)
    manifest = _workflow(contract)
    assert manifest.intent == "sculpture.create"
    facts = mesh_api.discovery_facts(manifest, [record])
    assert (facts.capability, facts.title, facts.intent) == (
        "sculpture", "Sculptures", "sculpture.create",
    )
    called = []
    def service_check(services, service_id, capability):
        called.append((service_id, capability))
        return "fixture service unavailable", None, None
    monkeypatch.setattr(mesh_api, "exclusive_pool_problem", service_check)
    assert _evidence(manifest, [record])["reason"] == "fixture service unavailable"
    assert called == [("trellis", "sculpture")]


@pytest.mark.parametrize("target, field, value", [
    ("row", "capability", "image"),
    ("row", "intent", "image.generate"),
    ("policy", "intent", "image.generate"),
    ("policy", "capability", "image"),
    ("contract", "capability", "image"),
    ("contract", "intent", "image.generate"),
])
def test_identity_mismatches_refuse_discovery_and_probe(record, contract, monkeypatch, target, field, value):
    _metadata(record)
    manifest = _workflow(contract)
    if target == "contract":
        changed = deepcopy(manifest.data)
        changed[field] = value
        manifest = replace(manifest, data=changed,
                           intent=value if field == "intent" else manifest.intent)
    else:
        destination = _row(record) if target == "row" else _row(record)["runtime_policy"]
        destination[field] = value
    def unexpected(*args):
        raise AssertionError("A rejected identity must not reach the service check")
    monkeypatch.setattr(mesh_api, "exclusive_pool_problem", unexpected)
    with pytest.raises(MeshManifestError, match="intent|capability"):
        mesh_api.discovery_facts(manifest, [record])
    answer = _evidence(manifest, [record])
    assert answer["available"] is False
    assert "intent" in answer["reason"] or "capability" in answer["reason"]


@pytest.mark.parametrize("capability", ["unregistered", "", None, []])
def test_unknown_capability_is_refused_without_keyerror(record, contract, capability):
    _metadata(record)
    _row(record)["capability"] = capability
    with pytest.raises(MeshManifestError, match="capability"):
        mesh_api.discovery_facts(_workflow(contract), [record])
    row, reason = mesh_api.route_row([record])
    assert row is None and "capability" in reason


def test_custom_modality_comes_from_metadata_not_the_media_type(record, contract, monkeypatch):
    monkeypatch.setattr(mesh_api, "MODALITY_WORDS", frozenset({
        *mesh_api.MODALITY_WORDS, "surface-model",
    }))
    _metadata(record)["output_modalities"] = ["surface-model"]
    facts = mesh_api.discovery_facts(_workflow(contract), [record])
    assert facts.output_modalities == ("surface-model",)
    assert facts.input_modalities == ("image",)
    assert facts.output_media_types == ("model/gltf-binary",)


@pytest.mark.parametrize("field, value", [
    ("input_modalities", ["arbitrary-word"]),
    ("output_modalities", ["arbitrary-word"]),
    ("input_modalities", "image"),
    ("output_modalities", []),
    ("output_modalities", [None]),
])
def test_invalid_modalities_are_refused(record, contract, field, value):
    _metadata(record)[field] = value
    with pytest.raises(MeshManifestError, match="modalit"):
        mesh_api.discovery_facts(_workflow(contract), [record])


def test_missing_metadata_is_refused_for_an_enabled_owner(record, contract):
    _row(record).pop("discovery_metadata", None)
    with pytest.raises(MeshManifestError, match="discovery_metadata"):
        mesh_api.discovery_facts(_workflow(contract), [record])


def test_complete_capability_and_existing_route_fields_match_base(record, contract):
    # Only the explicitly requested new metadata differs from the raw base row.
    actual, reason = mesh_api.route_row([record])
    assert reason is None
    assert {key: value for key, value in actual.items() if key != "discovery_metadata"} == BASE_ROUTE
    assert mesh_api.discovery_facts(_workflow(contract), [record]) == BASE_FACTS
    # Feeding the unchanged base row gives literally the same route_row result.
    base_record = deepcopy(record)
    base_row = _row(base_record)
    base_row.clear()
    base_row.update(deepcopy(BASE_ROUTE))
    assert mesh_api.route_row([base_record]) == (BASE_ROUTE, None)


@pytest.mark.parametrize("words", [None, "image", [], ["image", ""], ["image", 4], ["image", "image"]])
def test_modality_vocabulary_rejects_malformed_words(monkeypatch, words):
    from gateway import capability_ids
    fixture_path = Path("in-memory-vocabulary.yml")
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: yaml.safe_dump({
        "schema_version": 1, "modality_words": words,
    }))
    with pytest.raises(ValueError, match="modality_words"):
        capability_ids.load_modality_words(fixture_path)


@pytest.mark.parametrize("enabled", [None, False])
@pytest.mark.parametrize("name", ["renamed-result", "audio"])
def test_disabled_discovery_uses_output_media_type(record, contract, enabled, name):
    contract["outputs"] = {name: next(iter(contract["outputs"].values()))}
    record["enabled"] = enabled
    facts = mesh_api.discovery_facts(_workflow(contract), [] if enabled is None else [record])
    assert facts.output_modalities == ("mesh",)


def test_disabled_discovery_refuses_unknown_output_modality(contract):
    manifest = _workflow(contract)
    changed = deepcopy(manifest.data)
    changed["outputs"] = {"mesh": {"media_type": "application/x-unknown"}}
    with pytest.raises(MeshManifestError, match="output_modality_missing"):
        mesh_api.discovery_facts(replace(manifest, data=changed), [])


def test_output_media_mapping_is_replaceable_data(contract, monkeypatch):
    manifest = _workflow(contract)
    monkeypatch.setattr(mesh_api, "MEDIA_TYPE_MODALITIES", {manifest.output_media_type: "surface-model"})
    monkeypatch.setattr(mesh_api, "MODALITY_WORDS", {*mesh_api.MODALITY_WORDS, "surface-model"})
    assert mesh_api.discovery_facts(manifest, []).output_modalities == ("surface-model",)


@pytest.mark.parametrize("rows", [[], {"invalid": "mesh"}, {"model/gltf-binary": "unregistered"},
                                  {"model/gltf-binary": []}, {"model/": "mesh"}])
def test_media_modality_mapping_refuses_invalid_data(tmp_path, rows):
    from gateway.capability_ids import load_media_type_modalities
    path = tmp_path / "words.yml"
    path.write_text(yaml.safe_dump({"modality_words": ["mesh"], "media_type_modalities": rows}))
    with pytest.raises(ValueError, match="media_type_modalities"):
        load_media_type_modalities(path)


def test_media_modality_mapping_loads_registered_custom_words(tmp_path):
    from gateway.capability_ids import load_media_type_modalities
    path = tmp_path / "words.yml"
    path.write_text(yaml.safe_dump({"modality_words": ["surface-model"],
                                    "media_type_modalities": {"application/x-surface": "surface-model"}}))
    assert load_media_type_modalities(path) == {"application/x-surface": "surface-model"}
