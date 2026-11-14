"""Agreement between capability words and the inert operation catalog."""

import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from gateway.operation_catalog import CatalogError, load, project_capability_words


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "registry" / "capability_words.yml"
CATALOG = ROOT / "registry" / "operations.yml"
PROJECTED = ("protocol_words", "route_intents", "type_capabilities")
UNEXPRESSED = (
    "schema_version", "modality_words", "media_type_modalities", "capabilities", "audio_rule",
)


def test_projection_matches_source_order():
    source = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    catalog = load(CATALOG)
    projected = project_capability_words(catalog)
    assert not catalog.operations
    assert not catalog.relations("spelling")
    assert set(projected) == set(PROJECTED)
    for name in PROJECTED:
        assert list(projected[name].items()) == list(source[name].items())


def test_unexpressed_and_source_classification():
    source = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    rows = [row for row in load(CATALOG).unexpressed if row["source"] == SOURCE.name]
    assert [row["key"] for row in rows] == list(UNEXPRESSED)
    assert all(row["source"] == SOURCE.name for row in rows)
    assert all(row["reason"].strip() and "\n" not in row["reason"] for row in rows)
    assert set(source) == set(PROJECTED) | set(UNEXPRESSED)


def test_generator_reproduces_catalog_bytes(tmp_path):
    output = tmp_path / "generated.yml"
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_operation_catalog.py"),
         "--source", str(SOURCE), "--output", str(output)],
        check=True,
    )
    assert output.read_bytes() == CATALOG.read_bytes()


def _catalog(tmp_path, relations):
    path = tmp_path / "catalog.yml"
    path.write_text(json.dumps({
        "schema_version": 1, "operations": [], "relations": relations,
    }), encoding="utf-8")
    return path


def test_family_rejects_both_targets(tmp_path):
    row = {"kind": "family", "family": "display", "word": "sample", "operation": "missing"}
    with pytest.raises(CatalogError, match="rule family_target:"):
        load(_catalog(tmp_path, [row]))


def test_family_rejects_neither_target(tmp_path):
    with pytest.raises(CatalogError, match="rule family_target:"):
        load(_catalog(tmp_path, [{"kind": "family", "family": "display"}]))


def test_family_rejects_duplicate_word_in_context(tmp_path):
    row = {"kind": "family", "family": "display", "word": "sample", "context": "route"}
    with pytest.raises(CatalogError, match="rule unique_family_word_in_context:"):
        load(_catalog(tmp_path, [row, {**row, "family": "other"}]))


def test_family_allows_same_word_in_two_contexts(tmp_path):
    row = {"kind": "family", "family": "display", "word": "sample", "order": 0}
    catalog = load(_catalog(tmp_path, [
        {**row, "context": "protocol"}, {**row, "context": "route"},
    ]))
    assert not catalog.operations
    projected = project_capability_words(catalog)
    assert projected["protocol_words"] == {"sample": "display"}
    assert projected["route_intents"] == {"sample": "display"}
    assert projected["type_capabilities"] == {}


def test_projection_sorts_order(tmp_path):
    rows = [
        {"kind": "family", "word": "second", "family": "display", "context": "route", "order": 1},
        {"kind": "family", "word": "first", "family": "display", "context": "route", "order": 0},
    ]
    catalog = load(_catalog(tmp_path, rows))
    assert list(project_capability_words(catalog)["route_intents"]) == ["first", "second"]


def test_conservative_missing_contracts_are_exact_media_gaps():
    # Assumption: absence of request/result contracts is a gap, not an invented operation.
    media = ROOT / "registry" / "media_job_intents.yml"
    rows = yaml.safe_load(media.read_text(encoding="utf-8"))["intents"]
    catalog = load(CATALOG)
    expected = [
        {"source": media.name, "key": f"intents[{index}]:{row['intent']}",
         "reason": "Operation request/result mapping is not supplied by this table."}
        for index, row in enumerate(rows)
    ]
    actual = [dict(row) for row in catalog.unexpressed if row["source"] == media.name]
    assert actual == expected
    assert not catalog.operations


def test_conservative_media_gap_uses_explicit_boolean_not_route(tmp_path):
    from scripts.gen_operation_catalog import media_intent_gaps

    source = tmp_path / "media.yml"
    row = {"intent": "sample", "modality": "sample", "submittable": False,
           "route": "/sample", "component": "SampleRequest"}
    source.write_text(yaml.safe_dump({"intents": [row]}), encoding="utf-8")
    assert media_intent_gaps(source) == [{
        "source": source.name, "key": "intents[0]:sample",
        "reason": "Operation request/result mapping is not supplied by this table.",
    }]
    row["submittable"] = "false"
    source.write_text(yaml.safe_dump({"intents": [row]}), encoding="utf-8")
    with pytest.raises(ValueError, match="explicit boolean"):
        media_intent_gaps(source)


def test_media_gap_rejects_unclassified_fields_and_duplicate_intents(tmp_path):
    from scripts.gen_operation_catalog import media_intent_gaps

    source = tmp_path / "media.yml"
    row = {"intent": "sample", "modality": "sample", "submittable": False}
    for document, message in [
        ({"intents": [{**row, "unclassified": "value"}]}, "field must be classified"),
        ({"intents": [row, row]}, "Duplicate media intent"),
        ({"intents": [], "unclassified": []}, "top-level key must be classified"),
    ]:
        source.write_text(yaml.safe_dump(document), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            media_intent_gaps(source)

def test_conservative_local_contract_gaps_are_exact():
    # No closed operation contracts may be inferred from routing/default rows.
    source = ROOT / "registry" / "local_capability_rows.yml"
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    expected_keys = ["schema_version"]
    for section in ("alias_operations", "workflow_families", "backend_rows", "image_rows"):
        expected_keys.extend(f"{section}[{i}]" for i in range(len(document[section])))
    expected_keys.extend(("image_defaults", "workflow_defaults"))
    expected = [{
        "source": source.name, "key": key,
        "reason": ("Source format version, not an operation fact." if key == "schema_version"
                   else "Local routing/default rows lack a closed operation request/result mapping."),
    } for key in expected_keys]
    catalog = load(CATALOG)
    assert [dict(row) for row in catalog.unexpressed if row["source"] == source.name] == expected
    assert not catalog.operations
    assert {row["context"] for row in catalog.relations("family")} == {"protocol", "route", "type"}


def test_local_gaps_reject_unclassified_sections_and_bad_rows(tmp_path):
    from scripts.gen_operation_catalog import local_capability_gaps

    source = tmp_path / "local.yml"
    document = {"schema_version": 1, "alias_operations": [{}], "workflow_families": [],
                "backend_rows": [], "image_rows": [], "image_defaults": {}, "workflow_defaults": {}}
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert [row["key"] for row in local_capability_gaps(source)] == [
        "schema_version", "alias_operations[0]", "image_defaults", "workflow_defaults"]
    for invalid in ({**document, "extra": []}, {**document, "backend_rows": [None]},
                    {**document, "schema_version": True}):
        source.write_text(yaml.safe_dump(invalid), encoding="utf-8")
        with pytest.raises(ValueError):
            local_capability_gaps(source)
