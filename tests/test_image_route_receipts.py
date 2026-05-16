"""Focused tests for the image-endpoint route receipt helpers in proxy.py.

These exercise pure helpers only — no FastAPI client, no ComfyUI, no IO.
"""

from __future__ import annotations

import io
import json
import copy
import sys
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi import Response

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.receipts import RouteReceipt  # noqa: E402


def _png_bytes(color: tuple[int, int, int, int], size: tuple[int, int] = (2, 1)) -> bytes:
    from PIL import Image

    image = Image.new("RGBA", size, color)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _image_pixels(content: bytes) -> list[tuple[int, int, int, int]]:
    from PIL import Image

    image = Image.open(io.BytesIO(content)).convert("RGBA")
    return [
        image.getpixel((x, y))
        for y in range(image.height)
        for x in range(image.width)
    ]


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
        self.assertTrue(
            any(source.startswith("profile:") for source in receipt.policy_sources),
            receipt.policy_sources,
        )
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


class TestImageEditMaskSemantics(unittest.TestCase):
    def test_alpha_mask_matches_openai_edit_semantics(self) -> None:
        from PIL import Image

        source = _png_bytes((255, 0, 0, 255))
        generated = _png_bytes((0, 0, 255, 255))
        # Transparent left pixel edits; opaque right pixel preserves.
        mask = Image.new("RGBA", (2, 1), (0, 0, 0, 255))
        mask.putpixel((0, 0), (0, 0, 0, 0))
        output = io.BytesIO()
        mask.save(output, format="PNG")

        context, error = proxy._prepare_image_edit_mask_context(
            source_image_content=source,
            mask_content=output.getvalue(),
        )

        self.assertIsNone(error)
        assert context is not None
        self.assertEqual(context.mask_mode, "alpha")
        composited = proxy._apply_image_edit_mask_composite(context, generated)
        self.assertEqual(
            _image_pixels(composited),
            [(0, 0, 255, 255), (255, 0, 0, 255)],
        )

    def test_grayscale_mask_uses_white_as_edit_region(self) -> None:
        from PIL import Image

        source = _png_bytes((255, 0, 0, 255))
        generated = _png_bytes((0, 0, 255, 255))
        mask = Image.new("L", (2, 1), 0)
        mask.putpixel((0, 0), 255)
        output = io.BytesIO()
        mask.save(output, format="PNG")

        context, error = proxy._prepare_image_edit_mask_context(
            source_image_content=source,
            mask_content=output.getvalue(),
        )

        self.assertIsNone(error)
        assert context is not None
        self.assertEqual(context.mask_mode, "grayscale")
        composited = proxy._apply_image_edit_mask_composite(context, generated)
        self.assertEqual(
            _image_pixels(composited),
            [(0, 0, 255, 255), (255, 0, 0, 255)],
        )

    def test_mask_dimension_mismatch_is_rejected(self) -> None:
        context, error = proxy._prepare_image_edit_mask_context(
            source_image_content=_png_bytes((255, 0, 0, 255), size=(2, 1)),
            mask_content=_png_bytes((0, 0, 0, 255), size=(1, 1)),
        )

        self.assertIsNone(context)
        assert error is not None
        self.assertEqual(error.status_code, 400)
        body = json.loads(error.body)
        self.assertEqual(body["error"]["param"], "mask")
        self.assertEqual(body["error"]["code"], "invalid_mask_dimensions")

    def test_inpaint_workflow_is_preferred_only_when_it_exists(self) -> None:
        with patch.object(proxy, "load_comfy_workflow", lambda name: {"ok": True} if name.endswith("-inpaint") else None):
            self.assertEqual(
                proxy._prefer_inpaint_workflow_for_mask("flux2-klein-4b-edit"),
                "flux2-klein-4b-inpaint",
            )
        with patch.object(proxy, "load_comfy_workflow", lambda _name: None):
            self.assertEqual(
                proxy._prefer_inpaint_workflow_for_mask("flux2-klein-4b-edit"),
                "flux2-klein-4b-edit",
            )


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


class _MultipartUpload:
    def __init__(self, filename: str, content: bytes) -> None:
        self.filename = filename
        self._content = content

    async def read(self) -> bytes:
        return self._content


class _MultipartForm:
    def __init__(self, items: list[tuple[str, Any]]) -> None:
        self._items = items

    def multi_items(self) -> list[tuple[str, Any]]:
        return list(self._items)

    def get(self, key: str, default: Any = None) -> Any:
        for item_key, value in self._items:
            if item_key == key:
                return value
        return default


class _MultipartRequest:
    def __init__(self, items: list[tuple[str, Any]]) -> None:
        self._form = _MultipartForm(items)

    async def form(self) -> _MultipartForm:
        return self._form


class _UploadResponse:
    status_code = 200
    text = "ok"

    def __init__(self, name: str) -> None:
        self._name = name

    def json(self) -> dict[str, str]:
        return {"name": self._name}


class _FakeAsyncClient:
    uploads = 0
    upload_files: list[dict[str, Any]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def post(self, *_args: Any, **_kwargs: Any) -> _UploadResponse:
        type(self).uploads += 1
        type(self).upload_files.append(_kwargs.get("files") or {})
        return _UploadResponse(f"uploaded-{type(self).uploads}.png")


class TestProxyComfyEditRequestMask(unittest.IsolatedAsyncioTestCase):
    def _conditioning_manifest(
        self,
        *,
        inputs: dict[str, Any] | None = None,
        mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "id": "image.edit.flux2-klein-4b",
            "intent": "image.edit",
            "inputs": inputs
            or {
                "prompt": {"type": "string", "required": True},
                "source_image": {"type": "asset", "required": True},
                "control_image": {"type": "asset"},
                "controlnet_strength": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 2,
                },
            },
            "outputs": {"image": {"media_type": "image/png"}},
            "bindings": {
                "comfyui": {
                    "workflow": "workflows/images/controlnet-fixture.json",
                    "map": mapping
                    or {
                        "prompt": [{"node": "6", "input": "text"}],
                        "source_image": [{"node": "10", "input": "image"}],
                        "control_image": [{"node": "42", "input": "image"}],
                        "controlnet_strength": [{"node": "44", "input": "strength"}],
                    },
                }
            },
        }

    def _conditioning_workflow(self) -> dict[str, Any]:
        return {
            "_meta": {
                "name": "controlnet-fixture",
                "prompt_node": "6",
                "image_node": "10",
                "size_node": "47",
            },
            "6": {"inputs": {"text": ""}},
            "10": {"inputs": {"image": ""}},
            "42": {"inputs": {"image": ""}},
            "44": {"inputs": {"strength": 0.0}},
            "47": {"inputs": {"width": 512, "height": 512}},
            "48": {"inputs": {"width": 512, "height": 512}},
        }

    async def _run_edit_with_manifest(
        self,
        *,
        extension_block: dict[str, Any],
        extra_parts: list[tuple[str, Any]] | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> tuple[RouteReceipt, Response, dict[str, Any]]:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )
        request = _MultipartRequest(
            [
                ("prompt", "preserve geometry with edge conditioning"),
                ("model", "flux2-klein-4b-edit"),
                ("image", _MultipartUpload("source.png", _png_bytes((255, 0, 0, 255)))),
                *(extra_parts or []),
                (
                    "extensions",
                    json.dumps({"com.vividfish.workflow": extension_block}),
                ),
            ]
        )
        workflow = self._conditioning_workflow()
        captured: dict[str, Any] = {}

        async def fake_generate(**kwargs: Any) -> Response:
            captured.update(kwargs)
            return Response(
                content=json.dumps({"created": 1, "data": [{"b64_json": ""}]}),
                status_code=200,
                media_type="application/json",
            )

        _FakeAsyncClient.uploads = 0
        _FakeAsyncClient.upload_files = []
        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy.httpx, "AsyncClient", _FakeAsyncClient
        ), patch.object(
            proxy,
            "configured_downstream_headers",
            lambda _auth_context, _service: {},
        ), patch.object(
            proxy,
            "load_comfy_workflow",
            lambda _model: copy.deepcopy(workflow),
        ), patch.object(
            proxy,
            "_find_image_manifest",
            lambda _intent, _model: manifest,
        ), patch.object(
            proxy,
            "_generate_images_with_adaptive_batching",
            fake_generate,
        ):
            response = await proxy.proxy_comfy_edit_request(
                request,  # type: ignore[arg-type]
                proxy.NO_AUTH_CONTEXT,
                receipt=receipt,
            )
        return receipt, response, captured

    async def test_workflow_extension_asset_uploads_and_binds(self) -> None:
        receipt, response, captured = await self._run_edit_with_manifest(
            extension_block={
                "control_image": {"multipart_field": "control_image"},
                "controlnet_strength": 0.75,
            },
            extra_parts=[
                ("control_image", _MultipartUpload("edge.png", _png_bytes((0, 0, 0, 255)))),
            ],
            manifest=self._conditioning_manifest(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_FakeAsyncClient.uploads, 2)
        workflow_payload = captured["base_workflow"]
        self.assertEqual(workflow_payload["10"]["inputs"]["image"], "uploaded-1.png")
        self.assertEqual(workflow_payload["42"]["inputs"]["image"], "uploaded-2.png")
        self.assertEqual(workflow_payload["44"]["inputs"]["strength"], 0.75)
        self.assertIn("control_image", receipt.accepted_inputs)
        self.assertIn(
            "com.vividfish.workflow.control_image",
            receipt.accepted_inputs,
        )
        self.assertIn(
            "com.vividfish.workflow.controlnet_strength",
            receipt.accepted_inputs,
        )

    async def test_extension_asset_reference_requires_supported_asset_input(self) -> None:
        manifest = self._conditioning_manifest(
            inputs={
                "prompt": {"type": "string", "required": True},
                "source_image": {"type": "asset", "required": True},
                "sampler": {"type": "asset"},
            },
            mapping={
                "prompt": [{"node": "6", "input": "text"}],
                "source_image": [{"node": "10", "input": "image"}],
            },
        )

        receipt, response, captured = await self._run_edit_with_manifest(
            extension_block={"sampler": {"multipart_field": "extra_file"}},
            extra_parts=[
                ("extra_file", _MultipartUpload("extra.png", _png_bytes((0, 0, 0, 255)))),
            ],
            manifest=manifest,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(captured, {})
        self.assertIn(
            "extensions.com.vividfish.workflow.sampler",
            receipt.rejected_inputs,
        )
        self.assertIn("not a supported asset", response.body.decode())

    async def test_extension_asset_reference_requires_asset_typed_input(self) -> None:
        manifest = self._conditioning_manifest(
            inputs={
                "prompt": {"type": "string", "required": True},
                "source_image": {"type": "asset", "required": True},
                "denoise": {"type": "number", "minimum": 0, "maximum": 1},
            },
            mapping={
                "prompt": [{"node": "6", "input": "text"}],
                "source_image": [{"node": "10", "input": "image"}],
                "denoise": [{"node": "44", "input": "strength"}],
            },
        )

        receipt, response, captured = await self._run_edit_with_manifest(
            extension_block={"denoise": {"multipart_field": "extra_file"}},
            extra_parts=[
                ("extra_file", _MultipartUpload("extra.png", _png_bytes((0, 0, 0, 255)))),
            ],
            manifest=manifest,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(captured, {})
        self.assertIn(
            "extensions.com.vividfish.workflow.denoise",
            receipt.rejected_inputs,
        )
        self.assertIn("not an asset", response.body.decode())

    async def test_extension_asset_reference_requires_manifest_support(self) -> None:
        receipt, response, captured = await self._run_edit_with_manifest(
            extension_block={"control_image": {"multipart_field": "control_image"}},
            extra_parts=[
                ("control_image", _MultipartUpload("edge.png", _png_bytes((0, 0, 0, 255)))),
            ],
            manifest=None,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(captured, {})
        self.assertIn(
            "extensions.com.vividfish.workflow.control_image",
            receipt.rejected_inputs,
        )
        self.assertIn("not supported", response.body.decode())

    async def test_extension_asset_reference_cannot_reuse_reserved_image_field(self) -> None:
        receipt, response, captured = await self._run_edit_with_manifest(
            extension_block={"control_image": {"multipart_field": "image"}},
            manifest=self._conditioning_manifest(),
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(captured, {})
        self.assertIn(
            "extensions.com.vividfish.workflow.control_image",
            receipt.rejected_inputs,
        )
        self.assertIn("reserved", response.body.decode())

    async def test_multipart_mask_uses_post_composite_when_workflow_has_no_mask_binding(self) -> None:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )
        request = _MultipartRequest(
            [
                ("prompt", "replace the left pixel"),
                ("model", "flux2-klein-4b-edit"),
                ("image", _MultipartUpload("source.png", _png_bytes((255, 0, 0, 255)))),
                ("mask", _MultipartUpload("mask.png", _png_bytes((0, 0, 0, 0)))),
            ]
        )
        captured: dict[str, Any] = {}

        async def fake_generate(**kwargs: Any) -> Response:
            captured.update(kwargs)
            return Response(
                content=json.dumps({"created": 1, "data": [{"b64_json": ""}]}),
                status_code=200,
                media_type="application/json",
            )

        _FakeAsyncClient.uploads = 0
        _FakeAsyncClient.upload_files = []
        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy.httpx, "AsyncClient", _FakeAsyncClient
        ), patch.object(
            proxy,
            "configured_downstream_headers",
            lambda _auth_context, _service: {},
        ), patch.object(
            proxy,
            "_prefer_inpaint_workflow_for_mask",
            lambda model: model,
        ), patch.object(
            proxy,
            "_generate_images_with_adaptive_batching",
            fake_generate,
        ):
            response = await proxy.proxy_comfy_edit_request(
                request,  # type: ignore[arg-type]
                proxy.NO_AUTH_CONTEXT,
                receipt=receipt,
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("mask", receipt.accepted_inputs)
        self.assertEqual(captured["image_response_headers"]["X-Image-Mask"], "post-composite")
        self.assertEqual(captured["image_response_headers"]["X-Image-Mask-Source"], "alpha")
        self.assertTrue(callable(captured["image_transform"]))
        self.assertEqual(_FakeAsyncClient.uploads, 1)

    async def test_explicit_inpaint_workflow_requires_mask(self) -> None:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )
        request = _MultipartRequest(
            [
                ("prompt", "replace the left pixel"),
                ("model", "flux2-klein-4b-inpaint"),
                ("image", _MultipartUpload("source.png", _png_bytes((255, 0, 0, 255)))),
            ]
        )
        captured: dict[str, Any] = {}

        async def fake_generate(**kwargs: Any) -> Response:
            captured.update(kwargs)
            return Response(status_code=500)

        _FakeAsyncClient.uploads = 0
        _FakeAsyncClient.upload_files = []
        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy.httpx, "AsyncClient", _FakeAsyncClient
        ), patch.object(
            proxy,
            "configured_downstream_headers",
            lambda _auth_context, _service: {},
        ), patch.object(
            proxy,
            "_generate_images_with_adaptive_batching",
            fake_generate,
        ):
            response = await proxy.proxy_comfy_edit_request(
                request,  # type: ignore[arg-type]
                proxy.NO_AUTH_CONTEXT,
                receipt=receipt,
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(captured, {})
        self.assertIn("mask", receipt.rejected_inputs)
        self.assertIn("mask is required", response.body.decode())

    async def test_workflow_mask_binding_uploads_normalized_mask(self) -> None:
        from PIL import Image

        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )
        mask = Image.new("RGBA", (2, 1), (0, 0, 0, 255))
        mask.putpixel((0, 0), (0, 0, 0, 0))
        mask_output = io.BytesIO()
        mask.save(mask_output, format="PNG")
        request = _MultipartRequest(
            [
                ("prompt", "replace the left pixel"),
                ("model", "flux2-klein-4b-edit"),
                ("image", _MultipartUpload("source.png", _png_bytes((255, 0, 0, 255)))),
                ("mask", _MultipartUpload("mask.png", mask_output.getvalue())),
            ]
        )
        captured: dict[str, Any] = {}
        bound_inputs: dict[str, Any] = {}

        class _BindingResult:
            def __init__(self, request_body: dict[str, Any]) -> None:
                self.request = request_body

        def fake_bind(_manifest: Any, _backend: str, request_body: dict[str, Any], accepted: dict[str, Any]) -> _BindingResult:
            bound_inputs.update(accepted)
            return _BindingResult(request_body)

        async def fake_generate(**kwargs: Any) -> Response:
            captured.update(kwargs)
            return Response(
                content=json.dumps({"created": 1, "data": [{"b64_json": ""}]}),
                status_code=200,
                media_type="application/json",
            )

        _FakeAsyncClient.uploads = 0
        _FakeAsyncClient.upload_files = []
        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy.httpx, "AsyncClient", _FakeAsyncClient
        ), patch.object(
            proxy,
            "configured_downstream_headers",
            lambda _auth_context, _service: {},
        ), patch.object(
            proxy,
            "_manifest_route_managed_mask_input",
            lambda _manifest: "source_mask",
        ), patch.object(
            proxy,
            "bind_backend_request",
            fake_bind,
        ), patch.object(
            proxy,
            "_apply_manifest_route_inputs",
            lambda _manifest, workflow, _values: workflow,
        ), patch.object(
            proxy,
            "_generate_images_with_adaptive_batching",
            fake_generate,
        ):
            response = await proxy.proxy_comfy_edit_request(
                request,  # type: ignore[arg-type]
                proxy.NO_AUTH_CONTEXT,
                receipt=receipt,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_FakeAsyncClient.uploads, 2)
        self.assertEqual(bound_inputs, {"source_mask": "uploaded-2.png"})
        self.assertEqual(captured["image_response_headers"]["X-Image-Mask"], "workflow")
        self.assertEqual(captured["image_response_headers"]["X-Image-Mask-Source"], "alpha")
        self.assertIsNone(captured["image_transform"])
        mask_file_tuple = _FakeAsyncClient.upload_files[1]["image"]
        self.assertEqual(mask_file_tuple[0], "normalized-mask.png")
        normalized_pixels = _image_pixels(mask_file_tuple[1])
        self.assertEqual(
            [pixel[0] for pixel in normalized_pixels],
            [255, 0],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
