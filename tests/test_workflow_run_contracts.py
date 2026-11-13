"""The generic run route against the REAL snapshot, projections and port, with only the engine's HTTP faked
(CODE_TO_DATA_S4_S8 acceptance rows 1 to 4, 25 and 29 end to end; builder W).

``tests/test_workflow_run_route.py`` replaces the port's adapter. Here nothing in the gateway is replaced: the bundled
workflows come from the live snapshot provider, the worker dispatches through the production ``run_adapter`` and the
engine is a ``HandlerTransport`` that records every request it gets.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

import proxy
from tests.support.graph_service import GRAPH_SERVICE_ID
from gateway import workflow_run, discovery, media_jobs, receipt_store, request_spool, service_registry
from gateway.definition_store import DefinitionStore
from gateway.engine_protocol.node_graph import compile_workflow
from gateway.job_envelope import build_job_envelope, canonical_bytes
from tests.media_test_bytes import mp4_bytes, png_bytes
from tests.support.fake_upstream import FakeUpstreams, HandlerTransport
from tests.registry_test_helpers import write_service_registry_for_test
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
        if request.method == "POST" and path == "/upload/image":
            return httpx.Response(200, json={"name": "staged-input.png", "subfolder": "", "type": "input"})
        if request.method == "GET" and path == "/history/p1":
            return httpx.Response(200, json={"p1": {"status": {"status_str": "success", "completed": True, "messages": []},
                                                    "outputs": self._outputs}})
        if request.method == "GET" and path == "/view":
            return httpx.Response(200, content=self._media, headers={"content-type": self._media_type})
        return httpx.Response(404, json={})

    @property
    def calls(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


def publish(db, workflows: dict) -> None:
    """The registry the gateway serves: the engine as a service record, and the given owner workflows. Reloaded live."""
    registry = {"services": {GRAPH_SERVICE_ID: {"name": "Engine", "config": {
        "host": ENGINE_HOST, "port": ENGINE_PORT}}},
        "workflows": workflows}
    write_service_registry_for_test(service_registry.normalize_service_registry(registry), path=db)
    proxy.reload_runtime_config()


@pytest.fixture
def world(monkeypatch, tmp_path):
    db = use_admin_db(monkeypatch, tmp_path)
    media = tmp_path / "media_jobs.db"
    media_jobs.init_db(media)
    monkeypatch.setattr(media_jobs, "DB_PATH", media)
    receipt_store.init_db(receipt_store.DB_PATH)
    monkeypatch.setattr(proxy, "_WORKFLOW_RUN_SPOOL_ROOT", tmp_path / "spool")
    monkeypatch.setattr(proxy, "_WORKFLOW_JOB_SPOOL", request_spool.JobSpool(tmp_path / "spool"))
    monkeypatch.setattr(proxy, "_workflow_core_cache", None)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    for name in ("config", "VIDEO_ALIASES", "AUDIO_ALIASES"):      # a reload swaps these; the fixture puts them back
        monkeypatch.setattr(proxy, name, getattr(proxy, name))
    saved = proxy.GATEWAY_WORKFLOW_REGISTRY.contents()
    publish(db, {})
    fakes = FakeUpstreams(monkeypatch)
    fakes.router      # build the host router
    yield SimpleNamespace(media=media, tmp=tmp_path, fakes=fakes, monkeypatch=monkeypatch, db=db)
    proxy.GATEWAY_WORKFLOW_REGISTRY.restore(saved)


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


def discovery_root():
    from gateway.registry_snapshot import ROOT
    return ROOT


def template_of(manifest: dict) -> dict:
    return json.loads((discovery_root() / manifest["bindings"]["comfyui"]["workflow"]).read_text(encoding="utf-8"))


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
    # The node-graph round may read the engine's capacity first (the old image route's probe), so the prompt is the first submit.
    submits = [c for c in engine.calls if c.startswith("POST")]
    assert submits[0] == "POST /prompt" and "GET /history/p1" in engine.calls
    assert {c.split()[1] for c in engine.calls} <= {"/prompt", "/history/p1", "/view", "/ws", "/queue", "/system_stats"}


@pytest.mark.parametrize("workflow_id", ["image.analyze.face-likeness", "mesh.generate.trellis-2", "image.edit.image-identity"])
def test_a_binding_kind_without_a_registered_adapter_is_unavailable_and_calls_no_engine(world, workflow_id):  # W4
    engine = FakeGraphEngine(manifest_of(IMAGE_ID), png_bytes((9, 9, 9)), "image/png", "out.png")
    serve_engine(world, engine)
    response = call("post", f"/gateway/v1/workflows/{workflow_id}/run", json={"inputs": {}},
                    headers={"Prefer": "wait=5"})
    assert (response.status_code, response.json()["error"]["code"]) == (503, "binding_unavailable"), response.text
    assert engine.calls == []


FACTS = {"exposure": "default", "trust_tier": "stable", "resource_tier": "batch", "resource_class": "image", "tags": ["a"]}
OWNER_ID = "owner.inline.demo"


def owner_workflow(template_inline: bool, **record) -> dict:
    """The bundled image workflow as an owner's own record; its graph travels inside the saved document if asked."""
    manifest = copy.deepcopy(manifest_of(IMAGE_ID))
    manifest["id"] = OWNER_ID
    if template_inline:
        manifest["bindings"]["comfyui"]["workflow"] = {"inline": template_of(manifest_of(IMAGE_ID))}
    return {OWNER_ID: {"manifest": manifest, "policy": FACTS, "published": True, **record}}


def test_a_workflow_with_an_inline_template_runs_that_template(world):  # W2: the run core runs what the save accepted
    publish(world.db, owner_workflow(True))
    source = manifest_of(IMAGE_ID)
    engine = serve_engine(world, FakeGraphEngine(source, png_bytes((9, 9, 9)), "image/png", "out.png"))
    response = call("post", f"/gateway/v1/workflows/{OWNER_ID}/run", json={"inputs": {"prompt": "a red fox"}},
                    headers={"Prefer": "wait=20"})
    assert response.status_code == 200 and response.json()["status"] == "succeeded", response.text
    drawn = generated_of(world, response.json()["job_id"])
    expected = compile_workflow(source, "comfyui", template_of(source), {"prompt": "a red fox", **drawn})
    assert engine.prompts[0]["prompt"] == expected


MCP_META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}


def surfaces(world) -> dict[str, bool]:
    """Whether the workflow shows on each place a caller learns of workflows: the App catalog, OpenAPI, MCP."""
    catalog = call("get", "/gateway/v1/workflows")
    openapi = call("get", "/openapi.json")
    mcp = call("post", "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "capabilities", "arguments": {}, "_meta": MCP_META}},
        headers={"mcp-protocol-version": "2026-07-28", "mcp-method": "tools/call", "mcp-name": "capabilities"})
    assert catalog.status_code == openapi.status_code == mcp.status_code == 200, (catalog.text, mcp.text)
    return {"catalog": OWNER_ID in catalog.text, "openapi": OWNER_ID in openapi.text, "mcp": OWNER_ID in mcp.text}


def test_a_published_workflow_shows_on_every_surface_and_an_unpublished_one_on_none(world):  # rows 2 and 4
    publish(world.db, owner_workflow(False))
    assert surfaces(world) == {"catalog": True, "openapi": True, "mcp": True}
    publish(world.db, owner_workflow(False, published=False))
    assert surfaces(world) == {"catalog": False, "openapi": False, "mcp": False}
    engine = serve_engine(world, FakeGraphEngine(manifest_of(IMAGE_ID), png_bytes((9, 9, 9)), "image/png", "out.png"))
    refused = call("post", f"/gateway/v1/workflows/{OWNER_ID}/run", json={"inputs": {"prompt": "x"}})
    assert refused.status_code in (403, 404, 409), refused.text
    assert engine.calls == []


def test_a_disabled_workflow_refuses_new_runs_and_fails_a_queued_job_at_dispatch(world, monkeypatch):  # rows 2 and 4
    publish(world.db, owner_workflow(False))
    engine = serve_engine(world, FakeGraphEngine(manifest_of(IMAGE_ID), png_bytes((9, 9, 9)), "image/png", "out.png"))
    scheduled: list[str] = []
    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "schedule", lambda job_id: scheduled.append(job_id))
    queued = call("post", f"/gateway/v1/workflows/{OWNER_ID}/run", json={"inputs": {"prompt": "x"}})
    assert queued.status_code in (200, 202), queued.text
    job_id = queued.json()["job_id"]
    assert scheduled == [job_id] and queued.json()["status"] == "queued"

    publish(world.db, owner_workflow(False, state="disabled"))
    refused = call("post", f"/gateway/v1/workflows/{OWNER_ID}/run", json={"inputs": {"prompt": "x"}})
    assert (refused.status_code, refused.json()["error"]["code"]) == (409, "workflow_disabled"), refused.text

    asyncio.run(proxy._run_workflow_job(job_id))          # the worker takes the queued job
    job = media_jobs.get(job_id, path=world.media)
    assert (job["status"], job["error_code"]) == ("failed", "workflow_disabled")
    assert engine.calls == []


def digest(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize(("workflow_id", "inputs", "media", "media_type", "filename"), CASES)
def test_a_finished_run_names_the_contract_binding_and_template_it_ran(  # W5: the provenance the old route tests held
        world, workflow_id, inputs, media, media_type, filename):
    manifest = manifest_of(workflow_id)
    serve_engine(world, FakeGraphEngine(manifest, media(), media_type, filename))
    response = call("post", f"/gateway/v1/workflows/{workflow_id}/run", json={"inputs": inputs},
                    headers={"Prefer": "wait=20"})
    view = response.json()
    assert response.status_code == 200 and view["status"] == "succeeded", response.text
    store = DefinitionStore(world.media)
    envelope = store.envelope(view["job_id"])
    served = proxy.WORKFLOW_SNAPSHOTS.current().get(workflow_id)          # what the gateway served when the run was made
    contract = workflow_run.execution_contract(workflow_run.plain(served.contract))
    assert (contract["id"], contract["intent"]) == (manifest["id"], manifest["intent"])
    template_file = (discovery_root() / manifest["bindings"]["comfyui"]["workflow"]).read_bytes()
    assert envelope.source_id == workflow_id and envelope.binding_key == "comfyui"
    assert envelope.contract == digest(canonical_bytes(contract))
    assert envelope.binding == digest(canonical_bytes(manifest["bindings"]["comfyui"]))
    assert envelope.template == digest(template_file)
    assert store.read_blob(envelope.template) == template_file          # the very bytes are kept, not only named
    assert (view["binding_key"], view["record_revision"]) == (envelope.binding_key, envelope.record_revision)


def test_every_bundled_template_path_is_still_the_path_its_manifest_names(world):  # W2: the snapshot reads the same field
    from gateway import binding_descriptors
    descriptors = binding_descriptors.cached_shipped_binding_descriptors()
    checked = 0
    for workflow_id, manifest in discovery_manifests().items():
        entry = proxy.WORKFLOW_SNAPSHOTS.current().get(workflow_id)
        for key, block in manifest["bindings"].items():
            descriptor = descriptors.get(key)
            path = binding_descriptors.template_path(descriptor, block) if descriptor else None
            ref = entry.bindings[key].template
            assert (ref.path if ref else None) == path, (workflow_id, key)
            checked += path is not None
    assert checked >= 30


def discovery_manifests() -> dict:
    return discovery.bundled_workflow_manifests()


EDIT_ID = "image.edit.flux2-klein-4b"


def library_asset(world, seed: int) -> tuple[str, bytes]:
    """A picture in the caller's Library: the result of an image run, so the caller owns it as the rules say."""
    data = png_bytes((seed, seed, seed))
    serve_engine_once(world, FakeGraphEngine(manifest_of(IMAGE_ID), data, "image/png", "out.png"))
    done = call("post", f"/gateway/v1/workflows/{IMAGE_ID}/run", json={"inputs": {"prompt": f"p{seed}"}},
                headers={"Prefer": "wait=20"}).json()
    assert done["status"] == "succeeded", done
    return done["results"][0]["asset_id"], data


def serve_engine_once(world, engine: FakeGraphEngine) -> None:
    world.fakes.router._routes[ENGINE_HOST] = HandlerTransport(engine)      # replaces the previous engine for the next run


def test_a_library_asset_and_an_upload_of_the_same_bytes_are_one_request_identity(world, monkeypatch):  # W6
    asset_id, data = library_asset(world, 7)
    other_id, _ = library_asset(world, 8)
    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "schedule", lambda job_id: None)       # nothing needs to run
    headers = {"Idempotency-Key": "k-asset"}
    url = f"/gateway/v1/workflows/{EDIT_ID}/run"
    named = call("post", url, json={"inputs": {"prompt": "edit", "source_image": {"asset_id": asset_id}}}, headers=headers)
    assert named.status_code == 202 and named.json()["disposition"] == "new", named.text
    uploaded = call("post", url, data={"inputs": json.dumps({"prompt": "edit"})},
                    files={"source_image": ("a.png", data, "image/png")}, headers=headers)
    assert uploaded.status_code in (200, 202), uploaded.text
    assert (uploaded.json()["job_id"], uploaded.json()["disposition"]) == (named.json()["job_id"], "replay")
    different = call("post", url, json={"inputs": {"prompt": "edit", "source_image": {"asset_id": other_id}}}, headers=headers)
    assert (different.status_code, different.json()["error"]["code"]) == (409, "idempotency_conflict"), different.text


def upload(data: bytes, declared: str, **headers):
    return call("post", f"/gateway/v1/workflows/{EDIT_ID}/run", data={"inputs": json.dumps({"prompt": "edit"})},
                files={"source_image": ("a.bin", data, declared)}, headers=headers)


def test_the_type_of_an_upload_is_measured_from_its_bytes_not_taken_from_the_caller(world, monkeypatch):  # W7, row 29
    seen = {}

    async def hold(ctx):          # stands for the engine: it records what the adapter would be given
        seen["ref"] = ctx.media["source_image"][0]
        raise RuntimeError("stop here")

    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "run_adapter", hold)
    # Text declared as a picture: the measured type is not an image, so the input refuses it and keeps no bytes.
    refused = upload(b"this is not a picture", "image/png")
    assert refused.status_code in (415, 422), refused.text
    assert not [p for p in (world.tmp / "spool").rglob("*") if p.is_file()]
    # A picture declared as a binary blob: accepted by what it is; the declared type stays for the wire.
    accepted = upload(png_bytes((3, 3, 3)), "application/octet-stream", Prefer="wait=10")
    assert accepted.status_code in (200, 202), accepted.text
    ref = seen["ref"]
    assert (ref.media_type, ref.wire_media_type) == ("image/png", "application/octet-stream")


# -- Adversarial edges of W2/W7 and dispatch control (CODE_TO_DATA_S4_S8 5.2, 5.7; rows 4, 9, 29) -------------------------

@pytest.mark.parametrize(
    ("body", "measured", "declared"),
    [
        pytest.param(lambda: png_bytes((2, 4, 6)), "image/png", "video/mp4", id="png-declared-video"),
        pytest.param(mp4_bytes, "video/mp4", "image/png", id="video-declared-png"),
        pytest.param(lambda: png_bytes((2, 4, 6)), "image/png", "application/octet-stream", id="png-declared-unknown"),
        pytest.param(mp4_bytes, "video/mp4", "application/octet-stream", id="video-declared-unknown"),
    ],
)
def test_mismatched_upload_types_keep_measured_and_wire_types_separate(tmp_path, body, measured, declared):
    """Exercise the real spool with signatures split across chunks, not a supplied measurement stub."""
    data = body()
    spool = request_spool.FileRequestSpool(tmp_path, max_bytes=len(data) + 1)

    async def chunks():
        for offset in range(0, len(data), 3):
            yield data[offset:offset + 3]

    try:
        ref = asyncio.run(spool.stage("source", chunks(), filename="wrong-extension.txt", wire_media_type=declared))
        assert (ref.media_type, ref.wire_media_type) == (measured, declared)
        assert (ref.size, ref.digest) == (len(data), digest(data))
        staged = [path for path in (tmp_path / "staging").rglob("*") if path.is_file()]
        assert len(staged) == 1 and staged[0].read_bytes() == data
    finally:
        spool.release()
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.parametrize(
    ("body", "declared", "accepted"),
    [
        pytest.param(lambda: png_bytes((2, 4, 6)), "video/mp4", True, id="png-accepted-despite-video-label"),
        pytest.param(mp4_bytes, "image/png", False, id="video-refused-despite-png-label"),
        pytest.param(lambda: b"unrecognised binary\x00\x01\x02", "image/png", False, id="unknown-declared-png"),
        pytest.param(lambda: b"unrecognised binary\x00\x01\x02", "video/mp4", False, id="unknown-declared-video"),
        pytest.param(lambda: b"unrecognised binary\x00\x01\x02", "application/octet-stream", False, id="unknown-declared-unknown"),
    ],
)
def test_image_upload_validation_uses_bytes_in_both_directions(world, monkeypatch, body, declared, accepted):
    """The real route validates measured bytes before scheduling; no product failure is hidden by an adapter stub."""
    scheduled = []
    engine = serve_engine(world, FakeGraphEngine(manifest_of(IMAGE_ID), png_bytes((9, 9, 9)), "image/png", "out.png"))
    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "schedule", scheduled.append)
    response = upload(body(), declared)
    if accepted:
        assert response.status_code in (200, 202), response.text
        view = response.json()
        assert view["status"] == "queued" and scheduled == [view["job_id"]]
        payload = proxy._WORKFLOW_RUN_PORT.load_payload(view["job_id"])
        ref = payload["media"]["source_image"][0]
        assert (ref["media_type"], ref["wire_media_type"]) == ("image/png", declared)
    else:
        assert response.status_code in (415, 422), response.text
        assert scheduled == []
        assert not [path for path in (world.tmp / "spool").rglob("*") if path.is_file()]
    assert engine.calls == []


def _inline_template_boundary(metric, limit, excess):
    """A structural fixture, not an executable graph: exactly limit or limit+1 in one independent dimension."""
    if metric == "bytes":
        template = {"node": {"padding": ""}}
        template["node"]["padding"] = "x" * (limit + excess - len(canonical_bytes(template)))
        assert len(canonical_bytes(template)) == limit + excess
        return template
    if metric == "nodes":
        template = {str(index): {} for index in range(limit + excess)}
        assert len(template) == limit + excess
        return template
    template = 0
    for _ in range(limit + excess):
        template = {"nested": template}
    return template


@pytest.mark.parametrize("configured", [False, True], ids=["code-ceilings", "lowered-config"])
@pytest.mark.parametrize("metric", ["bytes", "nodes", "depth"])
@pytest.mark.parametrize("excess", [0, 1], ids=["at-limit", "one-over"])
def test_each_inline_template_bound_has_an_inclusive_edge(configured, metric, excess):
    """Check the real structural bound function independently of graph validity (matrix row 9)."""
    from gateway.workflow_registry_store import bound_problems, bounds_from_config, template_depth
    from gateway.workflow_runtime_types import BOUND_EXCEEDED

    config = {"workflow_execution": {"bounds": {
        "max_template_bytes": 256, "max_nodes": 8, "max_depth": 6,
    }}} if configured else {}
    bounds = bounds_from_config(config)
    limit = {"bytes": bounds.max_template_bytes, "nodes": bounds.max_nodes, "depth": bounds.max_depth}[metric]
    template = _inline_template_boundary(metric, limit, excess)
    measured = {"bytes": len(canonical_bytes(template)), "nodes": len(template), "depth": template_depth(template)}
    assert measured[metric] == limit + excess
    assert all(measured[name] <= ceiling for name, ceiling in (
        ("bytes", bounds.max_template_bytes), ("nodes", bounds.max_nodes), ("depth", bounds.max_depth),
    ) if name != metric)
    contract = copy.deepcopy(manifest_of(IMAGE_ID))
    contract["bindings"]["comfyui"]["workflow"] = {"inline": template}
    problems = bound_problems(contract, bounds)
    if excess:
        assert len(problems) == 1, problems
        problem = problems[0]
        assert problem.code == BOUND_EXCEEDED
        assert problem.path == ("bindings", "comfyui", "workflow", "inline")
        assert str(limit + 1) in problem.message and str(limit) in problem.message
    else:
        assert problems == []


@pytest.mark.parametrize(("workflow_id", "inputs", "media", "media_type", "filename"), CASES)
@pytest.mark.parametrize("change", ["disable", "unpublish"])
def test_owner_control_changes_between_submit_and_dispatch_for_each_capability(
        world, monkeypatch, workflow_id, inputs, media, media_type, filename, change):
    """5.2 and matrix rows 4/17: disable stops effects; unpublish hides new work but does not revoke admitted work."""
    manifest = copy.deepcopy(manifest_of(workflow_id))
    owner_id = "owner.queued." + workflow_id
    manifest["id"] = owner_id
    incarnation = "queued-control-" + workflow_id
    record = {"manifest": manifest, "policy": {**FACTS, "resource_class": media_type.split("/")[0]},
              "state": "active", "published": True, "incarnation": incarnation, "record_revision": 1}
    publish(world.db, {owner_id: record})
    engine = serve_engine(world, FakeGraphEngine(manifest, media(), media_type, filename))
    scheduled = []
    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "schedule", scheduled.append)
    url = f"/gateway/v1/workflows/{owner_id}/run"
    response = call("post", url, json={"inputs": inputs})
    assert response.status_code in (200, 202), response.text
    queued = response.json()
    assert queued["status"] == "queued" and scheduled == [queued["job_id"]]
    job_id = queued["job_id"]
    definitions = DefinitionStore(world.media)
    pin = definitions.envelope(job_id)
    assert pin.incarnation == incarnation
    assert engine.calls == []

    # Change only run control, preserving identity so a false workflow_deleted cannot make this test pass.
    record["record_revision"] = 2
    record["state" if change == "disable" else "published"] = "disabled" if change == "disable" else False
    publish(world.db, {owner_id: record})
    current = proxy.WORKFLOW_SNAPSHOTS.current().get(owner_id)
    assert current.incarnation == incarnation
    assert current.state == ("disabled" if change == "disable" else "active")
    assert current.published is (change != "unpublish")

    refused = call("post", url, json={"inputs": inputs})
    expected = (409, "workflow_disabled") if change == "disable" else (404, "workflow_not_found")
    assert (refused.status_code, refused.json()["error"]["code"]) == expected, refused.text
    assert scheduled == [job_id] and engine.calls == []

    asyncio.run(proxy._run_workflow_job(job_id))
    job = media_jobs.get(job_id, path=world.media)
    if change == "disable":
        assert (job["status"], job["error_code"]) == ("failed", "workflow_disabled"), job
        assert engine.calls == []
    else:
        # Design 5.2 check 4 and its table explicitly do not re-check published at dispatch.
        assert job["status"] == "succeeded", job
        assert len(engine.prompts) == 1
        expected_graph = compile_workflow(manifest, "comfyui", template_of(manifest), {**inputs, **generated_of(world, job_id)})
        assert engine.prompts[0]["prompt"] == expected_graph
        assert engine.calls.count("POST /prompt") == 1
        retained = definitions.envelope(job_id)
        assert (retained.contract, retained.binding, retained.template) == (pin.contract, pin.binding, pin.template)


def test_an_uploaded_source_image_reaches_the_engine_through_the_real_adapter(world):  # c2d-W-contracts-review-1108
    """The file a run was given is part of the request the adapter sends: the engine must receive its bytes, and the
    compiled prompt must name the file it holds, not leave the asset input without a value."""
    manifest = manifest_of(EDIT_ID)
    engine = serve_engine(world, FakeGraphEngine(manifest, png_bytes((9, 9, 9)), "image/png", "out.png"))
    data = png_bytes((1, 2, 3))
    response = call("post", f"/gateway/v1/workflows/{EDIT_ID}/run", data={"inputs": json.dumps({"prompt": "edit"})},
                    files={"source_image": ("a.png", data, "image/png")}, headers={"Prefer": "wait=20"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "succeeded", (response.text, engine.calls)
    sent = b"".join(request.content for request in engine.requests)
    assert data in sent, engine.calls
    # The prompt names the file the engine holds for this run.
    assert engine.calls.index("POST /upload/image") < engine.calls.index("POST /prompt")
    node = manifest["bindings"]["comfyui"]["map"]["source_image"][0]
    assert engine.prompts[0]["prompt"][node["node"]]["inputs"][node["input"]] == "staged-input.png"


# ----------------------------------------------------------------------------------------- rollback readiness


@pytest.fixture
def rollback_db(tmp_path):
    path = tmp_path / "rollback.db"
    media_jobs.init_db(path)
    return path


def _rollback_dump(path):
    with sqlite3.connect(path) as conn:
        return "\n".join(conn.iterdump())



@pytest.fixture(params=["empty", "media_jobs", "job_envelope"])
def legacy_rollback_db(tmp_path, request):
    """Historical schemas built without any application initializer."""
    path = tmp_path / "legacy-rollback.db"
    with sqlite3.connect(path) as conn:
        if request.param != "empty":
            conn.execute("CREATE TABLE media_jobs (job_id TEXT PRIMARY KEY, workflow TEXT, status TEXT, job_json TEXT)")
            for job_id, status, workflow in [
                ("z-queued", "queued", "saved.workflow"),
                ("a-queued", "queued", "saved.workflow"),
                ("finished", "succeeded", "saved.workflow"),
                ("other-media", "queued", None),
            ]:
                conn.execute("INSERT INTO media_jobs VALUES (?, ?, ?, ?)",
                             (job_id, workflow, status, json.dumps({"intent": "legacy.media"})))
        if request.param == "job_envelope":
            conn.execute("CREATE TABLE job_envelope (job_id TEXT PRIMARY KEY, envelope_json TEXT)")
            conn.execute("INSERT INTO job_envelope VALUES (?, ?)",
                         ("a-queued", json.dumps({"envelope_version": 1})))
    return path, request.param


def test_rollback_inventory_reads_uninitialized_and_legacy_schemas(legacy_rollback_db, monkeypatch):
    path, schema = legacy_rollback_db
    monkeypatch.setattr(media_jobs, "DB_PATH", path)
    before = _rollback_dump(path)
    expected = [] if schema == "empty" else [
        {"job_id": job_id, "workflow": "saved.workflow", "status": "queued",
         "envelope_version": 1 if pinned else None,
         "reason": "v1_compatible" if pinned else "pin_not_recorded"}
        for job_id, pinned in [("a-queued", schema == "job_envelope"), ("z-queued", False)]
    ]

    assert workflow_run.rollback_job_inventory() == expected
    assert _rollback_dump(path) == before


def test_rollback_ready_reads_uninitialized_and_legacy_schemas(legacy_rollback_db):
    path, _ = legacy_rollback_db
    before = _rollback_dump(path)

    assert workflow_run.assert_rollback_ready(path) is None
    assert _rollback_dump(path) == before


@pytest.mark.parametrize("envelope_json", [json.dumps({"envelope_version": 2, "source": {"kind": "workflow", "id": "w"}}), "not json"])
def test_rollback_refuses_unreadable_queued_jobs_without_a_tombstone_table(tmp_path, envelope_json):
    path = tmp_path / "queued-legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE media_jobs (job_id TEXT PRIMARY KEY, workflow TEXT, status TEXT, job_json TEXT)")
        conn.execute("CREATE TABLE job_envelope (job_id TEXT PRIMARY KEY, envelope_json TEXT)")
        conn.execute("INSERT INTO media_jobs VALUES ('queued-v2', 'saved.workflow', 'queued', '{}')")
        conn.execute("INSERT INTO job_envelope VALUES ('queued-v2', ?)", (envelope_json,))
    before = _rollback_dump(path)

    with pytest.raises(workflow_run.RollbackRefused) as refusal:
        workflow_run.assert_rollback_ready(path)
    assert refusal.value.code == "queued_jobs_unreadable_by_older_build"
    assert refusal.value.job_ids == ["queued-v2"]
    assert _rollback_dump(path) == before


@pytest.mark.parametrize("with_envelope", [False, True])
def test_rollback_running_jobs_still_refuse_without_a_tombstone_table(tmp_path, with_envelope):
    path = tmp_path / "running-legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE media_jobs (job_id TEXT PRIMARY KEY, workflow TEXT, status TEXT, job_json TEXT)")
        conn.executemany("INSERT INTO media_jobs VALUES (?, NULL, ?, '{}')",
                         [("z-running", "running"), ("a-running", "running"), ("queued", "queued")])
        if with_envelope:
            conn.execute("CREATE TABLE job_envelope (job_id TEXT PRIMARY KEY, envelope_json TEXT)")
    before = _rollback_dump(path)

    with pytest.raises(workflow_run.RollbackRefused) as refusal:
        workflow_run.assert_rollback_ready(path)
    assert refusal.value.code == "running_jobs"
    assert refusal.value.job_ids == ["a-running", "z-running"]
    assert _rollback_dump(path) == before


def _rollback_job(path, job_id, *, status="queued", version=None, identity="intent"):
    workflow = "saved.workflow" if identity == "saved" else None
    metadata = {"mode": workflow_run.RUN_INTENT} if identity == "metadata" else {}
    job = media_jobs.MediaJob(
        intent=workflow_run.RUN_INTENT if identity == "intent" else "legacy.media",
        job_id=job_id, workflow=workflow, status=status, metadata=metadata,
    )
    pin = None
    if version is not None:
        pin = build_job_envelope(
            source_kind="workflow", source_id="pinned.workflow",
            contract={"a": 1}, binding={"b": 1}, template=None,
            protocol_id="p", adapter_id="a", interface_version=1,
            service_id="s", origin="http://engine.test",
        )
    created = media_jobs.create(job, path=path, envelope=pin, require_envelope=pin is not None)
    if version == 1:
        # Match the real v1 compatibility fixture in test_workflow_run.py.
        old = pin.envelope.to_json()
        old["envelope_version"] = 1
        old["source"] = {key: old["source"][key] for key in ("kind", "id", "revision")}
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE job_envelope SET envelope_json=? WHERE job_id=?",
                         (json.dumps(old), created.job_id))
    return created


def test_rollback_inventory_reports_all_versions_and_states_without_changing_rows(rollback_db):
    expected = []
    for status in ("queued", "running"):
        for version in (None, 1, 2):
            for identity in ("intent", "metadata", "saved", "envelope"):
                if identity == "envelope" and version is None:
                    continue
                job_id = f"{status}-{version}-{identity}"
                _rollback_job(rollback_db, job_id, status=status, version=version, identity=identity)
                expected.append({
                    "job_id": job_id,
                    "workflow": "saved.workflow" if identity == "saved" else
                                "pinned.workflow" if version is not None else None,
                    "status": status, "envelope_version": version,
                    "reason": "pin_not_recorded" if version is None else
                              "v1_compatible" if version == 1 else "definition_unavailable",
                })
    for status in ("succeeded", "failed", "canceled"):
        for version in (None, 1, 2):
            _rollback_job(rollback_db, f"finished-{status}-{version}", status=status, version=version)
    _rollback_job(rollback_db, "non-workflow", identity="none")
    before = _rollback_dump(rollback_db)

    rows = workflow_run.rollback_job_inventory(rollback_db)

    assert rows == sorted(expected, key=lambda row: row["job_id"])
    assert all(set(row) == {"job_id", "workflow", "status", "envelope_version", "reason"} for row in rows)
    assert _rollback_dump(rollback_db) == before


def test_rollback_inventory_is_not_limited_to_a_history_page(rollback_db, monkeypatch):
    for index in reversed(range(53)):
        _rollback_job(rollback_db, f"job-{index:03}", version=2)
    monkeypatch.setattr(media_jobs, "DB_PATH", rollback_db)
    before = _rollback_dump(rollback_db)

    rows = workflow_run.rollback_job_inventory()

    assert len(rows) == 53
    assert [row["job_id"] for row in rows] == [f"job-{index:03}" for index in range(53)]
    assert {row["reason"] for row in rows} == {"definition_unavailable"}
    assert _rollback_dump(rollback_db) == before


def test_rollback_inventory_and_readiness_on_an_empty_database(rollback_db, monkeypatch):
    monkeypatch.setattr(media_jobs, "DB_PATH", rollback_db)
    before = _rollback_dump(rollback_db)

    assert workflow_run.rollback_job_inventory() == []
    assert workflow_run.assert_rollback_ready() is None
    assert _rollback_dump(rollback_db) == before


def test_rollback_refuses_retained_deleted_jobs_until_the_normal_reap(rollback_db):
    deleted_at = 1_800_000_000
    _rollback_job(rollback_db, "z-deleted", status="failed", version=2)
    _rollback_job(rollback_db, "a-deleted", status="succeeded", version=1)
    _rollback_job(rollback_db, "visible-finished", status="canceled")
    for job_id in ("z-deleted", "a-deleted"):
        media_jobs.tombstone_job(job_id, now=deleted_at, path=rollback_db)
    # An orphan tombstone cannot resurrect a row and must not block rollback.
    media_jobs.tombstone_job("gone-already", now=deleted_at, path=rollback_db)
    assert [row["job_id"] for row in media_jobs.recent(path=rollback_db)] == ["visible-finished"]
    before = _rollback_dump(rollback_db)

    with pytest.raises(workflow_run.RollbackRefused) as caught:
        workflow_run.assert_rollback_ready(rollback_db)
    assert caught.value.code == "retained_deleted_jobs"
    assert caught.value.job_ids == ["a-deleted", "z-deleted"]
    assert _rollback_dump(rollback_db) == before

    retention = workflow_run.TOMBSTONE_RETENTION_S
    assert retention == 24 * 3600
    # This is the same cutoff the normal job-service deletion path supplies.
    now = deleted_at + retention - 1
    assert media_jobs.reap_tombstones(before=now - retention, path=rollback_db) == 0
    with pytest.raises(workflow_run.RollbackRefused) as caught:
        workflow_run.assert_rollback_ready(rollback_db)
    assert caught.value.code == "retained_deleted_jobs"
    assert caught.value.job_ids == ["a-deleted", "z-deleted"]

    now += 1
    media_jobs.reap_tombstones(before=now - retention, path=rollback_db)
    assert workflow_run.assert_rollback_ready(rollback_db) is None
    assert media_jobs.get("a-deleted", path=rollback_db) is None
    assert media_jobs.get("z-deleted", path=rollback_db) is None
    assert [row["job_id"] for row in media_jobs.recent(path=rollback_db)] == ["visible-finished"]


def test_a_target_that_reads_tombstones_is_not_held_by_retained_deletes_but_running_and_unreadable_jobs_still_refuse(rollback_db):
    _rollback_job(rollback_db, "deleted", status="failed", version=2)
    media_jobs.tombstone_job("deleted", now=1_800_000_000, path=rollback_db)
    before = _rollback_dump(rollback_db)
    with pytest.raises(workflow_run.RollbackRefused) as caught:
        workflow_run.assert_rollback_ready(rollback_db)
    assert caught.value.code == "retained_deleted_jobs"
    assert workflow_run.assert_rollback_ready(rollback_db, target_reads_tombstones=True) is None
    assert _rollback_dump(rollback_db) == before

    _rollback_job(rollback_db, "running", status="running", version=2)
    with pytest.raises(workflow_run.RollbackRefused) as caught:
        workflow_run.assert_rollback_ready(rollback_db, target_reads_tombstones=True)
    assert (caught.value.code, caught.value.job_ids) == ("running_jobs", ["running"])
    media_jobs.tombstone_job("running", now=1_800_000_000, path=rollback_db)  # a tombstone never hides a running job
    with pytest.raises(workflow_run.RollbackRefused) as caught:
        workflow_run.assert_rollback_ready(rollback_db, target_reads_tombstones=True)
    assert caught.value.code == "running_jobs"


def test_a_target_that_reads_tombstones_still_refuses_queued_jobs_it_cannot_read(rollback_db):
    _rollback_job(rollback_db, "deleted", status="failed", version=2)
    media_jobs.tombstone_job("deleted", now=1_800_000_000, path=rollback_db)
    _rollback_job(rollback_db, "queued-v2", version=2)
    with pytest.raises(workflow_run.RollbackRefused) as caught:
        workflow_run.assert_rollback_ready(rollback_db, target_reads_tombstones=True)
    assert (caught.value.code, caught.value.job_ids) == ("queued_jobs_unreadable_by_older_build", ["queued-v2"])


def test_rollback_running_media_jobs_take_priority_then_clear_when_finished(rollback_db):
    _rollback_job(rollback_db, "z-workflow", status="running", version=2)
    _rollback_job(rollback_db, "a-other-media", status="running", identity="none")
    _rollback_job(rollback_db, "queued-workflow", version=2)
    _rollback_job(rollback_db, "retained-delete", status="failed")
    media_jobs.tombstone_job("retained-delete", now=1, path=rollback_db)
    before = _rollback_dump(rollback_db)

    with pytest.raises(workflow_run.RollbackRefused) as caught:
        workflow_run.assert_rollback_ready(rollback_db)
    assert caught.value.code == "running_jobs"
    assert caught.value.job_ids == ["a-other-media", "z-workflow"]
    assert _rollback_dump(rollback_db) == before

    media_jobs.update_status("a-other-media", "succeeded", path=rollback_db)
    media_jobs.update_status("z-workflow", "failed", path=rollback_db)
    before = _rollback_dump(rollback_db)
    with pytest.raises(workflow_run.RollbackRefused) as caught:
        workflow_run.assert_rollback_ready(rollback_db)
    assert caught.value.code == "retained_deleted_jobs"
    assert caught.value.job_ids == ["retained-delete"]
    assert _rollback_dump(rollback_db) == before

    media_jobs.reap_tombstones(before=1, path=rollback_db)
    before = _rollback_dump(rollback_db)
    # The older build cannot read the queued v2 job and would fail it, so the rollback waits for it.
    with pytest.raises(workflow_run.RollbackRefused) as caught:
        workflow_run.assert_rollback_ready(rollback_db)
    assert caught.value.code == "queued_jobs_unreadable_by_older_build"
    assert caught.value.job_ids == ["queued-workflow"]
    assert _rollback_dump(rollback_db) == before

    media_jobs.update_status("queued-workflow", "canceled", path=rollback_db)
    before = _rollback_dump(rollback_db)
    assert workflow_run.assert_rollback_ready(rollback_db) is None
    assert _rollback_dump(rollback_db) == before


def test_rollback_check_never_creates_a_missing_store(tmp_path):
    store = tmp_path / "absent" / "media_jobs.db"
    store.parent.mkdir()
    assert workflow_run.rollback_job_inventory(store) == []
    assert workflow_run.assert_rollback_ready(store) is None
    assert not store.exists()


def test_rollback_allows_queued_jobs_with_no_recorded_pin(rollback_db):
    # On an older store an unpinned queued job is a runnable v1 job, not one the older build cannot read.
    _rollback_job(rollback_db, "queued-v1", version=1)
    _rollback_job(rollback_db, "queued-unpinned", version=None)
    reasons = {row["job_id"]: row["reason"] for row in workflow_run.rollback_job_inventory(rollback_db)}
    assert reasons == {"queued-unpinned": workflow_run.PIN_NOT_RECORDED, "queued-v1": "v1_compatible"}
    assert workflow_run.assert_rollback_ready(rollback_db) is None


def test_a_run_core_video_job_is_read_and_deleted_through_the_video_routes_by_its_wire_id(world):  # S7 M7
    manifest = manifest_of(VIDEO_ID)
    engine = serve_engine(world, FakeGraphEngine(manifest, mp4_bytes(), "video/mp4", "out.mp4"))
    made = call("post", f"/gateway/v1/workflows/{VIDEO_ID}/run", json={"inputs": {"prompt": "waves at dusk"}},
                headers={"Prefer": "wait=20"})
    assert made.status_code == 200 and made.json()["status"] == "succeeded", (made.text, engine.calls)
    wire_id = f"video_{made.json()['job_id']}"
    status = call("get", f"/v1/videos/{wire_id}")
    assert status.status_code == 200, status.text
    assert (status.json()["id"], status.json()["status"], status.json()["azizam_status"]) == (wire_id, "completed", "succeeded")
    content = call("get", f"/v1/videos/{wire_id}/content")
    assert (content.status_code, content.headers["content-type"]) == (200, "video/mp4")
    assert content.content[4:8] == b"ftyp"          # the stored file, which the core's writer may have re-muxed
    # This world publishes no workflows of its own, so the derived per-kind policy delete enforces has no entry for the
    # video kind: give it the policy the GET route of the same path has.
    world.monkeypatch.setattr(proxy, "_video_route_policy", lambda: {
        "protocol": "openai.video", "intent": "video.generate", "backend": "comfyui", "capability_exposure": "default",
        "capability_trust_tier": "stable", "capability_resource_tier": "expensive"})
    gone = call("delete", f"/v1/videos/{wire_id}")
    assert (gone.status_code, gone.json()) == (200, {"id": wire_id, "object": "video.deleted", "deleted": True})
    for path in (f"/v1/videos/{wire_id}", f"/v1/videos/{wire_id}/content"):
        assert call("get", path).status_code in (400, 404)
    assert call("delete", f"/v1/videos/{wire_id}").status_code in (400, 404)
