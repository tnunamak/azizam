"""A request for N pictures through a real image entry returns N pictures, or fails loudly (never a quiet short answer).

The fake ComfyUI is stateful: a submitted graph's latent ``batch_size`` decides how many pictures its history lists, as the
real engine does. Each entry is driven through the real route code on the golden rig (``tests/support``).
"""
from __future__ import annotations

import asyncio
import json
import re

import httpx
import pytest

import proxy
from gateway import media_jobs as store_jobs
from tests import test_adapter_differential as base
from tests.support.fake_upstream import HandlerTransport
from tests.support.graph_service import set_derived
from tests.support.s7_parity_images_facts import data_items, doc, header, status
from tests.support.s7_parity_images_rig import ROW_PREFIX, observe

GEN = "/v1/images/generations"
JOB = "/gateway/v1/media/jobs/image-generations"
GRAPH_HOST = base.GRAPH_HOST
WORKFLOW = {"com.azizam.workflow": None}


class Engine:
    """ComfyUI that answers a prompt with as many pictures as its latent batch asks for (up to ``ceiling`` per run)."""

    def __init__(self, ceiling: int = 99):
        self.ceiling = ceiling
        self.batches: list[int] = []
        self._runs: dict[str, int] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/prompt":
            graph = json.loads(request.content)["prompt"]
            batch = min(self.ceiling, int(graph["6"]["inputs"]["batch_size"]))
            self.batches.append(batch)
            prompt_id = f"p{len(self.batches)}"
            self._runs[prompt_id] = batch
            return httpx.Response(200, json={"prompt_id": prompt_id})
        found = re.fullmatch(r"/history/(p\d+)", path)
        if found:
            prompt_id = found.group(1)
            images = [{"filename": f"{prompt_id}_{i}.png", "subfolder": "", "type": "output"} for i in range(self._runs[prompt_id])]
            return httpx.Response(200, json={prompt_id: {"status": {"status_str": "success", "completed": True, "messages": []},
                                                          "outputs": {"9": {"images": images}}}})
        if path == "/view":
            name = request.url.params.get("filename", "")
            digit = sum(map(ord, name)) % 200
            return httpx.Response(200, content=base._png((digit, 30, 200 - digit)), headers={"content-type": "image/png"})
        if path == "/system_stats":
            return httpx.Response(200, json={"devices": [{"name": "fk", "type": "cuda", "vram_total": 24 << 30, "vram_free": 20 << 30}]})
        if path == "/queue":
            return httpx.Response(200, json={"queue_running": [], "queue_pending": []})
        return httpx.Response(200, json={})


def engine_setup(engine: Engine, *, jobs: bool = False):
    def setup(rig):
        if jobs:
            base._capture_scheduling(rig)
        rig.set(proxy, "GRAPH_HOST", f"http://{GRAPH_HOST}")
        rig.monkeypatch.setitem(proxy.config, "backends", {**(proxy.config.get("backends") or {}), "comfy": {
            "enabled": True, "host": GRAPH_HOST, "port": 80, "protocol": "node-graph",
            "timeout": proxy.runtime().derived["GRAPH_TIMEOUT"]}})
        rig.fakes.router.add_transport(GRAPH_HOST, HandlerTransport(engine))
    return setup


def generation(batch_size=None, n=None, **fields):
    body = {"model": "krea2-turbo", "prompt": "a red fox", "response_format": "b64_json", "background": "opaque", **fields}
    if n is not None:
        body["n"] = n
    if batch_size is not None:
        body["extensions"] = {"com.azizam.workflow": {"batch_size": batch_size}}
    return {"json_body": body}


@pytest.mark.parametrize("row", [False, True], ids=["old", "row"])
@pytest.mark.parametrize("batch_size, n, ceiling", [(3, None, 99), (3, None, 1), (4, 2, 99), (2, None, 2)])
def test_generations_returns_every_requested_picture(tmp_path, row, batch_size, n, ceiling):
    engine = Engine(ceiling)
    obs = observe(tmp_path, GEN, generation(batch_size, n), engine_setup(engine), row=row)
    wanted = batch_size * (n or 1)
    returned = len(data_items(obs))
    assert status(obs) == 200, doc(obs)
    assert returned == wanted, f"asked for {wanted}, got {returned} (engine runs {engine.batches})"


@pytest.mark.parametrize("row", [False, True], ids=["old", "row"])
@pytest.mark.parametrize("batch_size, n", [(8, 2), (3, 4), (8, 10)])
def test_more_than_ten_pictures_is_refused_before_the_engine_is_called(tmp_path, row, batch_size, n):
    engine = Engine()
    obs = observe(tmp_path, GEN, generation(batch_size, n), engine_setup(engine), row=row)
    body = doc(obs)
    assert status(obs) in (400, 422), (status(obs), len(data_items(obs)), header(obs, "x-proxy-warning"))
    assert "batch_size" in json.dumps(body), body
    assert engine.batches == []


@pytest.mark.parametrize("row", [False, True], ids=["old", "row"])
def test_ten_pictures_still_come_back(tmp_path, row):
    engine = Engine()
    obs = observe(tmp_path, GEN, generation(5, 2), engine_setup(engine), row=row)
    assert status(obs) == 200, doc(obs)
    assert len(data_items(obs)) == 10


@pytest.mark.parametrize("row", [False, True], ids=["old", "row"])
def test_over_limit_job_fails_loudly(tmp_path, row):
    engine = Engine()
    request = generation(8, 2, response_format="url")
    obs = observe(tmp_path, JOB, job_flow(request, row), engine_setup(engine, jobs=True), row=row)
    texts = [str(call["response"]["body"]) for call in obs.trace["caller"]]
    assert engine.batches == [], texts
    assert not any('"status": "succeeded"' in t or '"status":"succeeded"' in t for t in texts), texts
    assert any("batch_size" in t for t in texts), texts


def _job_id(response):
    data = response.json()
    return data.get("job_id") or data.get("id")


def job_flow(request, row):
    async def go(rig):
        target = (ROW_PREFIX if row else "") + JOB
        first = await rig.call("POST", target, label="submit", **request)
        if first.status_code >= 400:
            return
        job_id = _job_id(first)
        if not row and rig.scheduled:
            await proxy._run_image_media_job(rig.scheduled[-1])
        else:
            for _ in range(3000):
                await asyncio.sleep(0)
                found = store_jobs.get(job_id, audience="internal", path=store_jobs.DB_PATH)
                if found is not None and store_jobs.is_terminal(str(found["status"])):
                    for _ in range(50):
                        await asyncio.sleep(0)
                    break
        for suffix in ("", "/content"):
            await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}{suffix}", label=f"GET {suffix or 'job'}")
    return go


def test_a_short_run_is_an_error_naming_both_counts(monkeypatch):
    """Rounds that end short of the request answer 502 with the counts, never 200 with fewer pictures."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from tests.test_image_batch_size import _krea_manifest_and_graph

    manifest, graph, meta = _krea_manifest_and_graph()
    graph["6"]["inputs"]["batch_size"] = 4
    calls = []

    async def run_round(client, workflow, headers, label, **kwargs):
        calls.append(label)
        return None, ([b"only"] if len(calls) == 1 else [])

    monkeypatch.setattr(proxy, "_graph_run_round", run_round)
    monkeypatch.setattr(proxy, "get_vram_estimate", lambda *args: None)
    monkeypatch.setattr(proxy, "graph_free_vram_bytes", AsyncMock(return_value=10**12))
    monkeypatch.setattr(proxy.media_delivery, "current", lambda: None)
    monkeypatch.setattr(proxy, "_build_image_response_payload", lambda *a, **k: proxy.Response(status_code=200))

    response = asyncio.run(proxy._generate_images_with_adaptive_batching(
        request=SimpleNamespace(), client=object(), base_workflow=graph, meta=meta, manifest=manifest,
        workflow_name="krea2-turbo", width=1024, height=1024, n_requested=1, response_format="b64_json", headers={}, fixed_seed=7))

    error = json.loads(response.body)["error"]
    assert response.status_code == 502 and error["code"] == "result_count_short"
    assert "4" in error["message"] and "1" in error["message"]
