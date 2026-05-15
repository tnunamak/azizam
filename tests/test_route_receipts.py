"""Standalone tests for route receipt diagnostics."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.receipts import RouteReceipt, record_validation_result  # noqa: E402


class FakeValidationResult:
    accepted = {"denoise": 0.35}
    rejected = {"surprise": "unknown input"}


class TestRouteReceipt(unittest.TestCase):
    def test_receipt_records_inputs_without_values(self) -> None:
        receipt = RouteReceipt(
            protocol="openai.images",
            path="/v1/images/edits",
            intent="image.edit",
            app_profile="image-agent",
            requested_workflow="image.edit.controlnet.example",
            backend="comfyui.local",
        )

        receipt.set_policy_decision(
            "allowed",
            reason="profile permits image edit workflows",
            source="profile:image-agent",
        )
        receipt.attach_manifest(
            manifest_id="image.edit.controlnet.example",
            manifest_version="0.1.0",
            manifest_hash="abc123",
        )
        record_validation_result(receipt, FakeValidationResult())
        receipt.ignore_input("seed", "backend selected deterministic seed")
        receipt.add_warning("fallback disabled by profile")
        receipt.job_id = "job_123"
        receipt.finish("rejected", latency_ms=12.34567)

        data = receipt.to_dict()
        self.assertTrue(data["request_id"].startswith("req_"))
        self.assertEqual(data["accepted_inputs"], ["denoise"])
        self.assertEqual(data["rejected_inputs"], {"surprise": "unknown input"})
        self.assertEqual(data["ignored_inputs"], {"seed": "backend selected deterministic seed"})
        self.assertEqual(data["warnings"], ["fallback disabled by profile"])
        self.assertEqual(data["policy_decision"], "allowed")
        self.assertEqual(data["manifest_id"], "image.edit.controlnet.example")
        self.assertEqual(data["manifest_hash"], "abc123")
        self.assertEqual(data["job_id"], "job_123")
        self.assertEqual(data["status"], "rejected")
        self.assertEqual(data["latency_ms"], 12.346)
        self.assertNotIn("0.35", str(data))

        client_data = receipt.to_dict(audience="client")
        self.assertNotIn("api_key_name", client_data)
        self.assertNotIn("backend", client_data)
        self.assertNotIn("policy_sources", client_data)
        self.assertNotIn("manifest_hash", client_data)
        self.assertEqual(client_data["job_id"], "job_123")

    def test_input_state_is_single_valued(self) -> None:
        receipt = RouteReceipt(protocol="openai.chat", intent="chat.generate")

        receipt.reject_input("reasoning_effort", "unsupported")
        receipt.accept_input("reasoning_effort")
        receipt.ignore_input("reasoning_effort", "profile strips it")

        self.assertEqual(receipt.accepted_inputs, [])
        self.assertEqual(receipt.rejected_inputs, {})
        self.assertEqual(receipt.ignored_inputs, {"reasoning_effort": "profile strips it"})

    def test_invalid_policy_decision_fails_fast(self) -> None:
        receipt = RouteReceipt(protocol="openai.chat", intent="chat.generate")
        with self.assertRaises(ValueError):
            receipt.set_policy_decision("maybe")


if __name__ == "__main__":
    unittest.main(verbosity=2)
