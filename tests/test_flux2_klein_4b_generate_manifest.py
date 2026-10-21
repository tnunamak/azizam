"""Tests for the Flux2 Klein 4B image-generation manifest binding.

These tests prove the workflow-extension contract generalizes beyond the
edit workflow:

- Standard OpenAI image-generation fields stay at the top level.
- Namespaced extension inputs bind to the expected ComfyUI nodes.
- Unsupported/unknown inputs are rejected or ignored with receipt-safe
  reasons.
- Raw prompt values never reach receipts.
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.image_extensions import (  # noqa: E402
    STANDARD_IMAGE_GENERATE_FIELDS,
    WORKFLOW_EXTENSION_NAMESPACE,
    bind_workflow_extensions,
    detect_unknown_top_level_fields,
    record_image_extension_binding,
    resolve_workflow_extensions,
)
from gateway.manifests import (  # noqa: E402
    apply_backend_binding,
    load_workflow_manifest,
    manifest_identity,
    validate_inputs,
)
from gateway.receipts import RouteReceipt  # noqa: E402


NS = WORKFLOW_EXTENSION_NAMESPACE
MANIFEST_PATH = ROOT / "manifests/workflows/flux2-klein-4b.yml"
WORKFLOW_PATH = ROOT / "workflows/images/flux2-klein-4b.json"

# Inputs the route exposes via the extensions envelope. ``prompt`` is a
# top-level OpenAI field so the route binds it directly rather than through
# the extensions namespace.
EXTENSION_INPUTS = ("width", "height", "steps", "guidance", "seed", "batch_size")


def _load_workflow() -> dict[str, object]:
    return json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))


class TestManifestShape(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_identity(self) -> None:
        identity = manifest_identity(self.manifest)
        self.assertEqual(identity.id, "image.generate.flux2-klein-4b")
        self.assertEqual(identity.version, "0.1.0")
        self.assertTrue(identity.trusted)
        self.assertEqual(len(identity.manifest_hash), 64)

    def test_intent_is_generation(self) -> None:
        self.assertEqual(self.manifest["intent"], "image.generate")

    def test_inputs_cover_stable_workflow_controls(self) -> None:
        inputs = set(self.manifest["inputs"])
        # The flux2-klein-4b workflow advertises stable nodes for prompt,
        # size, seed, batch_size, and guidance. Steps is a KSampler input.
        self.assertEqual(
            inputs,
            {"prompt", "width", "height", "steps", "guidance", "seed", "batch_size"},
        )

    def test_binding_targets_are_well_formed(self) -> None:
        mapping = self.manifest["bindings"]["comfyui"]["map"]
        # Every input declared in the manifest has a mapping entry.
        self.assertEqual(set(mapping.keys()), set(self.manifest["inputs"].keys()))
        for entries in mapping.values():
            self.assertIsInstance(entries, list)
            self.assertGreater(len(entries), 0)
            for entry in entries:
                self.assertIn("node", entry)
                self.assertIn("input", entry)


class TestManifestBindsToCommittedWorkflow(unittest.TestCase):
    """Validated inputs land at the documented ComfyUI nodes."""

    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_all_inputs_apply_to_correct_nodes(self) -> None:
        workflow = _load_workflow()
        original = copy.deepcopy(workflow)

        result = validate_inputs(
            self.manifest,
            {
                "prompt": "a luminous geode under desert moonlight",
                "width": 896,
                "height": 1152,
                "steps": 6,
                "guidance": 4.0,
                "seed": 12345,
                "batch_size": 2,
            },
        )
        self.assertTrue(result.ok, result.rejected)

        mutated = apply_backend_binding(self.manifest, "comfyui", workflow, result.accepted)

        # Source is untouched.
        self.assertEqual(workflow, original)
        # Prompt lands on the positive CLIPTextEncode (node 6) and NOT on the
        # empty negative encode (node 33).
        self.assertEqual(
            mutated["6"]["inputs"]["text"], "a luminous geode under desert moonlight"
        )
        self.assertEqual(mutated["33"]["inputs"]["text"], "")
        # Latent dimensions land on the EmptySD3LatentImage (node 27).
        self.assertEqual(mutated["27"]["inputs"]["width"], 896)
        self.assertEqual(mutated["27"]["inputs"]["height"], 1152)
        self.assertEqual(mutated["27"]["inputs"]["batch_size"], 2)
        # KSampler (node 31) carries steps + seed.
        self.assertEqual(mutated["31"]["inputs"]["steps"], 6)
        self.assertEqual(mutated["31"]["inputs"]["seed"], 12345)
        # FluxGuidance (node 35) carries guidance.
        self.assertEqual(mutated["35"]["inputs"]["guidance"], 4.0)

        # Test-owned destinations: do not build the oracle from the manifest map.
        expected = copy.deepcopy(original)
        expected["6"]["inputs"]["text"] = "a luminous geode under desert moonlight"
        expected["27"]["inputs"].update(width=896, height=1152, batch_size=2)
        expected["31"]["inputs"].update(steps=6, seed=12345)
        expected["35"]["inputs"]["guidance"] = 4.0
        self.assertEqual(mutated, expected, "flux2-klein-4b: graph changed outside binding destinations")

    def test_prompt_is_required(self) -> None:
        result = validate_inputs(self.manifest, {"width": 512, "height": 512})
        self.assertFalse(result.ok)
        self.assertEqual(result.rejected.get("prompt"), "required input missing")

    def test_out_of_range_inputs_are_rejected(self) -> None:
        result = validate_inputs(
            self.manifest,
            {
                "prompt": "x",
                "width": 4096,
                "guidance": 99.0,
                "batch_size": 0,
            },
        )
        self.assertIn("width", result.rejected)
        self.assertIn("guidance", result.rejected)
        self.assertIn("batch_size", result.rejected)


class TestExtensionContractGeneralizesToGeneration(unittest.TestCase):
    """The same extension envelope used for edit works for generation."""

    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(MANIFEST_PATH)

    def test_standard_openai_generate_fields_stay_top_level(self) -> None:
        # ``prompt``, ``model``, ``size``, ``n`` are OpenAI-native and must
        # not be flagged as unknown at the top level. Gateway-native controls
        # live under extensions.
        body = {
            "prompt": "x",
            "model": "image.generate.flux2-klein-4b",
            "n": 1,
            "size": "1024x1024",
            "response_format": "b64_json",
            "extensions": {NS: {"steps": 6}},
        }
        unknown = detect_unknown_top_level_fields(body, STANDARD_IMAGE_GENERATE_FIELDS)
        self.assertEqual(unknown, {})

    def test_top_level_gateway_control_is_rejected(self) -> None:
        # A caller mistake: ``steps`` belongs under extensions, not at the
        # top level. The binding must not silently drop it.
        body = {"prompt": "x", "steps": 8, "extensions": {NS: {}}}
        result = bind_workflow_extensions(
            body,
            self.manifest,
            "comfyui",
            _load_workflow(),
            supported_names=EXTENSION_INPUTS,
            standard_fields=STANDARD_IMAGE_GENERATE_FIELDS,
        )
        self.assertIn("steps", result.unknown_top_level)
        self.assertIn("extensions", result.unknown_top_level["steps"])
        self.assertFalse(result.ok)

    def test_namespaced_inputs_bind_to_expected_nodes(self) -> None:
        workflow = _load_workflow()
        original = copy.deepcopy(workflow)
        body = {
            "prompt": "neon koi school in shallow river",
            "model": "image.generate.flux2-klein-4b",
            "extensions": {
                NS: {
                    "width": 768,
                    "height": 1024,
                    "steps": 4,
                    "guidance": 3.5,
                    "seed": 99,
                    "batch_size": 1,
                }
            },
        }
        result = bind_workflow_extensions(
            body,
            self.manifest,
            "comfyui",
            workflow,
            supported_names=EXTENSION_INPUTS,
            standard_fields=STANDARD_IMAGE_GENERATE_FIELDS,
        )
        self.assertTrue(result.ok, (result.unknown_top_level, result.validation.rejected))
        assert result.request is not None
        # Width/height/batch_size landed on EmptySD3LatentImage (node 27).
        self.assertEqual(result.request["27"]["inputs"]["width"], 768)
        self.assertEqual(result.request["27"]["inputs"]["height"], 1024)
        self.assertEqual(result.request["27"]["inputs"]["batch_size"], 1)
        # Seed + steps landed on KSampler (node 31).
        self.assertEqual(result.request["31"]["inputs"]["seed"], 99)
        self.assertEqual(result.request["31"]["inputs"]["steps"], 4)
        # Guidance landed on FluxGuidance (node 35).
        self.assertEqual(result.request["35"]["inputs"]["guidance"], 3.5)

        self.assertEqual(workflow, original, "flux2-klein-4b extension binding: source mutated")
        expected = copy.deepcopy(original)
        expected["27"]["inputs"].update(width=768, height=1024, batch_size=1)
        expected["31"]["inputs"].update(seed=99, steps=4)
        expected["35"]["inputs"]["guidance"] = 3.5
        self.assertEqual(result.request, expected, "flux2-klein-4b extension binding: untouched graph differs")

    def test_unknown_extension_input_is_rejected_with_reason(self) -> None:
        # ``denoise`` is meaningful for img2img but not exposed by the
        # generation manifest. The contract must reject, not silently no-op.
        body = {
            "prompt": "x",
            "extensions": {NS: {"denoise": 0.4, "controlnet_strength": 1.1}},
        }
        result = resolve_workflow_extensions(
            body,
            self.manifest,
            supported_names=EXTENSION_INPUTS,
        )
        self.assertFalse(result.ok)
        self.assertEqual(
            set(result.rejected),
            {"denoise", "controlnet_strength"},
        )
        for reason in result.rejected.values():
            self.assertEqual(reason, "unknown input")

    def test_known_input_not_yet_wired_is_ignored_with_reason(self) -> None:
        # Narrow ``supported_names`` to a subset to simulate a binding that
        # has not wired up every manifest input yet.
        supported = ("steps", "guidance")
        body = {
            "prompt": "x",
            "extensions": {NS: {"steps": 4, "seed": 7, "batch_size": 2}},
        }
        result = resolve_workflow_extensions(
            body,
            self.manifest,
            supported_names=supported,
        )
        self.assertEqual(result.accepted, {"steps": 4})
        self.assertEqual(
            result.ignored,
            {
                "seed": "input not supported by current binding",
                "batch_size": "input not supported by current binding",
            },
        )


class TestReceiptIsRawValueSafe(unittest.TestCase):
    """Receipts hold names and reasons only — never raw values."""

    def test_receipt_does_not_record_raw_prompt_or_values(self) -> None:
        manifest = load_workflow_manifest(MANIFEST_PATH)
        secret_prompt = "do-not-log-this-prompt-blob"
        secret_seed = 4242424242
        secret_guidance = 7.77
        body = {
            "prompt": secret_prompt,  # standard field, never seen by binding
            "steps": 6,  # caller mistake: top-level gateway control
            "extensions": {
                NS: {
                    "guidance": secret_guidance,
                    "seed": secret_seed,
                    "denoise": 0.3,  # unknown extension input
                }
            },
        }
        result = bind_workflow_extensions(
            body,
            manifest,
            "comfyui",
            _load_workflow(),
            supported_names=EXTENSION_INPUTS,
            standard_fields=STANDARD_IMAGE_GENERATE_FIELDS,
        )
        receipt = RouteReceipt(protocol="openai.images", intent="image.generate")
        record_image_extension_binding(receipt, result)

        # Receipt surfaces names + reasons only.
        self.assertIn(f"{NS}.guidance", receipt.accepted_inputs)
        self.assertIn(f"{NS}.seed", receipt.accepted_inputs)
        self.assertEqual(
            receipt.rejected_inputs.get(f"{NS}.denoise"), "unknown input"
        )
        # Top-level mistake is flagged without namespace prefix.
        self.assertIn("steps", receipt.rejected_inputs)

        rendered = json.dumps(receipt.to_dict(), sort_keys=True, default=str)
        # No raw prompt or numeric values from the request body should leak.
        self.assertNotIn(secret_prompt, rendered)
        self.assertNotIn(str(secret_seed), rendered)
        self.assertNotIn(str(secret_guidance), rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
