"""A workflow row says whether to choose it today (film agent memo M5).

`status` is `current` or `superseded`; a superseded workflow names its replacement in `superseded_by` and still runs.
The value is data in the manifest, the schema closes it, and the REST workflows listing and MCP `capabilities` both
publish it. `capabilities` lists current workflows before the ones they replaced.
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import yaml

from gateway.discovery import WorkflowRegistry, summarize_workflow
from gateway.mcp_server import McpDeps, Tools
from gateway.workflow_contract import check_contract
from gateway.workflow_openapi import workflow_catalog

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / "manifests" / "workflows"


def _records():
    return WorkflowRegistry.from_directory(WORKFLOWS).records()


def _doc(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _codes(doc: dict) -> list[str | None]:
    return [error.as_dict().get("code") for error in check_contract(doc).errors]


def test_the_schema_accepts_a_superseded_workflow_that_names_its_replacement():
    doc = _doc("tangoflux.yml")
    assert doc["status"] == "superseded"
    assert check_contract(doc).ok, check_contract(doc).as_list()


def test_the_schema_refuses_a_replacement_on_a_current_or_unmarked_workflow():
    doc = copy.deepcopy(_doc("tangoflux.yml"))
    doc.pop("status")
    assert not check_contract(doc).ok
    doc["status"] = "current"
    assert not check_contract(doc).ok


def test_the_schema_refuses_a_status_it_does_not_know():
    doc = copy.deepcopy(_doc("sa3-small-sfx.yml"))
    doc["status"] = "retired"
    assert not check_contract(doc).ok, _codes(doc)


def test_every_bundled_replacement_is_a_current_bundled_workflow():
    by_id = {record.id: record for record in _records()}
    superseded = {record.id: record.contract for record in by_id.values() if record.contract.get("status") == "superseded"}
    assert superseded, "at least one bundled workflow is marked superseded (tangoflux)"
    for workflow_id, contract in superseded.items():
        target = contract.get("superseded_by")
        assert target in by_id and target != workflow_id, (workflow_id, target)
        assert (by_id[target].contract.get("status") or "current") == "current", (workflow_id, target)


def test_both_listings_publish_the_status():
    records = {record.id: record for record in _records()}
    rest = summarize_workflow(records["audio.generate.tangoflux"], "client")
    assert (rest["status"], rest["superseded_by"]) == ("superseded", "audio.generate.sa3-small-sfx")
    rest = summarize_workflow(records["audio.generate.sa3-small-sfx"], "client")
    assert (rest["status"], rest["superseded_by"]) == ("current", None)
    catalog = {entry["id"]: entry for entry in workflow_catalog(records.values())}
    assert (catalog["audio.generate.tangoflux"]["status"], catalog["audio.generate.tangoflux"]["superseded_by"]) == (
        "superseded", "audio.generate.sa3-small-sfx")
    assert catalog["audio.generate.sa3-small-sfx"]["status"] == "current"


def _tools() -> Tools:
    catalog = workflow_catalog(_records())
    deps = McpDeps(authenticate=lambda *_: None, call=None, catalog=lambda: catalog, aliases=lambda: {},  # type: ignore[arg-type]
                   store_asset=lambda *_: {}, read_asset=lambda *_: None, public_base_url=lambda _r: None)
    return Tools(deps, None, None)  # type: ignore[arg-type]


def test_capabilities_lists_current_workflows_before_the_ones_they_replaced():
    rows = asyncio.run(_tools().capabilities({}))["workflows"]
    statuses = [row.get("status", "current") for row in rows]
    assert "superseded" in statuses
    first_superseded = statuses.index("superseded")
    assert all(status == "superseded" for status in statuses[first_superseded:]), statuses
    tango = next(row for row in rows if row.get("status") == "superseded")
    sfx_rows = asyncio.run(_tools().capabilities({"intent": "audio.generate"}))["workflows"]
    assert sfx_rows[-1]["target"] == tango["target"]
    assert tango["superseded_by"] == "audio.generate.sa3-small-sfx"


def test_capabilities_for_one_target_names_its_replacement():
    rows = asyncio.run(_tools().capabilities({}))["workflows"]
    tango = next(row for row in rows if row.get("status") == "superseded")
    detail = asyncio.run(_tools().capabilities({"target": tango["target"]}))
    assert (detail["status"], detail["superseded_by"]) == ("superseded", "audio.generate.sa3-small-sfx")
