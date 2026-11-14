"""Loader contracts for shipped model-catalog operation rows."""

from __future__ import annotations

import copy

import pytest
import yaml

from gateway.capabilities import Capability
from gateway import models_catalog as catalog


def _document():
    return {
        "schema_version": 1,
        "row_id_placeholder": "$row_id",
        "operations": [{
            "id": "paint",
            "words": ["paint", "paint.preview"],
            "intent_prefixes": ["test.paint.", "test.brush."],
            "examples": [{
                "intent": "test.paint.preview",
                "kind": "paint-preview",
                "body": {"model": "$row_id", "options": [{"label": "literal"}]},
                "files": ["canvas"],
                "path": "/v1/test-paint",
            }],
        }],
    }


def _write(tmp_path, document):
    path = tmp_path / "models_catalog_operations.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


@pytest.mark.parametrize(("target", "key", "value"), [
    ("document", "unexpected", True),
    ("document", "schema_version", True),
    ("document", "schema_version", 2),
    ("document", "operations", {}),
    ("document", "row_id_placeholder", ""),
    ("row", "unexpected", True),
    ("row", "words", []),
    ("row", "intent_prefixes", []),
    ("row", "intent_prefixes", [""]),
    ("example", "unexpected", True),
    ("example", "body", []),
    ("example", "body", {"model": "not-a-placeholder"}),
    ("example", "path", "v1/test-paint"),
    ("example", "files", "canvas"),
])
def test_loader_refuses_invalid_rows_with_file_and_row(tmp_path, target, key, value):
    document = _document()
    target_row = {
        "document": document,
        "row": document["operations"][0],
        "example": document["operations"][0]["examples"][0],
    }[target]
    target_row[key] = value
    path = _write(tmp_path, document)

    with pytest.raises(ValueError) as error:
        catalog.load_models_catalog_operations(path)

    assert path.name in str(error.value)
    assert ("<document>" if target == "document" else "paint") in str(error.value)


@pytest.mark.parametrize("across_rows", [False, True])
def test_loader_refuses_duplicate_normalized_synonyms(tmp_path, across_rows):
    document = _document()
    if across_rows:
        document["operations"].append({
            "id": "other",
            "words": [" PAINT_PREVIEW "],
            "intent_prefixes": ["other."],
        })
    else:
        document["operations"][0]["words"].append(" PAINT_PREVIEW ")
    path = _write(tmp_path, document)

    with pytest.raises(ValueError) as error:
        catalog.load_models_catalog_operations(path)

    assert path.name in str(error.value)
    assert ("other" if across_rows else "paint") in str(error.value)
    assert "duplicate" in str(error.value).lower()


def test_loader_refuses_duplicate_intent_rows(tmp_path):
    document = _document()
    document["operations"][0]["examples"].append(
        copy.deepcopy(document["operations"][0]["examples"][0])
    )
    path = _write(tmp_path, document)

    with pytest.raises(ValueError, match="duplicate") as error:
        catalog.load_models_catalog_operations(path)

    assert path.name in str(error.value)
    assert "paint" in str(error.value)


def test_loader_refuses_duplicate_operation_ids(tmp_path):
    document = _document()
    document["operations"].append({
        "id": "paint", "words": ["new-word"], "intent_prefixes": ["new."],
    })
    path = _write(tmp_path, document)

    with pytest.raises(ValueError, match="duplicate") as error:
        catalog.load_models_catalog_operations(path)

    assert path.name in str(error.value)
    assert "paint" in str(error.value)


@pytest.mark.parametrize("contents", [None, "operations: [", "[]"])
def test_loader_refuses_unreadable_or_invalid_document(tmp_path, contents):
    path = tmp_path / "models_catalog_operations.yml"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")

    with pytest.raises(ValueError) as error:
        catalog.load_models_catalog_operations(path)

    assert path.name in str(error.value)
    assert "<document>" in str(error.value)


def test_test_only_row_supplies_operation_example_and_path(tmp_path, monkeypatch):
    prefixes, intents, placeholder = catalog.load_models_catalog_operations(
        _write(tmp_path, _document())
    )
    monkeypatch.setattr(catalog, "_CATALOG_OPERATION_PREFIXES", prefixes)
    monkeypatch.setattr(catalog, "_CATALOG_INTENT_ROWS", intents)
    monkeypatch.setattr(catalog, "_CATALOG_ROW_ID_PLACEHOLDER", placeholder)
    cap = Capability(intent="test.paint.preview", model="m", backend="local")
    other = Capability(intent="test.brush.stroke", model="m", backend="local")
    unrelated = Capability(intent="unrelated", model="m", backend="local")

    assert catalog._intent_prefixes_for_operation(" PAINT_PREVIEW ") == (
        "test.paint.", "test.brush.",
    )
    assert catalog._capabilities_for_operation([other, cap, unrelated], "paint") == [
        other, cap,
    ]
    assert catalog._examples("new-model", [cap]) == [{
        "kind": "paint-preview",
        "body": {"model": "new-model", "options": [{"label": "literal"}]},
        "files": ["canvas"],
    }]
    assert catalog._path_for_capability(cap) == "/v1/test-paint"
    assert catalog._examples("new-model", [unrelated, cap]) == [
        {"kind": "unrelated", "body": None},
    ]
    assert catalog._path_for_capability(unrelated) is None


def test_loaded_maps_are_deeply_read_only_and_examples_are_fresh(tmp_path, monkeypatch):
    prefixes, intents, placeholder = catalog.load_models_catalog_operations(
        _write(tmp_path, _document())
    )
    with pytest.raises(TypeError):
        prefixes["paint"] = ("changed",)
    with pytest.raises(TypeError):
        intents["test.paint.preview"] = {}
    with pytest.raises(TypeError):
        intents["test.paint.preview"]["body"]["options"][0]["label"] = "changed"
    with pytest.raises(TypeError):
        intents["test.paint.preview"]["files"][0] = "changed"

    monkeypatch.setattr(catalog, "_CATALOG_OPERATION_PREFIXES", prefixes)
    monkeypatch.setattr(catalog, "_CATALOG_INTENT_ROWS", intents)
    monkeypatch.setattr(catalog, "_CATALOG_ROW_ID_PLACEHOLDER", placeholder)
    cap = Capability(intent="test.paint.preview", model="m", backend="local")
    first = catalog._examples("first", [cap])
    first[0]["body"]["options"][0]["label"] = "changed"
    first[0]["files"].append("changed")

    assert catalog._examples("second", [cap]) == [{
        "kind": "paint-preview",
        "body": {"model": "second", "options": [{"label": "literal"}]},
        "files": ["canvas"],
    }]


def test_assumption_legacy_folding_and_insertion_order_remain_until_catalog_migration(tmp_path):
    """Characterize the existing contract, not a completed catalog migration."""
    document = _document()
    document["operations"][0]["words"] = [" Z_PAINT ", "Paint_PREVIEW"]
    second = copy.deepcopy(document["operations"][0])
    second["id"] = "other"
    second["words"] = [" A_PAINT "]
    second["intent_prefixes"] = ["other.second.", "other.first."]
    second["examples"][0]["intent"] = "other.preview"
    document["operations"].append(second)

    prefixes, intents, placeholder = catalog.load_models_catalog_operations(
        _write(tmp_path, document)
    )

    assert list(prefixes.items()) == [
        ("z.paint", ("test.paint.", "test.brush.")),
        ("paint.preview", ("test.paint.", "test.brush.")),
        ("a.paint", ("other.second.", "other.first.")),
    ]
    assert list(intents) == ["test.paint.preview", "other.preview"]
    for entry in intents.values():
        assert list(entry) == ["kind", "body", "path", "files"]
        assert entry["kind"] == "paint-preview"
        assert list(entry["body"]) == ["model", "options"]
        assert entry["body"]["model"] == "$row_id"
        assert tuple(entry["body"]["options"][0].items()) == (("label", "literal"),)
        assert entry["path"] == "/v1/test-paint"
        assert entry["files"] == ("canvas",)
    assert placeholder == "$row_id"
