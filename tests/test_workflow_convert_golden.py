"""Byte golden for the schema 1 to 2 converter over every schema 1 input the contract and migration tests use.

The golden file was generated from the converter before the intent-prefix table moved to data. This test never writes it:
a converter change that moves one output byte fails here.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import workflow_schema_migration as migration  # noqa: E402
from gateway.workflow_convert import convert_1_to_2  # noqa: E402

GOLDEN = ROOT / "tests" / "fixtures" / "workflows_v1_converted" / "converted.json"
FIXTURES = ROOT / "tests" / "fixtures" / "workflows_v1"
EXPECTED_FIXTURE_COUNT = 39


def _serialize(document: dict, notes: list) -> str:
    return json.dumps({"document": document, "notes": notes}, sort_keys=True, indent=1, default=str)


def _current() -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(FIXTURES.glob("*.yml")):
        document, notes = convert_1_to_2(yaml.safe_load(path.read_text(encoding="utf-8")))
        out["fixture:" + path.name] = _serialize(document, notes)
    for record_id, record in sorted(migration.frozen_release_records().items()):
        document, notes = convert_1_to_2(copy.deepcopy(record["manifest"]))
        out["frozen:" + record_id] = _serialize(document, notes)
    return out


def test_the_converter_output_for_every_schema_1_input_is_byte_identical_to_the_golden():
    current = _current()
    assert sum(key.startswith("fixture:") for key in current) == EXPECTED_FIXTURE_COUNT
    assert len(current) == 2 * EXPECTED_FIXTURE_COUNT
    expected = GOLDEN.read_text(encoding="utf-8")
    assert json.dumps(current, sort_keys=True, indent=1) + "\n" == expected
