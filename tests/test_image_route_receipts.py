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


class TestRecordImageInput(unittest.TestCase):
    def _receipt(self) -> RouteReceipt:
        return proxy.start_image_route_receipt(
            "/v1/images/generations", proxy.NO_AUTH_CONTEXT
        )

    def test_accept_records_field_name(self) -> None:
        receipt = self._receipt()
        proxy._record_image_input(receipt, "model")
        self.assertIn("model", receipt.accepted_inputs)
        self.assertNotIn("model", receipt.rejected_inputs)

    def test_reject_records_reason_not_value(self) -> None:
        receipt = self._receipt()
        proxy._record_image_input(receipt, "n", rejected_reason="out_of_range")
        self.assertEqual(receipt.rejected_inputs["n"], "out_of_range")
        self.assertNotIn("n", receipt.accepted_inputs)

    def test_ignore_records_reason(self) -> None:
        receipt = self._receipt()
        proxy._record_image_input(receipt, "size", ignored_reason="overridden_by_image")
        self.assertEqual(receipt.ignored_inputs["size"], "overridden_by_image")

    def test_none_receipt_is_safe(self) -> None:
        proxy._record_image_input(None, "model")  # must not raise

    def test_reject_supersedes_prior_accept(self) -> None:
        receipt = self._receipt()
        proxy._record_image_input(receipt, "model")
        proxy._record_image_input(receipt, "model", rejected_reason="model_incompatible")
        self.assertNotIn("model", receipt.accepted_inputs)
        self.assertEqual(receipt.rejected_inputs["model"], "model_incompatible")


class TestAttachImageManifest(unittest.TestCase):
    def _receipt(self) -> RouteReceipt:
        return proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )

    def test_none_receipt_is_safe(self) -> None:
        proxy._attach_image_manifest_if_available(None, "image.edit", "flux2-klein-4b-edit")

    def test_known_edit_model_attaches_manifest(self) -> None:
        receipt = self._receipt()
        proxy._attach_image_manifest_if_available(
            receipt, "image.edit", "flux2-klein-4b-edit"
        )
        # Manifest exists at manifests/workflows/flux2-klein-4b-edit.yml with id
        # image.edit.flux2-klein-4b. Helper strips '-edit' suffix to match.
        self.assertEqual(receipt.manifest_id, "image.edit.flux2-klein-4b")
        self.assertIsNotNone(receipt.manifest_hash)

    def test_unknown_model_records_deferred_warning(self) -> None:
        receipt = self._receipt()
        proxy._attach_image_manifest_if_available(
            receipt, "image.generate", "this-model-has-no-manifest"
        )
        self.assertIsNone(receipt.manifest_id)
        self.assertTrue(
            any("manifest_identity_deferred" in w for w in receipt.warnings),
            receipt.warnings,
        )


class TestFinishReceiptIncludesRoutingFacts(unittest.TestCase):
    def test_firehose_event_carries_routing_and_input_facts(self) -> None:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/generations", proxy.NO_AUTH_CONTEXT
        )
        receipt.requested_model = "gpt-image-1"
        receipt.resolved_model = "flux2-klein-4b"
        receipt.requested_workflow = "flux2-klein-4b"
        receipt.resolved_workflow = "Flux2 Klein 4B"
        receipt.accept_input("model")
        receipt.accept_input("prompt")
        receipt.reject_input("n", "out_of_range")
        receipt.add_warning("manifest_identity_deferred:no_match")
        receipt.add_fallback_reason("comfy_disabled")

        events: list[dict[str, Any]] = []

        def fake_record_event(event_type: str, summary: str, **extra: Any) -> None:
            events.append({"type": event_type, "summary": summary, **extra})

        with patch.object(proxy.firehose_metrics, "record_event", fake_record_event):
            proxy.finish_image_route_receipt(receipt, _StubResponse(status_code=200))

        evt = events[0]
        self.assertEqual(evt["requested_model"], "gpt-image-1")
        self.assertEqual(evt["resolved_model"], "flux2-klein-4b")
        self.assertEqual(evt["requested_workflow"], "flux2-klein-4b")
        self.assertEqual(evt["resolved_workflow"], "Flux2 Klein 4B")
        self.assertIn("model", evt["accepted_inputs"])
        self.assertIn("prompt", evt["accepted_inputs"])
        self.assertEqual(evt["rejected_inputs"]["n"], "out_of_range")
        self.assertIn("manifest_identity_deferred:no_match", evt["warnings"])
        self.assertIn("comfy_disabled", evt["fallback_reasons"])

    def test_event_keys_still_omit_raw_body_fields(self) -> None:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )
        receipt.accept_input("prompt")  # name only, never a value
        receipt.accept_input("image")

        events: list[dict[str, Any]] = []

        def fake_record_event(event_type: str, summary: str, **extra: Any) -> None:
            events.append({"type": event_type, "summary": summary, **extra})

        with patch.object(proxy.firehose_metrics, "record_event", fake_record_event):
            proxy.finish_image_route_receipt(receipt, _StubResponse(status_code=200))

        forbidden = {"prompt", "image", "mask", "body", "file", "filename"}
        for key in events[0]:
            self.assertNotIn(key.lower(), forbidden)


class TestProxyComfyRequestReceipt(unittest.IsolatedAsyncioTestCase):
    """Integration-ish: drive proxy_comfy_request with COMFY disabled or invalid
    bodies to exercise early-return receipt instrumentation without ComfyUI."""

    def _receipt(self) -> RouteReceipt:
        return proxy.start_image_route_receipt(
            "/v1/images/generations", proxy.NO_AUTH_CONTEXT
        )

    async def _run_with_json_body(self, body: Any) -> tuple[RouteReceipt, Any]:
        receipt = self._receipt()

        class _StubRequest:
            async def json(self_inner) -> Any:
                if isinstance(body, Exception):
                    raise body
                return body

        with patch.object(proxy, "COMFY_ENABLED", True):
            with patch.object(
                proxy,
                "configured_downstream_headers",
                lambda _ac, _b: {},
            ):
                response = await proxy.proxy_comfy_request(
                    _StubRequest(),  # type: ignore[arg-type]
                    proxy.NO_AUTH_CONTEXT,
                    receipt=receipt,
                )
        return receipt, response

    async def test_comfy_disabled_records_fallback(self) -> None:
        receipt = self._receipt()

        class _StubRequest:
            async def json(self_inner) -> Any:  # pragma: no cover
                return {}

        with patch.object(proxy, "COMFY_ENABLED", False):
            response = await proxy.proxy_comfy_request(
                _StubRequest(),  # type: ignore[arg-type]
                proxy.NO_AUTH_CONTEXT,
                receipt=receipt,
            )
        self.assertEqual(response.status_code, 503)
        self.assertIn("comfy_disabled", receipt.fallback_reasons)

    async def test_invalid_json_records_body_rejection(self) -> None:
        receipt, response = await self._run_with_json_body(ValueError("nope"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(receipt.rejected_inputs.get("body"), "invalid_json")

    async def test_bad_n_records_rejection(self) -> None:
        receipt, response = await self._run_with_json_body(
            {"prompt": "x", "n": "abc"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(receipt.rejected_inputs.get("n"), "not_integer")
        # model accepted before n was inspected
        self.assertIn("model", receipt.accepted_inputs)

    async def test_n_out_of_range_records_rejection(self) -> None:
        receipt, response = await self._run_with_json_body(
            {"prompt": "x", "n": 99}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(receipt.rejected_inputs.get("n"), "out_of_range")

    async def test_bad_response_format_records_rejection(self) -> None:
        receipt, response = await self._run_with_json_body(
            {"prompt": "x", "response_format": "weird"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            receipt.rejected_inputs.get("response_format"), "unsupported_value"
        )

    async def test_missing_prompt_records_rejection(self) -> None:
        receipt, response = await self._run_with_json_body(
            {"prompt": "", "model": "flux2-klein-4b"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(receipt.rejected_inputs.get("prompt"), "missing")
        self.assertEqual(receipt.requested_model, "flux2-klein-4b")
        self.assertEqual(receipt.resolved_model, "flux2-klein-4b")

    async def test_unknown_model_records_fallback(self) -> None:
        receipt, response = await self._run_with_json_body(
            {"prompt": "x", "model": "no-such-model-anywhere"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertTrue(
            any("unknown_model" in r for r in receipt.fallback_reasons),
            receipt.fallback_reasons,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
