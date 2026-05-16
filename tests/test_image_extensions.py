"""Tests for the image-route extension/manifest binding glue.

These tests exercise the pure binding layer in isolation — no FastAPI, no
ComfyUI, no IO beyond reading the committed Flux2 manifest.
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.extensions import ExtensionError  # noqa: E402
from gateway.image_extensions import (  # noqa: E402
    STANDARD_IMAGE_EDIT_FIELDS,
    STANDARD_IMAGE_GENERATE_FIELDS,
    WORKFLOW_EXTENSION_NAMESPACE,
    bind_workflow_extensions,
    detect_unknown_top_level_fields,
    record_image_extension_binding,
    resolve_workflow_extensions,
)
from gateway.manifests import load_workflow_manifest  # noqa: E402
from gateway.receipts import RouteReceipt  # noqa: E402


NS = WORKFLOW_EXTENSION_NAMESPACE


# Synthetic image-edit manifest used by most tests. The committed Flux2 edit
# workflow exposes denoise, but not ControlNet, so tests that need ControlNet
# use this manifest rather than pretending the real workflow has those nodes.
CONTROLNET_EDIT_MANIFEST = {
    "schema_version": 1,
    "id": "image.edit.controlnet.fixture",
    "intent": "image.edit",
    "inputs": {
        "prompt": {"type": "string", "required": True},
        "denoise": {"type": "number", "minimum": 0, "maximum": 1},
        "control_image": {"type": "asset"},
        "controlnet_strength": {"type": "number", "minimum": 0, "maximum": 2},
        "sampler": {"type": "string", "enum": ["euler", "dpmpp_2m"]},
    },
    "outputs": {"image": {"media_type": "image/png"}},
    "bindings": {
        "comfyui": {
            "workflow": "workflows/images/fixture-controlnet.json",
            "map": {
                "prompt": [{"node": "6", "input": "text"}],
                "denoise": [{"node": "31", "input": "denoise"}],
                "control_image": [{"node": "42", "input": "image"}],
                "controlnet_strength": [{"node": "44", "input": "strength"}],
            },
        },
        "llamacpp": {
            "endpoint": "/v1/edit",
            "map": {
                "prompt": [{"path": "input.prompt"}],
                "denoise": [{"path": "sampling.denoise"}],
                "control_image": [{"path": "controlnet.image"}],
                "controlnet_strength": [{"path": "controlnet.strength"}],
            },
        },
    },
}


def _fake_comfy_workflow() -> dict[str, object]:
    return {
        "6": {"inputs": {"text": "old"}},
        "31": {"inputs": {"denoise": 0.0}},
        "42": {"inputs": {"image": ""}},
        "44": {"inputs": {"strength": 0.0}},
    }


# The image route supplies ``prompt`` at the top level as a standard OpenAI
# field, so the extensions binding does *not* claim ``prompt`` as a supported
# extension. ``denoise``, ``control_image``, and ``controlnet_strength`` are
# the actually-wired extension inputs. ``control_image`` proves asset-shaped
# conditioning controls can be bound without becoming OpenAI top-level fields.
# ``sampler`` is spec'd in the manifest but intentionally left unsupported to
# exercise the "ignored with reason" path.
SUPPORTED = ("denoise", "control_image", "controlnet_strength")

CONTROLNET_LIKE_EXTENSION_CONTROLS = (
    "control_image",
    "controlnet_strength",
    "controlnet_start",
    "controlnet_end",
    "controlnet_model",
    "controlnet_preprocessor",
)


class TestStandardFields(unittest.TestCase):
    def test_generate_and_edit_field_sets_are_disjoint_in_intent(self) -> None:
        # Edits accept image/mask, generations do not.
        self.assertIn("image", STANDARD_IMAGE_EDIT_FIELDS)
        self.assertIn("mask", STANDARD_IMAGE_EDIT_FIELDS)
        self.assertNotIn("image", STANDARD_IMAGE_GENERATE_FIELDS)
        self.assertNotIn("mask", STANDARD_IMAGE_GENERATE_FIELDS)

    def test_extensions_envelope_is_standard(self) -> None:
        self.assertIn("extensions", STANDARD_IMAGE_GENERATE_FIELDS)
        self.assertIn("extensions", STANDARD_IMAGE_EDIT_FIELDS)

    def test_arbitrary_top_level_is_not_standard(self) -> None:
        for shouldnt_be_in in (
            "denoise",
            "control_image",
            "controlnet_strength",
            "sampler",
            "seed",
        ):
            self.assertNotIn(shouldnt_be_in, STANDARD_IMAGE_GENERATE_FIELDS)
            self.assertNotIn(shouldnt_be_in, STANDARD_IMAGE_EDIT_FIELDS)


class TestDetectUnknownTopLevelFields(unittest.TestCase):
    def test_known_fields_are_accepted(self) -> None:
        body = {"prompt": "x", "model": "m", "extensions": {}}
        self.assertEqual(
            detect_unknown_top_level_fields(body, STANDARD_IMAGE_GENERATE_FIELDS),
            {},
        )

    def test_top_level_extension_inputs_are_flagged(self) -> None:
        body = {
            "prompt": "x",
            "denoise": 0.4,
            "control_image": "control.png",
            "controlnet_strength": 1.1,
        }
        unknown = detect_unknown_top_level_fields(
            body, STANDARD_IMAGE_GENERATE_FIELDS
        )
        self.assertEqual(
            set(unknown.keys()),
            {"denoise", "control_image", "controlnet_strength"},
        )
        for reason in unknown.values():
            self.assertIn("extensions", reason)
            self.assertIn(WORKFLOW_EXTENSION_NAMESPACE, reason)


class TestResolveWorkflowExtensions(unittest.TestCase):
    def test_no_extensions_block_returns_empty_result(self) -> None:
        # The route supplies required inputs (e.g. prompt) at the top level,
        # not under extensions. The binding layer mirrors that by narrowing
        # supported_names to inputs that *do* belong in extensions.
        result = resolve_workflow_extensions(
            {"prompt": "x"},
            CONTROLNET_EDIT_MANIFEST,
            supported_names=("denoise", "controlnet_strength"),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.accepted, {})
        self.assertEqual(result.rejected, {})
        self.assertEqual(result.ignored, {})

    def test_unknown_extension_is_rejected_not_silently_dropped(self) -> None:
        # ``passthrough_denoise`` is not in the manifest at all, so it must
        # not silently drop — the binding layer rejects it with a reason.
        body = {
            "prompt": "x",
            "extensions": {
                NS: {"denoise": 0.4, "passthrough_denoise": 0.9},
            },
        }
        result = resolve_workflow_extensions(
            body, CONTROLNET_EDIT_MANIFEST, supported_names=SUPPORTED
        )
        self.assertFalse(result.ok)
        self.assertIn("passthrough_denoise", result.rejected)
        self.assertEqual(result.rejected["passthrough_denoise"], "unknown input")
        self.assertEqual(result.accepted, {"denoise": 0.4})

    def test_known_but_unsupported_input_is_ignored_with_reason(self) -> None:
        body = {
            "prompt": "x",
            "extensions": {NS: {"denoise": 0.4, "sampler": "euler"}},
        }
        result = resolve_workflow_extensions(
            body, CONTROLNET_EDIT_MANIFEST, supported_names=SUPPORTED
        )
        self.assertEqual(result.accepted, {"denoise": 0.4})
        self.assertEqual(
            result.ignored,
            {"sampler": "input not supported by current binding"},
        )

    def test_out_of_range_value_is_rejected(self) -> None:
        body = {"prompt": "x", "extensions": {NS: {"denoise": 1.7}}}
        result = resolve_workflow_extensions(
            body, CONTROLNET_EDIT_MANIFEST, supported_names=SUPPORTED
        )
        self.assertIn("denoise", result.rejected)

    def test_manifest_without_inputs_mapping_fails_fast(self) -> None:
        with self.assertRaises(ExtensionError):
            resolve_workflow_extensions(
                {}, {"inputs": ["not", "a", "mapping"]}, supported_names=()
            )


class TestBindWorkflowExtensions(unittest.TestCase):
    def test_valid_inputs_mutate_a_fake_backend_request(self) -> None:
        body = {
            "prompt": "matte watercolor rock arch",
            "model": "controlnet-fixture",
            "extensions": {
                NS: {
                    "denoise": 0.35,
                    "control_image": "control.png",
                    "controlnet_strength": 1.25,
                },
            },
        }
        source = _fake_comfy_workflow()
        original = copy.deepcopy(source)

        result = bind_workflow_extensions(
            body,
            CONTROLNET_EDIT_MANIFEST,
            "comfyui",
            source,
            supported_names=SUPPORTED,
            standard_fields=STANDARD_IMAGE_EDIT_FIELDS,
        )

        # The source backend request is never mutated.
        self.assertEqual(source, original)
        # The returned request carries the applied inputs.
        request = result.request
        assert request is not None
        self.assertEqual(request["31"]["inputs"]["denoise"], 0.35)
        self.assertEqual(request["42"]["inputs"]["image"], "control.png")
        self.assertEqual(request["44"]["inputs"]["strength"], 1.25)
        self.assertTrue(result.ok)
        self.assertEqual(result.validation.accepted["denoise"], 0.35)
        # Binding provenance lists the applied portable input names.
        assert result.binding is not None
        self.assertEqual(
            sorted(result.binding.applied_inputs),
            ["control_image", "controlnet_strength", "denoise"],
        )

    def test_dotted_path_backend_binding_is_portable(self) -> None:
        body = {
            "prompt": "x",
            "extensions": {
                NS: {
                    "denoise": 0.4,
                    "control_image": {"asset_id": "asset_123"},
                    "controlnet_strength": 0.9,
                }
            },
        }
        backend_request = {"input": {"prompt": "ignored"}, "sampling": {}}
        result = bind_workflow_extensions(
            body,
            CONTROLNET_EDIT_MANIFEST,
            "llamacpp",
            backend_request,
            supported_names=SUPPORTED,
            standard_fields=STANDARD_IMAGE_EDIT_FIELDS,
        )
        assert result.request is not None
        self.assertEqual(result.request["sampling"]["denoise"], 0.4)
        self.assertEqual(
            result.request["controlnet"]["image"],
            {"asset_id": "asset_123"},
        )
        self.assertEqual(result.request["controlnet"]["strength"], 0.9)

    def test_top_level_extension_input_is_flagged_not_applied(self) -> None:
        body = {
            "prompt": "x",
            "denoise": 0.5,  # caller mistake: belongs in extensions
            "extensions": {NS: {}},
        }
        result = bind_workflow_extensions(
            body,
            CONTROLNET_EDIT_MANIFEST,
            "comfyui",
            _fake_comfy_workflow(),
            supported_names=SUPPORTED,
            standard_fields=STANDARD_IMAGE_EDIT_FIELDS,
        )
        self.assertIn("denoise", result.unknown_top_level)
        self.assertFalse(result.ok)
        # The backend request was not mutated for that field.
        assert result.request is not None
        self.assertEqual(result.request["31"]["inputs"]["denoise"], 0.0)

    def test_accepted_input_without_backend_mapping_is_ignored(self) -> None:
        # Add a manifest input that has no binding map entry.
        manifest = copy.deepcopy(CONTROLNET_EDIT_MANIFEST)
        manifest["inputs"]["seed"] = {"type": "integer", "minimum": 0}
        body = {
            "prompt": "x",
            "extensions": {NS: {"seed": 42, "denoise": 0.4}},
        }
        supported = (*SUPPORTED, "seed")
        result = bind_workflow_extensions(
            body,
            manifest,
            "comfyui",
            _fake_comfy_workflow(),
            supported_names=supported,
            standard_fields=STANDARD_IMAGE_EDIT_FIELDS,
        )
        assert result.binding is not None
        self.assertIn("seed", result.binding.ignored_inputs)
        self.assertIn("no mapping", result.binding.ignored_inputs["seed"])
        # denoise still applied
        assert result.request is not None
        self.assertEqual(result.request["31"]["inputs"]["denoise"], 0.4)


class TestRecordImageExtensionBinding(unittest.TestCase):
    def test_receipt_contains_only_names_and_reasons(self) -> None:
        secret_prompt = "secret prompt do not log"
        body = {
            "prompt": secret_prompt,  # standard field — never reaches the receipt
            "controlnet_strength": 0.9,  # unknown top-level (caller mistake)
            "extensions": {
                NS: {
                    "denoise": 0.35,
                    "controlnet_strength": 1.25,
                    "sampler": "euler",       # ignored: not supported
                    "passthrough_x": 0.1,     # rejected: unknown
                }
            },
        }
        result = bind_workflow_extensions(
            body,
            CONTROLNET_EDIT_MANIFEST,
            "comfyui",
            _fake_comfy_workflow(),
            supported_names=SUPPORTED,
            standard_fields=STANDARD_IMAGE_EDIT_FIELDS,
        )
        receipt = RouteReceipt(protocol="openai.images", intent="image.edit")
        record_image_extension_binding(receipt, result)

        # Accepted namespaced names appear on the receipt.
        self.assertIn(f"{NS}.denoise", receipt.accepted_inputs)
        self.assertIn(f"{NS}.controlnet_strength", receipt.accepted_inputs)
        # Sampler is ignored with a reason.
        self.assertEqual(
            receipt.ignored_inputs.get(f"{NS}.sampler"),
            "input not supported by current binding",
        )
        # passthrough_x is rejected with a reason.
        self.assertEqual(
            receipt.rejected_inputs.get(f"{NS}.passthrough_x"),
            "unknown input",
        )
        # Top-level controlnet_strength is rejected (no namespace prefix; it
        # was at top level of the body).
        self.assertIn("controlnet_strength", receipt.rejected_inputs)
        self.assertIn(
            "extensions", receipt.rejected_inputs["controlnet_strength"]
        )
        # And the receipt does not carry raw values anywhere.
        rendered = str(receipt.to_dict())
        self.assertNotIn(secret_prompt, rendered)
        self.assertNotIn("0.35", rendered)
        self.assertNotIn("1.25", rendered)

    def test_accepted_but_unmapped_inputs_appear_as_ignored_on_receipt(self) -> None:
        manifest = copy.deepcopy(CONTROLNET_EDIT_MANIFEST)
        manifest["inputs"]["seed"] = {"type": "integer", "minimum": 0}
        body = {"prompt": "x", "extensions": {NS: {"seed": 7}}}
        supported = (*SUPPORTED, "seed")
        result = bind_workflow_extensions(
            body,
            manifest,
            "comfyui",
            _fake_comfy_workflow(),
            supported_names=supported,
            standard_fields=STANDARD_IMAGE_EDIT_FIELDS,
        )
        receipt = RouteReceipt(protocol="openai.images", intent="image.edit")
        record_image_extension_binding(receipt, result)
        # Validation accepted seed, but binding had no map for it. The receipt
        # surfaces that as ignored (under the namespace) with a reason.
        self.assertIn(
            f"{NS}.seed",
            receipt.ignored_inputs,
            receipt.ignored_inputs,
        )

    def test_non_receipt_object_fails_fast(self) -> None:
        result = bind_workflow_extensions(
            {"prompt": "x", "extensions": {NS: {}}},
            CONTROLNET_EDIT_MANIFEST,
            "comfyui",
            _fake_comfy_workflow(),
            supported_names=SUPPORTED,
            standard_fields=STANDARD_IMAGE_EDIT_FIELDS,
        )
        with self.assertRaises(ExtensionError):
            record_image_extension_binding(object(), result)


class TestRealFluxManifestKeepsContractHonest(unittest.TestCase):
    """The committed Flux2 edit manifest must expose only real controls.

    The current Flux2 Klein 4B edit workflow exposes denoise through a
    SplitSigmasDenoise node. It still does not expose ControlNet, so
    ControlNet controls must be rejected instead of silently no-oping.
    """

    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(
            ROOT / "manifests/workflows/flux2-klein-4b-edit.yml"
        )

    def test_controlnet_extension_is_rejected_against_real_manifest(self) -> None:
        body = {
            "extensions": {
                NS: {
                    "denoise": 0.35,
                    **{name: "x" for name in CONTROLNET_LIKE_EXTENSION_CONTROLS},
                }
            }
        }
        # Narrow supported_names so the required-missing check (which is
        # satisfied at the route by top-level fields) does not muddy the
        # rejection set we're asserting on.
        result = resolve_workflow_extensions(
            body,
            self.manifest,
            supported_names=("guidance", "steps", "width", "height", "denoise"),
        )
        self.assertEqual(result.accepted, {"denoise": 0.35})
        self.assertEqual(
            result.rejected,
            {name: "unknown input" for name in CONTROLNET_LIKE_EXTENSION_CONTROLS},
        )

    def test_real_manifest_inputs_still_bind(self) -> None:
        # Spec'd inputs (e.g. guidance and denoise) under the extensions envelope should
        # validate and apply through the real ComfyUI binding. The host route
        # passes the top-level standard fields (prompt, source_image) on its
        # own, so the binding here narrows ``supported_names`` to only the
        # extension-shaped controls.
        body = {"extensions": {NS: {"guidance": 3.5, "denoise": 0.42}}}
        # Build the smallest possible workflow surface the binding needs.
        backend_request = {
            "26": {"inputs": {"guidance": 4.0}},
            "49": {"inputs": {"denoise": 1.0}},
        }
        result = bind_workflow_extensions(
            body,
            self.manifest,
            "comfyui",
            backend_request,
            supported_names=("guidance", "steps", "width", "height", "denoise"),
            standard_fields=STANDARD_IMAGE_EDIT_FIELDS,
        )
        self.assertTrue(result.validation.ok, result.validation.rejected)
        assert result.request is not None
        self.assertEqual(result.request["26"]["inputs"]["guidance"], 3.5)
        self.assertEqual(result.request["49"]["inputs"]["denoise"], 0.42)


if __name__ == "__main__":
    unittest.main(verbosity=2)
