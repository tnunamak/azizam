"""Regressions for parameter validation before media backend effects."""

import copy
import json
import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import httpx
from openai import AsyncOpenAI
import proxy
from test_audio_media_job_integration import _StubJsonRequest
from test_image_route_receipts import (
    _FakeAsyncClient, _MultipartRequest, _MultipartUpload, _png_bytes,
)
from test_sync_media_routes import _wav_bytes


class _AudioClient(_FakeAsyncClient):
    submissions = []

    async def post(self, url, **kwargs):
        self.submissions.append(kwargs["json"])
        return httpx.Response(200, json={"prompt_id": "test-audio"})

    async def get(self, url, **kwargs):
        if "/history/" in url:
            return httpx.Response(200, json={"test-audio": {
                "status": {"status_str": "success"},
                "outputs": {"1": {"audio": [{"filename": "test.wav"}]}},
            }})
        return httpx.Response(200, content=_wav_bytes(), headers={"content-type": "audio/wav"})


class MediaContractValidationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name, value in (
            ("COMFY_ENABLED", True),
            ("_service_availability_response", lambda *a, **kw: None),
            ("configured_downstream_headers", lambda *a: {}),
        ):
            self.stack.enter_context(patch.object(proxy, name, value))
        self.stack.enter_context(patch.object(proxy.httpx, "AsyncClient", _FakeAsyncClient))
        _FakeAsyncClient.uploads = 0
        _FakeAsyncClient.upload_files = []

    def assert_invalid(self, response, param):
        self.assertEqual(response.status_code, 400, response.body)
        error = json.loads(response.body)["error"]
        self.assertEqual(set(error), {"message", "type", "param", "code"})
        self.assertTrue(isinstance(error["message"], str) and error["message"])
        self.assertEqual(error["type"], "invalid_request_error")
        self.assertEqual(error["param"], param)
        self.assertEqual(_FakeAsyncClient.uploads, 0)

    async def test_http3_02_generation_n_is_a_bounded_integer(self):
        for value in (True, False, 1.9, 1.0, float("inf"), float("nan"), "1", None, 0, 11):
            with self.subTest(n=value):
                response = await proxy.proxy_comfy_request(
                    _StubJsonRequest({"prompt": "a tree", "n": value}), proxy.NO_AUTH_CONTEXT,
                )
                self.assert_invalid(response, "n")

    async def test_generation_accepts_integer_count_boundaries(self):
        generate = AsyncMock(return_value=proxy.Response(status_code=200))
        with patch.object(proxy, "_generate_images_with_adaptive_batching", generate), patch.object(
            proxy, "resolve_image_background_decision",
            AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False)),
        ):
            for value in (1, 10):
                response = await proxy.proxy_comfy_request(
                    _StubJsonRequest({"model": "flux2-klein-4b", "prompt": "a tree", "n": value}),
                    proxy.NO_AUTH_CONTEXT,
                )
                self.assertEqual(response.status_code, 200, response.body)
                self.assertEqual(generate.call_args.kwargs["n_requested"], value)

    async def edit(self, *, size=None, count=1, variations=False):
        items = [("model", "flux2-klein-4b-edit")]
        if not variations:
            items.append(("prompt", "a tree"))
        if size is not None:
            items.append(("size", size))
        items.extend(("image[]", _MultipartUpload("source.png", _png_bytes((1, 2, 3, 255)))) for _ in range(count))
        handler = proxy.proxy_comfy_variations_request if variations else proxy.proxy_comfy_edit_request
        return await handler(_MultipartRequest(items), proxy.NO_AUTH_CONTEXT)

    async def test_http3_03_edit_and_variations_reject_invalid_sizes(self):
        for variations in (False, True):
            for size in ("garbage", "0x0", "-1x512", "512x0", "1.5x512", "", "512", "512x512x512"):
                with self.subTest(size=size, variations=variations):
                    self.assert_invalid(await self.edit(size=size, variations=variations), "size")

    async def test_edit_enforces_manifest_dimension_limits(self):
        for size in ("1x512", "512x1", "2049x512", "512x2049"):
            with self.subTest(size=size):
                self.assert_invalid(await self.edit(size=size), "size")

    async def test_edit_omitted_auto_and_valid_size_reach_generation(self):
        generate = AsyncMock(return_value=proxy.Response(status_code=200))
        with patch.object(proxy, "_generate_images_with_adaptive_batching", generate), patch.object(
            proxy, "resolve_image_background_decision",
            AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False)),
        ):
            for size in (None, "auto", "512x768", "512X768", " 512 x 768 "):
                with self.subTest(size=size):
                    response = await self.edit(size=size)
                    self.assertEqual(response.status_code, 200, response.body)
                    dimensions = (generate.call_args.kwargs["width"], generate.call_args.kwargs["height"])
                    self.assertEqual(dimensions, (2, 1) if size in (None, "auto") else (512, 768))

    async def test_http3_04_excess_images_do_not_upload(self):
        workflow = {"_meta": {"image_node": "1"}, "1": {"inputs": {"image": ""}}}
        with patch.object(proxy, "_select_edit_workflow", lambda model, **kw: model), patch.object(
            proxy, "load_comfy_workflow", return_value=workflow,
        ), patch.object(proxy, "_find_image_manifest", return_value=None):
            response = await self.edit(count=2)
        self.assert_invalid(response, "image[]")
        self.assertEqual(json.loads(response.body)["error"]["code"], "too_many_images")

    async def test_http3_04_missing_required_mask_does_not_upload(self):
        with patch.object(proxy, "_manifest_required_route_mask_input", return_value="source_mask"):
            response = await self.edit()
        self.assert_invalid(response, "mask")

    async def audio(self, **parameters):
        cfg = {"default_duration": 8, "min_seconds": 1, "max_seconds": 30}
        workflow = {"_meta": {"seed_node": "1", "duration_node": "1"}, "1": {"inputs": {"seed": 0, "duration": 8}}}
        with patch.object(proxy, "_resolve_audio_workflow", return_value=("test-audio", cfg)), patch.object(
            proxy, "_load_audio_workflow_file", return_value=copy.deepcopy(workflow),
        ), patch.object(proxy, "_load_audio_workflow_manifest", return_value=None):
            return await proxy.proxy_audio_generate(
                _StubJsonRequest({"prompt": "a tone", **parameters}), proxy.NO_AUTH_CONTEXT,
            )

    async def test_http3_05_audio_rejects_requested_encoding(self):
        for value in ("mp3", "wav", "flac", None, 42):
            with self.subTest(response_format=value):
                self.assert_invalid(await self.audio(response_format=value), "response_format")

    async def test_http3_06_audio_type_is_a_supported_string(self):
        for value in (None, 1, True, [], "speech", "Music", "music\r\n"):
            with self.subTest(type=value):
                self.assert_invalid(await self.audio(type=value), "type")

    async def test_http3_07_audio_duration_must_be_finite(self):
        for value in ("NaN", "Infinity", "-Infinity", float("nan"), float("inf")):
            with self.subTest(duration=value):
                self.assert_invalid(await self.audio(duration=value), "duration")

    async def test_http3_08_invalid_explicit_seed_is_not_randomized(self):
        for value in ("abc", None, True, 1.5, "1", -1, 2**32, float("inf")):
            with self.subTest(seed=value), patch.object(proxy.random, "randint") as randomize:
                self.assert_invalid(await self.audio(seed=value), "seed")
                randomize.assert_not_called()

    async def test_audio_native_success_and_seed_boundaries(self):
        _AudioClient.submissions = []
        with patch.object(proxy.httpx, "AsyncClient", _AudioClient), patch.object(
            proxy.asyncio, "sleep", AsyncMock(),
        ), patch.object(proxy.media_delivery, "attach_provenance", side_effect=lambda data, *a, **kw: data), patch.object(
            proxy.media_delivery, "deliver_bytes", side_effect=lambda data, *a, **kw: data,
        ), patch.object(proxy.random, "randint", return_value=123) as randomize:
            for audio_type in ("music", "sfx", "ambient"):
                for seed in (0, 2**32 - 1):
                    response = await self.audio(type=audio_type, seed=seed, response_format="native")
                    self.assertEqual(response.status_code, 200, response.body)
                    self.assertEqual(response.headers["content-type"], "audio/wav")
                    self.assertEqual(response.headers["x-audio-type"], audio_type)
                    self.assertEqual(response.body, _wav_bytes())
                    self.assertEqual(_AudioClient.submissions[-1]["prompt"]["1"]["inputs"]["seed"], seed)
            randomize.assert_not_called()
            response = await self.audio()
            self.assertEqual(response.status_code, 200, response.body)
            randomize.assert_called_once_with(0, 2**32 - 1)
            self.assertEqual(_AudioClient.submissions[-1]["prompt"]["1"]["inputs"]["seed"], 123)


class ModelRetrieveContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_http3_01_retrieve_for_both_openai_bases_and_prefixed_ids(self):
        entries = [{"id": name, "object": "model", "created": 1, "owned_by": "azizam"}
                   for name in ("test-model", "typesafe/jev-latest")]
        listed = proxy.Response(content=json.dumps({"data": entries}), media_type="application/json")
        with patch.object(proxy, "list_models", AsyncMock(return_value=listed)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://azizam.test") as client:
                for base in ("/v1", "/openai/v1"):
                    for entry in entries:
                        with self.subTest(base=base, model=entry["id"]):
                            response = await client.get(f"{base}/models/{entry['id']}")
                            self.assertEqual(response.status_code, 200, response.text)
                            self.assertEqual(response.json(), entry)
                for base in ("/v1", "/openai/v1"):
                    sdk = AsyncOpenAI(api_key="test", base_url=f"http://azizam.test{base}", http_client=client)
                    for entry in entries:
                        model = await sdk.models.retrieve(entry["id"])
                        self.assertEqual(model.id, entry["id"])
        routes = {route.path: route.endpoint for route in proxy.app.routes}
        self.assertIs(routes["/openai/v1/models/{model_id:path}"], routes["/v1/models/{model_id:path}"])


class OpenAIErrorEnvelopeTests(unittest.TestCase):
    def test_every_error_carries_all_four_keys(self):
        error = json.loads(proxy.openai_error_response("bad").body)["error"]
        self.assertEqual(error, {"message": "bad", "type": "invalid_request_error", "param": None, "code": None})
        error = json.loads(proxy.openai_error_response("bad", param="n", code="x").body)["error"]
        self.assertEqual((error["param"], error["code"]), ("n", "x"))
