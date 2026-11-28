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
    document["relations"].append({**document["relations"][0], "operation": "work.two"})
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


def test_catalog_group_key_unique_without_display(tmp_path):
    document = _document()
    document["relations"].append({
        "kind": "catalog_group", "group": "shared", "operations": ["work.one"],
    })
    assert all("display" not in row for row in document["relations"])
    catalog = load(_write(tmp_path, document))
    group = catalog.relations("catalog_group")[-1]
    assert group["group"] == "shared"
    assert group["operations"] == ("work.one",)
    assert "display" not in group

    document["relations"].append({
        "kind": "catalog_group", "group": "shared", "operations": ["work.two"],
    })
    with pytest.raises(CatalogError, match="rule unique_catalog_group: shared"):
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


def test_shipped_catalog_holds_media_operations_and_word_family_rows():
    # Media job intents come first, then the local alias operations and the models catalog words whose selector is one operation; the other relation kinds come in later steps.
    path = Path(__file__).resolve().parents[1] / "registry" / "operations.yml"
    catalog = load(path)
    assert len(catalog.operations) == 19
    assert len(catalog.relations("admission")) == 11
    assert catalog.relations("binding")
    spellings = catalog.relations("spelling")
    contexts = [row["context"] for row in spellings]
    assert contexts.count("legacy") == 11 and contexts.count("catalog") == 24 and len(contexts) == 35
    for kind in ("runtime_policy", "receipt", "catalog_group"):
        assert catalog.relations(kind) == ()
    families = catalog.relations("family")
    assert families and all("word" in row and "operation" not in row for row in families)


def test_catalog_cannot_supply_its_own_schema(tmp_path):
    (tmp_path / "operations.schema.json").write_text("{}")
    document = _document()
    document["schema_version"] = 99
    with pytest.raises(CatalogError, match="catalog row root: rule schema."):
        load(_write(tmp_path, document))

@pytest.mark.parametrize("row", ["not a mapping", [], None], ids=["string", "list", "null"])
def test_non_mapping_relation_names_position(tmp_path, row):
    document = _document()
    document["relations"][1] = row
    with pytest.raises(CatalogError, match="relations row 1: rule relation_mapping"):
        load(_write(tmp_path, document))


@pytest.mark.parametrize("index", range(7))
def test_exact_duplicate_relation_names_both_positions(tmp_path, index):
    document = _document()
    document["relations"].append(dict(reversed(list(document["relations"][index].items()))))
    with pytest.raises(CatalogError) as error:
        load(_write(tmp_path, document))
    assert str(error.value) == (
        f"relations row 7: rule unique_relation_row: duplicates relations row {index}"
    )
    assert "unique_spelling_in_context" not in str(error.value)
    assert "unique_family_word_in_context" not in str(error.value)


def test_exact_duplicate_word_family_names_both_positions(tmp_path):
    document = _document()
    row = {"kind": "family", "context": "protocol", "word": "work", "family": "work"}
    document["relations"].extend([row, dict(row)])
    with pytest.raises(CatalogError) as error:
        load(_write(tmp_path, document))
    assert str(error.value) == (
        "relations row 8: rule unique_relation_row: duplicates relations row 7"
    )


def test_distinct_word_family_in_same_context_refused(tmp_path):
    document = _document()
    row = {"kind": "family", "context": "protocol", "word": "work", "family": "work"}
    document["relations"].extend([row, {**row, "family": "other"}])
    with pytest.raises(CatalogError) as error:
        load(_write(tmp_path, document))
    assert str(error.value) == (
        "relations row 8: rule unique_family_word_in_context: ('protocol', 'work')"
    )


@pytest.mark.parametrize("field,value", [
    ("operation", "work.two"), ("path", "/other"), ("method", "PUT"),
])
def test_distinct_binding_relations_are_not_duplicates(tmp_path, field, value):
    document = _document()
    document["relations"].append({**document["relations"][1], field: value})
    assert len(load(_write(tmp_path, document)).relations("binding")) == 2


def test_family_relation_need_not_match_operation_family(tmp_path):
    document = _document()
    document["relations"].append({**document["relations"][6], "family": "other"})
    assert len(load(_write(tmp_path, document)).relations("family")) == 2


def test_nan_yaml_value_refused(tmp_path):
    path = _write(tmp_path, _document())
    path.write_text(path.read_text().replace('"requests.work"', '.nan', 1))
    with pytest.raises(CatalogError, match="rule parse"):
        load(path)


def test_yaml_anchor_and_alias_refused(tmp_path):
    path = _write(tmp_path, _document())
    text = path.read_text().replace('"requests.work"', '&shared "requests.work"', 1)
    path.write_text(text.replace('"results.work"', '*shared', 1))
    with pytest.raises(CatalogError, match="rule parse"):
        load(path)


def test_unquoted_timestamp_refused(tmp_path):
    path = _write(tmp_path, _document())
    path.write_text(path.read_text().replace('"requests.work"', '2026-11-14', 1))
    with pytest.raises(CatalogError, match="rule parse"):
        load(path)


def test_missing_operation_projection_loader_boundaries(tmp_path):
    from scripts.gen_operation_catalog import missing_model_operations, models_catalog_projection

    models = tmp_path / "models.json"
    models.write_text(json.dumps({
        "schema_version": 1, "row_id_placeholder": "$row",
        "operations": [{"id": "display", "words": ["word"],
                        "intent_prefixes": ["sample.exact"],
                        "examples": [{"intent": "sample.exact"}, {"intent": "sample.exact"}]}],
    }))
    operations, gaps = missing_model_operations(
        models, {"protocol_words": {"sample.exact": "sample"}}, [])
    _, spellings = models_catalog_projection(models, operations)
    document = {"schema_version": 1, "operations": operations,
                "relations": spellings, "unexpressed": gaps}
    catalog = load(_write(tmp_path, document))
    assert set(catalog.operations) == {"sample.exact"}
    assert catalog.operations["sample.exact"]["submittable_as_job"] is False
    assert catalog.operations["sample.exact"]["request"]["schema"] == "undeclared"
    assert catalog.operations["sample.exact"]["result"]["schema"] == "undeclared"
    for kind in ("admission", "binding", "runtime_policy", "receipt"):
        assert catalog.relations(kind) == ()
    document["relations"][0]["operation"] = "missing"
    with pytest.raises(CatalogError, match="defined_operation"):
        load(_write(tmp_path, document))
    document["relations"][0]["operation"] = "sample.exact"
    document["operations"][0]["request"] = {}
    with pytest.raises(CatalogError, match="schema"):
        load(_write(tmp_path, document))


def _display_document():
    document = _document()
    document["relations"][5]["display"] = {"label": "Works"}
    return document


@pytest.mark.parametrize("field,target", [
    ("hidden_by", "missing"), ("icon", "missing"),
    ("hidden_by", "work.one"), ("icon", "work.one"),
    ("hidden_by", "plain"), ("icon", "plain"),
])
def test_display_references_require_declared_display_family(tmp_path, field, target):
    document = _display_document()
    document["relations"].append(
        {"kind": "catalog_group", "group": "plain", "operations": ["work.one"]}
    )
    document["relations"][5]["display"][field] = [target] if field == "hidden_by" else target
    with pytest.raises(CatalogError, match="defined_display_family") as error:
        load(_write(tmp_path, document))
    assert target in str(error.value)


@pytest.mark.parametrize("change", [
    {"operations": ["work.one"]},
    {"display": {"label": "Different"}},
    {"why": "Another definition"},
])
def test_conflicting_group_key_names_key(tmp_path, change):
    document = _display_document()
    document["relations"].append({**document["relations"][5], **change})
    with pytest.raises(CatalogError, match="unique_catalog_group.*works"):
        load(_write(tmp_path, document))


@pytest.mark.parametrize("recommendation", [
    {"word": "work", "target_kind": "operation"},
    {"word": "work", "target_kind": "group", "target": "work.one"},
    {"word": " ", "target_kind": "group"},
    {"word": "work"},
])
def test_display_recommendation_targets_whole_group_only(tmp_path, recommendation):
    document = _display_document()
    document["relations"][5]["display"]["recommended_alias"] = recommendation
    with pytest.raises(CatalogError):
        load(_write(tmp_path, document))


@pytest.mark.parametrize("field,value", [
    ("unknown", True), ("admission", "anything"), ("family", "missing"),
    ("label", " "), ("display_order", -1), ("display_order", True),
    ("alias_hides_declared_names", "yes"), ("hidden_by", "works"),
    ("models_explainer", " padded "), ("try_help", " "), ("request_noun", ""),
    ("models_explainer", "text\n"), ("policy_hint", "hint\n"),
    ("policy_hint", "two words"),
])
def test_display_schema_is_closed_and_typed(tmp_path, field, value):
    document = _display_document()
    document["relations"][5]["display"][field] = value
    with pytest.raises(CatalogError):
        load(_write(tmp_path, document))


def test_display_order_unique_among_declared_families(tmp_path):
    document = _display_document()
    document["relations"][5]["display"]["display_order"] = 2
    document["relations"].append({
        "kind": "catalog_group", "group": "later", "operations": [],
        "display": {"label": "Later", "display_order": 2},
    })
    with pytest.raises(CatalogError, match="unique_display_order"):
        load(_write(tmp_path, document))


def test_display_forward_references_are_frozen_and_inert(tmp_path):
    document = _display_document()
    display = {
        "label": "Works", "member_label": "", "task_name": "Task",
        "models_explainer": "Explanation", "try_help": "Help",
        "request_noun": "request", "summary": "Summary", "display_order": 0,
        "icon": "later", "hidden_by": ["later", "works"],
        "alias_hides_declared_names": True,
        "recommended_alias": {"word": "both", "target_kind": "group"},
        "policy_hint": "opaque.policy",
    }
    document["relations"][5]["display"] = display
    document["relations"].append({
        "kind": "catalog_group", "group": "later", "operations": [],
        "display": {"label": "Later"},
    })
    catalog = load(_write(tmp_path, document))
    group, later = catalog.relations("catalog_group")
    assert group["operations"] == ("work.one", "work.two")
    assert group["display"]["hidden_by"] == ("later", "works")
    assert dict(group["display"]["recommended_alias"]) == display["recommended_alias"]
    assert "icon" not in later["display"]  # own-key default belongs to future projection
    assert "display_order" not in later["display"]
    with pytest.raises(TypeError):
        group["display"]["label"] = "changed"
    with pytest.raises(TypeError):
        group["display"]["recommended_alias"]["word"] = "changed"
    assert len(catalog.relations("admission")) == 1
    assert len(catalog.relations("runtime_policy")) == 1
    assert len(catalog.relations("spelling")) == 1


def test_display_recommendation_word_unique_in_context(tmp_path):
    document = _display_document()
    recommendation = {"word": "choice", "target_kind": "group"}
    document["relations"][5]["display"]["recommended_alias"] = recommendation
    document["relations"].append({
        "kind": "catalog_group", "group": "later", "operations": ["work.two"],
        "display": {"label": "Later", "recommended_alias": dict(recommendation)},
    })
    with pytest.raises(CatalogError, match="unique_display_recommendation.*choice"):
        load(_write(tmp_path, document))


def test_display_recommendation_context_is_separate_and_exact(tmp_path):
    document = _display_document()
    document["relations"][5]["display"]["recommended_alias"] = {
        "word": "work", "target_kind": "group",
    }
    document["relations"].append({
        "kind": "catalog_group", "group": "later", "operations": ["work.two"],
        "display": {
            "label": "Later",
            "recommended_alias": {"word": "Work", "target_kind": "group"},
        },
    })
    catalog = load(_write(tmp_path, document))
    groups = catalog.relations("catalog_group")
    assert [g["display"]["recommended_alias"]["word"] for g in groups] == ["work", "Work"]
    assert catalog.relations("spelling", "catalog")[0]["word"] == "work"
    assert catalog.relations("spelling", "display") == ()
    assert groups[0]["operations"] == ("work.one", "work.two")
    assert len(catalog.relations("admission")) == 1


def test_a_catalog_is_checked_once_per_content_and_the_file_is_read_on_every_load(tmp_path):
    path = _write(tmp_path, _document())
    first = load(path)
    assert load(path) is first  # the frozen result of identical contents is shared
    changed = _document()
    changed["operations"].append(_operation("work.three"))
    path.write_text(json.dumps(changed), encoding="utf-8")
    assert set(load(path).operations) == {"work.one", "work.two", "work.three"}  # a changed file is read again
    path.write_text(json.dumps(_document()), encoding="utf-8")
    assert load(path) is first


def test_a_catalog_that_fails_is_refused_on_every_load_and_never_kept(tmp_path):
    broken = _document()
    broken["relations"][0]["operation"] = "work.missing"
    path = _write(tmp_path, broken)
    for _ in range(2):
        with pytest.raises(CatalogError, match="defined_operation"):
            load(path)
    path.write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(CatalogError, match="rule parse"):
        load(path)
    with pytest.raises(CatalogError, match="rule parse"):
        load(tmp_path / "absent.yml")
