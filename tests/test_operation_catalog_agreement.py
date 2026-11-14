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
    rows = load(CATALOG).unexpressed
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
