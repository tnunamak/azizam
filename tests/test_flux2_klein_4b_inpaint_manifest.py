"""Contract tests for the native Flux2 Klein inpaint workflow."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.extension_catalog import summarize_extensions  # noqa: E402
from gateway.discovery import WorkflowRegistry  # noqa: E402
from gateway.image_extensions import WORKFLOW_EXTENSION_NAMESPACE as NS  # noqa: E402
from gateway.manifests import bind_backend_request, load_workflow_manifest  # noqa: E402


MANIFEST_PATH = ROOT / "manifests/workflows/flux2-klein-4b-inpaint.yml"
WORKFLOW_PATH = ROOT / "workflows/images/flux2-klein-4b-inpaint.json"


class TestFlux2KleinInpaintManifest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)
        self.workflow = proxy.load_comfy_workflow("flux2-klein-4b-inpaint")

    def test_workflow_file_loads(self) -> None:
        self.assertTrue(WORKFLOW_PATH.exists())
        self.assertIsNotNone(self.workflow)
        assert self.workflow is not None
        self.assertEqual(self.workflow["_meta"]["name"], "flux2-klein-4b-inpaint")
        self.assertEqual(self.workflow["21"]["class_type"], "SetLatentNoiseMask")
        self.assertEqual(self.workflow["17"]["class_type"], "ImageToMask")

    def test_mask_and_inpaint_controls_bind_to_comfy_nodes(self) -> None:
        assert self.workflow is not None
        workflow = {k: v for k, v in self.workflow.items() if k != "_meta"}

        result = bind_backend_request(
            self.manifest,
            "comfyui",
            workflow,
            {
                "prompt": "paint a glass window into the masked area",
                "source_image": "source.png",
                "mask": "mask.png",
                "width": 768,
                "height": 512,
                "steps": 12,
                "guidance": 3.25,
                "denoise": 0.58,
                "seed": 1234,
                "mask_expand": 8,
                "mask_feather": 12,
            },
        )

        bound = result.request
        self.assertEqual(bound["6"]["inputs"]["text"], "paint a glass window into the masked area")
        self.assertEqual(bound["10"]["inputs"]["image"], "source.png")
        self.assertEqual(bound["12"]["inputs"]["image"], "mask.png")
        self.assertEqual(bound["11"]["inputs"]["width"], 768)
        self.assertEqual(bound["14"]["inputs"]["height"], 512)
        self.assertEqual(bound["48"]["inputs"]["steps"], 12)
        self.assertEqual(bound["26"]["inputs"]["guidance"], 3.25)
        self.assertEqual(bound["49"]["inputs"]["denoise"], 0.58)
        self.assertEqual(bound["25"]["inputs"]["noise_seed"], 1234)
        self.assertEqual(bound["18"]["inputs"]["expand"], 8)
        self.assertEqual(bound["19"]["inputs"]["left"], 12)
        self.assertEqual(bound["19"]["inputs"]["right"], 12)

    def test_route_managed_size_uses_manifest_bindings(self) -> None:
        assert self.workflow is not None
        workflow = {k: v for k, v in self.workflow.items() if k != "_meta"}

        bound = proxy._apply_manifest_route_inputs(
            self.manifest,
            workflow,
            {"width": 640, "height": 448},
        )

        self.assertEqual(bound["11"]["inputs"]["width"], 640)
        self.assertEqual(bound["11"]["inputs"]["height"], 448)
        self.assertEqual(bound["14"]["inputs"]["width"], 640)
        self.assertEqual(bound["14"]["inputs"]["height"], 448)
        self.assertEqual(bound["48"]["inputs"]["width"], 640)
        self.assertEqual(bound["48"]["inputs"]["height"], 448)

    def test_masked_flux_edit_prefers_committed_inpaint_workflow(self) -> None:
        self.assertEqual(
            proxy._prefer_inpaint_workflow_for_mask("flux2-klein-4b-edit"),
            "flux2-klein-4b-inpaint",
        )

    def test_discovery_exposes_only_extension_owned_inpaint_controls(self) -> None:
        registry = WorkflowRegistry.from_directory(ROOT / "manifests/workflows")

        catalog = summarize_extensions(registry, audience="client")
        workflow_targets = [
            workflow
            for extension in catalog["extensions"]
            if extension["namespace"] == NS
            for route in extension["routes"]
            if route["route"] == "/v1/images/edits"
            for target in route["targets"]
            for workflow in target["workflows"]
            if workflow["id"] == "image.edit.flux2-klein-4b-inpaint"
        ]

        self.assertEqual(len(workflow_targets), 1)
        controls = set(workflow_targets[0]["controls"])
        self.assertEqual(
            controls,
            {"denoise", "guidance", "mask_expand", "mask_feather", "seed", "steps"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
