"""A disabled selected inpaint workflow is refused before an engine call."""
import copy
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

import proxy
from tests.support.graph_service import set_derived
from tests.support.config_snapshot import derived_patch
import pytest
from test_image_route_receipts import _MultipartRequest, _MultipartUpload, _png_bytes
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401

NAMESPACE = "com.azizam.workflow"


@pytest.fixture
def _serve_upload(fake_upstreams, monkeypatch):
    fake = fake_upstreams.asgi(load_scenario("comfyui/upload-ok-any"))
    set_derived(monkeypatch, "GRAPH_HOST", fake.base_url)
    return fake


class DisabledInpaintTests(IsolatedAsyncioTestCase):
    @pytest.fixture(autouse=True)
    def _inject(self, _serve_upload):
        self.fake = _serve_upload

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        for name, value in (
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
        stack.enter_context(derived_patch("GRAPH_ENABLED", True))
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
        self.assertEqual(response.status_code, 409, response.body)
        import json
        self.assertEqual(json.loads(response.body)["error"]["code"], "workflow_disabled")
        self.assertEqual(seen, [])
        self.assertEqual(self.fake.requests, [])

    async def test_closed_selected_inpaint_is_refused_when_manifest_lookup_misses(self):
        from dataclasses import replace

        provider = proxy.GATEWAY_REGISTRY_PROVIDER
        snapshot = provider.current()
        workflow_id = "image.edit.qwen-image-2.1-inpaint"
        entry = replace(snapshot.get(workflow_id), state="disabled")
        snapshot = replace(snapshot, workflows={**snapshot.workflows, workflow_id: entry})
        request = _MultipartRequest([
            ("model", "qwen-image-2.1-edit"), ("prompt", "make it blue"),
            ("image", _MultipartUpload("source.png", _png_bytes((1, 2, 3, 255)))),
            ("mask", _MultipartUpload("mask.png", _png_bytes((1, 2, 3, 0))))])
        with patch.object(type(provider), "current", lambda self: snapshot), patch.object(proxy, "WORKFLOW_SNAPSHOTS", provider):
            await self.submitted(proxy.proxy_graph_image_edit_request, request, with_manifest=False)
