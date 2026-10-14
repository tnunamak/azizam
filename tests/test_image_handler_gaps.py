"""Offline regressions for image handlers at a71e3e5.

The ComfyUI upload, submit, history and cancel calls are served by the FK
comfyui fake (tests/support/fake_upstream/scenarios/comfyui/); the gateway
code and its httpx client are real. Run only with the repository hermetic runner.
"""
import asyncio
import copy
import json
import socket
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import httpx
import proxy
import pytest
from gateway.receipts import RouteReceipt
from test_image_route_receipts import _MultipartRequest, _MultipartUpload, _png_bytes
from tests.support.fake_upstream import fake_upstreams, load_scenario, read_deadline  # noqa: F401

HANG_BOUND_SECONDS = 10


@pytest.fixture
def serve_comfy(fake_upstreams, monkeypatch):
    """Serve a comfyui scenario as the gateway's ComfyUI host; ``wire`` uses a real local port."""

    def serve(name, *, wire=False, **values):
        scenario = load_scenario(f"comfyui/{name}", **values)
        fake = fake_upstreams.loopback(scenario) if wire else fake_upstreams.asgi(scenario)
        monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)
        return fake

    serve.fakes = fake_upstreams
    serve.monkeypatch = monkeypatch
    return serve


def _refused_comfy_host(monkeypatch):
    """A local port that was just free: the connection is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        host = f"http://127.0.0.1:{probe.getsockname()[1]}"
    monkeypatch.setattr(proxy, "COMFY_HOST", host)


class ImageHandlerGapTests(IsolatedAsyncioTestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, serve_comfy):
        self.serve = serve_comfy

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
        self.stack.enter_context(patch.object(proxy.media_delivery, "current", return_value=None))
        self.stack.enter_context(patch.object(proxy.random, "randint", return_value=999))
        self.stack.enter_context(patch.object(proxy, "_build_image_response_payload",
                                              side_effect=self.payload))

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
            ("upload-text-reply", {"status": 503, "text": "offline"}, 502, "Image upload failed"),
            ("upload-json-reply", {"status": 200, "reply": {}}, 502, "No filename returned"),
            ("upload-slow-first-byte", {"wire": True}, 504, "timeout"),
            (None, {}, 502, "unavailable"),
        ]
        for scenario, values, status, message in cases:
            with self.subTest(status=status, message=message):
                fake = None
                if scenario is None:
                    _refused_comfy_host(self.serve.monkeypatch)
                else:
                    fake = self.serve(scenario, **values)
                deadline = (
                    read_deadline(self.serve.monkeypatch, self.serve.fakes.clock)
                    if scenario == "upload-slow-first-byte" else None
                )
                generate = AsyncMock()
                with patch.object(proxy, "_generate_images_with_adaptive_batching", generate):
                    response = await asyncio.wait_for(proxy.proxy_comfy_edit_request(
                        self.edit_request(), proxy.NO_AUTH_CONTEXT,
                    ), timeout=HANG_BOUND_SECONDS)
                self.assertEqual(response.status_code, status, response.body)
                self.assertIn(message, json.loads(response.body)["error"]["message"])
                if fake is not None:
                    self.assertEqual(len(fake.requests), 1)
                    self.assertIn(b'filename="source.png"', fake.requests[0].body)
                if deadline is not None:
                    self.assertEqual(deadline.read_fired, 1)
                generate.assert_not_awaited()

    async def test_second_reference_upload_failure_does_not_generate(self):
        self.workflow["_meta"].pop("image_node")
        self.workflow["_meta"]["image_nodes"] = ["i", "j"]
        self.workflow["j"] = {"inputs": {"image": ""}}
        for scenario, values, message in (
            ("upload-second-text-reply", {"status": 503, "text": "second upload failed"}, "second upload failed"),
            ("upload-second-json-reply", {"status": 200, "reply": {}}, "No filename returned"),
        ):
            with self.subTest(message=message):
                fake = self.serve(scenario, **values)
                generate = AsyncMock()
                with patch.object(proxy, "_generate_images_with_adaptive_batching", generate):
                    response = await proxy.proxy_comfy_edit_request(
                        self.edit_request(extra_image=True), proxy.NO_AUTH_CONTEXT,
                    )
                self.assertEqual(response.status_code, 502, response.body)
                self.assertIn(message, json.loads(response.body)["error"]["message"])
                self.assertEqual(len(fake.requests), 2)
                self.assertIn(b'filename="second.png"', fake.calls("second")[0].body)
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
        fake = self.serve("upload-ok-any")
        with patch.object(proxy, "_comfy_run_round", side_effect=run):
            responses = await asyncio.wait_for(asyncio.gather(
                proxy.proxy_comfy_edit_request(left, proxy.NO_AUTH_CONTEXT),
                proxy.proxy_comfy_edit_request(right, proxy.NO_AUTH_CONTEXT),
            ), timeout=2)
        self.assertEqual([r.status_code for r in responses], [200, 200])
        self.assertEqual(sorted(w["p"]["inputs"]["text"] for w in seen), ["LEFT", "RIGHT"])
        self.assertIsNot(seen[0], seen[1])
        self.assertEqual(self.workflow, before)
        self.assertEqual(len(fake.calls("upload")), 2)

    async def test_edit_seed_reaches_each_actual_round_without_mutating_template(self):
        seen = []

        async def run(client, workflow, *args, **kwargs):
            seen.append(copy.deepcopy(workflow))
            return None, [b"fake-image"]

        before = copy.deepcopy(self.workflow)
        self.serve("upload-ok-any")
        with patch.object(proxy, "_comfy_run_round", side_effect=run):
            response = await proxy.proxy_comfy_edit_request(
                self.edit_request(n=2, seed=41), proxy.NO_AUTH_CONTEXT,
            )
        self.assertEqual(response.status_code, 200, response.body)
        self.assertEqual([w["s"]["inputs"]["noise_seed"] for w in seen], [41, 42])
        self.assertEqual(self.workflow, before)

    async def test_non_object_generation_json_is_400_before_backend_effects(self):
        fake = self.serve("upload-ok-any")
        for body in (None, [], 42, True):
            with self.subTest(body=body):
                request = SimpleNamespace(json=AsyncMock(return_value=body))
                with patch.object(proxy, "_generate_images_with_adaptive_batching",
                                  new_callable=AsyncMock) as generate:
                    response = await proxy.proxy_comfy_request(request, proxy.NO_AUTH_CONTEXT)
                self.assertEqual(response.status_code, 400, response.body)
                error = json.loads(response.body)["error"]
                self.assertEqual(error["type"], "invalid_request_error")
                self.assertIsInstance(error["message"], str)
                self.assertTrue(error["message"])
                generate.assert_not_awaited()
                self.assertEqual(len(fake.calls("upload")), 0)


class ImageRouteCancellationGapTests(IsolatedAsyncioTestCase):
    async def test_public_routes_persist_one_cancelled_receipt_and_reraise(self):
        cases = [
            (proxy.images_generations, "proxy_comfy_request", "image.generate"),
            (proxy.images_edits, "proxy_comfy_edit_request", "image.edit"),
            (proxy.images_variations, "proxy_comfy_variations_request", "image.variations"),
        ]
        for route, worker_name, intent in cases:
            with self.subTest(intent=intent):
                entered = asyncio.Event()
                receipt = RouteReceipt(protocol="openai.images", intent=intent)

                async def worker(*args, **kwargs):
                    entered.set()
                    await asyncio.Event().wait()

                with patch.object(proxy, "authenticate_request", return_value=proxy.NO_AUTH_CONTEXT), patch.object(
                    proxy, "start_image_route_receipt", return_value=receipt
                ), patch.object(
                    proxy, "_apply_image_route_policy", return_value=SimpleNamespace(denied=False)
                ), patch.object(
                    proxy, worker_name, side_effect=worker
                ), patch.object(
                    proxy.receipt_store, "record"
                ) as record, patch.object(proxy.firehose_metrics, "record_event"):
                    task = asyncio.create_task(route(SimpleNamespace()))
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=2)
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                        record.assert_called_once_with(receipt)
                        self.assertEqual(receipt.status, "cancelled")
                        self.assertIsNotNone(receipt.finished_at_ms)
                    finally:
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task, return_exceptions=True)


class ImageRoundCancellationGapTests(IsolatedAsyncioTestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, serve_comfy):
        self.serve = serve_comfy

    async def test_disconnect_cancels_only_its_prompt_and_finalizes_probe(self):
        fake = self.serve("round-client-disconnect")
        probe = SimpleNamespace(start=Mock(), stop=AsyncMock())
        request = SimpleNamespace(is_disconnected=AsyncMock(return_value=True))
        with patch.object(proxy, "_build_comfy_submit_payload", side_effect=lambda w: {"prompt": w}):
            async with httpx.AsyncClient() as client:
                error, images = await asyncio.wait_for(proxy._comfy_run_round(
                    client, {}, {}, "disconnect", request=request, vram_probe=probe,
                ), timeout=HANG_BOUND_SECONDS)
        self.assertEqual(error.status_code, 499)
        self.assertEqual(json.loads(error.body)["error"]["code"], "image_client_disconnected")
        self.assertEqual(images, [])
        self.assertEqual([r.path for r in fake.requests], ["/prompt", "/api/jobs/mine/cancel"])
        probe.start.assert_called_once()
        probe.stop.assert_awaited_once()

    async def test_task_cancellation_preserves_cancel_even_if_backend_cancel_fails(self):
        for cancel_status in (200, 503):
            with self.subTest(cancel_status=cancel_status):
                fake = self.serve("round-task-cancelled", cancel_status=cancel_status)
                probe = SimpleNamespace(start=Mock(), stop=AsyncMock())
                with patch.object(proxy, "_build_comfy_submit_payload", side_effect=lambda w: {"prompt": w}):
                    async with httpx.AsyncClient() as client:
                        task = asyncio.create_task(proxy._comfy_run_round(
                            client, {}, {}, "cancel", vram_probe=probe,
                        ))
                        try:
                            # The fake holds the history answer: the round is waiting on it.
                            await asyncio.wait_for(self.serve.fakes.clock.until_waiting(), timeout=HANG_BOUND_SECONDS)
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                        finally:
                            if not task.done():
                                task.cancel()
                                await asyncio.gather(task, return_exceptions=True)
                self.assertEqual([r.path for r in fake.requests], ["/prompt", "/history/mine", "/api/jobs/mine/cancel"])
                probe.stop.assert_awaited_once()
