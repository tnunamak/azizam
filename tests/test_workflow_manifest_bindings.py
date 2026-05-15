"""Standalone tests for workflow manifest validation and backend bindings."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.manifests import (  # noqa: E402
    ManifestError,
    apply_backend_binding,
    load_workflow_manifest,
    manifest_identity,
    validate_inputs,
)
from gateway.receipts import RouteReceipt, record_validation_result  # noqa: E402


CONTROLNET_MANIFEST = {
    "schema_version": 1,
    "id": "image.edit.controlnet.example",
    "intent": "image.edit",
    "inputs": {
        "prompt": {"type": "string", "required": True},
        "denoise": {"type": "number", "minimum": 0, "maximum": 1},
        "controlnet_strength": {"type": "number", "minimum": 0, "maximum": 2},
    },
    "outputs": {"image": {"media_type": "image/png"}},
    "bindings": {
        "comfyui": {
            "workflow": "workflows/images/example-controlnet.json",
            "map": {
                "prompt": [{"node": "6", "input": "text"}],
                "denoise": [{"node": "31", "input": "denoise"}],
                "controlnet_strength": [{"node": "44", "input": "strength"}],
            },
        }
    },
}


class TestWorkflowManifestBindings(unittest.TestCase):
    def test_validated_inputs_apply_to_comfyui_binding(self) -> None:
        workflow = {
            "6": {"inputs": {"text": "old"}},
            "31": {"inputs": {"denoise": 0.75}},
            "44": {"inputs": {"strength": 0.5}},
        }
        original = copy.deepcopy(workflow)

        result = validate_inputs(
            CONTROLNET_MANIFEST,
            {
                "prompt": "underwater rock arch",
                "denoise": 0.35,
                "controlnet_strength": 1.25,
            },
        )
        self.assertTrue(result.ok)

        mutated = apply_backend_binding(
            CONTROLNET_MANIFEST,
            "comfyui",
            workflow,
            result.accepted,
        )

        self.assertEqual(workflow, original)
        self.assertEqual(mutated["6"]["inputs"]["text"], "underwater rock arch")
        self.assertEqual(mutated["31"]["inputs"]["denoise"], 0.35)
        self.assertEqual(mutated["44"]["inputs"]["strength"], 1.25)

    def test_unknown_and_out_of_range_inputs_are_rejected(self) -> None:
        result = validate_inputs(
            CONTROLNET_MANIFEST,
            {
                "prompt": "underwater rock arch",
                "denoise": 1.7,
                "passthrough_denoise": 0.2,
            },
        )
        receipt = RouteReceipt(protocol="openai.images", intent="image.edit")
        record_validation_result(receipt, result)

        self.assertEqual(receipt.accepted_inputs, ["prompt"])
        self.assertEqual(
            receipt.rejected_inputs,
            {
                "denoise": "denoise must be <= 1",
                "passthrough_denoise": "unknown input",
            },
        )

    def test_sample_flux_manifest_binds_to_committed_workflow_shape(self) -> None:
        manifest = load_workflow_manifest(
            ROOT / "manifests/workflows/flux2-klein-4b-edit.yml"
        )
        identity = manifest_identity(manifest)
        self.assertEqual(identity.id, "image.edit.flux2-klein-4b")
        self.assertEqual(identity.version, "0.1.0")
        self.assertTrue(identity.trusted)
        self.assertEqual(len(identity.manifest_hash), 64)
        workflow = json.loads(
            (ROOT / "workflows/images/flux2-klein-4b-edit.json").read_text(encoding="utf-8")
        )

        result = validate_inputs(
            manifest,
            {
                "prompt": "matte watercolor rock arch",
                "source_image": "api-input.png",
                "width": 896,
                "height": 768,
                "steps": 8,
                "guidance": 3.5,
            },
        )
        self.assertTrue(result.ok)

        mutated = apply_backend_binding(manifest, "comfyui", workflow, result.accepted)
        self.assertEqual(mutated["6"]["inputs"]["text"], "matte watercolor rock arch")
        self.assertEqual(mutated["10"]["inputs"]["image"], "api-input.png")
        self.assertEqual(mutated["48"]["inputs"]["width"], 896)
        self.assertEqual(mutated["47"]["inputs"]["width"], 896)
        self.assertEqual(mutated["48"]["inputs"]["height"], 768)
        self.assertEqual(mutated["47"]["inputs"]["height"], 768)
        self.assertEqual(mutated["48"]["inputs"]["steps"], 8)
        self.assertEqual(mutated["26"]["inputs"]["guidance"], 3.5)

    def test_missing_binding_target_fails_fast(self) -> None:
        result = validate_inputs(
            CONTROLNET_MANIFEST,
            {"prompt": "x", "denoise": 0.5},
        )
        with self.assertRaises(ManifestError):
            apply_backend_binding(
                CONTROLNET_MANIFEST,
                "comfyui",
                {"6": {"inputs": {}}},
                result.accepted,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
