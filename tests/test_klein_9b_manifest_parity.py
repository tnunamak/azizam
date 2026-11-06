"""The FLUX.2 Klein 9B pair submits the same graph with or without its manifest.

Before the manifests existed, both workflows ran through the manifest-less path
(metadata nodes). The handlers run for real here against the shipped graphs; only
the ComfyUI round and image encoding are replaced. Run only with the hermetic runner.
"""
import copy
import itertools
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import proxy
import pytest
from test_image_route_receipts import _MultipartRequest, _MultipartUpload, _png_bytes
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401

NAMESPACE = "com.azizam.workflow"


@pytest.fixture
def _serve_upload(fake_upstreams, monkeypatch):
    fake = fake_upstreams.asgi(load_scenario("comfyui/upload-ok-any"))
    monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)


class KleinNineBParityTests(IsolatedAsyncioTestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, _serve_upload):
        pass

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, value in (
            ("COMFY_ENABLED", True),
            ("_dispatch_admission_response", Mock(return_value=None)),
            ("_service_availability_response", Mock(return_value=None)),
            ("configured_downstream_headers", Mock(return_value={})),
            ("_workflow_target_policy_decision", Mock(return_value=None)),
            ("resolve_image_background_decision",
             AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False))),
            ("get_vram_estimate", Mock(return_value=100)),
            ("graph_free_vram_bytes", AsyncMock(return_value=10**12)),
            ("compute_max_batch", Mock(return_value=1)),
        ):
            stack.enter_context(patch.object(proxy, name, value))
        stack.enter_context(patch.object(proxy.media_delivery, "current", return_value=None))
        stack.enter_context(patch.object(
            proxy, "_build_image_response_payload",
            side_effect=lambda *a, **k: proxy.Response(b"{}", status_code=200),
        ))

    async def submitted(self, handler, request, *, with_manifest, manifest_id=None):
        seen, found = [], []

        async def run(client, workflow, *args, **kwargs):
            seen.append(copy.deepcopy(workflow))
            return None, [b"fake-image"]

        real = proxy._find_image_manifest

        def recording(intent, model):
            manifest = real(intent, model)
            found.append(manifest and manifest.get("id"))
            return manifest

        finder = recording if with_manifest else (lambda intent, model: None)
        with patch.object(proxy, "_graph_run_round", side_effect=run), patch.object(
            proxy, "_find_image_manifest", finder,
        ):
            response = await handler(request, proxy.NO_AUTH_CONTEXT)
        self.assertEqual(response.status_code, 200, response.body)
        if with_manifest:
            # Without this, a missing manifest would compare the fallback with itself.
            self.assertIn(manifest_id, found)
        return seen

    async def test_generation_graph_is_identical_with_and_without_manifest(self):
        for seed in (41, None):
            with self.subTest(seed=seed):
                results = []
                for with_manifest in (False, True):
                    body = {"model": "flux2-klein", "prompt": "a red barn", "size": "768x512", "n": 2}
                    # Without a manifest the gateway rejects extensions, so the old path
                    # can only draw seeds; make it draw the seeds the new path is given.
                    if seed is not None and with_manifest:
                        body["extensions"] = {NAMESPACE: {"seed": seed}}
                    request = SimpleNamespace(json=AsyncMock(return_value=body))
                    if seed is None:
                        draws = iter([101, 102])
                    elif with_manifest:
                        # Draws that differ from the supplied seed: ignoring it would show.
                        draws = itertools.count(901)
                    else:
                        draws = iter([seed, seed + 1])
                    with patch.object(proxy.random, "randint", side_effect=lambda *_: next(draws)):
                        results.append(await self.submitted(
                            proxy.proxy_graph_image_request, request, with_manifest=with_manifest,
                            manifest_id="image.generate.flux2-klein"))
                self.assertEqual(results[0], results[1])
                seeds = [g["31"]["inputs"]["seed"] for g in results[1]]
                self.assertEqual(seeds, [41, 42] if seed is not None else [101, 102])
                self.assertEqual(results[1][0]["6"]["inputs"]["text"], "a red barn")
                self.assertEqual(results[1][0]["27"]["inputs"]["width"], 768)

    async def test_edit_graph_is_identical_with_and_without_manifest(self):
        # The edit manifest keeps seed route-owned, so callers never supply one.
        for seed in (None,):
            with self.subTest(seed=seed):
                results = []
                for with_manifest in (False, True):
                    items = [
                        ("model", "flux2-klein-edit"), ("prompt", "make it blue"), ("n", "2"),
                        ("size", "768x512"),
                        ("image", _MultipartUpload("source.png", _png_bytes((1, 2, 3, 255)))),
                    ]
                    with patch.object(proxy.random, "randint", side_effect=[101, 102]):
                        results.append(await self.submitted(
                            proxy.proxy_graph_image_edit_request, _MultipartRequest(items),
                            with_manifest=with_manifest, manifest_id="image.edit.flux2-klein"))
                self.assertEqual(results[0], results[1])
                seeds = [g["25"]["inputs"]["noise_seed"] for g in results[1]]
                self.assertEqual(seeds, [101, 102])
                self.assertEqual(results[1][0]["6"]["inputs"]["text"], "make it blue")
