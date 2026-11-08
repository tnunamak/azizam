"""The generic run route against the REAL snapshot, projections and port, with only the engine's HTTP faked
(CODE_TO_DATA_S4_S8 acceptance rows 1 to 4, 25 and 29 end to end; builder W).

``tests/test_workflow_run_route.py`` replaces the port's adapter. Here nothing in the gateway is replaced: the bundled
workflows come from the live snapshot provider, the worker dispatches through the production ``run_adapter`` and the
engine is a ``HandlerTransport`` that records every request it gets.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

import proxy
from gateway import discovery, media_jobs, receipt_store, request_spool
from gateway.definition_store import DefinitionStore
from gateway.engine_protocol.node_graph import compile_workflow
from tests.media_test_bytes import mp4_bytes, png_bytes
from tests.support.fake_upstream import FakeUpstreams, HandlerTransport
from tests.support.gateway import gateway_client, use_admin_db

ENGINE_HOST = "engine.fk.test"
ENGINE_PORT = 8188
IMAGE_ID = "image.generate.flux2-klein-4b"
VIDEO_ID = "video.generate.ltx-2.5-t2v"


class FakeGraphEngine:
    """A graph engine over HTTP: it takes a prompt, reports it done, and serves the files the manifest's selectors name.
    ``requests`` holds every request, in order."""

    def __init__(self, manifest: dict, media: bytes, media_type: str, filename: str) -> None:
        self.requests: list[httpx.Request] = []
        self.prompts: list[dict] = []
        block = manifest["bindings"]["comfyui"]
        outputs: dict = {}
        for selector in block["selectors"].values():
            outputs.setdefault(selector["node"], {})[selector["key"]] = [
                {"filename": filename, "subfolder": "", "type": "output"}]
        self._outputs, self._media, self._media_type = outputs, media, media_type

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == "/prompt":
            self.prompts.append(json.loads(request.content))
            return httpx.Response(200, json={"prompt_id": "p1"})
        if request.method == "GET" and path == "/history/p1":
            return httpx.Response(200, json={"p1": {"status": {"status_str": "success", "completed": True, "messages": []},
                                                    "outputs": self._outputs}})
        if request.method == "GET" and path == "/view":
            return httpx.Response(200, content=self._media, headers={"content-type": self._media_type})
        return httpx.Response(404, json={})

    @property
    def calls(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


@pytest.fixture
def world(monkeypatch, tmp_path):
    use_admin_db(monkeypatch, tmp_path)
    media = tmp_path / "media_jobs.db"
    media_jobs.init_db(media)
    monkeypatch.setattr(media_jobs, "DB_PATH", media)
    receipt_store.init_db(receipt_store.DB_PATH)
    monkeypatch.setattr(proxy, "_WORKFLOW_RUN_SPOOL_ROOT", tmp_path / "spool")
    monkeypatch.setattr(proxy, "_WORKFLOW_JOB_SPOOL", request_spool.JobSpool(tmp_path / "spool"))
    monkeypatch.setattr(proxy, "_workflow_core_cache", None)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    monkeypatch.setitem(proxy.config.setdefault("backends", {}), "comfyui",
                        {"host": ENGINE_HOST, "port": ENGINE_PORT, "enabled": True})
    fakes = FakeUpstreams(monkeypatch)
    fakes.router      # build the host router
    return SimpleNamespace(media=media, tmp=tmp_path, fakes=fakes, monkeypatch=monkeypatch)


def serve_engine(world, engine: FakeGraphEngine) -> FakeGraphEngine:
    world.fakes.router.add_transport(ENGINE_HOST, HandlerTransport(engine))
    return engine


def call(method: str, path: str, **kwargs):
    async def go():
        async with gateway_client() as client:
            return await getattr(client, method)(path, **kwargs)
    return asyncio.run(go())


def manifest_of(workflow_id: str) -> dict:
    return discovery.bundled_workflow_manifests()[workflow_id]


def template_of(manifest: dict) -> dict:
    from gateway.registry_snapshot import ROOT
    return json.loads((ROOT / manifest["bindings"]["comfyui"]["workflow"]).read_text(encoding="utf-8"))


def generated_of(world, job_id: str) -> dict:
    envelope = DefinitionStore(world.media).envelope(job_id)
    return {name: entry["value"] for name, entry in envelope.generated.items() if "value" in entry}


CASES = [
    pytest.param(IMAGE_ID, {"prompt": "a red fox"}, lambda: png_bytes((9, 9, 9)), "image/png", "out.png", id="image"),
    pytest.param(VIDEO_ID, {"prompt": "waves at dusk"}, mp4_bytes, "video/mp4", "out.mp4", id="video"),
]


@pytest.mark.parametrize(("workflow_id", "inputs", "media", "media_type", "filename"), CASES)
def test_a_bundled_workflow_runs_through_the_core_and_the_engine_gets_the_adapters_request(  # W4, row 1
        world, workflow_id, inputs, media, media_type, filename):
    manifest = manifest_of(workflow_id)
    engine = serve_engine(world, FakeGraphEngine(manifest, media(), media_type, filename))
    response = call("post", f"/gateway/v1/workflows/{workflow_id}/run", json={"inputs": inputs},
                    headers={"Prefer": "wait=20"})
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["status"] == "succeeded" and view["disposition"] == "new", (view, engine.calls)
    assert [r["media_type"] for r in view["results"]] == [media_type]
    # The engine got one prompt: the graph the binding compiles for these inputs and the generated values the core drew.
    assert len(engine.prompts) == 1
    drawn = generated_of(world, view["job_id"])
    expected = compile_workflow(manifest, "comfyui", template_of(manifest), {**inputs, **drawn})
    assert engine.prompts[0]["prompt"] == expected
    assert set(engine.prompts[0]) <= {"prompt", "client_id", "extra_data"}
    assert engine.calls[0] == "POST /prompt" and "GET /history/p1" in engine.calls
    assert {c.split()[1] for c in engine.calls} <= {"/prompt", "/history/p1", "/view", "/ws", "/queue"}


@pytest.mark.parametrize("workflow_id", ["face-likeness", "mesh.generate.trellis-2"])
def test_a_binding_kind_without_a_registered_adapter_is_unavailable_and_calls_no_engine(world, workflow_id):  # W4
    engine = FakeGraphEngine(manifest_of(IMAGE_ID), png_bytes((9, 9, 9)), "image/png", "out.png")
    serve_engine(world, engine)
    response = call("post", f"/gateway/v1/workflows/{workflow_id}/run", json={"inputs": {}},
                    headers={"Prefer": "wait=5"})
    assert response.status_code in (409, 422, 503), response.text
    assert response.json()["error"]["code"] in ("binding_unavailable", "invalid_inputs"), response.text
    assert engine.calls == []
