"""A workflow's batch_size is an image count: every image the graph makes is returned."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

import proxy

ROOT = Path(__file__).resolve().parents[1]


def _krea_manifest_and_graph():
    manifest = yaml.safe_load((ROOT / "manifests/workflows/krea2-turbo.yml").read_text())
    graph = json.loads((ROOT / manifest["bindings"]["comfyui"]["workflow"]).read_text())
    meta = graph.pop("_meta")
    return manifest, graph, meta


@pytest.fixture
def fake_comfy(monkeypatch):
    """Stands in for ComfyUI: one prompt yields one image per latent batch slot."""
    submitted = []

    async def run_round(client, graph, headers, label, **kwargs):
        batch = graph["6"]["inputs"]["batch_size"]
        submitted.append(batch)
        return None, [f"{label}-{i}".encode() for i in range(batch)]

    captured = []

    def build_payload(request, images, *args, **kwargs):
        captured.append(list(images))
        return proxy.Response(status_code=200, headers={"X-Image-Returned": str(len(images))})

    monkeypatch.setattr(proxy, "_graph_run_round", run_round)
    monkeypatch.setattr(proxy, "_build_image_response_payload", build_payload)
    monkeypatch.setattr(proxy, "get_vram_estimate", lambda *args: None)
    monkeypatch.setattr(proxy, "graph_free_vram_bytes", AsyncMock(return_value=10**12))
    monkeypatch.setattr(proxy.media_delivery, "current", lambda: None)
    return SimpleNamespace(submitted=submitted, captured=captured)


def _generate(graph, meta, manifest, n):
    return asyncio.run(proxy._generate_images_with_adaptive_batching(
        request=SimpleNamespace(), client=object(), base_workflow=graph, meta=meta,
        manifest=manifest, workflow_name="krea2-turbo", width=1024, height=1024,
        n_requested=n, response_format="b64_json", headers={}, fixed_seed=7,
    ))


def test_batch_size_two_returns_two_images_for_one_request(fake_comfy):
    manifest, graph, meta = _krea_manifest_and_graph()
    bound, error = asyncio.run(proxy._apply_workflow_extensions_to_image_request(
        request_body={"prompt": "a fox", "extensions": {"com.azizam.workflow": {"batch_size": 2}}},
        manifest=manifest, backend_request=copy.deepcopy(graph),
        standard_fields=proxy.STANDARD_IMAGE_GENERATE_FIELDS, receipt=None,
    ))
    assert error is None
    assert bound["6"]["inputs"]["batch_size"] == 2

    response = _generate(bound, meta, manifest, n=1)

    assert response.status_code == 200
    assert len(fake_comfy.captured[0]) == 2
    assert response.headers["X-Image-Returned"] == "2"


@pytest.mark.parametrize("n, batch, expected", [(1, 1, 1), (2, 2, 4), (6, 2, 10)])
def test_n_times_batch_size_is_the_image_count_capped_at_ten(fake_comfy, n, batch, expected):
    manifest, graph, meta = _krea_manifest_and_graph()
    graph["6"]["inputs"]["batch_size"] = batch

    response = _generate(graph, meta, manifest, n=n)

    assert response.status_code == 200
    assert len(fake_comfy.captured[0]) == expected
