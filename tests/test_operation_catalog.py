"""Catalog fixtures are inline; no reader or execution behavior is changed."""

from dataclasses import FrozenInstanceError
import json
from pathlib import Path

import pytest

from gateway.operation_catalog import CatalogError, load


def _operation(identifier="work.one"):
    return {
        "id": identifier, "family": "work",
        "request": {"schema": "requests.work"},
        "result": {"schema": "results.work"},
        "submittable_as_job": False,
    }


def _document():
    return {
        "schema_version": 1,
        "operations": [_operation(), _operation("work.two")],
        "relations": [
            {"kind": "spelling", "context": "catalog", "word": "work", "operation": "work.one"},
            {"kind": "binding", "operation": "work.one", "method": "POST",
             "path": "/work", "representation": "json", "modes": ["sync"]},
            {"kind": "admission", "operation": "work.one", "intent": "permit.work"},
            {"kind": "runtime_policy", "operation": "work.one", "policy": "policy.work"},
            {"kind": "receipt", "operation": "work.one", "intent": "receipt.work"},
            {"kind": "catalog_group", "group": "works", "operations": ["work.one", "work.two"]},
            {"kind": "family", "operation": "work.one", "family": "work"},
        ],
    }


def _write(tmp_path, document):
    path = tmp_path / "catalog.yml"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_valid_catalog_loads_and_keeps_relation_order(tmp_path):
    document = _document()
    document["relations"].extend([
        {"kind": "spelling", "context": "catalog", "word": "second", "operation": "work.two"},
        {"kind": "spelling", "context": "legacy", "word": "work", "operation": "work.two"},
    ])
    catalog = load(_write(tmp_path, document))
    assert tuple(catalog.operations) == ("work.one", "work.two")
    assert [r["word"] for r in catalog.relations("spelling", "catalog")] == ["work", "second"]
    assert [r["operation"] for r in catalog.relations("spelling", "legacy")] == ["work.two"]
    assert len(catalog.relations("spelling")) == 3
    assert not hasattr(catalog, "resolve")
    assert catalog.operations["work.one"]["submittable_as_job"] is False
    assert catalog.relations("family")[0]["family"] == "work"


def test_deeply_frozen(tmp_path):
    catalog = load(_write(tmp_path, _document()))
    with pytest.raises(TypeError):
        catalog.operations["other"] = {}
    with pytest.raises(TypeError):
        catalog.operations["work.one"]["request"]["schema"] = "changed"
    with pytest.raises(TypeError):
        catalog.relations("binding")[0]["path"] = "/changed"
    assert catalog.relations("binding")[0]["modes"] == ("sync",)
    with pytest.raises(FrozenInstanceError):
        catalog.operations = {}


def test_unknown_operation_refused(tmp_path):
    document = _document()
    document["relations"][0]["operation"] = "missing"
    with pytest.raises(CatalogError, match="relations row 0: rule defined_operation"):
        load(_write(tmp_path, document))


def test_unknown_relation_kind_refused(tmp_path):
    document = _document()
    document["relations"][0]["kind"] = "untyped"
    with pytest.raises(CatalogError, match="relations row 0: rule relation_kind"):
        load(_write(tmp_path, document))


def test_duplicate_spelling_in_context_refused(tmp_path):
    document = _document()
    document["relations"].append(dict(document["relations"][0]))
    with pytest.raises(CatalogError, match="relations row 7: rule unique_spelling_in_context"):
        load(_write(tmp_path, document))


def test_invalid_spelling_context_refused(tmp_path):
    document = _document()
    document["relations"][0]["context"] = "anywhere"
    with pytest.raises(CatalogError, match="relations row 0: rule schema.enum"):
        load(_write(tmp_path, document))


@pytest.mark.parametrize("field", ["method", "path", "representation", "modes"])
def test_binding_fields_required(tmp_path, field):
    document = _document()
    del document["relations"][1][field]
    with pytest.raises(CatalogError, match="relations row 1: rule schema.required"):
        load(_write(tmp_path, document))


@pytest.mark.parametrize("field,value", [("modes", []), ("path", "relative"), ("method", "")])
def test_binding_fields_typed(tmp_path, field, value):
    document = _document()
    document["relations"][1][field] = value
    with pytest.raises(CatalogError, match="relations row 1: rule schema."):
        load(_write(tmp_path, document))


def test_catalog_group_unknown_operation_refused(tmp_path):
    document = _document()
    document["relations"][5]["operations"].append("missing")
    with pytest.raises(CatalogError, match="relations row 5: rule defined_operation"):
        load(_write(tmp_path, document))


@pytest.mark.parametrize("index", range(7))
@pytest.mark.parametrize("field", ["serves", "offering_task", "execute", "permissions"])
def test_relation_cannot_grant_execution(tmp_path, index, field):
    document = _document()
    document["relations"][index][field] = ["work.two"]
    with pytest.raises(CatalogError, match=f"relations row {index}: rule no_execution_fields"):
        load(_write(tmp_path, document))


def test_duplicate_operation_id_refused(tmp_path):
    document = _document()
    document["operations"].append(_operation())
    with pytest.raises(CatalogError, match="operations row 2: rule unique_operation_id"):
        load(_write(tmp_path, document))


@pytest.mark.parametrize("value", [True, 2, "1"])
def test_schema_version_refused(tmp_path, value):
    document = _document()
    document["schema_version"] = value
    with pytest.raises(CatalogError, match="catalog row root: rule schema."):
        load(_write(tmp_path, document))


def test_schema_references_and_metadata_are_separate(tmp_path):
    document = _document()
    document["operations"][0].update(modality="custom-modality", media_type="application/x-custom")
    catalog = load(_write(tmp_path, document))
    row = catalog.operations["work.one"]
    assert row["request"]["schema"] == "requests.work"
    assert row["modality"] == "custom-modality"
    assert row["media_type"] == "application/x-custom"


def test_request_schema_required(tmp_path):
    document = _document()
    document["operations"][0]["request"] = {"modality": "text"}
    with pytest.raises(CatalogError, match="operations row 0: rule schema."):
        load(_write(tmp_path, document))


def test_duplicate_yaml_key_refused(tmp_path):
    path = tmp_path / "catalog.yml"
    path.write_text("schema_version: 1\nschema_version: 1\noperations: []\nrelations: []\n")
    with pytest.raises(CatalogError, match="catalog row root: rule duplicate_key"):
        load(path)


def test_yaml_fixture_loads(tmp_path):
    path = tmp_path / "catalog.yml"
    path.write_text("schema_version: 1\noperations: []\nrelations: []\n")
    assert not load(path).operations


def test_shipped_empty_catalog_loads():
    path = Path(__file__).resolve().parents[1] / "registry" / "operations.yml"
    catalog = load(path)
    assert not catalog.operations
    for kind in ("spelling", "binding", "admission", "runtime_policy", "receipt", "catalog_group", "family"):
        assert catalog.relations(kind) == ()


def test_catalog_cannot_supply_its_own_schema(tmp_path):
    (tmp_path / "operations.schema.json").write_text("{}")
    document = _document()
    document["schema_version"] = 99
    with pytest.raises(CatalogError, match="catalog row root: rule schema."):
        load(_write(tmp_path, document))
