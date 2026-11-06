"""ComfyUI out-of-memory recovery over the wire: the image batcher's one unload-and-retry.

``proxy._generate_images_with_adaptive_batching`` reads the error text of a
failed round and, when ComfyUI is idle, frees its models and runs the round
once more. Each case serves one FK comfyui scenario and asserts the response,
the exact call order the engine saw, and that nothing else was called.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

import proxy
from tests.support.fake_upstream import FakeUpstream, FakeUpstreams, fake_upstreams, load_scenario  # noqa: F401

HANG_BOUND_SECONDS = 10
WORKFLOW = {"1": {"class_type": "FkNode", "inputs": {"text": "a red fox"}}}


def _generate(fakes: FakeUpstreams, monkeypatch, scenario: str):
    fake = fakes.asgi(load_scenario(f"comfyui/{scenario}"))
    monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)
    monkeypatch.setattr(proxy, "get_vram_estimate", lambda *a: 100)  # known cost: no VRAM probe round
    monkeypatch.setattr(proxy, "graph_free_vram_bytes", AsyncMock(return_value=10**12))
    monkeypatch.setattr(proxy, "IMAGE_FREE_VRAM_ON_EXHAUSTION", True)
    monkeypatch.setattr(proxy, "_build_image_response_payload", lambda request, images, response_format, warning_headers, **kw: proxy.Response(
        json.dumps({"count": len(images)}), status_code=200, media_type="application/json", headers=warning_headers))

    async def call():
        async with asyncio.timeout(HANG_BOUND_SECONDS), httpx.AsyncClient() as client:
            return await proxy._generate_images_with_adaptive_batching(
                request=SimpleNamespace(), client=client, base_workflow=json.loads(json.dumps(WORKFLOW)),
                meta={}, manifest=None, workflow_name="fk", width=512, height=512,
                n_requested=1, response_format="b64_json", headers={},
            )

    with patch.object(proxy.asyncio, "sleep", AsyncMock()):
        return asyncio.run(call()), fake


def _order(fake: FakeUpstream) -> list[str]:
    return [request.exchange for request in fake.requests]


def test_an_out_of_memory_round_on_an_idle_engine_frees_models_and_retries_once(fake_upstreams, monkeypatch) -> None:
    response, fake = _generate(fake_upstreams, monkeypatch, "image-oom-then-complete")

    assert response.status_code == 200, response.body
    assert json.loads(response.body) == {"count": 1}
    assert response.headers["X-ComfyUI-Models-Freed"] == "true"
    assert _order(fake) == ["prompt-oom", "queue", "free", "prompt", "history", "view"]
    assert fake.calls("prompt-oom")[0].json["prompt"] == WORKFLOW


def test_an_out_of_memory_round_on_a_busy_engine_unloads_nothing_and_reports_the_error(fake_upstreams, monkeypatch) -> None:
    response, fake = _generate(fake_upstreams, monkeypatch, "image-oom-busy-queue")

    assert response.status_code == 502
    # the engine's own text is not passed on; the gateway says what it understood (decision D44)
    assert json.loads(response.body)["error"]["message"] == "ComfyUI: The engine ran out of memory. Try a smaller request."
    assert "CUDA" not in response.body.decode()
    assert "X-ComfyUI-Models-Freed" not in response.headers
    assert _order(fake) == ["prompt-oom", "queue"]


def test_a_refused_background_removal_round_is_retried_without_it_and_the_gateways_reason_is_reported(fake_upstreams, monkeypatch) -> None:
    """The fallback's reason is the gateway's sentence for the failed round, not the engine's text (the batcher's RMBG fallback)."""
    events: list[dict] = []
    monkeypatch.setattr(proxy.firehose_metrics, "record_event", lambda kind, message, **fields: events.append({"kind": kind, **fields}))
    fake = fake_upstreams.asgi(load_scenario("comfyui/image-rmbg-fails-then-completes"))
    monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)
    monkeypatch.setattr(proxy, "get_vram_estimate", lambda *a: 100)
    monkeypatch.setattr(proxy, "graph_free_vram_bytes", AsyncMock(return_value=10**12))
    monkeypatch.setattr(proxy, "_build_image_response_payload", lambda request, images, response_format, warning_headers, **kw: proxy.Response(
        json.dumps({"count": len(images)}), status_code=200, media_type="application/json", headers=warning_headers))
    fallback = {"2": {"class_type": "FkFallbackNode", "inputs": {"text": "a red fox"}}}

    async def call():
        async with asyncio.timeout(HANG_BOUND_SECONDS), httpx.AsyncClient() as client:
            return await proxy._generate_images_with_adaptive_batching(
                request=SimpleNamespace(), client=client, base_workflow=json.loads(json.dumps(WORKFLOW)),
                meta={}, manifest=None, workflow_name="fk+rmbg", width=512, height=512,
                n_requested=1, response_format="b64_json", headers={},
                background_removal_decision=proxy.ImageBackgroundRemovalDecision(True, 1.0, "transparent asked"),
                fallback_workflow=fallback, fallback_workflow_name="fk",
            )

    with patch.object(proxy.asyncio, "sleep", AsyncMock()):
        response = asyncio.run(call())

    assert response.status_code == 200, response.body
    assert [request.exchange for request in fake.requests] == ["prompt-failed", "prompt", "history", "view"]
    assert fake.calls("prompt-failed")[0].json["prompt"] == WORKFLOW
    assert fake.calls("prompt")[0].json["prompt"] == fallback
    [event] = [event for event in events if event["kind"] == "image_background_removal_fallback"]
    assert event["reason"] == "ComfyUI: The engine refused this request."
    assert "exploded" not in str(event)
