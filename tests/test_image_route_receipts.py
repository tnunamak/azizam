"""Focused tests for the image-endpoint route receipt helpers in proxy.py.

These exercise pure helpers only — no FastAPI client, no ComfyUI, no IO.
"""

from __future__ import annotations

import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.receipts import RouteReceipt  # noqa: E402


def _identity(name: str) -> proxy.AuthIdentity:
    return proxy.AuthIdentity(name=name, key_hash=f"sha256:{name}")


@dataclass
class _StubResponse:
    status_code: int


class TestStartImageRouteReceipt(unittest.TestCase):
    def test_generations_receipt_records_routing_facts(self) -> None:
        auth_context = proxy.AuthContext(identity=_identity("alice"))

        receipt = proxy.start_image_route_receipt(
            "/v1/images/generations", auth_context
        )

        self.assertIsInstance(receipt, RouteReceipt)
        self.assertEqual(receipt.protocol, "openai.images")
        self.assertEqual(receipt.path, "/v1/images/generations")
        self.assertEqual(receipt.intent, "image.generate")
        self.assertEqual(receipt.backend, "comfyui")
        self.assertEqual(receipt.api_key_name, "alice")
        self.assertEqual(receipt.policy_decision, "allowed")
        self.assertEqual(receipt.policy_sources, ["proxy:legacy-auth"])
        self.assertTrue(receipt.policy_reasons)
        self.assertTrue(receipt.request_id.startswith("req_"))
        self.assertIsNone(receipt.status)
        self.assertIsNone(receipt.latency_ms)

    def test_edits_receipt_uses_image_edit_intent(self) -> None:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )

        self.assertEqual(receipt.intent, "image.edit")
        self.assertIsNone(receipt.api_key_name)
        self.assertEqual(receipt.policy_decision, "allowed")

    def test_unknown_path_defaults_to_image_generate(self) -> None:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/unknown", proxy.NO_AUTH_CONTEXT
        )
        self.assertEqual(receipt.intent, "image.generate")


class TestFinishImageRouteReceipt(unittest.TestCase):
    def _finish(self, status_code: int) -> tuple[RouteReceipt, list[dict[str, Any]]]:
        auth_context = proxy.AuthContext(identity=_identity("alice"))
        receipt = proxy.start_image_route_receipt(
            "/v1/images/generations", auth_context
        )
        events: list[dict[str, Any]] = []

        def fake_record_event(event_type: str, summary: str, **extra: Any) -> None:
            events.append({"type": event_type, "summary": summary, **extra})

        with patch.object(proxy.firehose_metrics, "record_event", fake_record_event):
            proxy.finish_image_route_receipt(receipt, _StubResponse(status_code=status_code))
        return receipt, events

    def test_success_response_marks_ok_and_emits_event(self) -> None:
        receipt, events = self._finish(200)

        self.assertEqual(receipt.status, "ok")
        self.assertIsNotNone(receipt.latency_ms)
        self.assertEqual(len(events), 1)
        evt = events[0]
        self.assertEqual(evt["type"], "image_route_receipt")
        self.assertEqual(evt["path"], "/v1/images/generations")
        self.assertEqual(evt["intent"], "image.generate")
        self.assertEqual(evt["backend"], "comfyui")
        self.assertEqual(evt["api_key_name"], "alice")
        self.assertEqual(evt["status"], "ok")
        self.assertEqual(evt["http_status"], 200)
        self.assertEqual(evt["policy_decision"], "allowed")
        self.assertEqual(evt["request_id"], receipt.request_id)

    def test_4xx_response_marks_rejected(self) -> None:
        receipt, events = self._finish(400)
        self.assertEqual(receipt.status, "rejected")
        self.assertEqual(events[0]["status"], "rejected")
        self.assertEqual(events[0]["http_status"], 400)

    def test_5xx_response_marks_error(self) -> None:
        receipt, _ = self._finish(503)
        self.assertEqual(receipt.status, "error")

    def test_event_payload_has_no_body_or_prompt_keys(self) -> None:
        _, events = self._finish(200)
        forbidden = {"prompt", "image", "mask", "body", "file", "filename"}
        for key in events[0]:
            self.assertNotIn(key.lower(), forbidden)

    def test_firehose_failure_does_not_raise(self) -> None:
        auth_context = proxy.AuthContext(identity=_identity("alice"))
        receipt = proxy.start_image_route_receipt(
            "/v1/images/generations", auth_context
        )

        def boom(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("firehose down")

        with patch.object(proxy.firehose_metrics, "record_event", boom):
            proxy.finish_image_route_receipt(receipt, _StubResponse(status_code=200))

        self.assertEqual(receipt.status, "ok")


class TestImageReceiptStatus(unittest.TestCase):
    def test_status_buckets(self) -> None:
        self.assertEqual(proxy._image_receipt_status(200), "ok")
        self.assertEqual(proxy._image_receipt_status(204), "ok")
        self.assertEqual(proxy._image_receipt_status(400), "rejected")
        self.assertEqual(proxy._image_receipt_status(404), "rejected")
        self.assertEqual(proxy._image_receipt_status(500), "error")
        self.assertEqual(proxy._image_receipt_status(0), "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
