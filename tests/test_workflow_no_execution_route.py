"""No-route recommendations stay absent in both workflow catalogs."""

from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway import workflows_catalog as member_catalog  # noqa: E402
from gateway.workflow_openapi import workflow_catalog  # noqa: E402


def record(intent, *, seconds=31):
    return SimpleNamespace(
        id=f"{intent}.demo",
        intent=intent,
        contract={
            "capability": "image" if intent == "image.generate" else "audio",
            "inputs": {"prompt": {"type": "string", "required": True}},
            "outputs": {},
            "bindings": {},
            "typical_seconds": seconds,
        },
    )


def member_row(monkeypatch, workflow, openapi_row):
    # Keep this synthetic-record test independent of discovery's record storage.
    # The actual recommendation, input mapping and example reader run unchanged.
    monkeypatch.setattr(member_catalog, "summarize_workflow", lambda _record, _audience: {
        "display_name": "Demo",
        "supported_backends": [],
        "outputs": [],
        "typical_seconds": workflow.contract["typical_seconds"],
    })
    return member_catalog._workflow_row(
        workflow,
        capability_rows=[],
        openapi_row=openapi_row,
        aliases=None,
        receipts=[],
        jobs=[],
        base_url="https://gateway.example",
    )


@pytest.mark.parametrize("intent", ["speech.synthesis", "future.unknown"])
def test_no_route_has_no_recommended_mode(intent):
    (row,) = workflow_catalog([record(intent)])
    assert row["short_id"] == "demo"
    assert row["execution"] == {"recommended": None, "sync": None, "async": None}


@pytest.mark.parametrize("intent", ["speech.synthesis", "future.unknown"])
def test_member_catalog_explains_missing_execution_route(monkeypatch, intent):
    workflow = record(intent)
    (openapi_row,) = workflow_catalog([workflow])
    row = member_row(monkeypatch, workflow, openapi_row)

    assert row["example_request"] == {
        "method": None,
        "path": None,
        "content_type": None,
        "body": {},
        "form": {},
        "files": {},
        "curl": "",
        "python": "",
        "javascript": "",
        "reason": "No execution route is available for this workflow.",
    }
    assert row["examples"] == {"curl": "", "python": "", "javascript": ""}
    assert row["openapi"]["execution"] == {"recommended": None, "sync": None, "async": None}
    assert [item["name"] for item in row["inputs"]] == ["prompt"]
    (prompt,) = row["inputs"]
    assert prompt["accepted"] is False
    assert prompt["ignored"] is True
    assert prompt["request_field"] is None
    assert prompt["applies"] == "No execution route"


@pytest.mark.parametrize("seconds,recommended,path", [
    (30, "sync", "/v1/images/generations"),
    (31, "async", "/gateway/v1/media/jobs"),
])
def test_image_recommendation_and_member_example_keep_boundary(monkeypatch, seconds, recommended, path):
    workflow = record("image.generate", seconds=seconds)
    (openapi_row,) = workflow_catalog([workflow])
    execution = openapi_row["execution"]
    assert execution["recommended"] == recommended
    assert execution["sync"]["path"] == "/v1/images/generations"
    assert execution["sync"]["model"] == "demo"
    assert execution["async"]["submit"]["path"] == "/gateway/v1/media/jobs"
    assert execution["async"]["submit"]["model"] == "demo"

    row = member_row(monkeypatch, workflow, openapi_row)
    assert row["openapi"]["execution"] == execution
    assert row["example_request"]["path"] == path
    assert row["example_request"]["method"] == "POST"
    assert row["example_request"]["content_type"] == "application/json"
    assert "reason" not in row["example_request"]
    assert all(row["examples"].values())
    prompt = next(item for item in row["inputs"] if item["name"] == "prompt")
    assert prompt["accepted"] is True
    assert prompt["request_field"] == "prompt"
