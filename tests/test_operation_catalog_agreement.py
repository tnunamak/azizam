"""Agreement between capability words and the inert operation catalog."""

import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from gateway.operation_catalog import (
    CatalogError, load, project_capability_words, project_media_job_intents,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "registry" / "capability_words.yml"
CATALOG = ROOT / "registry" / "operations.yml"
LOCAL_SOURCE = ROOT / "registry" / "local_capability_rows.yml"
PROJECTED = ("protocol_words", "route_intents", "type_capabilities")
UNEXPRESSED = (
    "schema_version", "modality_words", "media_type_modalities", "capabilities", "audio_rule",
)


def _media_intents():
    media = ROOT / "registry" / "media_job_intents.yml"
    return {row["intent"] for row in yaml.safe_load(media.read_text(encoding="utf-8"))["intents"]}


def _alias_rows():
    return yaml.safe_load(LOCAL_SOURCE.read_text(encoding="utf-8"))["alias_operations"]


# Operations the generator declares from models catalog rows with one exact selector,
# agreeing examples and one corroborated family (missing_model_operations).
MODEL_DECLARED = {"speech.translation", "text.completions", "text.rerank"}


def _catalog_intents():
    return (_media_intents() | MODEL_DECLARED
            | {row["intent"] for row in _alias_rows() if row["intent"] is not None})


def _alias_spellings():
    words = []
    for row in _alias_rows():
        if row["intent"] is not None:
            words.extend((word, row["intent"])
                         for word in [row["name"], *row.get("shared_operations", [])]
                         if word != row["intent"])
    return words


def _models_spellings():
    source = ROOT / "registry" / "models_catalog_operations.yml"
    rows = yaml.safe_load(source.read_text(encoding="utf-8"))["operations"]
    known = set(load(CATALOG).operations)
    spellings = []
    for row in rows:
        intents = {example["intent"] for example in row.get("examples", [])}
        if len(intents) != 1:
            continue
        intent = next(iter(intents))
        if intent not in known or row["intent_prefixes"] != [intent]:
            continue
        spellings.extend((word, intent) for word in dict.fromkeys([row["id"], *row["words"]])
                         if word != intent)
    return spellings


def test_models_single_intent_rows_project_each_word():
    catalog = load(CATALOG)
    spellings = catalog.relations("spelling", "catalog")
    expected = _models_spellings()
    assert expected
    assert [(row["word"], row["operation"]) for row in spellings] == expected
    assert [row["order"] for row in spellings] == list(range(len(expected)))
    assert len({word for word, _intent in expected}) == len(expected)


def test_models_ambiguous_examples_stay_gaps(tmp_path):
    from scripts.gen_operation_catalog import generate

    source, local = _alias_fixture_sources(tmp_path, [
        {"name": "first", "intent": "sample.first"},
        {"name": "second", "intent": "sample.second"},
    ], {"sample.first": "sample", "sample.second": "sample"})
    models = tmp_path / "models.yml"
    models.write_text(yaml.safe_dump({
        "schema_version": 1, "row_id_placeholder": "$row",
        "operations": [{"id": "ambiguous", "words": ["either"],
                        "intent_prefixes": ["sample."],
                        "examples": [{"intent": "sample.first"}, {"intent": "sample.second"}]}],
    }), encoding="utf-8")
    document = json.loads(generate(source, local_source=local, models_source=models))
    assert not [row for row in document["relations"]
                if row["kind"] == "spelling" and row["context"] == "catalog"]
    assert document["unexpressed"][-1] == {
        "source": models.name, "key": "operations[0]:ambiguous",
        "reason": "Missing operation identity needs one exact singleton selector.",
    }


@pytest.mark.parametrize(
    "prefixes",
    [["sample."], [], ["sample.first", "sample.second"], ["sample.first", "sample.first"]],
    ids=["broad", "empty", "multiple", "duplicate"],
)
def test_models_non_exact_selectors_stay_gaps(tmp_path, prefixes):
    from scripts.gen_operation_catalog import models_catalog_projection, MODELS_GAP_REASON

    source = tmp_path / "models.yml"
    source.write_text(yaml.safe_dump({
        "schema_version": 1, "row_id_placeholder": "$row",
        "operations": [{"id": "first", "words": ["spelling"],
                        "intent_prefixes": prefixes,
                        "examples": [{"intent": "sample.first"}]}],
    }), encoding="utf-8")
    gaps, spellings = models_catalog_projection(source, [{"id": "sample.first"}])
    assert spellings == []
    assert gaps[-1] == {
        "source": source.name, "key": "operations[0]:first", "reason": MODELS_GAP_REASON,
    }


def test_models_word_projection_deduplicates_and_preserves_gaps(tmp_path):
    from scripts.gen_operation_catalog import (
        models_catalog_projection, MODELS_GAP_REASON, MODELS_WORDS_GAP_REASON,
    )

    source = tmp_path / "models.yml"
    rows = [
        {"id": "first", "words": ["first", "spelling", "spelling", "sample.first"],
         "intent_prefixes": ["sample.first"],
         "examples": [{"intent": "sample.first"}, {"intent": "sample.first"}]},
        {"id": "empty", "words": ["empty"], "intent_prefixes": []},
        {"id": "unknown", "words": ["unknown"], "intent_prefixes": [],
         "examples": [{"intent": "sample.missing"}]},
    ]
    document = {"schema_version": 1, "row_id_placeholder": "$row", "operations": rows}
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    gaps, spellings = models_catalog_projection(source, [{"id": "sample.first"}])
    assert spellings == [
        {"kind": "spelling", "context": "catalog", "word": word,
         "operation": "sample.first", "order": order}
        for order, word in enumerate(["first", "spelling"])
    ]
    assert [gap["reason"] for gap in gaps[2:]] == [
        MODELS_WORDS_GAP_REASON, MODELS_GAP_REASON, MODELS_GAP_REASON,
    ]
    document["operations"] = [rows[0], {
        **rows[0], "id": "second", "words": ["spelling"],
    }]
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate catalog word 'spelling' in rows 'first' and 'second'"):
        models_catalog_projection(source, [{"id": "sample.first"}])


def test_projection_matches_source_order():
    source = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    catalog = load(CATALOG)
    projected = project_capability_words(catalog)
    assert set(catalog.operations) == _catalog_intents()
    assert [(row["word"], row["operation"]) for row in catalog.relations("spelling")] == _alias_spellings() + _models_spellings()
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


def test_media_job_intents_table_rebuilds_from_the_catalog():
    from gateway import media_job_intents

    media = ROOT / "registry" / "media_job_intents.yml"
    rows = yaml.safe_load(media.read_text(encoding="utf-8"))["intents"]
    catalog = load(CATALOG)
    assert project_media_job_intents(catalog) == rows
    assert media_job_intents.load_rows() == media_job_intents.parse_rows({"intents": rows})
    assert [row for row in catalog.unexpressed if row["source"] == media.name] == []


def test_media_operations_name_a_published_component_or_say_undeclared():
    catalog = load(CATALOG)
    media = {row["operation"] for row in catalog.relations("admission")}
    assert len(media) == 11
    for identifier in media:
        operation = catalog.operations[identifier]
        for side in ("request", "result"):
            schema = operation[side]["schema"]
            assert schema == "undeclared" or schema.startswith("openapi:")
        assert operation["family"] == identifier.split(".", 1)[0]
        submit = [b for b in catalog.relations("binding")
                  if b["operation"] == identifier and "job" in b["modes"]]
        assert bool(submit) == (operation["request"]["schema"] != "undeclared")


def test_media_operation_uses_explicit_boolean_not_route(tmp_path):
    from scripts.gen_operation_catalog import media_intent_operations

    source = tmp_path / "media.yml"
    row = {"intent": "sample.make", "modality": "sample", "submittable": False,
           "route": "/sample", "component": "SampleRequest"}
    source.write_text(yaml.safe_dump({"intents": [row]}), encoding="utf-8")
    operations, relations = media_intent_operations(source)
    assert operations == [{
        "id": "sample.make", "family": "sample",
        "request": {"schema": "openapi:SampleRequest"}, "result": {"schema": "undeclared"},
        "submittable_as_job": False, "modality": "sample",
    }]
    assert [r["kind"] for r in relations] == ["admission", "binding"]
    row["submittable"] = "false"
    source.write_text(yaml.safe_dump({"intents": [row]}), encoding="utf-8")
    with pytest.raises(ValueError, match="explicit boolean"):
        media_intent_operations(source)


def test_media_source_rejects_unclassified_fields_duplicates_and_half_routes(tmp_path):
    from scripts.gen_operation_catalog import media_intent_operations

    source = tmp_path / "media.yml"
    row = {"intent": "sample", "modality": "sample", "submittable": False}
    for document, message in [
        ({"intents": [{**row, "unclassified": "value"}]}, "field must be classified"),
        ({"intents": [row, row]}, "Duplicate media intent"),
        ({"intents": [], "unclassified": []}, "top-level key must be classified"),
        ({"intents": [{**row, "route": "/sample"}]}, "route and its component"),
    ]:
        source.write_text(yaml.safe_dump(document), encoding="utf-8")
        with pytest.raises(ValueError, match=message):
            media_intent_operations(source)

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
    for index, row in enumerate(document["alias_operations"]):
        if row["intent"] is not None:
            next(gap for gap in expected if gap["key"] == f"alias_operations[{index}]")["reason"] = (
                "Local alias id and spellings are projected; modalities, execution and flags are not yet expressed."
            )
    catalog = load(CATALOG)
    assert [dict(row) for row in catalog.unexpressed if row["source"] == source.name] == expected
    assert set(catalog.operations) == _catalog_intents()
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


def test_conservative_models_examples_are_gaps_not_operation_contracts():
    # Assumption: examples/prefixes do not define request/result contracts.
    source = ROOT / "registry" / "models_catalog_operations.yml"
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    expected = [
        {"source": source.name, "key": "schema_version",
         "reason": "Source format version, not an operation fact."},
        {"source": source.name, "key": "row_id_placeholder",
         "reason": "Example substitution marker, not an operation fact."},
    ]
    from scripts.gen_operation_catalog import MODELS_GAP_REASON, MODELS_WORDS_GAP_REASON

    catalog = load(CATALOG)
    for index, row in enumerate(document["operations"]):
        intents = {example["intent"] for example in row.get("examples", [])}
        projected = (len(intents) == 1 and next(iter(intents)) in catalog.operations
                     and row["intent_prefixes"] == sorted(intents))
        selectors = row["intent_prefixes"]
        if projected:
            reason = MODELS_WORDS_GAP_REASON
        elif len(selectors) != 1 or selectors[0].endswith(".") or "*" in selectors[0]:
            reason = "Missing operation identity needs one exact singleton selector."
        elif not intents or intents != {selectors[0]}:
            reason = "Exact selector and every example intent must agree."
        elif selectors[0] not in catalog.operations:
            reason = "Missing operation identity needs one corroborated exact family mapping."
        else:
            reason = MODELS_GAP_REASON
        expected.append({
            "source": source.name, "key": f"operations[{index}]:{row['id']}",
            "reason": reason,
        })
    assert [dict(row) for row in catalog.unexpressed if row["source"] == source.name] == expected
    from gateway.operation_catalog import project_models_legacy_spellings
    assert list(project_models_legacy_spellings(catalog).items()) == _alias_spellings()
    assert set(catalog.operations) == _catalog_intents()
    assert len(catalog.relations("spelling", "legacy")) == len(_alias_spellings())


def test_models_legacy_projection_uses_literal_relation_rows_without_folding():
    from gateway.operation_catalog import Catalog, project_models_legacy_spellings

    catalog = Catalog({"sample": {"id": "sample"}}, (
        {"kind": "spelling", "context": "legacy", "word": "Sample_Word", "operation": "sample"},
        {"kind": "spelling", "context": "legacy", "word": "sample.word", "operation": "sample"},
        {"kind": "spelling", "context": "route", "word": "excluded", "operation": "sample"},
    ))
    assert list(project_models_legacy_spellings(catalog).items()) == [
        ("Sample_Word", "sample"), ("sample.word", "sample"),
    ]
    assert "sample_word" not in project_models_legacy_spellings(catalog)


def test_models_gap_rejects_unclassified_fields_and_duplicate_ids(tmp_path):
    from scripts.gen_operation_catalog import models_catalog_gaps

    source = tmp_path / "models.yml"
    row = {"id": "sample", "words": ["Sample_Word"], "intent_prefixes": ["sample."]}
    document = {"schema_version": 1, "row_id_placeholder": "$row", "operations": [row]}
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    assert [gap["key"] for gap in models_catalog_gaps(source)] == [
        "schema_version", "row_id_placeholder", "operations[0]:sample",
    ]
    for invalid in (
        {**document, "extra": []},
        {**document, "schema_version": True},
        {**document, "operations": [row, row]},
        {**document, "operations": [{**row, "extra": "ignored"}]},
        {**document, "operations": [{**row, "words": "not-a-list"}]},
    ):
        source.write_text(yaml.safe_dump(invalid), encoding="utf-8")
        with pytest.raises(ValueError):
            models_catalog_gaps(source)


def test_conservative_frontend_bindings_are_exact_gaps_not_equivalences():
    source = ROOT / "registry" / "protocol_frontends.yml"
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    expected = [{
        "source": source.name, "key": f"{section}[{index}]:{row['id']}",
        "reason": "Admission, runtime policy and receipt bindings need defined operation targets and closed relation field contracts; they are not spelling equivalences.",
    } for section in ("records", "frontends")
      for index, row in enumerate(document[section])]
    catalog = load(CATALOG)
    assert [dict(row) for row in catalog.unexpressed if row["source"] == source.name] == expected
    assert set(catalog.operations) == _catalog_intents()
    assert [(row["word"], row["operation"]) for row in catalog.relations("spelling")] == _alias_spellings() + _models_spellings()


def test_frontend_gap_does_not_equate_admission_and_runtime_policy(tmp_path):
    from scripts.gen_operation_catalog import protocol_frontend_gaps

    source = tmp_path / "frontends.yml"
    row = {"id": "sample", "registered_routes": [{
        "operation": "operation-a", "intent": "admission-b",
        "runtime_policy": {"intent": "runtime-c"},
    }]}
    document = {"records": [row], "frontends": [], "input_contract": {}}
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    gaps = protocol_frontend_gaps(source)
    assert gaps == [{
        "source": source.name, "key": "records[0]:sample",
        "reason": "Admission, runtime policy and receipt bindings need defined operation targets and closed relation field contracts; they are not spelling equivalences.",
    }]
    assert all("operation" not in gap and "word" not in gap for gap in gaps)
    for invalid in (
        {**document, "records": [row, row]},
        {**document, "records": [{**row, "registered_routes": [None]}]},
        {**document, "unexpected": {}},
    ):
        source.write_text(yaml.safe_dump(invalid), encoding="utf-8")
        with pytest.raises(ValueError):
            protocol_frontend_gaps(source)



@pytest.mark.parametrize("document", [
    "intents: []\nintents: []\n",
    "intents:\n  - {intent: first, intent: second, modality: sample, submittable: false}\n",
    "intents:\n  - {intent: sample, modality: sample, submittable: false, component: First, component: Second}\n",
])
def test_conservative_media_source_rejects_duplicate_keys_before_projection(tmp_path, document):
    # Conservative assumption: no last-key-wins interpretation of source facts.
    from scripts.gen_operation_catalog import media_intent_operations

    source = tmp_path / "media.yml"
    source.write_text(document, encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate media mapping key"):
        media_intent_operations(source)


def test_conservative_media_source_preserves_order_without_duplicate_keys(tmp_path):
    from scripts.gen_operation_catalog import media_intent_operations

    source = tmp_path / "media.yml"
    rows = [
        {"intent": "second", "modality": "sample", "submittable": False},
        {"intent": "first", "modality": "sample", "submittable": True,
         "component": "SampleRequest", "route": "/sample"},
    ]
    source.write_text(yaml.safe_dump({"intents": rows}), encoding="utf-8")
    operations, _relations = media_intent_operations(source)
    assert [operation["id"] for operation in operations] == ["second", "first"]


def test_alias_operations_and_legacy_spellings_match_source():
    catalog = load(CATALOG)
    source = yaml.safe_load(SOURCE.read_text(encoding="utf-8"))
    spellings = catalog.relations("spelling", "legacy")
    assert [(row["word"], row["operation"]) for row in spellings] == _alias_spellings()
    assert [row["order"] for row in spellings] == list(range(len(spellings)))
    assert len(spellings) == 11
    assert len(set(catalog.operations) - _media_intents() - MODEL_DECLARED) == 5
    for row in _alias_rows():
        intent = row["intent"]
        if intent is None:
            continue
        assert intent in catalog.operations
        assert any(spelling["word"] == row["name"] and spelling["operation"] == intent
                   for spelling in spellings)
        if intent not in _media_intents():
            expected = {"id": intent, "family": source["protocol_words"][intent],
                        "request": {"schema": "undeclared"}, "result": {"schema": "undeclared"},
                        "submittable_as_job": False}
            if row.get("output_modalities"):
                expected["modality"] = row["output_modalities"][0]
            assert dict(catalog.operations[intent]) == expected
    assert dict(_alias_spellings())["translations"] == "speech.transcription"
    assert "speech.translation" not in {row["intent"] for row in _alias_rows()}


def _alias_fixture_sources(tmp_path, rows, words):
    source = tmp_path / "words.yml"
    document = {name: {} for name in PROJECTED}
    document.update({name: [] for name in UNEXPRESSED})
    document["protocol_words"] = words
    source.write_text(yaml.safe_dump(document), encoding="utf-8")
    local = tmp_path / "local.yml"
    local.write_text(yaml.safe_dump({
        "schema_version": 1, "alias_operations": rows, "workflow_families": [],
        "backend_rows": [], "image_rows": [], "image_defaults": {}, "workflow_defaults": {},
    }), encoding="utf-8")
    return source, local


def test_alias_intent_missing_protocol_family_is_refused(tmp_path):
    from scripts.gen_operation_catalog import generate

    source, local = _alias_fixture_sources(
        tmp_path, [{"name": "sample", "intent": "sample.missing"}], {})
    with pytest.raises(ValueError, match=r"sample\.missing"):
        generate(source, local_source=local)


def test_alias_duplicate_legacy_name_is_refused_by_loader(tmp_path):
    from scripts.gen_operation_catalog import generate

    source, local = _alias_fixture_sources(tmp_path, [
        {"name": "duplicate", "intent": "sample.first"},
        {"name": "duplicate", "intent": "sample.second"},
    ], {"sample.first": "sample", "sample.second": "sample"})
    output = tmp_path / "catalog.yml"
    output.write_text(generate(source, local_source=local), encoding="utf-8")
    with pytest.raises(CatalogError, match="unique_spelling_in_context"):
        load(output)


def _reader_sources(tmp_path):
    source = tmp_path / "capability_words.yml"
    source.write_text(json.dumps({
        "schema_version": 1,
        "capabilities": [{"id": "old", "label": "Old"}, {"id": "new", "label": "New"}],
        "protocol_words": {"shared": "old", "Sample_Word": "old"},
        "route_intents": {"shared": "old", "route.only": "old"},
        "type_capabilities": {"sample": "old"},
    }), encoding="utf-8")
    rows = [
        {"kind": "family", "context": context, "word": word, "family": "new", "order": order}
        for context, words in (
            ("protocol", ["Sample_Word", "shared"]),
            ("route", ["route.only", "shared"]),
            ("type", ["sample"]),
        )
        for order, word in enumerate(words)
    ]
    catalog = tmp_path / "operations.yml"
    catalog.write_text(json.dumps({"schema_version": 1, "operations": [], "relations": rows[::-1]}),
                       encoding="utf-8")
    return source, catalog


def test_capability_reader_catalog_shipped_source_wins(tmp_path, monkeypatch):
    from gateway import capability_ids as ids

    source, catalog = _reader_sources(tmp_path)
    # The pre-cutover reader accepts this same call but returns the old source maps.
    monkeypatch.setattr(ids, "CAPABILITY_WORDS_PATH", source)
    protocol, intents = ids.load_capability_words(source)
    assert list(protocol.items()) == [("Sample_Word", "new"), ("shared", "new")]
    assert list(intents.items()) == [
        ("Sample_Word", "new"), ("shared", "new"), ("route.only", "new")]
    types, audio, hidden = ids.load_capability_classification(source)
    assert dict(types) == {"sample": "new"}
    assert dict(audio) == dict(hidden) == {}
    monkeypatch.setattr(ids, "INTENT_CAPABILITIES", intents)
    assert ids.capability_of_word("  Sample_Word  ") == "new"
    assert ids.capability_of_word("  SAMPLE_word  ") == "SAMPLE_word"
    for table in (protocol, intents, types):
        with pytest.raises(TypeError):
            table["injected"] = "old"
    frozen = load(catalog)
    with pytest.raises(TypeError):
        frozen.relations("family")[0]["family"] = "old"


def test_capability_reader_catalog_explicit_and_custom_contract(tmp_path):
    from gateway import capability_ids as ids

    source, catalog = _reader_sources(tmp_path)
    assert ids.load_capability_words(source)[0]["shared"] == "old"
    for binding in (catalog, load(catalog)):
        assert ids.load_capability_words(source, catalog=binding)[0]["shared"] == "new"
        assert ids.load_capability_classification(source, catalog=binding)[0]["sample"] == "new"
        assert len(ids._load_capability_data(source, catalog=binding)) == 4
        assert len(ids.load_capability_presentation(source, catalog=binding)) == 3
        assert list(ids.load_capability_display_order(source, catalog=binding)) == ["old", "new"]
        assert ids.load_capability_alias_name_hiding(source, catalog=binding) == frozenset()
        assert len(ids.load_capability_member_words(source, catalog=binding)) == 2
        assert len(ids.load_capability_screen_words(source, catalog=binding)) == 3


def test_capability_reader_catalog_absent_source_sections(tmp_path, monkeypatch):
    from gateway import capability_ids as ids

    source, _ = _reader_sources(tmp_path)
    document = json.loads(source.read_text(encoding="utf-8"))
    for section in PROJECTED:
        del document[section]
    source.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(ids, "CAPABILITY_WORDS_PATH", source)
    assert ids.load_capability_words(source)[0]["shared"] == "new"
    assert ids.load_capability_classification(source)[0]["sample"] == "new"


@pytest.mark.parametrize("context", ["protocol", "route", "type"])
def test_capability_reader_catalog_invalid_family_target(tmp_path, context):
    from gateway import capability_ids as ids

    source, catalog = _reader_sources(tmp_path)
    document = json.loads(catalog.read_text(encoding="utf-8"))
    next(row for row in document["relations"] if row["context"] == context)["family"] = "absent"
    catalog.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown capability"):
        ids.load_capability_words(source, catalog=catalog)


def test_capability_reader_catalog_conflict_and_no_fallback(tmp_path, monkeypatch):
    from gateway import capability_ids as ids

    source, catalog = _reader_sources(tmp_path)
    document = json.loads(catalog.read_text(encoding="utf-8"))
    next(row for row in document["relations"]
         if row["context"] == "route" and row["word"] == "shared")["family"] = "old"
    catalog.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(ValueError, match="conflicting capabilities"):
        ids.load_capability_words(source, catalog=catalog)
    monkeypatch.setattr(ids, "CAPABILITY_WORDS_PATH", source)
    catalog.write_text("{", encoding="utf-8")
    with pytest.raises(CatalogError):
        ids.load_capability_words(source)
    catalog.unlink()
    with pytest.raises(CatalogError):
        ids.load_capability_words(source)


def _missing_projection_input(tmp_path, selectors=None, examples=None):
    models = tmp_path / "models.json"
    models.write_text(json.dumps({
        "schema_version": 1, "row_id_placeholder": "$row",
        "operations": [{"id": "display", "words": ["literal"],
                        "intent_prefixes": ["sample.exact"] if selectors is None else selectors,
                        "examples": ([{"intent": "sample.exact"}, {"intent": "sample.exact"}]
                                     if examples is None else examples)}],
    }), encoding="utf-8")
    return models


def test_missing_operation_projection_exact_and_null_local(tmp_path):
    from scripts.gen_operation_catalog import (
        missing_model_operations, models_catalog_projection, local_alias_operations,
    )

    source, local = _alias_fixture_sources(tmp_path, [
        {"name": "unpublished", "intent": None, "input_modalities": None,
         "output_modalities": None, "execution": None, "publish_capability": False},
    ], {})
    vocabulary = yaml.safe_load(source.read_text(encoding="utf-8"))
    vocabulary["route_intents"] = {"sample.exact": "display"}
    models = _missing_projection_input(tmp_path)
    local_operations, local_relations = local_alias_operations(local, {}, [])
    assert local_operations == local_relations == []
    operations, missing = missing_model_operations(models, vocabulary, local_operations)
    gaps, spellings = models_catalog_projection(models, operations)
    output = tmp_path / "result.json"
    output.write_text(json.dumps({"schema_version": 1, "operations": operations,
                                 "relations": spellings, "unexpressed": gaps + missing}))
    catalog = load(output)
    assert set(catalog.operations) == {"sample.exact"}
    assert dict(catalog.operations["sample.exact"]) == {
        "id": "sample.exact", "family": "display",
        "request": {"schema": "undeclared"}, "result": {"schema": "undeclared"},
        "submittable_as_job": False,
    }
    assert {row["operation"] for row in catalog.relations("spelling")} == {"sample.exact"}
    for kind in ("admission", "binding", "runtime_policy", "receipt"):
        assert catalog.relations(kind) == ()
    assert missing == []


@pytest.mark.parametrize("selectors,examples,vocabulary,reason", [
    ([], [{"intent": "sample.exact"}], {"route_intents": {"sample.exact": "display"}}, "singleton"),
    (["sample.exact", "sample.other"], [{"intent": "sample.exact"}], {}, "singleton"),
    (["sample."], [{"intent": "sample.exact"}], {}, "singleton"),
    (["sample."], [{"intent": "sample."}], {"route_intents": {"sample.": "display"}}, "singleton"),
    (["sample.exact"], [{"intent": "sample.other"}], {}, "agree"),
    (["sample.exact"], [], {}, "agree"),
    (["sample.exact"], [{"intent": "sample.exact"}], {}, "family"),
    (["sample.exact"], [{"intent": "sample.exact"}],
     {"protocol_words": {"sample.exact": "first"}, "route_intents": {"sample.exact": "second"}}, "family"),
])
def test_missing_operation_projection_gaps(tmp_path, selectors, examples, vocabulary, reason):
    from scripts.gen_operation_catalog import missing_model_operations, models_catalog_projection

    models = _missing_projection_input(tmp_path, selectors, examples)
    operations, gaps = missing_model_operations(models, vocabulary, [])
    assert operations == [] and reason in gaps[0]["reason"]
    _, spellings = models_catalog_projection(models, operations)
    output = tmp_path / "result.json"
    output.write_text(json.dumps({"schema_version": 1, "operations": operations,
                                 "relations": spellings, "unexpressed": gaps}))
    catalog = load(output)
    assert not catalog.operations and not catalog.relations("spelling")
    for kind in ("admission", "binding", "runtime_policy", "receipt"):
        assert catalog.relations(kind) == ()


def test_missing_operation_projection_conflicting_declaration(tmp_path):
    from scripts.gen_operation_catalog import missing_model_operations

    models = _missing_projection_input(tmp_path)
    existing = [{"id": "sample.exact", "family": "different",
                 "request": {"schema": "undeclared"}, "result": {"schema": "undeclared"},
                 "submittable_as_job": False}]
    with pytest.raises(ValueError, match="Conflicting declared operation"):
        missing_model_operations(models, {"route_intents": {"sample.exact": "display"}}, existing)
    assert existing[0]["family"] == "different"
