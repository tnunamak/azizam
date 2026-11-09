"""Parity golden for gateway/model_metadata.py (tests/fixtures/c2d_parity/model_metadata.json).

Today's output, stored as literal JSON and compared field by field: from_operation for every operation,
operations_of per model type (with and without input narrowing), from_service_capabilities for every
service capability name plus the vision case and unknown names, from_origin for both origins, and
from_contract over every shipped workflow manifest (and non-mapping values), from_capability over every key of
the intent table, each family prefix and unfamiliar intents, from_fields over listed-field dicts, the merge/common/
apply_declared combinators, and annotate_entries and metadata_index over a fixed list of entries. A refactor that moves the tables to data and changes
any answer fails here. Only tests/fixtures/c2d_parity/capture.py writes the golden.

List order is compared as returned. operations_of returns the table's order, and that order is the contract.
No helper is stubbed: the module is pure and the manifests are the committed ones.
"""

from __future__ import annotations

import pytest

from tests.support import c2d_parity
from tests.support.c2d_snapshots import capture_model_metadata

GOLDEN = c2d_parity.load_golden("model_metadata")
ACTUAL = capture_model_metadata()


def test_input_lists_match_golden():
    for key in ("operations", "types", "service_capability_cases", "workflow_ids", "capability_intents", "from_fields_cases"):
        assert ACTUAL[key] == GOLDEN[key], f"{key}: the set of cases changed (a manifest or table gained or lost an entry)"


@pytest.mark.parametrize("operation", GOLDEN["operations"])
def test_from_operation_matches_golden(operation):
    assert ACTUAL["from_operation"][operation] == GOLDEN["from_operation"][operation]


@pytest.mark.parametrize("type_", GOLDEN["types"])
def test_operations_of_matches_golden(type_):
    keys = sorted(key for key in GOLDEN["operations_of"] if key.startswith(f"{type_}|"))
    assert keys, type_
    for key in keys:
        assert ACTUAL["operations_of"][key] == GOLDEN["operations_of"][key], key
    assert ACTUAL["operations_of_non_string_type"] == GOLDEN["operations_of_non_string_type"]


@pytest.mark.parametrize("case", GOLDEN["service_capability_cases"])
def test_from_service_capabilities_matches_golden(case):
    assert ACTUAL["from_service_capabilities"][case] == GOLDEN["from_service_capabilities"][case]


def test_from_origin_matches_golden():
    assert ACTUAL["from_origin"] == GOLDEN["from_origin"]


@pytest.mark.parametrize("workflow_id", GOLDEN["workflow_ids"])
def test_from_contract_matches_golden(workflow_id):
    assert ACTUAL["from_contract"][workflow_id] == GOLDEN["from_contract"][workflow_id]


def test_from_contract_non_mapping_matches_golden():
    assert ACTUAL["from_contract_non_mapping"] == GOLDEN["from_contract_non_mapping"]


def test_capability_tables_match_golden():
    assert ACTUAL["capability_intent_table"] == GOLDEN["capability_intent_table"]
    assert ACTUAL["capability_intent_families"] == GOLDEN["capability_intent_families"]


@pytest.mark.parametrize("intent", GOLDEN["capability_intents"])
def test_from_capability_matches_golden(intent):
    keys = sorted(key for key in GOLDEN["from_capability"] if key.startswith(f"{intent}|"))
    assert keys, intent
    for key in keys:
        assert ACTUAL["from_capability"][key] == GOLDEN["from_capability"][key], key


@pytest.mark.parametrize("case", GOLDEN["from_fields_cases"])
def test_from_fields_matches_golden(case):
    assert ACTUAL["from_fields"][case] == GOLDEN["from_fields"][case]


@pytest.mark.parametrize("name", sorted(GOLDEN["combinators"]))
def test_combinators_match_golden(name):
    assert ACTUAL["combinators"][name] == GOLDEN["combinators"][name]


@pytest.mark.parametrize("section", sorted(GOLDEN["annotate"]))
def test_annotate_entries_matches_golden(section):
    actual, golden = ACTUAL["annotate"][section], GOLDEN["annotate"][section]
    if isinstance(golden, list):
        assert len(actual) == len(golden)
        for got, want in zip(actual, golden):
            assert got == want, f"{section}: entry {want.get('id') if isinstance(want, dict) else want}"
    else:
        assert actual == golden
