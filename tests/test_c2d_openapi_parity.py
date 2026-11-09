"""Parity goldens for gateway/workflow_openapi.py and gateway/workflows_catalog.py.

For every shipped workflow: the OpenAPI fragment that ``augment_openapi`` adds (its component schemas,
its ``x-azizam-workflows`` entry, the routes whose extension schema carries it, its run path) and the
catalog row and examples that ``build_workflows_catalog`` produces. The inputs (base URL, aliases, video
limits, capability rows, policy facts) are fixed in tests/support/c2d_snapshots.py.

Each golden item holds the SHA-256 of its canonical JSON and, when under 20 KB, the full JSON. The test
compares the hash first. On a mismatch it names the workflow and, when the full JSON is stored, the
first differing field. Only tests/fixtures/c2d_parity/capture.py writes the goldens.

List order is compared as returned (catalog order is sorted by workflow id by the code under test).
No helper is stubbed: both modules are pure and the manifests are the committed ones.
"""

from __future__ import annotations

import pytest

from tests.support import c2d_parity
from tests.support.c2d_snapshots import capture_openapi, capture_workflows_catalog

OPENAPI_GOLDEN = c2d_parity.load_golden("openapi")
CATALOG_GOLDEN = c2d_parity.load_golden("workflows_catalog")
OPENAPI = capture_openapi()
CATALOG = capture_workflows_catalog()


def _assert_item(actual: dict, golden: dict, label: str) -> None:
    if actual["sha256"] == golden["sha256"]:
        return
    if "full" in golden and "full" in actual:
        for field in sorted(set(actual["full"]) | set(golden["full"])):
            assert actual["full"].get(field) == golden["full"].get(field), f"{label}: field {field} differs"
    pytest.fail(f"{label}: canonical JSON differs (sha256 {actual['sha256'][:12]} != {golden['sha256'][:12]}); "
                f"{'no full copy stored' if 'full' not in golden else 'full copies equal field by field: ordering inside a list?'}")


def test_openapi_workflow_set_matches_golden():
    assert sorted(OPENAPI["workflows"]) == sorted(OPENAPI_GOLDEN["workflows"])
    assert OPENAPI["inputs"] == OPENAPI_GOLDEN["inputs"]


@pytest.mark.parametrize("workflow_id", sorted(OPENAPI_GOLDEN["workflows"]))
def test_openapi_fragment_matches_golden(workflow_id):
    _assert_item(OPENAPI["workflows"][workflow_id], OPENAPI_GOLDEN["workflows"][workflow_id], workflow_id)


def test_openapi_shared_parts_match_golden():
    _assert_item(OPENAPI["shared"], OPENAPI_GOLDEN["shared"], "shared (info, non-workflow components, catalog order)")


def test_catalog_workflow_set_and_order_match_golden():
    assert CATALOG["workflow_ids_in_catalog_order"] == CATALOG_GOLDEN["workflow_ids_in_catalog_order"]
    assert CATALOG["inputs"] == CATALOG_GOLDEN["inputs"]
    assert CATALOG["envelope"] == CATALOG_GOLDEN["envelope"]


@pytest.mark.parametrize("workflow_id", sorted(CATALOG_GOLDEN["workflows"]))
def test_catalog_examples_match_golden(workflow_id):
    _assert_item(CATALOG["workflows"][workflow_id]["examples"], CATALOG_GOLDEN["workflows"][workflow_id]["examples"],
                 f"{workflow_id} examples")


@pytest.mark.parametrize("workflow_id", sorted(CATALOG_GOLDEN["workflows"]))
def test_catalog_row_matches_golden(workflow_id):
    _assert_item(CATALOG["workflows"][workflow_id]["row"], CATALOG_GOLDEN["workflows"][workflow_id]["row"],
                 f"{workflow_id} row")
