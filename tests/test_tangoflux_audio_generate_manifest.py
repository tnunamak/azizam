"""Tests for the TangoFlux audio.generate manifest binding.

Pure tests — no subprocess, no network, no ComfyUI. They prove the
audio.generate workflow-extension contract mirrors the image.generate
contract:

- The manifest validates schema_version/id/intent/inputs/outputs/bindings.
- Validated inputs land on the documented ComfyUI nodes in a deep copy
  of the committed workflow JSON (source workflow is not mutated).
- Required inputs are enforced; out-of-range values are rejected.
- Unknown ComfyUI inputs would raise ManifestError (binding-shape guard).
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.manifests import (  # noqa: E402
    ManifestError,
    apply_backend_binding,
    load_workflow_manifest,
    manifest_identity,
    validate_inputs,
)


MANIFEST_PATH = ROOT / "manifests/workflows/tangoflux.yml"
WORKFLOW_PATH = ROOT / "workflows/audio/tangoflux.json"


def _load_workflow() -> dict[str, Any]:
    return json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))


class TestManifestShape(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_identity(self) -> None:
        identity = manifest_identity(self.manifest)
        self.assertEqual(identity.id, "audio.generate.tangoflux")
        self.assertEqual(identity.version, "0.1.0")
        self.assertTrue(identity.trusted)
        self.assertEqual(len(identity.manifest_hash), 64)

    def test_intent_is_audio_generate(self) -> None:
        self.assertEqual(self.manifest["intent"], "audio.generate")

    def test_inputs_cover_workflow_controls(self) -> None:
        self.assertEqual(
            set(self.manifest["inputs"]),
            {"prompt", "duration", "steps", "guidance", "seed", "batch_size"},
        )

    def test_output_is_audio_wav(self) -> None:
        self.assertEqual(self.manifest["outputs"]["audio"]["media_type"], "audio/wav")

    def test_binding_targets_are_well_formed(self) -> None:
        mapping = self.manifest["bindings"]["comfyui"]["map"]
        self.assertEqual(set(mapping.keys()), set(self.manifest["inputs"].keys()))
        for entries in mapping.values():
            self.assertIsInstance(entries, list)
            self.assertGreater(len(entries), 0)
            for entry in entries:
                self.assertIn("node", entry)
                self.assertIn("input", entry)


class TestManifestBindsToCommittedWorkflow(unittest.TestCase):
    """Validated inputs land at the documented TangoFluxSampler node."""

    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_all_inputs_apply_to_sampler_node(self) -> None:
        workflow = _load_workflow()
        original = copy.deepcopy(workflow)

        result = validate_inputs(
            self.manifest,
            {
                "prompt": "a soft rainfall on a tin roof",
                "duration": 8.0,
                "steps": 40,
                "guidance": 3.5,
                "seed": 4242,
                "batch_size": 1,
            },
        )
        self.assertTrue(result.ok, result.rejected)

        mutated = apply_backend_binding(
            self.manifest, "comfyui", workflow, result.accepted
        )

        # Source workflow not mutated.
        self.assertEqual(workflow, original)
        # All inputs land on the TangoFluxSampler (node 2).
        node = mutated["2"]["inputs"]
        self.assertEqual(node["prompt"], "a soft rainfall on a tin roof")
        self.assertEqual(node["duration"], 8.0)
        self.assertEqual(node["steps"], 40)
        self.assertEqual(node["guidance_scale"], 3.5)
        self.assertEqual(node["seed"], 4242)
        self.assertEqual(node["batch_size"], 1)

    def test_prompt_is_required(self) -> None:
        result = validate_inputs(self.manifest, {"duration": 5.0})
        self.assertFalse(result.ok)
        self.assertEqual(result.rejected.get("prompt"), "required input missing")

    def test_out_of_range_inputs_are_rejected(self) -> None:
        result = validate_inputs(
            self.manifest,
            {
                "prompt": "x",
                "duration": 999,
                "steps": 0,
                "guidance": 99.0,
                "batch_size": 0,
            },
        )
        self.assertIn("duration", result.rejected)
        self.assertIn("steps", result.rejected)
        self.assertIn("guidance", result.rejected)
        self.assertIn("batch_size", result.rejected)

    def test_wrong_type_is_rejected(self) -> None:
        result = validate_inputs(
            self.manifest,
            {"prompt": "x", "duration": "ten", "seed": True},
        )
        self.assertIn("duration", result.rejected)
        # Booleans must not satisfy integer typing.
        self.assertIn("seed", result.rejected)

    def test_unknown_input_is_rejected(self) -> None:
        result = validate_inputs(
            self.manifest,
            {"prompt": "x", "denoise": 0.4, "controlnet_strength": 1.0},
        )
        self.assertIn("denoise", result.rejected)
        self.assertIn("controlnet_strength", result.rejected)
        for reason in (
            result.rejected["denoise"],
            result.rejected["controlnet_strength"],
        ):
            self.assertEqual(reason, "unknown input")


class TestBindingShapeGuards(unittest.TestCase):
    """Manifest binding shape must fail fast for missing nodes/inputs."""

    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_missing_target_node_fails_fast(self) -> None:
        workflow = _load_workflow()
        del workflow["2"]
        result = validate_inputs(self.manifest, {"prompt": "x"})
        with self.assertRaises(ManifestError):
            apply_backend_binding(self.manifest, "comfyui", workflow, result.accepted)


if __name__ == "__main__":
    unittest.main()
