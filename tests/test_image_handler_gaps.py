"""Proposed offline regressions for image handlers at a71e3e5.
Run only with the repository hermetic runner after including this file in
the test Git tree. This proposal has not been executed.
"""
import asyncio
import copy
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import httpx
import proxy
from gateway.receipts import RouteReceipt
from test_image_route_receipts import _MultipartRequest, _MultipartUpload, _png_bytes


class FakeUploadClient:
    replies = []
    calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def post(self, url, **kwargs):
        assert url.endswith("/upload/image"), url
        type(self).calls.append((url, kwargs))
        if type(self).replies:
            result = type(self).replies.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        return httpx.Response(200, json={"name": "uploaded.png"})


class ImageHandlerGapTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.workflow = {
            "_meta": {
                "name": "fixture-edit", "image_node": "i",
                "prompt_node": "p", "seed_node": "s",
            },
            "i": {"inputs": {"image": ""}},
            "p": {"inputs": {"text": ""}},
            "s": {"inputs": {"noise_seed": 0}},
        }
        self.manifest = {
            "schema_version": 1,
            "id": "image.edit.fixture",
            "intent": "image.edit",
            "inputs": {
                "prompt": {"type": "string"},
                "source_image": {"type": "asset"},
                "seed": {"type": "integer", "minimum": 0},
            },
            "outputs": {"image": {"media_type": "image/png"}},
            "bindings": {"comfyui": {
                "workflow": "fixtures/image.json",
                "map": {
                    "prompt": [{"node": "p", "input": "text"}],
                    "source_image": [{"node": "i", "input": "image"}],
                    "seed": [{"node": "s", "input": "noise_seed"}],
                },
            }},
        }
        for name, value in (
            ("COMFY_ENABLED", True),
            ("_dispatch_admission_response", Mock(return_value=None)),
            ("_service_availability_response", Mock(return_value=None)),
            ("configured_downstream_headers", Mock(return_value={})),
            ("resolve_model_for_operation",
             Mock(return_value=SimpleNamespace(is_openai_alias=False))),
            ("_select_edit_workflow", lambda model, **kwargs: model),
            ("_find_image_manifest", Mock(return_value=self.manifest)),
            ("load_comfy_workflow", Mock(return_value=self.workflow)),
            ("_workflow_target_policy_decision", Mock(return_value=None)),
            ("resolve_image_background_decision",
             AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False))),
            ("get_vram_estimate", Mock(return_value=100)),
            ("comfy_free_vram_bytes", AsyncMock(return_value=10**12)),
            ("compute_max_batch", Mock(return_value=1)),
            ("IMAGE_FREE_VRAM_ON_EXHAUSTION", True),
        ):
            self.stack.enter_context(patch.object(proxy, name, value))
        self.stack.enter_context(patch.object(proxy.httpx, "AsyncClient", FakeUploadClient))
        self.stack.enter_context(patch.object(proxy.media_delivery, "current", return_value=None))
        self.stack.enter_context(patch.object(proxy.random, "randint", return_value=999))
        self.stack.enter_context(patch.object(proxy, "_build_image_response_payload",
                                              side_effect=self.payload))
        FakeUploadClient.calls = []
        FakeUploadClient.replies = []

    @staticmethod
    def payload(request, images, response_format, warning_headers, **kwargs):
        # Deliberately stub persistence/encoding; those have separate byte tests.
        return proxy.Response(
            json.dumps({"count": len(images), "format": response_format}),
            status_code=200, media_type="application/json", headers=warning_headers,
        )

    def edit_request(self, *, n=1, seed=None, extra_image=False, mask=False):
        items = [
            ("model", "fixture-edit"), ("prompt", "change the color"),
            ("n", str(n)),
            ("image", _MultipartUpload("source.png", _png_bytes((1, 2, 3, 255)))),
        ]
        if seed is not None:
            items.append(("extensions", json.dumps({"com.azizam.workflow": {"seed": seed}})))
        if extra_image:
            items.append(("image[]", _MultipartUpload("second.png", _png_bytes((4, 5, 6, 255)))))
        if mask:
            items.append(("mask", _MultipartUpload("mask.png", _png_bytes((0, 0, 0, 0)))))
        return _MultipartRequest(items)

    async def batch(self, **overrides):
        kwargs = dict(
            request=SimpleNamespace(), client=object(),
            base_workflow={"s": {"inputs": {"seed": 0}}, "b": {"inputs": {"batch_size": 1}}},
            meta={"batch_size_node": "b", "batch_size_field": "batch_size"},
            seed_node="s", seed_field="seed", workflow_name="fixture",
            width=512, height=512, n_requested=2, response_format="b64_json",
            headers={}, fixed_seed=41,
        )
        kwargs.update(overrides)
        return await proxy._generate_images_with_adaptive_batching(**kwargs)

    async def test_real_oom_retry_is_single_image_same_seed_and_only_once(self):
        oom = proxy.Response(
            json.dumps({"error": {"message": "CUDA out of memory"}}),
            status_code=502, media_type="application/json",
        )
        seen = []
        outcomes = [(oom, []), (None, [b"a"]), (None, [b"b"])]

        async def run(client, workflow, *args, **kwargs):
            seen.append(copy.deepcopy(workflow))
            return outcomes.pop(0)

        free = AsyncMock(return_value=True)
        with patch.object(proxy, "_comfy_run_round", side_effect=run), patch.object(
            proxy, "_free_comfy_models_if_idle", free
        ), patch.object(proxy, "compute_max_batch", return_value=2):
            response = await self.batch()
        self.assertEqual(response.status_code, 200, response.body)
        self.assertEqual([w["b"]["inputs"]["batch_size"] for w in seen], [2, 1, 1])
        self.assertEqual([w["s"]["inputs"]["seed"] for w in seen], [41, 41, 42])
        free.assert_awaited_once()
        self.assertEqual(response.headers["X-ComfyUI-Models-Freed"], "true")
        self.assertEqual(response.headers["X-Image-Returned"], "2")

    async def test_repeated_oom_or_busy_queue_does_not_loop(self):
        for can_free, expected_rounds in ((True, 2), (False, 1)):
            with self.subTest(can_free=can_free):
                oom = proxy.Response(
                    json.dumps({"error": {"message": "CUDA out of memory"}}),
                    status_code=502, media_type="application/json",
                )
                run = AsyncMock(return_value=(oom, []))
                free = AsyncMock(return_value=can_free)
                with patch.object(proxy, "_comfy_run_round", run), patch.object(
                    proxy, "_free_comfy_models_if_idle", free
                ):
                    response = await self.batch()
                self.assertIs(response, oom)
                self.assertEqual(run.await_count, expected_rounds)
                free.assert_awaited_once()

    async def test_empty_and_partial_results_are_bounded_and_honestly_reported(self):
        for first, expected_status, expected_rounds in (
            ([], 502, 2), ([b"one"], 200, 4),
        ):
            with self.subTest(first=first):
                run = AsyncMock(side_effect=[(None, first)] + [(None, [])] * 4)
                with patch.object(proxy, "_comfy_run_round", run):
                    response = await self.batch()
                self.assertEqual(response.status_code, expected_status, response.body)
                self.assertEqual(run.await_count, expected_rounds)
                if expected_status == 200:
                    self.assertEqual(response.headers["X-Image-Returned"], "1")
                    self.assertIn("requested n=2 but produced 1", response.headers["X-Proxy-Warning"])

    async def test_failed_upload_never_submits_a_generation(self):
        cases = [
            (httpx.Response(503, text="offline"), 502, "Image upload failed"),
            (httpx.Response(200, json={}), 502, "No filename returned"),
            (httpx.ReadTimeout("slow"), 504, "timeout"),
            (httpx.ConnectError("offline"), 502, "unavailable"),
        ]
        for reply, status, message in cases:
            with self.subTest(status=status, message=message):
                FakeUploadClient.calls = []
                FakeUploadClient.replies = [reply]
                generate = AsyncMock()
                with patch.object(proxy, "_generate_images_with_adaptive_batching", generate):
                    response = await proxy.proxy_comfy_edit_request(
                        self.edit_request(), proxy.NO_AUTH_CONTEXT,
                    )
                self.assertEqual(response.status_code, status, response.body)
                self.assertIn(message, json.loads(response.body)["error"]["message"])
                self.assertEqual(len(FakeUploadClient.calls), 1)
                generate.assert_not_awaited()

    async def test_second_reference_upload_failure_does_not_generate(self):
        self.workflow["_meta"].pop("image_node")
        self.workflow["_meta"]["image_nodes"] = ["i", "j"]
        self.workflow["j"] = {"inputs": {"image": ""}}
        for second, message in (
            (httpx.Response(503, text="second upload failed"), "second upload failed"),
            (httpx.Response(200, json={}), "No filename returned"),
        ):
            with self.subTest(message=message):
                FakeUploadClient.calls = []
                FakeUploadClient.replies = [
                    httpx.Response(200, json={"name": "first.png"}), second,
                ]
                generate = AsyncMock()
                with patch.object(proxy, "_generate_images_with_adaptive_batching", generate):
                    response = await proxy.proxy_comfy_edit_request(
                        self.edit_request(extra_image=True), proxy.NO_AUTH_CONTEXT,
                    )
                self.assertEqual(response.status_code, 502, response.body)
                self.assertIn(message, json.loads(response.body)["error"]["message"])
                self.assertEqual(len(FakeUploadClient.calls), 2)
                generate.assert_not_awaited()

    async def test_concurrent_edits_keep_prompt_and_template_isolated(self):
        both_started = asyncio.Event()
        seen = []

        async def run(client, workflow, *args, **kwargs):
            seen.append(workflow)
            if len(seen) == 2:
                both_started.set()
            await both_started.wait()
            return None, [b"fake"]

        left = self.edit_request()
        right = self.edit_request()
        left._form._items = [(k, "LEFT" if k == "prompt" else v)
                            for k, v in left._form._items]
        right._form._items = [(k, "RIGHT" if k == "prompt" else v)
                             for k, v in right._form._items]
        before = copy.deepcopy(self.workflow)
        with patch.object(proxy, "_comfy_run_round", side_effect=run):
            responses = await asyncio.wait_for(asyncio.gather(
                proxy.proxy_comfy_edit_request(left, proxy.NO_AUTH_CONTEXT),
                proxy.proxy_comfy_edit_request(right, proxy.NO_AUTH_CONTEXT),
            ), timeout=2)
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(sorted(w["p"]["inputs"]["text"] for w in seen), ["LEFT", "RIGHT"])
        self.assertIsNot(seen[0], seen[1])
        self.assertEqual(self.workflow, before)


class ImageRoundCancellationGapTests(IsolatedAsyncioTestCase):
    async def test_disconnect_cancels_only_its_prompt_and_finalizes_probe(self):
        calls = []

        def respond(request):
            calls.append(request.url.path)
            if request.url.path == "/prompt":
                return httpx.Response(200, json={"prompt_id": "mine"})
            if request.url.path == "/api/jobs/mine/cancel":
                return httpx.Response(200, json={})
            raise AssertionError("Unexpected backend operation: " + request.url.path)

        probe = SimpleNamespace(start=Mock(), stop=AsyncMock())
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
        with patch.object(proxy, "_build_comfy_submit_payload", side_effect=lambda w: {"prompt": w}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                error, images = await proxy._comfy_run_round(
                    client, {}, {}, "disconnect", request=request, vram_probe=probe,
                )
        self.assertEqual(error.status_code, 499)
        self.assertEqual(json.loads(error.body)["error"]["code"], "image_client_disconnected")
        self.assertEqual(images, [])
        self.assertEqual(calls, ["/prompt", "/api/jobs/mine/cancel"])
        probe.start.assert_called_once()
        probe.stop.assert_awaited_once()

    async def test_task_cancellation_preserves_cancel_even_if_backend_cancel_fails(self):
        for cancel_status in (200, 503):
            with self.subTest(cancel_status=cancel_status):
                entered = asyncio.Event()
                calls = []

                async def respond(request):
                    calls.append(request.url.path)
                    if request.url.path == "/prompt":
                        return httpx.Response(200, json={"prompt_id": "mine"})
                    if request.url.path == "/history/mine":
                        entered.set()
                        await asyncio.Event().wait()
                    if request.url.path == "/api/jobs/mine/cancel":
                        return httpx.Response(cancel_status, json={})
                    raise AssertionError(request.url.path)

                probe = SimpleNamespace(start=Mock(), stop=AsyncMock())
                with patch.object(proxy, "_build_comfy_submit_payload", side_effect=lambda w: {"prompt": w}):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                        task = asyncio.create_task(proxy._comfy_run_round(
                            client, {}, {}, "cancel", vram_probe=probe,
                        ))
                        try:
                            await asyncio.wait_for(entered.wait(), timeout=2)
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                        finally:
                            if not task.done():
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                self.assertEqual(calls, ["/prompt", "/history/mine", "/api/jobs/mine/cancel"])
                probe.stop.assert_awaited_once()

