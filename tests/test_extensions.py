"""Tests for the pure typed request-extension contract."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.extensions import (  # noqa: E402
    ExtensionError,
    ExtensionValidationResult,
    extract_extension_namespace,
    record_extension_result,
    validate_extension_inputs,
)
from gateway.receipts import RouteReceipt  # noqa: E402


NAMESPACE = "com.vividfish.workflow"


CONTROLNET_INPUT_SPECS = {
    "prompt": {"type": "string", "required": True},
    "denoise": {"type": "number", "minimum": 0, "maximum": 1},
    "controlnet_strength": {"type": "number", "minimum": 0, "maximum": 2},
    "sampler": {"type": "string", "enum": ["euler", "dpmpp_2m"]},
}

# Inputs the current binding has actually wired up.
CONTROLNET_SUPPORTED = {"prompt", "denoise", "controlnet_strength"}


class TestExtractExtensionNamespace(unittest.TestCase):
    def test_missing_container_returns_empty(self) -> None:
        self.assertEqual(extract_extension_namespace({}, NAMESPACE), {})

    def test_missing_namespace_returns_empty(self) -> None:
        body = {"extensions": {"com.other.thing": {"foo": 1}}}
        self.assertEqual(extract_extension_namespace(body, NAMESPACE), {})

    def test_extracts_namespace_block(self) -> None:
        body = {"extensions": {NAMESPACE: {"denoise": 0.4}}}
        self.assertEqual(
            extract_extension_namespace(body, NAMESPACE),
            {"denoise": 0.4},
        )

    def test_non_mapping_container_fails_fast(self) -> None:
        with self.assertRaises(ExtensionError):
            extract_extension_namespace({"extensions": [1, 2]}, NAMESPACE)

    def test_non_mapping_namespace_fails_fast(self) -> None:
        body = {"extensions": {NAMESPACE: ["denoise"]}}
        with self.assertRaises(ExtensionError):
            extract_extension_namespace(body, NAMESPACE)


class TestValidateExtensionInputs(unittest.TestCase):
    def test_accepts_valid_inputs(self) -> None:
        result = validate_extension_inputs(
            CONTROLNET_INPUT_SPECS,
            {
                "prompt": "underwater rock arch",
                "denoise": 0.35,
                "controlnet_strength": 1.25,
            },
            namespace=NAMESPACE,
            supported_names=CONTROLNET_SUPPORTED,
        )
        self.assertTrue(result.ok)
        self.assertEqual(
            result.accepted,
            {
                "prompt": "underwater rock arch",
                "denoise": 0.35,
                "controlnet_strength": 1.25,
            },
        )
        self.assertEqual(result.rejected, {})
        self.assertEqual(result.ignored, {})
        self.assertEqual(result.namespace, NAMESPACE)

    def test_unknown_input_is_rejected_not_silently_dropped(self) -> None:
        result = validate_extension_inputs(
            CONTROLNET_INPUT_SPECS,
            {"prompt": "x", "passthrough_denoise": 0.2},
            namespace=NAMESPACE,
            supported_names=CONTROLNET_SUPPORTED,
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.rejected, {"passthrough_denoise": "unknown input"})
        self.assertEqual(result.accepted, {"prompt": "x"})

    def test_out_of_range_value_is_rejected(self) -> None:
        result = validate_extension_inputs(
            CONTROLNET_INPUT_SPECS,
            {"prompt": "x", "denoise": 1.7},
            namespace=NAMESPACE,
            supported_names=CONTROLNET_SUPPORTED,
        )
        self.assertEqual(result.rejected, {"denoise": "denoise must be <= 1"})
        self.assertNotIn("denoise", result.accepted)

    def test_wrong_type_is_rejected(self) -> None:
        result = validate_extension_inputs(
            CONTROLNET_INPUT_SPECS,
            {"prompt": "x", "denoise": "halfway"},
            namespace=NAMESPACE,
            supported_names=CONTROLNET_SUPPORTED,
        )
        self.assertIn("denoise", result.rejected)
        self.assertIn("must be a number", result.rejected["denoise"])

    def test_enum_violation_is_rejected(self) -> None:
        result = validate_extension_inputs(
            CONTROLNET_INPUT_SPECS,
            {"prompt": "x", "sampler": "lms"},
            namespace=NAMESPACE,
            supported_names=CONTROLNET_INPUT_SPECS.keys(),
        )
        self.assertIn("sampler", result.rejected)

    def test_unsupported_known_input_is_ignored_with_reason(self) -> None:
        # `sampler` is in specs but not in this binding's supported set.
        result = validate_extension_inputs(
            CONTROLNET_INPUT_SPECS,
            {"prompt": "x", "sampler": "euler", "denoise": 0.4},
            namespace=NAMESPACE,
            supported_names=CONTROLNET_SUPPORTED,
        )
        self.assertEqual(result.accepted, {"prompt": "x", "denoise": 0.4})
        self.assertEqual(
            result.ignored,
            {"sampler": "input not supported by current binding"},
        )
        self.assertEqual(result.rejected, {})

    def test_missing_required_input_is_rejected(self) -> None:
        result = validate_extension_inputs(
            CONTROLNET_INPUT_SPECS,
            {"denoise": 0.4},
            namespace=NAMESPACE,
            supported_names=CONTROLNET_SUPPORTED,
        )
        self.assertEqual(result.rejected, {"prompt": "required input missing"})

    def test_default_supported_is_all_specs(self) -> None:
        # Without supported_names, every spec input is considered supported.
        result = validate_extension_inputs(
            CONTROLNET_INPUT_SPECS,
            {"prompt": "x", "sampler": "euler"},
            namespace=NAMESPACE,
        )
        self.assertEqual(result.accepted, {"prompt": "x", "sampler": "euler"})
        self.assertEqual(result.ignored, {})

    def test_supported_names_unknown_input_fails_fast(self) -> None:
        with self.assertRaises(ExtensionError):
            validate_extension_inputs(
                CONTROLNET_INPUT_SPECS,
                {"prompt": "x"},
                namespace=NAMESPACE,
                supported_names=("prompt", "not_in_specs"),
            )

    def test_bad_specs_shape_fails_fast(self) -> None:
        with self.assertRaises(ExtensionError):
            validate_extension_inputs(
                [{"name": "prompt"}],  # type: ignore[arg-type]
                {},
                namespace=NAMESPACE,
            )

    def test_bad_individual_spec_fails_fast(self) -> None:
        with self.assertRaises(ExtensionError):
            validate_extension_inputs(
                {"prompt": ["not", "a", "mapping"]},  # type: ignore[dict-item]
                {"prompt": "x"},
                namespace=NAMESPACE,
            )


class TestRecordExtensionResult(unittest.TestCase):
    def test_receipt_records_namespaced_names_only(self) -> None:
        receipt = RouteReceipt(protocol="openai.images", intent="image.edit")
        result = ExtensionValidationResult(
            namespace=NAMESPACE,
            accepted={"denoise": 0.35, "prompt": "secret prompt do not log"},
            rejected={"passthrough_denoise": "unknown input"},
            ignored={"sampler": "input not supported by current binding"},
        )

        record_extension_result(receipt, result)

        self.assertEqual(
            sorted(receipt.accepted_inputs),
            sorted([f"{NAMESPACE}.denoise", f"{NAMESPACE}.prompt"]),
        )
        self.assertEqual(
            receipt.rejected_inputs,
            {f"{NAMESPACE}.passthrough_denoise": "unknown input"},
        )
        self.assertEqual(
            receipt.ignored_inputs,
            {f"{NAMESPACE}.sampler": "input not supported by current binding"},
        )
        # Receipt must never carry raw extension values.
        self.assertNotIn("secret prompt do not log", str(receipt.to_dict()))
        self.assertNotIn("0.35", str(receipt.to_dict()))

    def test_empty_namespace_records_bare_names(self) -> None:
        receipt = RouteReceipt(protocol="openai.images", intent="image.edit")
        result = ExtensionValidationResult(
            namespace="",
            accepted={"denoise": 0.4},
        )
        record_extension_result(receipt, result)
        self.assertEqual(receipt.accepted_inputs, ["denoise"])

    def test_non_receipt_object_fails_fast(self) -> None:
        with self.assertRaises(ExtensionError):
            record_extension_result(
                object(),
                ExtensionValidationResult(namespace=NAMESPACE),
            )

    def test_end_to_end_extract_validate_record(self) -> None:
        body = {
            "workflow": "flux2-edit-controlnet",
            "extensions": {
                NAMESPACE: {
                    "prompt": "matte watercolor rock arch",
                    "denoise": 0.35,
                    "sampler": "euler",
                    "passthrough_denoise": 0.9,
                }
            },
        }
        supplied = extract_extension_namespace(body, NAMESPACE)
        result = validate_extension_inputs(
            CONTROLNET_INPUT_SPECS,
            supplied,
            namespace=NAMESPACE,
            supported_names=CONTROLNET_SUPPORTED,
        )
        receipt = RouteReceipt(protocol="openai.images", intent="image.edit")
        record_extension_result(receipt, result)

        self.assertEqual(
            sorted(receipt.accepted_inputs),
            sorted([f"{NAMESPACE}.prompt", f"{NAMESPACE}.denoise"]),
        )
        self.assertEqual(
            receipt.rejected_inputs,
            {f"{NAMESPACE}.passthrough_denoise": "unknown input"},
        )
        self.assertEqual(
            receipt.ignored_inputs,
            {f"{NAMESPACE}.sampler": "input not supported by current binding"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
