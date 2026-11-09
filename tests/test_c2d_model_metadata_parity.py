"""Parity golden for gateway/model_metadata.py (tests/fixtures/c2d_parity/model_metadata.json).

Today's output, stored as literal JSON and compared field by field: from_operation for every operation,
operations_of per model type (with and without input narrowing), from_service_capabilities for every
service capability name plus the vision case and unknown names, from_origin for both origins, and
from_contract over every shipped workflow manifest. A refactor that moves the tables to data and changes
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
    for key in ("operations", "types", "service_capability_cases", "workflow_ids"):
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
    assert ACTUAL["from_contract_non_mapping"] == GOLDEN["from_contract_non_mapping"]
