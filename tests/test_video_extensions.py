"""Tests for video workflow extension manifest bindings."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.image_extensions import WORKFLOW_EXTENSION_NAMESPACE as NS  # noqa: E402
from gateway.manifests import load_workflow_manifest  # noqa: E402
from gateway.video_extensions import (  # noqa: E402
    STANDARD_VIDEO_CREATE_FIELDS,
    bind_video_workflow_extensions,
)

MANIFEST_PATH = ROOT / "tests" / "fixtures" / "video_extensions" / "manifest.yml"
WORKFLOW_PATH = ROOT / "tests" / "fixtures" / "video_extensions" / "workflow.json"


def _load_workflow() -> dict:
    with WORKFLOW_PATH.open("r", encoding="utf-8") as f:
        workflow = json.load(f)
    workflow.pop("_meta", None)
    return workflow


class TestVideoWorkflowExtensions(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_extensions_bind_only_manifest_documented_inputs(self) -> None:
        workflow = _load_workflow()
        original = copy.deepcopy(workflow)
        body = {
            "prompt": "top level prompt",
            "model": "test-extensions",
            "extensions": {
                NS: {
                    "steps": 28,
                    "stage1_cfg": 4.5,
                    "stage2_cfg": 1.25,
                }
            },
        }

        result = bind_video_workflow_extensions(body, self.manifest, workflow)

        self.assertTrue(result.ok, result.validation.rejected)
        self.assertEqual(workflow, original)
        self.assertEqual(result.request["22"]["inputs"]["steps"], 28)
        self.assertEqual(result.request["18"]["inputs"]["cfg"], 4.5)
        self.assertEqual(result.request["31"]["inputs"]["cfg"], 1.25)

    def test_inputs_alias_binds_to_workflow_namespace(self) -> None:
        result = bind_video_workflow_extensions(
            {
                "prompt": "x",
                "inputs": {"steps": 32, "stage1_cfg": 5.5},
            },
            self.manifest,
            _load_workflow(),
        )

        self.assertTrue(result.ok, result.validation.rejected)
        self.assertEqual(result.request["22"]["inputs"]["steps"], 32)
        self.assertEqual(result.request["18"]["inputs"]["cfg"], 5.5)

    def test_route_managed_extension_input_is_rejected(self) -> None:
        result = bind_video_workflow_extensions(
            {"prompt": "x", "extensions": {NS: {"seed": 1234, "width": 960}}},
            self.manifest,
            _load_workflow(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(set(result.validation.rejected), {"seed", "width"})
        self.assertIn("standard", result.validation.rejected["seed"])

    def test_unknown_top_level_workflow_control_is_rejected(self) -> None:
        result = bind_video_workflow_extensions(
            {"prompt": "x", "denoise": 0.5},
            self.manifest,
            _load_workflow(),
        )

        self.assertFalse(result.ok)
        self.assertIn("denoise", result.unknown_top_level)

    def test_unknown_extension_input_is_rejected(self) -> None:
        result = bind_video_workflow_extensions(
            {"prompt": "x", "extensions": {NS: {"denoise": 0.5}}},
            self.manifest,
            _load_workflow(),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.validation.rejected["denoise"], "unknown input")

    def test_standard_video_fields_are_not_top_level_rejections(self) -> None:
        result = bind_video_workflow_extensions(
            {name: None for name in STANDARD_VIDEO_CREATE_FIELDS},
            self.manifest,
            _load_workflow(),
        )

        self.assertEqual(result.unknown_top_level, {})


if __name__ == "__main__":
    unittest.main()
