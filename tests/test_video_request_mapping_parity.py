"""Pin the video request mapping and catalog request examples of every shipped video workflow.

The golden file was captured from the code before the mapping read the video section's declared roles.
Do not regenerate it to bless a change: a difference here is a change in what the catalog teaches callers.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from gateway import workflow_openapi as openapi
from gateway import workflows_catalog as catalog
from gateway.discovery import WorkflowRegistry

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "tests" / "fixtures" / "video_request_mapping_parity.json").read_text(encoding="utf-8"))
SHIPPED_VIDEO_WORKFLOW_COUNT = 12
RENDERED_CODE = ("curl", "javascript", "python")


class ShippedVideoRequestMappingParityTests(unittest.TestCase):
    def test_every_shipped_video_workflow_keeps_its_mapping_and_examples(self) -> None:
        registry = WorkflowRegistry.from_directory(ROOT / "manifests" / "workflows")
        self.assertEqual(registry.errors, [])
        records = [r for r in registry.records() if r.intent.startswith("video.")]
        self.assertEqual(len(records), SHIPPED_VIDEO_WORKFLOW_COUNT)
        self.assertEqual(sorted(r.id for r in records), sorted(GOLDEN))
        rows = {row["id"]: row for row in openapi.workflow_catalog(records)}
        for record in records:
            with self.subTest(workflow=record.id):
                example = catalog._workflow_row(
                    record, capability_rows=[], openapi_row=rows[record.id], aliases=None,
                    receipts=[], jobs=[], base_url="https://gateway.test",
                )["example_request"]
                actual = {
                    "mapping": {mode: openapi.request_mapping(record, mode) for mode in ("sync", "async")},
                    "example": {k: v for k, v in example.items() if k not in RENDERED_CODE},
                }
                self.assertEqual(json.loads(json.dumps(actual, default=str)), GOLDEN[record.id])


if __name__ == "__main__":
    unittest.main()
