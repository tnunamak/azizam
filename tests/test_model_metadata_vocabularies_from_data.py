"""Metadata vocabularies and semantic roles are release data, not Python enums."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
import yaml

from gateway import model_metadata as mm


def _document():
    return {
        "schema_version": 1,
        "types": ["language", "olfactory", "image"],
        "input_modalities": ["image", "file", "scent", "text"],
        "output_modalities": ["text", "image", "scent"],
        "tags": ["olfaction", "vision"],
        "tag_types": ["language", "olfactory"],
        "derivation": {
            "scalar_input": "text",
            "fallback_output": "text",
            "default_type": "olfactory",
            "output_priority": [
                {"modality": "scent", "type": "olfactory"},
                {"modality": "image", "type": "image"},
            ],
            "input_priority": [
                {"modality": "scent", "type": "olfactory"},
                {"modality": "image", "type": "image"},
            ],
        },
        "implications": [
            {"type": "olfactory", "tag": "olfaction", "input_modality": "scent"},
        ],
    }


def _write(tmp_path, name, document):
    path = tmp_path / name
    if name not in ("operations.yml", "intents.yml", "services.yml"):
        document = {"schema_version": 1, "exact": [], "families": [], "contract": document}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _install(tmp_path, monkeypatch, document):
    contract = mm.load_metadata_contract(_write(tmp_path, "contract.yml", document))
    monkeypatch.setattr(mm, "METADATA_CONTRACT", contract)
    return contract


def test_metadata_contract_accepts_unfamiliar_modality_and_declared_implication(tmp_path, monkeypatch):
    document = _document()
    _install(tmp_path, monkeypatch, document)
    metadata = {
        "type": "olfactory",
        "input_modalities": ["text", "scent", "image", "scent"],
        "output_modalities": ["scent", "text"],
    }
    rows = {"records": [{"registered_routes": [
        {"operation": operation, "model_metadata": metadata}
        for operation in mm.ALIAS_OPERATIONS
    ]}]}
    operations = mm.load_operation_metadata(_write(tmp_path, "operations.yml", rows))
    assert tuple(operations) == tuple(mm.ALIAS_OPERATIONS)
    expected = mm.ModelMetadata("olfactory", ("image", "scent", "text"), ("text", "scent"))
    assert all(value == expected for value in operations.values())
    fields = {**expected.to_fields(), "tags": ["vision", "olfaction", "vision"]}
    assert mm.from_fields(fields) == mm.ModelMetadata(
        "olfactory", expected.input_modalities, expected.output_modalities,
        tags=("olfaction", "vision"),
    )
    declared = {
        "type": "olfactory", "input_modalities": ["text"],
        "output_modalities": ["scent", "text"], "tags": ["olfaction"],
    }
    result = mm.apply_declared(mm.ModelMetadata("image"), declared)
    assert result == mm.ModelMetadata(
        "olfactory", ("scent", "text"), ("text", "scent"), tags=("olfaction",),
    )
    assert mm.apply_declared(None, {
        **declared, "input_modalities": ["scent"], "tags": [],
    }).tags == ("olfaction",)
    entries = [{"id": "unknown"}]
    mm.annotate_entries(entries, mm.MetadataSources(declared=lambda _: declared))
    assert mm.from_fields(entries[0]) == result
    assert mm.type_of(["image", "scent"], ["text"]) == "olfactory"
    assert mm.type_of([], ["image", "scent"]) == "olfactory"
    assert mm.type_of(["file"], ["text"]) == "olfactory"
    assert mm.type_of(["scent"], []) is None
    # Neither semantic role depends on the first vocabulary member.
    assert mm.from_contract({
        "inputs": {"prompt": {"type": "string"}},
        "outputs": {"report": {"media_type": "application/json"}},
    }) == mm.ModelMetadata("olfactory", ("text",), ("text",))
    document["implications"] = []
    _install(tmp_path, monkeypatch, document)
    assert mm.apply_declared(None, declared).input_modalities == ("text",)
    assert mm.apply_declared(None, {
        **declared, "input_modalities": ["scent"], "tags": [],
    }).tags == ()


def test_metadata_contract_validates_other_registry_rows_and_combinators(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, _document())
    metadata = mm.ModelMetadata("olfactory", ("scent",), ("scent",))
    monkeypatch.setattr(mm, "OPERATION_METADATA", {"custom": metadata})
    capability = mm.load_capability_intent_metadata(_write(tmp_path, "capability.yml", {
        "schema_version": 1,
        "exact": [{"intent": "unfamiliar.sample", "model_metadata": {
            "type": "olfactory", "input_modalities": ["text", "scent"],
            "output_modalities": ["scent"],
        }}],
        "families": [],
    }))
    assert capability.exact["unfamiliar.sample"].input_modalities == ("scent", "text")
    service = mm.load_service_capability_metadata(_write(tmp_path, "service.yml", {
        "schema_version": 1, "capabilities": [{"capability": "custom", "operation": "custom"}],
        "augmentation": {"capability": "extended", "match_type": "olfactory",
                         "input_modalities": ["text", "scent"]},
    }))
    assert service.augmentation.input_modalities == ("scent", "text")
    monkeypatch.setattr(mm, "SERVICE_CAPABILITY_METADATA", service)
    assert mm.from_service_capabilities(["custom", "extended"]).input_modalities == ("scent", "text")
    other = mm.ModelMetadata("olfactory", ("text", "scent"), ("scent",))
    assert mm.merge([metadata, other]).input_modalities == ("scent", "text")
    assert mm.common([metadata, other]).input_modalities == ("scent",)


def test_metadata_contract_implications_close_transitively_and_are_type_scoped(tmp_path, monkeypatch):
    document = _document()
    document["implications"] = [
        {"type": "olfactory", "tag": "vision", "input_modality": "image"},
        {"type": "olfactory", "tag": "vision", "input_modality": "scent"},
        *document["implications"],
    ]
    _install(tmp_path, monkeypatch, document)
    value = mm.ModelMetadata("olfactory", tags=("olfaction",))
    result = mm.apply_declared(value, None)
    assert result.input_modalities == ("image", "scent")
    assert result.tags == ("olfaction", "vision")
    assert mm.apply_declared(result, None) == result
    assert mm.apply_declared(mm.ModelMetadata("language", tags=("olfaction",)), None).input_modalities == ()
    assert mm.apply_declared(mm.ModelMetadata("image", tags=("olfaction",)), None).tags == ()


def test_metadata_contract_is_deeply_immutable(tmp_path):
    contract = mm.load_metadata_contract(_write(tmp_path, "contract.yml", _document()))
    with pytest.raises(FrozenInstanceError):
        contract.types = ()
    with pytest.raises(TypeError):
        contract.input_modalities[0] = "changed"
    with pytest.raises(FrozenInstanceError):
        contract.output_priority[0].type = "changed"
    with pytest.raises(FrozenInstanceError):
        contract.implications[0].tag = "changed"


@pytest.mark.parametrize("location,value", [
    (("schema_version",), True),
    (("schema_version",), 2),
    (("unknown",), "not allowed"),
    (("types",), []),
    (("types",), ["language", "language"]),
    (("types",), ["Language"]),
    (("types",), ["gateway.module.Type"]),
    (("input_modalities",), ["text", "text"]),
    (("output_modalities",), "text"),
    (("tags",), ["vision", "vision"]),
    (("tag_types",), ["missing"]),
    (("tag_types",), ["language", "language"]),
    (("derivation", "unknown"), "not allowed"),
    (("derivation", "scalar_input"), "missing"),
    (("derivation", "fallback_output"), "missing"),
    (("derivation", "default_type"), "missing"),
    (("derivation", "output_priority"), [{"modality": "text", "type": "language"}]),
    (("derivation", "input_priority"), [{"modality": "missing", "type": "language"}]),
    (("derivation", "output_priority"), [{"modality": "scent", "type": "missing"}]),
    (("derivation", "input_priority"), [{"modality": "scent", "type": "olfactory",
                                        "import": "gateway.module"}]),
    (("derivation", "input_priority"), [{"modality": "scent", "type": "olfactory"}] * 2),
    (("implications",), [{"type": "image", "tag": "vision", "input_modality": "image"}]),
    (("implications",), [{"type": "olfactory", "tag": "missing", "input_modality": "scent"}]),
    (("implications",), [{"type": "olfactory", "tag": "olfaction", "input_modality": "missing"}]),
    (("implications",), [{"type": "olfactory", "tag": "olfaction", "input_modality": "scent",
                         "callable": "gateway.module"}]),
    (("implications",), [{"type": "olfactory", "tag": "olfaction", "input_modality": "scent"}] * 2),
])
def test_metadata_contract_refuses_invalid_data(tmp_path, location, value):
    document = _document()
    parent = document
    for key in location[:-1]:
        parent = parent[key]
    parent[location[-1]] = value
    with pytest.raises(ValueError):
        mm.load_metadata_contract(_write(tmp_path, "invalid.yml", document))


@pytest.mark.parametrize("duplicate", ["schema_version: 1\n", "derivation:\n  scalar_input: text\n"])
def test_metadata_contract_refuses_duplicate_yaml_fields(tmp_path, duplicate):
    path = _write(tmp_path, "duplicate.yml", _document())
    path.write_text(path.read_text(encoding="utf-8") + duplicate, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        mm.load_metadata_contract(path)


def test_metadata_contract_refuses_missing_nested_fields(tmp_path):
    document = _document()
    del document["derivation"]["default_type"]
    with pytest.raises(ValueError, match="missing"):
        mm.load_metadata_contract(_write(tmp_path, "missing.yml", document))
