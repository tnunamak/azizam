"""Public video creates submit the same engine bodies as the direct old handler.

The session id is pinned; graphs, upload filenames and embedded workflow data
are compared without normalization. Schema and queue probes cover fact sources
and acceptance-time chain template capture.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
from pathlib import Path

import pytest
import yaml

import proxy
from gateway.engine_protocol.types import CanonicalRequest
from tests.media_test_bytes import png_bytes
from gateway.manifests import enforce_contract, ManifestError
from tests.support.access import access_context, everything
from tests.test_frontend_shapes import json_request
from tests.test_s7_parity_video_create import submitted, world  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def fixed_submission_session(monkeypatch):
    # Old and job-backed calls create independent websocket sessions. Pin only
    # that transport identifier so the complete engine bodies can be compared.
    from gateway.engine_protocol import node_graph
    build = node_graph.build_submit_payload

    def pinned(workflow, ui_for, **extra):
        return build(workflow, ui_for, **{**extra, "client_id": "video-parity-session"})

    monkeypatch.setattr(node_graph, "build_submit_payload", pinned)


@pytest.mark.parametrize("missing", ["length", "grid", "size"])
def test_reference_variant_cannot_omit_required_video_facts(missing):
    manifest = yaml.safe_load((ROOT / "manifests/workflows/video-ltx-2.5-i2v.yml").read_text())
    enforce_contract(manifest)
    incomplete = copy.deepcopy(manifest)
    del incomplete["video"][missing]
    with pytest.raises(ManifestError, match=missing):
        enforce_contract(incomplete)


@pytest.mark.parametrize("body", [
    pytest.param({"model": "ltx-2.5-t2v", "seconds": 16}, id="long-variant-seconds"),
    pytest.param({"model": "ltx-2.5-t2v", "shots": [
        {"prompt": "first scene", "seconds": 4}, {"prompt": "second scene", "seconds": 4},
    ]}, id="long-variant-shots"),
    pytest.param({"model": "ltx-2.5-long", "seconds": 16}, id="direct-chain"),
    pytest.param({"model": "minimax-h3-t2v", "inputs": {"steps": 6}}, id="scalar-inputs"),
])
def test_public_handoff_submits_the_old_handlers_graph(world, body):
    """Pin a usable direct-old versus public-route oracle before moving these cases."""
    body = {"prompt": "a moving scene", "seed": 7, **body}

    async def drive():
        old = await proxy.proxy_graph_video_create(
            json_request(body), access_context(everything(media_metadata="none", receipt_detail="full")),
            body_override=body)
        assert old.status_code == 200, old.body
        assert len(world.engine.prompts) == 1
        response = await world.rig.call("POST", "/v1/videos", json_body=body)
        assert response.status_code == 200, response.text
        if body["model"] in ("ltx-2.5-t2v", "ltx-2.5-long"):
            assert response.json()["id"].startswith("video_mj_")
        await submitted(world, 2)
        assert json.dumps(world.engine.prompts["p1"], sort_keys=True) == json.dumps(world.engine.prompts["p2"], sort_keys=True)

    asyncio.run(drive())


def test_static_declared_chain_is_not_the_planned_long_submission(world):
    """A static graph bind cannot replace the old chain planner for a long clip."""
    body = {"model": "ltx-2.5-long", "prompt": "a moving scene", "seconds": 16, "seed": 7}
    manifest = yaml.safe_load((ROOT / "manifests/workflows/video-ltx-2.5-long.yml").read_text())
    enforce_contract(manifest)

    async def drive():
        old = await proxy.proxy_graph_video_create(
            json_request(body), access_context(everything(media_metadata="none", receipt_detail="full")),
            body_override=body)
        assert old.status_code == 200, old.body
        request = CanonicalRequest(op="video.generate", body={
            "prompt": body["prompt"], "seed": 7, "width": 960, "height": 512,
        }, params={"manifest": manifest, "template": json.loads(
            (ROOT / manifest["bindings"]["comfyui"]["workflow"]).read_text())})
        graph, _ = proxy._graph_adapter("videos").compile("video.generate", request)
        planned = world.engine.prompts["p1"]["prompt"]
        assert graph != planned
        assert len(planned) > len(graph)

    asyncio.run(drive())


def test_valid_reference_variant_matches_old_submission_without_a_handoff(world):
    from tests.test_s7_parity_video_create import submitted
    from gateway.frontend_shapes import video_create

    # GatewayRig changes the request spool root per test. Drop the lazily
    # cached job spool so adoption reads this test's staging directory.
    world.rig.set(proxy, "_WORKFLOW_JOB_SPOOL", None)
    manifest = yaml.safe_load((ROOT / "manifests/workflows/video-ltx-2.5-i2v.yml").read_text())
    enforce_contract(manifest)
    body = {"model": "ltx-2.5-t2v", "prompt": "a moving scene", "seed": 7,
            "input_reference": "data:image/png;base64," + base64.b64encode(png_bytes()).decode()}

    async def drive():
        old = await proxy.proxy_graph_video_create(
            json_request(body), access_context(everything(media_metadata="none", receipt_detail="full")),
            body_override=body)
        assert old.status_code == 200, old.body
        assert len(world.engine.prompts) == 1

        async def no_handoff(*args, **kwargs):
            raise AssertionError("the declared create reached the old handler")
        world.rig.monkeypatch.setitem(proxy._ROW_ROUTE_DEPS.legacy, video_create.LEGACY_CREATE, no_handoff)
        response = await world.rig.call("POST", "/v1/videos", json_body=body)
        assert response.status_code == 200, response.text
        assert response.json()["id"].startswith("video_mj_")
        await submitted(world, 2)
        # This schema-valid variant already used the row path on main, whose
        # integer frame-rate representation is a published V5c delta. Pin that
        # exact representation; new hand-off cases compare old bytes unchanged.
        expected = copy.deepcopy(world.engine.prompts["p1"])
        for target in manifest["bindings"]["comfyui"]["map"]["frame_rate"]:
            field = expected["prompt"][target["node"]]["inputs"]
            assert type(field[target["input"]]) is float
            field[target["input"]] = int(field[target["input"]])
        assert json.dumps(expected, sort_keys=True) == json.dumps(world.engine.prompts["p2"], sort_keys=True)

    asyncio.run(drive())


def test_missing_video_section_reads_the_same_configured_facts_as_the_old_handler(world):
    from dataclasses import replace
    from tests.support.config_snapshot import publish_section

    original = proxy.WORKFLOW_SNAPSHOTS.current()
    workflow_id = "video.generate.ltx-2.5-t2v"
    chosen = original.get(workflow_id)
    contract = {key: value for key, value in chosen.contract.items() if key != "video"}
    from gateway.workflow_run import plain
    enforce_contract(plain(contract))
    changed = replace(chosen, contract=contract)

    class Snapshot:
        workflows = {**original.workflows, workflow_id: changed}

        def __getattr__(self, name):
            return getattr(original, name)

        def get(self, key):
            return changed if key == workflow_id else original.get(key)

        def by_intent(self, intent):
            return tuple(self.get(entry.id) for entry in original.by_intent(intent))

    class Provider:
        def current(self):
            return Snapshot()

    world.rig.set(proxy, "WORKFLOW_SNAPSHOTS", Provider())
    world.rig.set(proxy, "_WORKFLOW_JOB_SPOOL", None)
    videos = copy.deepcopy(proxy.config["videos"])
    videos["workflows"]["video-ltx-2.5-t2v"].update(default_size="640x384", frame_rate=12)
    publish_section(world.rig.monkeypatch, "videos", videos)
    body = {"model": "ltx-2.5-t2v", "prompt": "same configured facts", "seed": 7}

    async def drive():
        old = await proxy.proxy_graph_video_create(
            json_request(body), access_context(everything(media_metadata="none", receipt_detail="full")), body_override=body)
        assert old.status_code == 200, old.body
        response = await world.rig.call("POST", "/v1/videos", json_body=body)
        assert response.status_code == 200, response.text
        assert response.json()["id"].startswith("video_mj_")
        await submitted(world, 2)
        left, right = (world.engine.prompts[key]["prompt"] for key in ("p1", "p2"))
        assert json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True), {key: (left.get(key), right.get(key)) for key in left.keys() | right.keys() if left.get(key) != right.get(key)}
        assert response.json()["size"] == "640x384"
        assert world.engine.prompts["p2"]["prompt"]["23"]["inputs"]["value"] == 12

    asyncio.run(drive())


@pytest.mark.parametrize("case", ["named-image", "named-image-object", "mapped-reference", "variant-no-facts", "url-image", "vision-object", "secondary-image", "subject-source", "subject-point", "subject-no-fit"])
def test_complex_inputs_submit_the_old_handlers_graph_on_the_declared_path(world, case):
    import subprocess
    import httpx
    from tests.support.fake_upstream import HandlerTransport

    world.rig.set(proxy, "_WORKFLOW_JOB_SPOOL", None)
    auth = access_context(everything(media_metadata="none", receipt_detail="full"))
    image = png_bytes()
    data_uri = "data:image/png;base64," + base64.b64encode(image).decode()
    asset_id = proxy.store_asset_bytes(image, owner_key_hash=proxy._asset_owner_key(auth), keep_prompt=False)
    body = {"model": "ltx-2.5-t2v", "prompt": "same resolved input", "seed": 7}
    if case == "named-image":
        body["input_reference"] = asset_id
    elif case == "named-image-object":
        body["input_reference"] = {"asset_id": asset_id}
    elif case in ("mapped-reference", "variant-no-facts"):
        from dataclasses import replace
        from gateway.workflow_run import plain
        original = proxy.WORKFLOW_SNAPSHOTS.current()
        workflow_id = "video.generate.ltx-2.5-i2v"
        chosen = original.get(workflow_id)
        contract = plain(chosen.contract)
        if case == "mapped-reference":
            contract["inputs"]["first_frame"] = contract["inputs"].pop("image")
            contract["bindings"]["comfyui"]["map"]["first_frame"] = contract["bindings"]["comfyui"]["map"].pop("image")
            contract["video"]["roles"]["image"] = "first_frame"
        else:
            del contract["video"]
        enforce_contract(contract)
        changed = replace(chosen, contract=contract,
                          bindings={key: replace(binding, binding=contract["bindings"][key])
                                    for key, binding in chosen.bindings.items()})
        class Snapshot:
            workflows = {**original.workflows, workflow_id: changed}
            def __getattr__(self, name): return getattr(original, name)
            def get(self, key): return self.workflows.get(key)
            def by_intent(self, intent): return tuple(entry for entry in self.workflows.values() if entry.intent == intent)
        class Provider:
            def current(self): return Snapshot()
        world.rig.set(proxy, "WORKFLOW_SNAPSHOTS", Provider())
        body.update(model="ltx-2.5-i2v" if case == "mapped-reference" else "ltx-2.5-t2v", input_reference=data_uri)
    elif case == "url-image":
        world.rig.fakes.router.add_transport("93.184.216.34", HandlerTransport(
            lambda request: httpx.Response(200, stream=httpx.ByteStream(image), headers={"content-type": "image/png"})))
        from gateway import media_fetch
        async def addresses(host, port):
            return ["93.184.216.34"]
        world.rig.set(media_fetch, "_resolve_addresses", addresses)
        body["input_reference"] = "https://media.fk.test/reference.png"
    elif case == "vision-object":
        body["input_reference"] = {"image_url": {"url": data_uri}}
    elif case == "secondary-image":
        body.update(model="ltx-2.5-keyframes", input_reference=data_uri,
                    extensions={"com.azizam.workflow": {"last_frame": {"asset_id": asset_id}}})
    else:
        source = world.tmp / "source.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=black:s=32x32:d=3:r=12", "-c:v", "libx264", "-threads", "1", str(source)], check=True)
        video = "data:video/mp4;base64," + base64.b64encode(source.read_bytes()).decode()
        block = {"reference_image": {"asset_id": asset_id}, "source_video": video}
        if case == "subject-point":
            block.update(source_point_x=0.4, source_point_y=0.6)
        if case == "subject-no-fit":
            block["reference_fit"] = False
        body.update(model="video-scail-2-replace", extensions={"com.azizam.workflow": block})

    async def drive():
        old = await proxy.proxy_graph_video_create(json_request(body), auth, body_override=body)
        assert old.status_code == 200, old.body
        import re
        def upload_names():
            return [re.findall(rb'filename="([^"]+)"', data) for method, path, data in world.engine.calls
                    if method == "POST" and path == "/upload/image"]
        old_names = upload_names()
        response = await world.rig.call("POST", "/v1/videos", json_body=body)
        assert response.status_code == 200, response.text
        assert response.json()["id"].startswith("video_mj_")
        await submitted(world, 2)
        left, right = (world.engine.prompts[key] for key in ("p1", "p2"))
        assert json.dumps(left, sort_keys=True) == json.dumps(right, sort_keys=True), {key: (left["prompt"].get(key), right["prompt"].get(key))
                               for key in left["prompt"].keys() | right["prompt"].keys()
                               if left["prompt"].get(key) != right["prompt"].get(key)}
        assert upload_names()[len(old_names):] == old_names
        if case.startswith("subject"):
            assert response.json()["actual_seconds"] == 2.75
            assert response.json()["seconds"] == "3"

    asyncio.run(drive())


def test_queued_chain_uses_accepted_template_and_options(world):
    from tests.support.config_snapshot import publish_section
    from gateway import video_extend
    body = {"model": "ltx-2.5-t2v", "prompt": "accepted plan", "seconds": 16, "seed": 7,
            "extensions": {"com.azizam.workflow": {"state_ledger": "same scene", "context_seconds": 1.0}}}
    videos = copy.deepcopy(proxy.config["videos"])
    # Select a different registered chain from operator data, rather than the
    # short manifest's default variant. Both paths must honor that selection.
    videos["workflows"]["video-ltx-2.5-t2v"]["chain_workflow"] = "video-minimax-h3-long"
    publish_section(world.rig.monkeypatch, "videos", videos)
    queued = []
    world.rig.set(proxy, "_schedule_workflow_run", queued.append)

    async def drive():
        old = await proxy.proxy_graph_video_create(
            json_request(body), access_context(everything(media_metadata="none", receipt_detail="full")), body_override=body)
        assert old.status_code == 200, old.body
        response = await world.rig.call("POST", "/v1/videos", json_body=body)
        assert response.status_code == 200, response.text
        assert len(world.engine.prompts) == 1 and len(queued) == 1
        # Remove the live template after acceptance. Execution must reconstruct
        # its planner from the captured template document, not this lookup.
        key = videos["workflows"]["video-minimax-h3-long"]["builder"]
        world.rig.monkeypatch.delitem(video_extend.ENGINES, key)
        await proxy._run_workflow_job(queued[0])
        assert len(world.engine.prompts) == 2
        assert json.dumps(world.engine.prompts["p1"], sort_keys=True) == json.dumps(world.engine.prompts["p2"], sort_keys=True)

    asyncio.run(drive())


@pytest.mark.parametrize("extra", [
    {"model": "ltx-2.5-long", "seconds": 16},
    {"model": "ltx-2.5-t2v", "seconds": 16},
    {"model": "ltx-2.5-t2v", "shots": [
        {"prompt": "first", "seconds": 4}, {"prompt": "second", "seconds": 4}]},
], ids=["direct", "long-variant", "storyboard"])
def test_chain_post_and_get_keep_the_old_stitched_response(world, extra):
    from tests.test_s7_parity_video_create import await_status
    body = {"prompt": "stitched response", "seed": 7, **extra}
    auth = access_context(everything(media_metadata="none", receipt_detail="full"))
    fields = ("seconds", "total_seconds", "segments", "extended_from_video_id")
    async def drive():
        old = await proxy.proxy_graph_video_create(json_request(body), auth, body_override=body)
        assert old.status_code == 200, old.body
        old_body = json.loads(old.body)
        made = await world.rig.call("POST", "/v1/videos", json_body=body)
        assert made.status_code == 200, made.text
        # Job ids and wall times are independent. Compare every other POST field.
        independent = {"id", "created_at", "expires_at"}
        assert {key: value for key, value in made.json().items() if key not in independent} == {
            key: value for key, value in old_body.items() if key not in independent}
        await await_status(world, made.json()["id"])
        before = json.loads((await proxy.proxy_graph_video_status(old_body["id"], auth)).body)
        after = (await world.rig.call("GET", "/v1/videos/" + made.json()["id"])).json()
        expected_get = {key: before.get(key) for key in fields}
        # The old GET calls the count segments_total; POST calls it segments.
        expected_get["segments"] = before["segments_total"]
        assert {key: after[key] for key in fields} == expected_get
        shared = ("model", "size", "actual_seconds", "azizam_resolved_model", "status", "progress", "error")
        assert {key: after[key] for key in shared} == {key: before[key] for key in shared}
    asyncio.run(drive())


@pytest.mark.parametrize("missing", [True, False])
def test_missing_config_row_is_not_a_present_row_with_optional_facts_omitted(world, missing):
    from dataclasses import replace
    from gateway.workflow_run import plain
    from tests.support.config_snapshot import publish_section
    original = proxy.WORKFLOW_SNAPSHOTS.current()
    workflow_id = "video.generate.ltx-2.5-t2v"
    chosen = original.get(workflow_id)
    contract = plain(chosen.contract)
    del contract["video"]
    enforce_contract(contract)
    changed = replace(chosen, contract=contract)
    class Snapshot:
        workflows = {**original.workflows, workflow_id: changed}
        def __getattr__(self, name): return getattr(original, name)
        def get(self, key): return self.workflows.get(key)
        def by_intent(self, intent): return tuple(entry for entry in self.workflows.values() if entry.intent == intent)
    class Provider:
        def current(self): return Snapshot()
    world.rig.set(proxy, "WORKFLOW_SNAPSHOTS", Provider())
    videos = copy.deepcopy(proxy.config["videos"])
    if missing:
        del videos["workflows"]["video-ltx-2.5-t2v"]
    else:
        cfg = videos["workflows"]["video-ltx-2.5-t2v"]
        videos["workflows"]["video-ltx-2.5-t2v"] = {key: cfg[key] for key in ("file", "node_roles")}
    publish_section(world.rig.monkeypatch, "videos", videos)
    body = {"model": "video-ltx-2.5-t2v", "prompt": "same facts source", "seed": 7}
    async def drive():
        old = await proxy.proxy_graph_video_create(json_request(body), access_context(), body_override=body)
        new = await world.rig.call("POST", "/v1/videos", json_body=body)
        assert new.status_code == old.status_code, (new.text, old.body)
        if missing:
            assert new.status_code == 404
            assert new.json() == json.loads(old.body)
        else:
            assert new.status_code == 200
            await submitted(world, 2)
            assert json.dumps(world.engine.prompts["p1"], sort_keys=True) == json.dumps(world.engine.prompts["p2"], sort_keys=True)
    asyncio.run(drive())


@pytest.mark.parametrize("model,reason", [("video-ltx-2.5-keyframes", "ltx_latent_8np1_stride"),
                                          ("video-minimax-h3-keyframes", "frame_count_stride")])
def test_duration_reason_matches_the_old_handler_for_configured_and_generic_grids(world, model, reason):
    uri = "data:image/png;base64," + base64.b64encode(png_bytes()).decode()
    world.rig.set(proxy, "_WORKFLOW_JOB_SPOOL", None)
    body = {"model": model, "prompt": "same duration header", "seconds": 3.2, "seed": 7,
            "input_reference": uri, "extensions": {"com.azizam.workflow": {"last_frame": uri}}}
    async def drive():
        old = await proxy.proxy_graph_video_create(json_request(body), access_context(), body_override=body)
        new = await world.rig.call("POST", "/v1/videos", json_body=body)
        assert new.status_code == old.status_code == 200
        assert new.headers["x-duration-adjusted"] == old.headers["x-duration-adjusted"]
        assert new.headers["x-duration-adjusted"].endswith("reason=" + reason)
    asyncio.run(drive())


def test_existing_row_duration_reason_keeps_the_main_public_answer(world):
    # Measured on fd30cbfca through POST /v1/videos, not the unmounted old handler.
    async def drive():
        answer = await world.rig.call("POST", "/v1/videos", json_body={
            "model": "video-ltx-2.5-t2v", "prompt": "main reference", "seconds": 3.2, "seed": 7})
        assert answer.status_code == 200, answer.text
        assert answer.headers["x-duration-adjusted"] == "requested=3.2 actual=3.3750 reason=frame_count_stride"
    asyncio.run(drive())
