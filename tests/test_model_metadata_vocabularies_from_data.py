"""Metadata vocabularies and semantic roles are release data, not Python enums."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import json

import pytest
import yaml

from gateway import model_metadata as mm


def _document():
    return {
        "schema_version": 1,
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


def _schema():
    # Unfamiliar vocabulary is now supplied by the published schema contract.
    return {"additionalProperties": {"properties": {"model_metadata": {"properties": {
        "type": {"enum": ["language", "olfactory", "image"]},
        "input_modalities": {"items": {"enum": ["image", "file", "scent", "text"]}},
        "output_modalities": {"items": {"enum": ["text", "image", "scent"]}},
        "tags": {"items": {"enum": ["olfaction", "vision"]}},
    }}}}}


def _write_schema(tmp_path, schema=None):
    path = tmp_path / "models.schema.json"
    path.write_text(json.dumps(_schema() if schema is None else schema), encoding="utf-8")
    return path


def _load_contract(tmp_path, name, document):
    return mm.load_metadata_contract(
        _write(tmp_path, name, document), schema_path=_write_schema(tmp_path),
    )


def _write(tmp_path, name, document):
    path = tmp_path / name
    if name not in ("operations.yml", "capability.yml", "service.yml"):
        document = {"schema_version": 1, "exact": [], "families": [], "contract": document}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _install(tmp_path, monkeypatch, document):
    contract = _load_contract(tmp_path, "contract.yml", document)
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
    contract = _load_contract(tmp_path, "contract.yml", _document())
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
        _load_contract(tmp_path, "invalid.yml", document)


@pytest.mark.parametrize("duplicate", ["schema_version: 1\n", "derivation:\n  scalar_input: text\n"])
def test_metadata_contract_refuses_duplicate_yaml_fields(tmp_path, duplicate):
    path = _write(tmp_path, "invalid.yml", _document())
    # Duplicate inside the semantic contract, not as an unrelated outer field.
    repeated = "".join("  " + line + "\n" for line in duplicate.splitlines())
    path.write_text(path.read_text(encoding="utf-8") + repeated, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        mm.load_metadata_contract(path, schema_path=_write_schema(tmp_path))


def test_metadata_contract_refuses_missing_nested_fields(tmp_path):
    document = _document()
    del document["derivation"]["default_type"]
    with pytest.raises(ValueError, match="missing"):
        _load_contract(tmp_path, "missing.yml", document)


@pytest.mark.parametrize("location,value", [
    (("type",), []),
    (("type",), ["language", "language"]),
    (("type",), ["Language"]),
    (("type",), ["gateway.module.Type"]),
    (("input_modalities", "items"), ["text", "text"]),
    (("output_modalities", "items"), "text"),
    (("tags", "items"), ["vision", "vision"]),
])
def test_metadata_contract_refuses_invalid_schema_vocabulary(tmp_path, location, value):
    schema = _schema()
    parent = schema["additionalProperties"]["properties"]["model_metadata"]["properties"]
    for key in location:
        parent = parent[key]
    parent["enum"] = value
    schema_path = _write_schema(tmp_path, schema)
    with pytest.raises(ValueError) as failure:
        mm.load_metadata_contract(
            _write(tmp_path, "contract.yml", _document()), schema_path=schema_path,
        )
    assert str(schema_path) in str(failure.value)
    assert ".".join((*location, "enum")) in str(failure.value)


@pytest.mark.parametrize("roster", ["types", "input_modalities", "output_modalities", "tags"])
def test_metadata_contract_refuses_shadow_yaml_vocabulary(tmp_path, roster):
    document = _document()
    document[roster] = ["shadow"]
    with pytest.raises(ValueError, match="unknown fields"):
        _load_contract(tmp_path, "shadow.yml", document)
