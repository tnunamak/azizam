"""Tests for workflow dependency validation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.manifests import ManifestError  # noqa: E402
from gateway.workflow_requirements import validate_workflow_requirements  # noqa: E402


BASE_MANIFEST = {
    "schema_version": 1,
    "id": "image.generate.controlnet.example",
    "intent": "image.generate",
    "inputs": {"prompt": {"type": "string", "required": True}},
    "outputs": {"image": {"media_type": "image/png"}},
    "bindings": {"comfyui": {"workflow": "workflows/images/example.json", "map": {}}},
}


class TestWorkflowRequirements(unittest.TestCase):
    def test_missing_custom_node_class_is_reported_with_safe_name(self) -> None:
        manifest = {
            **BASE_MANIFEST,
            "requires": {
                "custom_nodes": [{"class": "ControlNetApplyAdvanced"}],
            },
        }

        result = validate_workflow_requirements(
            manifest,
            {"node_classes": ["KSampler"]},
        )

        self.assertFalse(result.ok)
        self.assertEqual(
            result.as_dict(),
            {
                "ok": False,
                "missing": [
                    {
                        "kind": "custom_node",
                        "name": "ControlNetApplyAdvanced",
                        "reason": "custom node class is not present in backend inventory",
                    }
                ],
            },
        )

    def test_missing_model_artifact_is_reported_without_backend_paths(self) -> None:
        manifest = {
            **BASE_MANIFEST,
            "requires": {
                "models": [
                    {
                        "role": "controlnet",
                        "path": "controlnet/advanced/depth-control.safetensors",
                    }
                ],
            },
        }

        result = validate_workflow_requirements(
            manifest,
            {
                "models": [
                    {
                        "role": "controlnet",
                        "path": "/Users/tim/ComfyUI/models/controlnet/other.safetensors",
                    }
                ]
            },
        )

        self.assertFalse(result.ok)
        self.assertEqual(len(result.missing), 1)
        self.assertEqual(result.missing[0].kind, "model")
        self.assertEqual(result.missing[0].name, "controlnet:depth-control.safetensors")
        self.assertNotIn("/Users/tim", str(result.as_dict()))

    def test_satisfied_requirements_return_ok(self) -> None:
        manifest = {
            **BASE_MANIFEST,
            "requires": {
                "custom_nodes": ["ControlNetApplyAdvanced"],
                "models": [
                    {
                        "role": "checkpoint",
                        "pattern": "flux/*.safetensors",
                        "sha256": "abc123",
                    }
                ],
            },
        }

        result = validate_workflow_requirements(
            manifest,
            {
                "custom_nodes": {"classes": ["KSampler", "ControlNetApplyAdvanced"]},
                "model_artifacts": [
                    {
                        "role": "checkpoint",
                        "path": "/srv/comfy/models/flux/klein.safetensors",
                        "sha256": "abc123",
                    }
                ],
            },
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.as_dict(), {"ok": True, "missing": []})

    def test_malformed_requires_shape_fails_fast(self) -> None:
        manifest = {
            **BASE_MANIFEST,
            "requires": {
                "custom_nodes": "ControlNetApplyAdvanced",
                "models": [{"role": "checkpoint", "path": "a.safetensors"}],
            },
        }

        with self.assertRaisesRegex(
            ManifestError,
            "requires.custom_nodes must be a list",
        ):
            validate_workflow_requirements(manifest, {})


if __name__ == "__main__":
    unittest.main()
