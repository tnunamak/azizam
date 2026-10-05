"""Focused tests for the image-endpoint route receipt helpers in proxy.py.

These exercise pure helpers only — no FastAPI client, no ComfyUI, no IO.
"""

from __future__ import annotations

import base64
import io
import json
import copy
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

from fastapi import Response
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.policy import AppProfile  # noqa: E402
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


def _lookup_from_identities(*identities: proxy.AuthIdentity):
    by_hash = {identity.key_hash: identity for identity in identities}

    def lookup(key_hash: str, **_kwargs):
        return by_hash.get(key_hash)

    return lookup


@dataclass
class _StubResponse:
    status_code: int


class _StubUrlRequest:
    headers: dict[str, str] = {}
    url = SimpleNamespace(scheme="http", netloc="gateway.test")


class _StubMultipartForm:
    def __init__(self, items: list[tuple[str, Any]]) -> None:
        self._items = items

    def multi_items(self) -> list[tuple[str, Any]]:
        return list(self._items)

    def get(self, key: str, default: Any = None) -> Any:
        for item_key, value in self._items:
            if item_key == key:
                return value
        return default


class _StubFormRequest:
    def __init__(self, form: _StubMultipartForm) -> None:
        self._form = form

    async def form(self) -> _StubMultipartForm:
        return self._form


class _StubUpload:
    filename = "input.png"
    content_type = "image/png"

    async def read(self) -> bytes:
        return _png_bytes((1, 2, 3, 255))


class TestGeneratedImageAssets(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache_dir = Path(self.tmp.name)
        self.cache_patch = patch.object(proxy, "IMAGE_CACHE_DIR", self.cache_dir)
        self.secret_patch = patch.object(proxy, "_image_url_signing_secret", b"test-secret")
        self.cache_patch.start()
        self.secret_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.addCleanup(self.secret_patch.stop)

    def test_image_response_includes_reusable_asset_for_b64_and_url(self) -> None:
        raw = _png_bytes((1, 2, 3, 255))
        request = _StubUrlRequest()

        b64_response = proxy._build_image_response_payload(
            request,  # type: ignore[arg-type]
            [raw],
            "b64_json",
            {},
        )
        b64_body = json.loads(b64_response.body.decode("utf-8"))
        b64_item = b64_body["data"][0]
        self.assertTrue(b64_item["asset_id"].startswith("img_"))
        self.assertIn("/v1/assets/", b64_item["asset_url"])
        self.assertEqual(b64_item["media_type"], "image/png")
        self.assertIn("b64_json", b64_item)

        url_response = proxy._build_image_response_payload(
            request,  # type: ignore[arg-type]
            [raw],
            "url",
            {},
        )
        url_body = json.loads(url_response.body.decode("utf-8"))
        url_item = url_body["data"][0]
        self.assertEqual(url_item["asset_id"], b64_item["asset_id"])
        self.assertIn("/v1/images/", url_item["url"])
        self.assertIn("/v1/assets/", url_item["asset_url"])

    def test_response_echoes_output_format_background_quality_size(self) -> None:
        raw = _png_bytes((1, 2, 3, 255))
        request = _StubUrlRequest()

        response = proxy._build_image_response_payload(
            request,  # type: ignore[arg-type]
            [raw],
            "b64_json",
            {},
            output_format="png",
            background="opaque",
            quality="high",
            size="1024x1024",
        )
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["output_format"], "png")
        self.assertEqual(body["background"], "opaque")
        self.assertEqual(body["quality"], "high")
        self.assertEqual(body["size"], "1024x1024")

    def test_size_adjustment_helper_sets_header_and_receipt_metadata(self) -> None:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/generations", proxy.NO_AUTH_CONTEXT
        )

        headers = proxy._record_image_size_adjustment(
            receipt,
            requested="1000x600",
            effective="1024x1024",
        )

        self.assertEqual(headers["X-Vivid-Size-Adjusted"], "1000x600->1024x1024")
        self.assertEqual(
            receipt.metadata["image_size_adjustment"],
            {"requested": "1000x600", "effective": "1024x1024"},
        )
        self.assertIn("size_adjusted:1000x600->1024x1024", receipt.warnings)

    def test_response_never_echoes_auto_quality(self) -> None:
        """OpenAI's own response schema only accepts a resolved quality
        tier -- "auto" is a request-time convenience it never echoes back.
        A strict SDK client rejects a response containing it."""
        raw = _png_bytes((1, 2, 3, 255))
        request = _StubUrlRequest()

        response = proxy._build_image_response_payload(
            request,  # type: ignore[arg-type]
            [raw],
            "b64_json",
            {},
            quality="auto",
        )
        body = json.loads(response.body.decode("utf-8"))
        self.assertNotIn("quality", body)

    def test_output_format_jpeg_reencodes_bytes_and_media_type(self) -> None:
        from PIL import Image

        raw = _png_bytes((1, 2, 3, 255))
        request = _StubUrlRequest()

        response = proxy._build_image_response_payload(
            request,  # type: ignore[arg-type]
            [raw],
            "b64_json",
            {},
            output_format="jpeg",
            output_compression=80,
        )
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["output_format"], "jpeg")
        item = body["data"][0]
        self.assertEqual(item["media_type"], "image/jpeg")
        decoded = base64.b64decode(item["b64_json"])
        image = Image.open(io.BytesIO(decoded))
        self.assertEqual(image.format, "JPEG")

    async def test_background_removal_failure_fallback_echoes_opaque(self) -> None:
        """When RMBG fails mid-round, the retry renders without background
        removal (opaque) -- the response must say so, not keep claiming the
        originally-requested "transparent"."""
        raw = _png_bytes((10, 20, 30, 255))
        workflow = {"6": {"inputs": {"text": ""}}}
        fallback_workflow = copy.deepcopy(workflow)
        error_response = Response(
            content=json.dumps({"error": {"message": "RMBG node crashed"}}),
            status_code=500,
            media_type="application/json",
        )
        calls = {"n": 0}

        async def fake_run_round(*_args, **_kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return error_response, []
            return None, [raw]

        request = _StubUrlRequest()
        decision = proxy.ImageBackgroundRemovalDecision(
            remove_background=True,
            confidence=1.0,
            reason="client requested transparent background",
            source="request",
        )
        with patch.object(
            proxy, "_comfy_run_round", fake_run_round
        ), patch.object(
            proxy, "get_vram_estimate", lambda *_a, **_k: 1024
        ), patch.object(
            proxy, "comfy_free_vram_bytes", AsyncMock(return_value=10**10)
        ):
            response = await proxy._generate_images_with_adaptive_batching(
                request=request,  # type: ignore[arg-type]
                client=object(),  # type: ignore[arg-type]
                base_workflow=workflow,
                meta={},
                seed_node=None,
                seed_field="seed",
                workflow_name="fixture",
                width=512,
                height=512,
                n_requested=1,
                response_format="b64_json",
                headers={},
                background_removal_decision=decision,
                fallback_workflow=fallback_workflow,
                fallback_workflow_name="fixture",
                output_format="png",
                output_background="transparent",
            )
        self.assertEqual(response.status_code, 200)
        body = json.loads(response.body)
        self.assertEqual(body["background"], "opaque")

    async def _run_adaptive(self, *, n: int, estimate: int, free_seq, freed: bool):
        raw = _png_bytes((10, 20, 30, 255))
        batches: list[int] = []

        async def fake_run_round(_client, workflow, *_a, **_k):
            batches.append(workflow["b"]["inputs"]["batch_size"])
            return None, [raw] * batches[-1]

        free_iter = iter(free_seq)
        last = {"v": free_seq[-1]}

        async def fake_free(_client):
            last["v"] = next(free_iter, last["v"])
            return last["v"]

        free_models = AsyncMock(return_value=freed)
        meta = {"batch_size_node": "b", "batch_size_field": "batch_size"}
        with patch.object(proxy, "_comfy_run_round", fake_run_round), patch.object(
            proxy, "get_vram_estimate", lambda *_a, **_k: estimate
        ), patch.object(proxy, "comfy_free_vram_bytes", fake_free), patch.object(
            proxy, "_free_comfy_models_if_idle", free_models
        ), patch.object(proxy, "IMAGE_VRAM_RESERVE_BYTES", 1 << 30), patch.object(
            proxy, "IMAGE_FREE_VRAM_ON_EXHAUSTION", True
        ):
            response = await proxy._generate_images_with_adaptive_batching(
                request=_StubUrlRequest(),  # type: ignore[arg-type]
                client=object(),  # type: ignore[arg-type]
                base_workflow={"b": {"inputs": {"batch_size": 1}}},
                meta=meta,
                seed_node=None,
                seed_field="seed",
                workflow_name="fixture",
                width=1024,
                height=1024,
                n_requested=n,
                response_format="b64_json",
                headers={},
            )
        return response, batches, free_models

    async def test_single_image_not_refused_when_free_below_estimate(self) -> None:
        gib = 1 << 30
        response, batches, free_models = await self._run_adaptive(
            n=1, estimate=int(16.05 * gib), free_seq=[int(16.74 * gib)] * 6, freed=False
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(batches, [1])
        free_models.assert_awaited()  # idle free is still tried first

    async def test_single_image_submits_after_idle_free_still_insufficient(self) -> None:
        gib = 1 << 30
        response, batches, _ = await self._run_adaptive(
            n=1, estimate=16 * gib, free_seq=[2 * gib] * 6, freed=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(batches, [1])

    async def test_multi_image_falls_back_to_batch_one_rounds(self) -> None:
        gib = 1 << 30
        response, batches, free_models = await self._run_adaptive(
            n=2, estimate=16 * gib, free_seq=[2 * gib] * 8, freed=True
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(batches, [1, 1])
        # Idle models are freed at most once per request, not before every round.
        self.assertEqual(free_models.await_count, 1)

    async def test_estimate_still_sizes_larger_batches(self) -> None:
        gib = 1 << 30
        response, batches, free_models = await self._run_adaptive(
            n=4, estimate=2 * gib, free_seq=[9 * gib] * 4, freed=False
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(batches, [4])
        free_models.assert_not_awaited()

    async def test_image_edit_accepts_openai_image_array_multipart_field(self) -> None:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )
        request = _StubFormRequest(
            _StubMultipartForm([
                ("image[]", _StubUpload()),
                ("model", "flux2-klein-4b-edit"),
            ])
        )

        with patch.object(proxy, "COMFY_ENABLED", True):
            response = await proxy.proxy_comfy_edit_request(
                request,  # type: ignore[arg-type]
                proxy.NO_AUTH_CONTEXT,
                receipt=receipt,
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"prompt is required", response.body)
        self.assertIn("image[]", receipt.accepted_inputs)
        self.assertNotIn("image[]", receipt.rejected_inputs)

    def test_signed_asset_content_serves_cached_image(self) -> None:
        raw = _png_bytes((4, 5, 6, 255))
        asset_id = proxy.store_image_bytes(raw)
        expires = int(proxy.time.time()) + 60
        sig = proxy._sign_asset_token(asset_id, expires)

        request = _StubUrlRequest()
        request.query_params = {"expires": str(expires), "sig": sig}
        response = proxy._signed_asset_content_response(
            asset_id,  # type: ignore[arg-type]
            request,  # type: ignore[arg-type]
            invalid_message="Invalid asset id",
            missing_message="Asset not found",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.media_type, "image/png")
        self.assertEqual(response.body, raw)

    async def test_asset_id_resolves_as_input_reference(self) -> None:
        raw = _png_bytes((7, 8, 9, 255))
        asset_id = proxy.store_image_bytes(raw)

        content, filename = await proxy._resolve_input_reference({"asset_id": asset_id})

        self.assertEqual(content, raw)
        self.assertTrue(filename.startswith(asset_id))
        self.assertTrue(filename.endswith(".png"))

    async def test_asset_id_reuse_is_owner_bound_when_metadata_exists(self) -> None:
        raw = _png_bytes((10, 11, 12, 255))
        asset_id = proxy.store_image_bytes(raw, owner_key_hash="sha256:alice")

        content, _ = await proxy._resolve_input_reference(
            {"asset_id": asset_id},
            proxy.AuthContext(identity=_identity("alice")),
        )
        self.assertEqual(content, raw)

        with self.assertRaisesRegex(
            proxy._InputReferenceError,
            "not accessible",
        ):
            await proxy._resolve_input_reference(
                {"asset_id": asset_id},
                proxy.AuthContext(identity=_identity("bob")),
            )

    def test_asset_content_route_accepts_signed_url_without_bearer(self) -> None:
        raw_key = "opk_asset_test"
        raw = _png_bytes((13, 14, 15, 255))
        owner_hash = proxy.hash_api_key(raw_key)
        asset_id = proxy.store_image_bytes(raw, owner_key_hash=owner_hash)
        expires = int(proxy.time.time()) + 60
        sig = proxy._sign_asset_token(asset_id, expires)
        path = f"/v1/assets/{asset_id}/content"
        query = {"expires": str(expires), "sig": sig}
        client = TestClient(proxy.app)
        bob_hash = proxy.hash_api_key("bob")

        with patch.object(proxy, "AUTH_ENABLED", True), patch.object(
            proxy,
            "runtime_identity_for_app_credential_hash",
            side_effect=_lookup_from_identities(
                proxy.AuthIdentity(name="alice", key_hash=owner_hash),
                proxy.AuthIdentity(name="bob", key_hash=bob_hash),
            ),
        ):
            no_auth = client.get(path, params=query)
            self.assertEqual(no_auth.status_code, 200, no_auth.text)
            self.assertEqual(no_auth.headers["content-type"], "image/png")
            self.assertEqual(no_auth.content, raw)

            missing_sig = client.get(
                path,
                params={"expires": str(expires)},
            )
            self.assertEqual(missing_sig.status_code, 400)

            bad_sig = client.get(
                path,
                params={"expires": str(expires), "sig": "bad"},
            )
            self.assertEqual(bad_sig.status_code, 403)

            wrong_owner = client.get(
                path,
                params=query,
                headers={"Authorization": "Bearer bob"},
            )
            self.assertEqual(wrong_owner.status_code, 200, wrong_owner.text)
            self.assertEqual(wrong_owner.content, raw)

            ok = client.get(
                path,
                params=query,
                headers={"Authorization": f"Bearer {raw_key}"},
            )
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertEqual(ok.headers["content-type"], "image/png")
            self.assertEqual(ok.content, raw)

    def test_image_content_routes_accept_signed_url_without_bearer(self) -> None:
        raw_key = "opk_image_policy_test"
        raw = _png_bytes((16, 17, 18, 255))
        owner_hash = proxy.hash_api_key(raw_key)
        asset_id = proxy.store_image_bytes(raw, owner_key_hash=owner_hash)
        expires = int(proxy.time.time()) + 60
        sig = proxy._sign_asset_token(asset_id, expires)
        query = {"expires": str(expires), "sig": sig}
        client = TestClient(proxy.app)

        for path in (
            f"/v1/images/{asset_id}/content",
            f"/v1/assets/{asset_id}/content",
        ):
            with self.subTest(path=path), patch.object(
                proxy,
                "AUTH_ENABLED",
                True,
            ):
                ok = client.get(
                    path,
                    params=query,
                )
                self.assertEqual(ok.status_code, 200, ok.text)
                self.assertEqual(ok.headers["content-type"], "image/png")
                self.assertEqual(ok.content, raw)

    def test_generic_asset_content_accepts_signed_url_without_bearer(self) -> None:
        raw_key = "opk_audio_asset_policy_test"
        raw = b"RIFF....WAVEfmt "
        owner_hash = proxy.hash_api_key(raw_key)
        asset_id = proxy.store_asset_bytes(
            raw,
            kind="aud",
            suffix=".wav",
            owner_key_hash=owner_hash,
        )
        expires = int(proxy.time.time()) + 60
        sig = proxy._sign_asset_token(asset_id, expires, "full")
        query = {"expires": str(expires), "md": "full", "sig": sig}
        client = TestClient(proxy.app)

        with patch.object(proxy, "AUTH_ENABLED", True):
            ok = client.get(
                f"/v1/assets/{asset_id}/content",
                params=query,
            )
            self.assertEqual(ok.status_code, 200, ok.text)
            self.assertEqual(ok.headers["content-type"], "audio/wav")
            self.assertEqual(ok.content, raw)

    def test_gateway_asset_metadata_uses_asset_kind_profile_and_owner(self) -> None:
        raw_key = "opk_gateway_asset_test"
        raw = _png_bytes((19, 20, 21, 255))
        owner_hash = proxy.hash_api_key(raw_key)
        asset_id = proxy.store_image_bytes(raw, owner_key_hash=owner_hash)
        client = TestClient(proxy.app)
        chat_only = AppProfile(
            id="chat-only",
            protocols=frozenset({"openai.chat"}),
        )
        image_profile = AppProfile(
            id="image",
            protocols=frozenset({"openai.images"}),
        )

        def identity(profile: AppProfile) -> proxy.AuthIdentity:
            return proxy.AuthIdentity(
                name="gateway-asset-policy",
                key_hash=owner_hash,
                profile=profile,
            )

        with patch.object(proxy, "AUTH_ENABLED", True), patch.object(
            proxy,
            "runtime_identity_for_app_credential_hash",
            side_effect=_lookup_from_identities(identity(chat_only)),
        ):
            denied = client.get(
                f"/gateway/v1/assets/{asset_id}",
                headers={"Authorization": f"Bearer {raw_key}"},
            )
            self.assertEqual(denied.status_code, 403, denied.text)
            self.assertEqual(denied.json()["error"]["code"], "policy_denied")

        with patch.object(proxy, "AUTH_ENABLED", True), patch.object(
            proxy,
            "runtime_identity_for_app_credential_hash",
            side_effect=_lookup_from_identities(identity(image_profile)),
        ):
            ok = client.get(
                f"/gateway/v1/assets/{asset_id}",
                headers={"Authorization": f"Bearer {raw_key}"},
            )

        self.assertEqual(ok.status_code, 200, ok.text)
        payload = ok.json()
        self.assertEqual(payload["object"], "gateway.asset")
        self.assertEqual(payload["id"], asset_id)
        self.assertEqual(payload["kind"], "image")
        self.assertEqual(payload["media_type"], "image/png")
        self.assertEqual(payload["size_bytes"], len(raw))
        self.assertIn(f"/v1/assets/{asset_id}/content", payload["content_url"])
        self.assertTrue(payload["owner_scoped"])

    def test_gateway_asset_content_accepts_same_signed_url_contract(self) -> None:
        raw_key = "opk_gateway_asset_content_test"
        raw = b"RIFF....WAVEfmt "
        owner_hash = proxy.hash_api_key(raw_key)
        asset_id = proxy.store_asset_bytes(
            raw,
            kind="aud",
            suffix=".wav",
            owner_key_hash=owner_hash,
        )
        expires = int(proxy.time.time()) + 60
        sig = proxy._sign_asset_token(asset_id, expires, "full")
        audio_profile = AppProfile(
            id="audio",
            protocols=frozenset({"openai.audio"}),
        )
        identity = proxy.AuthIdentity(
            name="gateway-asset-content",
            key_hash=owner_hash,
            profile=audio_profile,
        )

        with patch.object(proxy, "AUTH_ENABLED", True), patch.object(
            proxy,
            "runtime_identity_for_app_credential_hash",
            side_effect=_lookup_from_identities(identity),
        ):
            ok = TestClient(proxy.app).get(
                f"/gateway/v1/assets/{asset_id}/content",
                params={"expires": str(expires), "md": "full", "sig": sig},
                headers={"Authorization": f"Bearer {raw_key}"},
            )

        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(ok.headers["content-type"], "audio/wav")
        self.assertEqual(ok.content, raw)

    def test_invalid_generic_asset_id_uses_signed_asset_validation(self) -> None:
        raw_key = "opk_invalid_asset_policy_test"
        owner_hash = proxy.hash_api_key(raw_key)
        asset_id = "bad_asset_id"
        expires = int(proxy.time.time()) + 60
        sig = proxy._sign_asset_token(asset_id, expires)
        chat_only = AppProfile(
            id="chat-only",
            protocols=frozenset({"openai.chat"}),
        )
        identity = proxy.AuthIdentity(
            name="invalid-asset-policy",
            key_hash=owner_hash,
            profile=chat_only,
        )

        with patch.object(proxy, "AUTH_ENABLED", True), patch.object(
            proxy,
            "runtime_identity_for_app_credential_hash",
            side_effect=_lookup_from_identities(identity),
        ):
            response = TestClient(proxy.app).get(
                f"/v1/assets/{asset_id}/content",
                params={"expires": str(expires), "sig": sig},
                headers={"Authorization": f"Bearer {raw_key}"},
            )

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["message"], "Invalid asset id")


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

    def test_receipt_records_when_comfy_models_were_unloaded(self) -> None:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/generations", proxy.NO_AUTH_CONTEXT
        )
        response = Response(status_code=200, headers={"X-ComfyUI-Models-Freed": "true"})
        with patch.object(proxy.receipt_store, "record", lambda _receipt: None), patch.object(
            proxy.firehose_metrics, "record_event", lambda *_args, **_kwargs: None
        ):
            proxy.finish_image_route_receipt(receipt, response)
        self.assertIs(receipt.metadata.get("comfyui_models_freed_for_vram"), True)

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

    async def test_unknown_model_returns_model_not_found(self) -> None:
        receipt, response = await self._run_with_json_body(
            {"prompt": "x", "model": "no-such-model-anywhere"}
        )
        self.assertEqual(response.status_code, 404)
        error = json.loads(response.body)["error"]
        self.assertEqual(error["code"], "model_not_found")
        self.assertEqual(error["param"], "model")
        self.assertFalse(receipt.fallback_reasons)

    async def test_bad_output_format_records_rejection(self) -> None:
        receipt, response = await self._run_with_json_body(
            {"prompt": "x", "output_format": "bmp"}
        )
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["param"], "output_format")
        self.assertIsNotNone(receipt.rejected_inputs.get("output_format"))

    async def test_bad_output_compression_records_rejection(self) -> None:
        receipt, response = await self._run_with_json_body(
            {"prompt": "x", "output_format": "jpeg", "output_compression": 150}
        )
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["param"], "output_compression")

    async def test_bad_quality_records_rejection(self) -> None:
        receipt, response = await self._run_with_json_body(
            {"prompt": "x", "quality": "ultra"}
        )
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["param"], "quality")

    async def test_transparent_background_with_jpeg_output_is_rejected(self) -> None:
        receipt, response = await self._run_with_json_body(
            {"prompt": "x", "background": "transparent", "output_format": "jpeg"}
        )
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["param"], "background")
        self.assertEqual(
            receipt.rejected_inputs.get("background"), "conflicts_with_output_format"
        )

    async def test_transparent_background_with_webp_output_is_accepted(self) -> None:
        """Only jpeg conflicts with transparent; webp must pass this check
        (it may still fail later for unrelated reasons, e.g. unknown model)."""
        receipt, response = await self._run_with_json_body(
            {
                "prompt": "x",
                "background": "transparent",
                "output_format": "webp",
                "model": "no-such-model-anywhere",
            }
        )
        # Falls through to the unknown-model 404, not a background/output_format
        # conflict, proving the combination itself was accepted.
        self.assertNotEqual(receipt.rejected_inputs.get("background"), "conflicts_with_output_format")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(json.loads(response.body)["error"]["code"], "model_not_found")
        self.assertFalse(receipt.fallback_reasons)


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
    def test_manifest_source_alpha_mask_uses_openai_edit_region(self) -> None:
        from PIL import Image

        mask = Image.new("RGBA", (2, 1), (0, 0, 0, 255))
        mask.putpixel((0, 0), (0, 0, 0, 0))
        mask_bytes = io.BytesIO()
        mask.save(mask_bytes, format="PNG")
        context, error = proxy._prepare_image_edit_mask_context(
            source_image_content=_png_bytes((200, 100, 50, 255)),
            mask_content=mask_bytes.getvalue(),
        )
        self.assertIsNone(error)
        assert context is not None

        manifest = proxy._find_image_manifest(
            "image.edit", "qwen-image-2.1-inpaint"
        )
        self.assertTrue(proxy._manifest_uses_source_alpha_mask(manifest))
        prepared = proxy._apply_edit_mask_as_source_alpha(context)
        self.assertEqual([p[3] for p in _image_pixels(prepared)], [0, 255])

    async def test_default_qwen_inpaint_route_uploads_alpha_masked_source(self) -> None:
        from PIL import Image

        mask = Image.new("RGBA", (2, 1), (0, 0, 0, 255))
        mask.putpixel((0, 0), (0, 0, 0, 0))
        mask_bytes = io.BytesIO()
        mask.save(mask_bytes, format="PNG")
        request = _MultipartRequest(
            [
                ("prompt", "replace the left pixel"),
                ("image", _MultipartUpload("source.png", _png_bytes((255, 0, 0, 255)))),
                ("mask", _MultipartUpload("mask.png", mask_bytes.getvalue())),
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
            "resolve_image_background_decision",
            AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False)),
        ), patch.object(
            proxy,
            "_generate_images_with_adaptive_batching",
            fake_generate,
        ):
            response = await proxy.proxy_comfy_edit_request(
                request,  # type: ignore[arg-type]
                proxy.NO_AUTH_CONTEXT,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_FakeAsyncClient.uploads, 1)
        uploaded_source = _FakeAsyncClient.upload_files[0]["image"][1]
        self.assertEqual([p[3] for p in _image_pixels(uploaded_source)], [0, 255])
        self.assertIsNone(captured["image_transform"])
        self.assertEqual(captured["image_response_headers"]["X-Image-Mask"], "workflow")
        self.assertNotIn("mask_mode:post_composite", captured.get("warnings", []))

    async def test_unknown_edit_model_returns_model_not_found(self) -> None:
        request = _MultipartRequest(
            [
                ("prompt", "edit this image"),
                ("model", "no-such-edit-model"),
                (
                    "image",
                    _MultipartUpload("source.png", _png_bytes((0, 0, 0, 255))),
                ),
            ]
        )
        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy, "_service_availability_response", return_value=None
        ):
            response = await proxy.proxy_comfy_edit_request(
                request, proxy.NO_AUTH_CONTEXT  # type: ignore[arg-type]
            )

        self.assertEqual(response.status_code, 404)
        error = json.loads(response.body)["error"]
        self.assertEqual(error["code"], "model_not_found")
        self.assertEqual(error["param"], "model")

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

    async def test_sdk_multipart_bracket_extensions_are_bound(self) -> None:
        receipt, response, captured = await self._run_edit_with_manifest(
            extension_block={},
            extra_parts=[
                ("extensions[com.vividfish.workflow][controlnet_strength]", "0.75"),
            ],
            manifest=self._conditioning_manifest(
                inputs={
                    "prompt": {"type": "string", "required": True},
                    "source_image": {"type": "asset", "required": True},
                    "controlnet_strength": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
                mapping={
                    "prompt": [{"node": "6", "input": "text"}],
                    "source_image": [{"node": "10", "input": "image"}],
                    "controlnet_strength": [{"node": "44", "input": "strength"}],
                },
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            captured["base_workflow"]["44"]["inputs"]["strength"],
            0.75,
        )
        self.assertIn(
            "com.vividfish.workflow.controlnet_strength",
            receipt.accepted_inputs,
        )

    async def test_promptless_workflow_accepts_missing_prompt(self) -> None:
        manifest = {
            "schema_version": 1,
            "id": "image.edit.promptless",
            "intent": "image.edit",
            "version": "0.1.0",
            "inputs": {
                "source_image": {"type": "asset", "required": True},
                "scale": {"type": "number", "enum": [2, 4], "default": 2},
            },
            "outputs": {"image": {"media_type": "image/png"}},
            "bindings": {
                "comfyui": {
                    "workflow": "workflows/images/promptless.json",
                    "map": {
                        "source_image": [{"node": "10", "input": "image"}],
                        "scale": [{"node": "44", "input": "strength"}],
                    },
                }
            },
        }
        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )
        request = _MultipartRequest(
            [
                ("model", "promptless"),
                ("image", _MultipartUpload("source.png", _png_bytes((255, 0, 0, 255)))),
            ]
        )
        workflow = self._conditioning_workflow()
        captured: dict[str, Any] = {}

        async def fake_generate(**kwargs: Any) -> Response:
            captured.update(kwargs)
            generated_png = base64.b64encode(_png_bytes((1, 2, 3, 255))).decode("ascii")
            return Response(
                content=json.dumps({"created": 1, "data": [{"b64_json": generated_png}]}),
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

        self.assertEqual(response.status_code, 200, response.body.decode("utf-8"))
        self.assertEqual(captured["base_workflow"]["6"]["inputs"]["text"], "")
        self.assertEqual(
            receipt.ignored_inputs.get("prompt"),
            "workflow does not use prompt",
        )

    async def test_workflow_extension_asset_id_uploads_and_binds(self) -> None:
        with tempfile.TemporaryDirectory() as cache_dir, patch.object(
            proxy, "IMAGE_CACHE_DIR", Path(cache_dir)
        ):
            control_bytes = _png_bytes((0, 0, 0, 255))
            asset_id = proxy.store_image_bytes(control_bytes)

            receipt, response, captured = await self._run_edit_with_manifest(
                extension_block={
                    "control_image": {"asset_id": asset_id},
                    "controlnet_strength": 0.75,
                },
                manifest=self._conditioning_manifest(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(_FakeAsyncClient.uploads, 2)
        workflow_payload = captured["base_workflow"]
        self.assertEqual(workflow_payload["42"]["inputs"]["image"], "uploaded-2.png")
        uploaded_control = _FakeAsyncClient.upload_files[1]["image"]
        self.assertEqual(uploaded_control[1], control_bytes)
        self.assertIn(
            "com.vividfish.workflow.control_image",
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


class TestEditWorkflowSelection(unittest.TestCase):
    def test_configured_image_defaults_and_fallback_workflows(self) -> None:
        self.assertEqual(proxy.COMFY_DEFAULT_MODEL, "krea2-turbo")
        self.assertEqual(proxy.IMAGE_EDIT_DEFAULT_MODEL, "qwen-image-2.1-edit")
        self.assertEqual(
            proxy.IMAGE_VARIATIONS_DEFAULT_WORKFLOW, "qwen-image-2.1-edit"
        )
        self.assertIsNotNone(proxy.load_comfy_workflow("flux2-klein-4b"))
        self.assertIsNotNone(proxy.load_comfy_workflow("flux2-klein-4b-edit"))
        workflow = proxy.load_comfy_workflow("krea2-turbo")
        assert workflow is not None
        meta = workflow.pop("_meta")
        self.assertTrue(proxy.set_workflow_background_removal(workflow, meta, True))
        self.assertEqual(workflow["9"]["inputs"]["images"], ["10", 0])
        workflow = proxy.load_comfy_workflow("krea2-turbo")
        assert workflow is not None
        meta = workflow.pop("_meta")
        self.assertTrue(proxy.set_workflow_background_removal(workflow, meta, True))
        self.assertEqual(workflow["9"]["inputs"]["images"], ["10", 0])

    def test_default_edit_routes_by_input_shape(self) -> None:
        with (
            patch.object(proxy, "IMAGE_EDIT_DEFAULT_MODEL", "qwen-image-2.1-edit"),
            patch.object(
                proxy,
                "IMAGE_MULTIREF_EDIT_DEFAULT_WORKFLOW",
                "qwen-image-2.1-edit-multiref",
            ),
        ):
            self.assertEqual(
                proxy._select_edit_workflow(
                    "qwen-image-2.1-edit", image_count=1, has_mask=False
                ),
                "qwen-image-2.1-edit",
            )
            self.assertEqual(
                proxy._select_edit_workflow(
                    "qwen-image-2.1-edit", image_count=2, has_mask=False
                ),
                "qwen-image-2.1-edit-multiref",
            )
            self.assertEqual(
                proxy._select_edit_workflow(
                    "qwen-image-2.1-edit-multiref", image_count=2, has_mask=False
                ),
                "qwen-image-2.1-edit-multiref",
            )
            self.assertEqual(
                proxy._select_edit_workflow(
                    "qwen-image-2.1-edit", image_count=1, has_mask=True
                ),
                "qwen-image-2.1-inpaint",
            )

    def test_explicit_fallback_model_stays_selected_for_multi_image(self) -> None:
        with patch.object(proxy, "IMAGE_EDIT_DEFAULT_MODEL", "qwen-image-2.1-edit"):
            self.assertEqual(
                proxy._select_edit_workflow(
                    "flux2-klein-4b", image_count=2, has_mask=False
                ),
                "flux2-klein-4b-edit",
            )

    def test_explicit_direct_edit_workflow_is_not_given_an_edit_suffix(self) -> None:
        self.assertEqual(
            proxy._select_edit_workflow(
                "seedvr2-upscale", image_count=1, has_mask=False
            ),
            "seedvr2-upscale",
        )

    def test_multiref_manifest_declares_required_route_bound_image(self) -> None:
        manifest = proxy._find_image_manifest(
            "image.edit", "qwen-image-2.1-edit-multiref"
        )
        self.assertEqual(
            proxy._manifest_required_route_bound_inputs(manifest, "comfyui"),
            ("reference_image_2",),
        )


class TestProxyComfyEditRequestMultiImage(unittest.IsolatedAsyncioTestCase):
    """Multi-reference-image edit handling (proxy.py's `image[]` fix).

    Covers the bug described in the media architecture brief: the edit route
    used to read only the first multipart value per key (`form.get(...)`),
    silently dropping every additional `image[]` part. These tests exercise
    the fixed path via `form.multi_items()` collection plus the new
    `_meta.image_nodes` capacity declaration.
    """

    def _single_image_workflow(self) -> dict[str, Any]:
        return {
            "_meta": {
                "name": "single-image-fixture",
                "prompt_node": "6",
                "image_node": "10",
            },
            "6": {"inputs": {"text": ""}},
            "10": {"inputs": {"image": ""}},
        }

    def _multi_image_workflow(self) -> dict[str, Any]:
        return {
            "_meta": {
                "name": "multi-image-fixture",
                "prompt_node": "6",
                "image_nodes": ["10", "42"],
            },
            "6": {"inputs": {"text": ""}},
            "10": {"inputs": {"image": ""}},
            "42": {"inputs": {"image": ""}},
        }

    async def test_second_image_reaches_comfy_upload_not_dropped(self) -> None:
        """Regression: a second `image[]` part must actually be read.

        Before the fix, `form.get("image[]")` silently returned only the
        first upload and the second file was never even `.read()`. Asserting
        two uploads (not one) against a multi-slot workflow proves the second
        file is no longer dropped.
        """
        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )
        request = _MultipartRequest(
            [
                ("prompt", "combine these two references"),
                ("model", "flux2-klein-4b-edit"),
                ("image[]", _MultipartUpload("a.png", _png_bytes((255, 0, 0, 255)))),
                ("image[]", _MultipartUpload("b.png", _png_bytes((0, 255, 0, 255)))),
            ]
        )
        workflow = self._multi_image_workflow()
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
            lambda _intent, _model: None,
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
        # One upload per reference image (mask/extension uploads excluded here).
        self.assertEqual(_FakeAsyncClient.uploads, 2)
        bound_workflow = captured["base_workflow"]
        self.assertEqual(bound_workflow["10"]["inputs"]["image"], "uploaded-1.png")
        self.assertEqual(bound_workflow["42"]["inputs"]["image"], "uploaded-2.png")

    async def test_default_two_image_route_binds_real_multiref_manifest(self) -> None:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )
        request = _MultipartRequest(
            [
                ("prompt", "combine these two references"),
                ("image[]", _MultipartUpload("a.png", _png_bytes((255, 0, 0, 255)))),
                ("image[]", _MultipartUpload("b.png", _png_bytes((0, 255, 0, 255)))),
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
            "resolve_image_background_decision",
            AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False)),
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
        self.assertEqual(receipt.resolved_model, "qwen-image-2.1-edit-multiref")
        self.assertEqual(_FakeAsyncClient.uploads, 2)
        workflow = captured["base_workflow"]
        self.assertEqual(workflow["4"]["inputs"]["image"], "uploaded-1.png")
        self.assertEqual(workflow["11"]["inputs"]["image"], "uploaded-2.png")

    async def test_extra_images_rejected_with_clear_400_when_workflow_supports_one(
        self,
    ) -> None:
        """A workflow declaring only `image_node` (capacity 1) must reject,
        not silently drop, a second reference image."""
        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )
        request = _MultipartRequest(
            [
                ("prompt", "combine these two references"),
                ("model", "flux2-klein-4b-edit"),
                ("image[]", _MultipartUpload("a.png", _png_bytes((255, 0, 0, 255)))),
                ("image[]", _MultipartUpload("b.png", _png_bytes((0, 255, 0, 255)))),
            ]
        )
        workflow = self._single_image_workflow()

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
            lambda _intent, _model: None,
        ):
            response = await proxy.proxy_comfy_edit_request(
                request,  # type: ignore[arg-type]
                proxy.NO_AUTH_CONTEXT,
                receipt=receipt,
            )

        self.assertEqual(response.status_code, 400)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["code"], "too_many_images")
        self.assertIn("at most 1", body["error"]["message"])
        self.assertIn("image[]", receipt.rejected_inputs)
        self.assertEqual(receipt.rejected_inputs["image[]"], "too_many_images")

    async def test_single_image_still_works_without_regression(self) -> None:
        """A plain single-`image` edit (the common case) is unaffected."""
        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )
        request = _MultipartRequest(
            [
                ("prompt", "make it blue"),
                ("model", "flux2-klein-4b-edit"),
                ("image", _MultipartUpload("a.png", _png_bytes((255, 0, 0, 255)))),
            ]
        )
        workflow = self._single_image_workflow()
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
            lambda _intent, _model: None,
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
        self.assertEqual(_FakeAsyncClient.uploads, 1)
        self.assertEqual(captured["base_workflow"]["10"]["inputs"]["image"], "uploaded-1.png")


class TestJpegBackgroundAutoBypass(unittest.IsolatedAsyncioTestCase):
    """jpeg has no alpha channel: an "auto"/omitted background must never be
    resolved to removal for a jpeg request, or the response would advertise
    a transparency the encoded bytes can't carry."""

    def _workflow(self) -> dict[str, Any]:
        return {
            "_meta": {
                "name": "fixture",
                "prompt_node": "6",
                "size_node": "27",
                "background_removal": {
                    "save_node": "9",
                    "plain_image": ["8", 0],
                    "transparent_image": ["90", 0],
                    "rmbg_node": "90",
                },
            },
            "6": {"inputs": {"text": ""}},
            "27": {"inputs": {"width": 512, "height": 512}},
            "8": {"inputs": {}},
            "9": {"inputs": {"images": ["8", 0], "filename_prefix": ""}, "class_type": "SaveImage"},
            "90": {"inputs": {"image": ["8", 0]}},
        }

    async def test_jpeg_never_triggers_auto_background_removal(self) -> None:
        workflow = self._workflow()
        captured: dict[str, Any] = {}

        async def fake_generate(**kwargs: Any) -> Response:
            captured.update(kwargs)
            return Response(
                content=json.dumps({"created": 1, "data": []}),
                status_code=200,
                media_type="application/json",
            )

        class _StubRequest:
            async def json(self_inner) -> Any:
                return {"prompt": "a cat", "output_format": "jpeg"}

        decide_mock = AsyncMock(
            return_value=proxy.ImageBackgroundRemovalDecision(
                remove_background=True,
                confidence=0.9,
                reason="llm said remove",
                source="llm",
            )
        )

        with patch.object(proxy, "COMFY_ENABLED", True), patch.object(
            proxy, "configured_downstream_headers", lambda _a, _b: {}
        ), patch.object(
            proxy, "load_comfy_workflow", lambda _m: copy.deepcopy(workflow)
        ), patch.object(
            proxy, "_find_image_manifest", lambda _i, _m: None
        ), patch.object(
            proxy, "_generate_images_with_adaptive_batching", fake_generate
        ), patch.object(
            proxy, "decide_image_background_removal", decide_mock
        ):
            response = await proxy.proxy_comfy_request(
                _StubRequest(), proxy.NO_AUTH_CONTEXT  # type: ignore[arg-type]
            )

        self.assertEqual(response.status_code, 200)
        # The LLM-backed "auto" decision path must never even be consulted --
        # force_bypass short-circuits before it, for jpeg.
        decide_mock.assert_not_called()
        self.assertEqual(captured["output_background"], "opaque")


class TestQualityStepsWiring(unittest.IsolatedAsyncioTestCase):
    """`quality` -> sampler steps, only when a manifest declares `quality_steps`."""

    def _workflow(self) -> dict[str, Any]:
        return {
            "_meta": {"name": "quality-fixture", "prompt_node": "6", "image_node": "10"},
            "6": {"inputs": {"text": ""}},
            "10": {"inputs": {"image": ""}},
            "50": {"inputs": {"steps": 4}},
        }

    def _manifest(self, *, quality_steps: dict[str, int] | None) -> dict[str, Any]:
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "id": "image.edit.quality-fixture",
            "intent": "image.edit",
            "inputs": {
                "prompt": {"type": "string", "required": True},
                "source_image": {"type": "asset", "required": True},
                "steps": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "outputs": {"image": {"media_type": "image/png"}},
            "bindings": {
                "comfyui": {
                    "workflow": "workflows/images/quality-fixture.json",
                    "map": {
                        "prompt": [{"node": "6", "input": "text"}],
                        "source_image": [{"node": "10", "input": "image"}],
                        "steps": [{"node": "50", "input": "steps"}],
                    },
                }
            },
        }
        if quality_steps is not None:
            manifest["quality_steps"] = quality_steps
        return manifest

    async def _run(
        self, *, quality: str, manifest: dict[str, Any]
    ) -> tuple[RouteReceipt, Response, dict[str, Any]]:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )
        request = _MultipartRequest(
            [
                ("prompt", "sharpen the details"),
                ("model", "flux2-klein-4b-edit"),
                ("image", _MultipartUpload("source.png", _png_bytes((255, 0, 0, 255)))),
                ("quality", quality),
            ]
        )
        workflow = self._workflow()
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

    async def test_quality_maps_to_steps_when_manifest_declares_table(self) -> None:
        manifest = self._manifest(quality_steps={"low": 2, "medium": 4, "high": 12})
        receipt, response, captured = await self._run(quality="high", manifest=manifest)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["base_workflow"]["50"]["inputs"]["steps"], 12)

    async def test_quality_auto_leaves_steps_untouched(self) -> None:
        manifest = self._manifest(quality_steps={"low": 2, "medium": 4, "high": 12})
        receipt, response, captured = await self._run(quality="auto", manifest=manifest)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["base_workflow"]["50"]["inputs"]["steps"], 4)

    async def test_quality_without_manifest_table_is_recorded_not_applied(self) -> None:
        manifest = self._manifest(quality_steps=None)
        receipt, response, captured = await self._run(quality="high", manifest=manifest)
        self.assertEqual(response.status_code, 200)
        # No silent lie: quality is accepted (no 400) but the workflow's steps
        # value is untouched, and the receipt records *why* via ignore_input.
        self.assertEqual(captured["base_workflow"]["50"]["inputs"]["steps"], 4)
        self.assertIn("quality", receipt.ignored_inputs)


class TestProxyComfyEditRequestOutputShape(unittest.IsolatedAsyncioTestCase):
    """output_format/output_compression/quality validation on /v1/images/edits.

    These all fail before any ComfyUI upload, so no COMFY_ENABLED patch or
    workflow fixture is needed beyond enabling the route.
    """

    async def _run(self, extra_fields: list[tuple[str, Any]]) -> tuple[RouteReceipt, Response]:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/edits", proxy.NO_AUTH_CONTEXT
        )
        request = _MultipartRequest([("model", "flux2-klein-4b-edit"), *extra_fields])
        with patch.object(proxy, "COMFY_ENABLED", True):
            response = await proxy.proxy_comfy_edit_request(
                request,  # type: ignore[arg-type]
                proxy.NO_AUTH_CONTEXT,
                receipt=receipt,
            )
        return receipt, response

    async def test_bad_output_format_rejected(self) -> None:
        receipt, response = await self._run([("output_format", "bmp")])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"]["param"], "output_format")

    async def test_bad_output_compression_rejected(self) -> None:
        receipt, response = await self._run(
            [("output_format", "webp"), ("output_compression", "300")]
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"]["param"], "output_compression")

    async def test_bad_quality_rejected(self) -> None:
        receipt, response = await self._run([("quality", "ultra")])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"]["param"], "quality")

    async def test_transparent_background_with_jpeg_rejected(self) -> None:
        receipt, response = await self._run(
            [("background", "transparent"), ("output_format", "jpeg")]
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"]["param"], "background")


class TestProxyComfyVariationsRequest(unittest.IsolatedAsyncioTestCase):
    """`/v1/images/variations` -- routed through an edit-capable workflow with
    a fixed generic prompt, since OpenAI's variations request has no prompt."""

    def _edit_workflow(self) -> dict[str, Any]:
        return {
            "_meta": {
                "name": "flux2-klein-4b-edit",
                "prompt_node": "6",
                "image_node": "10",
            },
            "6": {"inputs": {"text": ""}},
            "10": {"inputs": {"image": ""}},
            "49": {"inputs": {"denoise": 1.0}},
        }

    def _edit_manifest(self, *, with_denoise: bool) -> dict[str, Any]:
        inputs: dict[str, Any] = {
            "prompt": {"type": "string", "required": True},
            "source_image": {"type": "asset", "required": True},
        }
        mapping: dict[str, Any] = {
            "prompt": [{"node": "6", "input": "text"}],
            "source_image": [{"node": "10", "input": "image"}],
        }
        if with_denoise:
            inputs["denoise"] = {"type": "number", "minimum": 0, "maximum": 1}
            mapping["denoise"] = [{"node": "49", "input": "denoise"}]
        return {
            "schema_version": 1,
            "id": "image.edit.flux2-klein-4b",
            "intent": "image.edit",
            "inputs": inputs,
            "outputs": {"image": {"media_type": "image/png"}},
            "bindings": {"comfyui": {"workflow": "workflows/images/flux2-klein-4b-edit.json", "map": mapping}},
        }

    async def _run(
        self,
        *,
        fields: list[tuple[str, Any]],
        manifest: dict[str, Any] | None,
    ) -> tuple[RouteReceipt, Response, dict[str, Any]]:
        receipt = proxy.start_image_route_receipt(
            "/v1/images/variations", proxy.NO_AUTH_CONTEXT
        )
        request = _MultipartRequest(fields)
        workflow = self._edit_workflow()
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
            response = await proxy.proxy_comfy_variations_request(
                request,  # type: ignore[arg-type]
                proxy.NO_AUTH_CONTEXT,
                receipt=receipt,
            )
        return receipt, response, captured

    async def test_unknown_field_is_rejected(self) -> None:
        # `prompt` is a valid /v1/images/edits field but not a valid
        # /v1/images/variations field -- OpenAI itself rejects it there.
        receipt, response, _ = await self._run(
            fields=[
                ("image", _MultipartUpload("a.png", _png_bytes((1, 2, 3, 255)))),
                ("prompt", "please do X"),
            ],
            manifest=self._edit_manifest(with_denoise=True),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(json.loads(response.body)["error"]["param"], "prompt")

    async def test_missing_image_is_rejected(self) -> None:
        receipt, response, _ = await self._run(
            fields=[], manifest=self._edit_manifest(with_denoise=True)
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(receipt.rejected_inputs.get("image"), "missing")

    async def test_multiple_images_rejected_with_clear_error(self) -> None:
        receipt, response, _ = await self._run(
            fields=[
                ("image", _MultipartUpload("a.png", _png_bytes((1, 2, 3, 255)))),
                ("image", _MultipartUpload("b.png", _png_bytes((4, 5, 6, 255)))),
            ],
            manifest=self._edit_manifest(with_denoise=True),
        )
        self.assertEqual(response.status_code, 400)
        body = json.loads(response.body)
        self.assertEqual(body["error"]["code"], "too_many_images")
        self.assertEqual(receipt.rejected_inputs.get("image"), "too_many_images")

    async def test_success_injects_fixed_prompt_and_default_workflow(self) -> None:
        receipt, response, captured = await self._run(
            fields=[("image", _MultipartUpload("a.png", _png_bytes((1, 2, 3, 255))))],
            manifest=self._edit_manifest(with_denoise=False),
        )
        self.assertEqual(response.status_code, 200)
        workflow_payload = captured["base_workflow"]
        self.assertEqual(
            workflow_payload["6"]["inputs"]["text"], proxy.IMAGE_VARIATIONS_PROMPT
        )
        self.assertEqual(workflow_payload["10"]["inputs"]["image"], "uploaded-1.png")
        # No model sent by the client -- nothing to preserve as "requested".
        self.assertIsNone(receipt.requested_model)
        self.assertEqual(
            receipt.metadata["variations_delegated_workflow"],
            proxy.IMAGE_VARIATIONS_DEFAULT_WORKFLOW,
        )

    async def test_denoise_applied_only_when_manifest_declares_it(self) -> None:
        receipt, response, captured = await self._run(
            fields=[("image", _MultipartUpload("a.png", _png_bytes((1, 2, 3, 255))))],
            manifest=self._edit_manifest(with_denoise=True),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            captured["base_workflow"]["49"]["inputs"]["denoise"],
            proxy.IMAGE_VARIATIONS_DENOISE,
        )

    async def test_denoise_not_injected_when_manifest_lacks_it(self) -> None:
        receipt, response, captured = await self._run(
            fields=[("image", _MultipartUpload("a.png", _png_bytes((1, 2, 3, 255))))],
            manifest=self._edit_manifest(with_denoise=False),
        )
        self.assertEqual(response.status_code, 200)
        # Workflow's own default (1.0) stands -- no unsupported-extension 400.
        self.assertEqual(captured["base_workflow"]["49"]["inputs"]["denoise"], 1.0)

    async def test_configured_default_workflow_is_honored(self) -> None:
        with patch.object(proxy, "IMAGE_VARIATIONS_DEFAULT_WORKFLOW", "flux2-klein-4b-inpaint"):
            receipt, response, _ = await self._run(
                fields=[("image", _MultipartUpload("a.png", _png_bytes((1, 2, 3, 255))))],
                manifest=self._edit_manifest(with_denoise=False),
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(receipt.requested_model)
        self.assertEqual(
            receipt.metadata["variations_delegated_workflow"], "flux2-klein-4b-inpaint"
        )

    async def test_openai_model_alias_routes_to_configured_default(self) -> None:
        """A real OpenAI client's `model=dall-e-2` (the historical -- and for
        many SDK versions, default -- variations model) must resolve to the
        configured variations workflow, not a literal (nonexistent)
        `dall-e-2-edit` local workflow. The receipt must keep recording what
        the client actually asked for (`dall-e-2`), not silently relabel it
        as a request for the internal delegated workflow."""
        receipt, response, _ = await self._run(
            fields=[
                ("image", _MultipartUpload("a.png", _png_bytes((1, 2, 3, 255)))),
                ("model", "dall-e-2"),
            ],
            manifest=self._edit_manifest(with_denoise=False),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(receipt.requested_model, "dall-e-2")
        self.assertEqual(
            receipt.metadata["variations_delegated_workflow"],
            proxy.IMAGE_VARIATIONS_DEFAULT_WORKFLOW,
        )

    async def test_concrete_local_workflow_name_is_honored_literally(self) -> None:
        receipt, response, _ = await self._run(
            fields=[
                ("image", _MultipartUpload("a.png", _png_bytes((1, 2, 3, 255)))),
                ("model", "flux2-klein-4b"),
            ],
            manifest=self._edit_manifest(with_denoise=False),
        )
        self.assertEqual(response.status_code, 200)
        # The client's own model string is preserved as requested_model...
        self.assertEqual(receipt.requested_model, "flux2-klein-4b")
        # ...while the actually-delegated (suffixed) workflow is recorded
        # separately, not conflated with what the client asked for.
        self.assertEqual(
            receipt.metadata["variations_delegated_workflow"], "flux2-klein-4b-edit"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
