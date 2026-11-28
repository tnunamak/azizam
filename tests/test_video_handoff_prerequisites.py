"""Public video creates submit the same engine bodies as the frozen main handler.

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
from tests.support import video_main_oracle as main_video
from gateway.engine_protocol.types import CanonicalRequest
from tests.media_test_bytes import png_bytes
from gateway.manifests import enforce_contract, ManifestError
from tests.support.access import access_context, everything
from tests.test_frontend_shapes import json_request
from tests.test_s7_parity_video_create import submitted, world  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def non_video_scalar_contract():
    from tests.workflow_runtime_fakes import entry

    contract = {
        "schema_version": 2, "id": "test.scalar", "version": "1", "intent": "test.scalar",
        "inputs": {"amount": {"type": "number"}, "enabled": {"type": "boolean"}},
        "outputs": {"result": {"media_type": "application/json"}},
        "bindings": {"test": {"map": {
            "amount": [{"path": "payload.amount"}],
            "enabled": [{"path": "payload.enabled"}],
        }}},
    }
    return contract, entry(contract)


@pytest.mark.parametrize("name,value,expected_type,token", [
    pytest.param("amount", 4, int, "4", id="json-int"),
    pytest.param("amount", 4.0, float, "4.0", id="json-float"),
    pytest.param("enabled", True, bool, "true", id="json-boolean"),
    pytest.param("amount", "4", float, "4.0", id="form-number"),
])
def test_non_video_scalar_coercion_and_binding_preserve_json_types(
        non_video_scalar_contract, name, value, expected_type, token):
    from gateway.frontend_shapes._base import coerce
    from gateway.manifests import bind_backend_request, validate_inputs

    contract, chosen = non_video_scalar_contract
    supplied = {name: value}
    supplied_type = type(value)
    supplied_json = json.dumps(supplied, sort_keys=True, separators=(",", ":"))
    coerced = {name: coerce(chosen, name, value)}
    assert type(coerced[name]) is expected_type
    assert json.dumps(coerced[name]) == token
    validated = validate_inputs(contract, coerced)
    assert validated.rejected == {}
    assert validated.errors == []
    accepted = validated.accepted
    expected_json = '{"' + name + '":' + token + '}'
    assert type(accepted[name]) is expected_type
    assert json.dumps(accepted, sort_keys=True, separators=(",", ":")) == expected_json

    template = {"payload": {"untouched": "keep"}}
    template_before = copy.deepcopy(template)
    bound = bind_backend_request(contract, "test", template, accepted)
    assert bound.applied_inputs == [name]
    assert bound.ignored_inputs == {}
    assert type(bound.request["payload"][name]) is expected_type
    assert json.dumps(bound.request["payload"][name]) == token
    assert json.dumps(bound.request, sort_keys=True, separators=(",", ":")) == (
        '{"payload":{"' + name + '":' + token + ',"untouched":"keep"}}'
    )

    # Equality alone cannot detect an int changed to float, or a bool to an int.
    assert type(supplied[name]) is supplied_type
    assert json.dumps(supplied, sort_keys=True, separators=(",", ":")) == supplied_json
    assert type(coerced[name]) is expected_type
    assert type(accepted[name]) is expected_type
    assert json.dumps(coerced, sort_keys=True, separators=(",", ":")) == expected_json
    assert json.dumps(accepted, sort_keys=True, separators=(",", ":")) == expected_json
    assert template == template_before


def test_non_video_number_refuses_json_boolean_without_coercing_it(non_video_scalar_contract):
    from gateway.frontend_shapes._base import coerce
    from gateway.manifests import validate_inputs

    contract, chosen = non_video_scalar_contract
    supplied = {"amount": True}
    coerced = {"amount": coerce(chosen, "amount", supplied["amount"])}
    assert type(coerced["amount"]) is bool
    assert json.dumps(coerced, separators=(",", ":")) == '{"amount":true}'
    validated = validate_inputs(contract, coerced)
    assert validated.accepted == {}
    assert validated.rejected == {"amount": "amount must be a number"}
    assert [(error.path, error.code) for error in validated.errors] == [
        (("inputs", "amount"), "value_invalid"),
    ]
    assert type(supplied["amount"]) is bool
    assert type(coerced["amount"]) is bool
    assert json.dumps(supplied, separators=(",", ":")) == '{"amount":true}'
    assert json.dumps(coerced, separators=(",", ":")) == '{"amount":true}'


SUBMIT_FAILURE_FIXTURE_CONTRACT = {
    "_reference": {
        "revision": "fd30cbfcaa8e7f079474b4c8e6d221bb5f18a814",
        "proof_commit": "3cb3bf253",
        "probe_sha256": "c43eb01c821e302351c67af805659723c31e71bab28a394042771e7ee0de105e",
        "route": "POST /v1/videos",
    },
    "request": {
        "model": "ltx-2.5-long",
        "prompt": "x",
        "seconds": 16,
        "seed": 7,
    },
    "status": 502,
    "body": {
        "error": {
            "message": "ComfyUI unavailable at http://comfy.fk.test",
            "type": "api_error",
        },
    },
}


def validate_submit_failure_fixture(fixture: dict) -> None:
    assert fixture == SUBMIT_FAILURE_FIXTURE_CONTRACT, "submit failure fixture drift"


def test_submit_failure_fixture_contract():
    fixture = json.loads(
        (ROOT / "tests/fixtures/video_submit_failure_fd30cbfca.json").read_text()
    )
    assert fixture == SUBMIT_FAILURE_FIXTURE_CONTRACT
    validate_submit_failure_fixture(fixture)


@pytest.mark.parametrize("field", ["revision", "error.type"])
def test_submit_failure_fixture_contract_rejects_drift(field):
    fixture = json.loads(
        (ROOT / "tests/fixtures/video_submit_failure_fd30cbfca.json").read_text()
    )
    validate_submit_failure_fixture(fixture)
    changed = copy.deepcopy(fixture)
    if field == "revision":
        changed["_reference"]["revision"] = "changed-revision"
    else:
        changed["body"]["error"]["type"] = "changed-error-type"
    with pytest.raises(AssertionError, match="submit failure fixture drift"):
        validate_submit_failure_fixture(changed)
    assert fixture == SUBMIT_FAILURE_FIXTURE_CONTRACT


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
        old = await main_video.create(
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
        old = await main_video.create(
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
        old = await main_video.create(
            json_request(body), access_context(everything(media_metadata="none", receipt_detail="full")),
            body_override=body)
        assert old.status_code == 200, old.body
        assert len(world.engine.prompts) == 1

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
        old = await main_video.create(
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


@pytest.mark.parametrize("case", ["named-image", "named-image-object", "mapped-reference", "variant-no-facts", "url-image", "vision-object", "secondary-image"])
def test_complex_inputs_submit_the_old_handlers_graph_on_the_declared_path(world, case):
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

    async def drive():
        old = await main_video.create(json_request(body), auth, body_override=body)
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
        old = await main_video.create(
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


def test_queued_chain_keeps_accepted_grid_size_and_segments(world):
    from tests.support.config_snapshot import publish_section
    from tests.test_s7_parity_video_create import MODEL, await_status
    from gateway import video_extend

    world.rig.set(proxy, "_WORKFLOW_JOB_SPOOL", None)
    name, configured = proxy._chain_workflow_for(MODEL)
    assert name is not None and configured is not None
    original = copy.deepcopy(configured)
    engine = video_extend.engine_for(original["builder"])
    assert engine is not None
    body = {"model": MODEL, "prompt": "accepted configuration", "seconds": 16, "seed": 7}
    shots = [(body["prompt"], body["seconds"])]
    segments = engine.plan_segments(
        shots, fps=float(original.get("frame_rate", 24)), has_source=False,
        max_segment_seconds=float(original.get("chain_segment_seconds", proxy._video_chain_segment_seconds())))
    expected_segments = len(segments)
    assert expected_segments > 1
    queued = []
    world.rig.set(proxy, "_schedule_workflow_run", queued.append)

    async def drive():
        auth = access_context(everything(media_metadata="none", receipt_detail="full"))
        old = await main_video.create(json_request(body), auth, body_override=body)
        assert old.status_code == 200, old.body
        old_post = json.loads(old.body)
        expected_graph = copy.deepcopy(world.engine.prompts["p1"])
        expected_units = copy.deepcopy(proxy._video_chain_units["p1"])
        assert old_post["segments"] == expected_segments
        old_status = await main_video.status(old_post["id"], auth)
        assert old_status.status_code == 200, old_status.body
        before = json.loads(old_status.body)
        fields = ("seconds", "actual_seconds", "total_seconds", "size")
        expected = {key: before[key] for key in fields}
        assert before["segments_total"] == expected_segments

        response = await world.rig.call("POST", "/v1/videos", json_body=body)
        assert response.status_code == 200, response.text
        accepted = response.json()
        assert accepted["id"].startswith("video_mj_")
        assert len(queued) == 1 and len(world.engine.prompts) == 1
        assert {key: accepted[key] for key in fields} == {key: old_post[key] for key in fields}
        assert accepted["segments"] == expected_segments

        videos = copy.deepcopy(proxy.config["videos"])
        changed = videos["workflows"][name]
        changed.update(frame_rate=12, default_size="320x192", chain_segment_seconds=4)
        assert float(original.get("frame_rate", 24)) != changed["frame_rate"]
        assert old_post["size"] != changed["default_size"]
        changed_segments = engine.plan_segments(
            shots, fps=12, has_source=False, max_segment_seconds=4)
        assert len(changed_segments) != expected_segments
        publish_section(world.rig.monkeypatch, "videos", videos)
        live_name, live = proxy._chain_workflow_for(MODEL)
        assert live_name == name
        assert {key: live[key] for key in ("frame_rate", "default_size", "chain_segment_seconds")} == {
            "frame_rate": 12, "default_size": "320x192", "chain_segment_seconds": 4}
        assert video_extend.engine_for(original["builder"]) is engine

        await proxy._run_workflow_job(queued[0])
        await await_status(world, accepted["id"])
        assert len(world.engine.prompts) == 2
        assert json.dumps(world.engine.prompts["p2"], sort_keys=True) == json.dumps(expected_graph, sort_keys=True)
        assert len(proxy._video_chain_units["p2"]) == expected_segments
        assert proxy._video_chain_units["p2"] == expected_units
        status = await world.rig.call("GET", "/v1/videos/" + accepted["id"])
        assert status.status_code == 200, status.text
        after = status.json()
        assert {key: after[key] for key in fields} == expected
        assert after["segments"] == expected_segments
        assert after["segments_total"] == expected_segments

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
        old = await main_video.create(json_request(body), auth, body_override=body)
        assert old.status_code == 200, old.body
        old_body = json.loads(old.body)
        made = await world.rig.call("POST", "/v1/videos", json_body=body)
        assert made.status_code == 200, made.text
        # Job ids and wall times are independent. Compare every other POST field.
        independent = {"id", "created_at", "expires_at"}
        assert {key: value for key, value in made.json().items() if key not in independent} == {
            key: value for key, value in old_body.items() if key not in independent}
        await await_status(world, made.json()["id"])
        before = json.loads((await main_video.status(old_body["id"], auth)).body)
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
        old = await main_video.create(json_request(body), access_context(), body_override=body)
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
        old = await main_video.create(json_request(body), access_context(), body_override=body)
        new = await world.rig.call("POST", "/v1/videos", json_body=body)
        assert new.status_code == old.status_code == 200
        assert new.headers["x-duration-adjusted"] == old.headers["x-duration-adjusted"]
        assert new.headers["x-duration-adjusted"].endswith("reason=" + reason)
    asyncio.run(drive())


def test_declared_chain_failure_matches_the_old_segment_error(world):
    import httpx
    from tests.support.fake_upstream import HandlerTransport
    from tests.test_s7_parity_video_create import HOST, await_status
    world.engine.failed = True
    def handle(request):
        response = world.engine(request)
        if request.url.path.startswith('/history/'):
            body = response.json()
            for entry in body.values():
                entry['status']['messages'] = [['execution_error', {'node_id': 's1_sample', 'exception_message': 'boom'}]]
            return httpx.Response(200, json=body)
        return response
    world.rig.monkeypatch.setitem(world.rig.fakes.router._routes, HOST, HandlerTransport(handle))
    body = {'model':'ltx-2.5-long', 'prompt':'failing chain', 'seconds':20, 'seed':7}
    auth = access_context(everything(media_metadata='none', receipt_detail='full'))
    async def drive():
        old = await main_video.create(json_request(body), auth, body_override=body)
        before = json.loads((await main_video.status(json.loads(old.body)['id'], auth)).body)
        made = await world.rig.call('POST','/v1/videos',json_body=body)
        after = await await_status(world, made.json()['id'], status='failed')
        assert after['error'] == before['error']
        assert after['error']['message'].startswith('segment 2 of 3 failed (1 finished);')
    asyncio.run(drive())


def test_declared_chain_progress_uses_all_units_and_live_node_events(world):
    from tests.support.fake_upstream import HandlerTransport
    from tests.test_s7_parity_video_create import HOST, await_status
    gate = asyncio.Event()
    async def handle(request):
        if request.url.path.startswith('/history/'):
            await gate.wait()
        return world.engine(request)
    world.rig.monkeypatch.setitem(world.rig.fakes.router._routes, HOST, HandlerTransport(handle))
    async def drive():
        made = await world.rig.call('POST','/v1/videos',json_body={'model':'ltx-2.5-long','prompt':'progress','seconds':20,'seed':7})
        await submitted(world, 1)
        # Submission registration precedes the first history observation.
        for _ in range(100):
            if 'p1' in proxy._video_chain_units and len(proxy._video_chain_units['p1']) == 3:
                break
            await asyncio.sleep(0)
        units = proxy._video_chain_units['p1']
        assert len(units) == 3
        await proxy._record_event({'type':'progress','data':{'prompt_id':'p1','node':units[1]['sample'],'value':5,'max':10}})
        status = (await world.rig.call('GET','/v1/videos/'+made.json()['id'])).json()
        assert status['segments_total'] == 3 and status['segments_completed'] == 1
        assert status['progress'] == proxy._video_percent('p1', units[1]['sample'], 'sampling', 0.5)
        gate.set()
        await await_status(world, made.json()['id'])
    asyncio.run(drive())


def test_existing_row_duration_reason_keeps_the_main_public_answer(world):
    # Measured on fd30cbfca through POST /v1/videos, not the unmounted old handler.
    async def drive():
        answer = await world.rig.call("POST", "/v1/videos", json_body={
            "model": "video-ltx-2.5-t2v", "prompt": "main reference", "seconds": 3.2, "seed": 7})
        assert answer.status_code == 200, answer.text
        assert answer.headers["x-duration-adjusted"] == "requested=3.2 actual=3.3750 reason=frame_count_stride"
    asyncio.run(drive())


@pytest.mark.parametrize("case", ["operator-seconds", "operator-shots"])
def test_operator_chain_without_a_manifest_long_variant_keeps_mains_refusal(world, case):
    from dataclasses import replace
    from gateway.workflow_run import plain
    original = proxy.WORKFLOW_SNAPSHOTS.current()
    key = "video.generate.ltx-2.5-t2v"
    entry = original.get(key)
    contract = plain(entry.contract)
    contract["video"]["variants"].pop("long")
    enforce_contract(contract)
    changed = replace(entry, contract=contract)
    class Snapshot:
        workflows = {**original.workflows, key: changed}
        def get(self, workflow_id):
            return changed if workflow_id == key else original.get(workflow_id)
        def by_intent(self, intent):
            return tuple(self.get(item.id) for item in original.by_intent(intent))
        def __getattr__(self, name):
            return getattr(original, name)
    world.rig.set(proxy, "WORKFLOW_SNAPSHOTS", type("Provider", (), {"current": lambda self: Snapshot()})())
    expected = json.loads((ROOT / "tests/fixtures/video_operator_chain_fd30cbfca.json").read_text())[case]
    async def drive():
        response = await world.rig.call("POST", "/v1/videos", json_body=expected["request"])
        assert response.status_code == expected["status"] == 400
        assert response.json() == expected["body"]
        assert world.engine.calls == []
    asyncio.run(drive())


def test_main_video_oracle_function_bytes_are_pinned():
    main_video.checked_source()


@pytest.mark.parametrize("case", ["row-plain", "row-resolution"])
def test_missing_workflow_row_matches_the_main_public_route(world, case):
    from tests.support.config_snapshot import publish_section
    expected = json.loads((ROOT / "tests/fixtures/video_public_rows_fd30cbfca.json").read_text())[case]
    videos = copy.deepcopy(proxy.config["videos"])
    del videos["workflows"]["video-ltx-2.5-t2v"]
    publish_section(world.rig.monkeypatch, "videos", videos)
    world.rig.set(proxy, "_WORKFLOW_JOB_SPOOL", None)
    async def drive():
        response = await world.rig.call("POST", "/v1/videos", json_body=expected["request"])
        assert response.status_code == expected["status"], response.text
        independent = {"id", "created_at", "completed_at", "expires_at", "updated_at"}
        assert {key:value for key,value in response.json().items() if key not in independent} == expected["body"]
        if response.status_code == 200:
            await submitted(world, 1)
        else:
            assert world.engine.prompts == {}
    asyncio.run(drive())


@pytest.mark.parametrize("path", ["/v1/videos", "/openai/v1/videos"])
def test_chain_policy_denial_matches_main_before_engine_or_receipt(world, path):
    from tests.support.access import authenticated, identity, plan_for
    from tests.test_s7_parity_video_create import rows
    expected = json.loads((ROOT / "tests/fixtures/video_chain_receipts_fd30cbfca.json").read_text())["chain-denied"]
    async def drive():
        with authenticated(identity(plan_for(caps=(), categories=()))):
            response = await world.rig.call("POST", path, json_body=expected["request"])
        assert response.status_code == expected["status"] == 403
        assert response.json() == expected["body"]
        assert rows(world.rig.receipts_db, "receipts") == expected["receipts"] == []
        assert world.engine.calls == []
    asyncio.run(drive())


@pytest.mark.parametrize("path", ["/v1/videos", "/openai/v1/videos"])
def test_chain_create_keeps_main_receipt_fields_and_names_the_declared_job(world, path):
    from tests.test_s7_parity_video_create import rows, receipt, await_status
    captured = json.loads((ROOT / "tests/fixtures/video_chain_receipts_fd30cbfca.json").read_text())
    expected = captured["chain-receipt"]
    world.engine.pending = True
    async def drive():
        try:
            response = await world.rig.call("POST", path, json_body=expected["request"])
            assert response.status_code == expected["status"] == 200, response.text
            assert len(rows(world.rig.receipts_db, "receipts")) == 1
            kept = receipt(world, response.headers["x-request-id"])
            # Wall times and independent ids vary; a declared submit receipt
            # additionally names its job, as the accepted job-path contract says.
            assert kept["job_id"] == response.json()["id"].removeprefix("video_")
            evidence = kept.pop("resource_decision")
            assert evidence["outcome"] == "allowed"
            assert any(fact["name"] == "available" and fact["value"] is True for fact in evidence["facts"])
            assert {key:value for key,value in kept.items() if key not in captured["receipt_exclusions"]} == expected["receipts"][0]
        finally:
            world.engine.pending = False
        await await_status(world, response.json()["id"])
    asyncio.run(drive())


def test_chain_submit_failure_is_the_accepted_deferred_error_with_main_cause(world):
    import httpx
    from tests.support.fake_upstream import HandlerTransport
    from tests.test_s7_parity_video_create import HOST, await_status, receipt
    expected = json.loads((ROOT / "tests/fixtures/video_submit_failure_fd30cbfca.json").read_text())
    assert expected["status"] == 502
    def handle(request):
        if request.method == "POST" and request.url.path == "/prompt":
            raise httpx.ConnectError("connection refused", request=request)
        return world.engine(request)
    world.rig.monkeypatch.setitem(world.rig.fakes.router._routes, HOST, HandlerTransport(handle))
    async def drive():
        created = await world.rig.call("POST", "/v1/videos", json_body=expected["request"])
        assert created.status_code == 200, created.text
        assert created.json()["status"] == "queued"
        assert created.json()["id"].startswith("video_mj_")
        failed = await await_status(world, created.json()["id"], status="failed")
        assert expected["body"]["error"]["message"] == "ComfyUI unavailable at http://comfy.fk.test"
        # The existing job view removes backend addresses from public errors.
        assert failed["error"] == {"code": "video_generation_failed", "message": "ComfyUI unavailable"}
        kept = receipt(world, created.headers["x-request-id"])
        assert kept["status"] == "succeeded" and kept["metadata"]["status_code"] == 200
    asyncio.run(drive())


def test_queued_chain_freezes_operator_facts_through_execution_and_polling(world):
    from tests.support.config_snapshot import publish_section
    from tests.support.fake_upstream import HandlerTransport
    from tests.test_s7_parity_video_create import HOST
    from gateway import video_extend

    body = {"model": "ltx-2.5-long", "prompt": "accepted operator plan", "seconds": 20, "seed": 7,
            "extensions": {"com.azizam.workflow": {"state_ledger": "same scene", "context_seconds": 1.0}}}
    auth = access_context(everything(media_metadata="none", receipt_detail="full"))
    name = "video-ltx-2.5-long"
    plan_a = copy.deepcopy(proxy.config["videos"])
    plan_a["workflows"][name].update(frame_rate=24, default_size="960x512", chain_segment_seconds=8)
    builder_a = plan_a["workflows"][name]["builder"]
    builder_b = plan_a["workflows"]["video-minimax-h3-long"]["builder"]
    assert builder_a != builder_b
    assert builder_a in video_extend.ENGINES and builder_b in video_extend.ENGINES
    publish_section(world.rig.monkeypatch, "videos", plan_a)
    queued = []
    world.rig.set(proxy, "_schedule_workflow_run", queued.append)

    async def drive():
        # The frozen main handler, under A, supplies the independent complete
        # graph and units before either the accepted job or any config mutation.
        old = await main_video.create(json_request(body), auth, body_override=body)
        assert old.status_code == 200, old.body
        expected_post = json.loads(old.body)
        expected_graph = copy.deepcopy(world.engine.prompts["p1"])
        expected_units = copy.deepcopy(proxy._video_chain_units["p1"])
        assert expected_post["segments"] >= 2
        assert len(expected_units) >= 2
        made = await world.rig.call("POST", "/v1/videos", json_body=body)
        assert made.status_code == 200, made.text
        accepted = made.json()
        fields = ("seconds", "total_seconds", "segments", "size", "actual_seconds",
                  "model", "azizam_resolved_model", "extended_from_video_id")
        assert {key: accepted[key] for key in fields} == {key: expected_post[key] for key in fields}
        assert len(queued) == 1 and len(world.engine.prompts) == 1

        # These are the actual workflow-row keys used by the existing main
        # oracle, not invented runner hooks. Keep both templates installed.
        plan_b = copy.deepcopy(plan_a)
        plan_b["workflows"][name].update(
            builder=builder_b, frame_rate=12, default_size="640x384", chain_segment_seconds=4)
        publish_section(world.rig.monkeypatch, "videos", plan_b)
        assert builder_a in video_extend.ENGINES and builder_b in video_extend.ENGINES

        # Discriminate a no-op fixture: a fresh main request now observes B.
        # Never use this graph as the expected result for the accepted A job.
        live = await main_video.create(json_request(body), auth, body_override=body)
        assert live.status_code == 200, live.body
        live_post = json.loads(live.body)
        assert live_post["size"] != expected_post["size"]
        assert live_post["segments"] != expected_post["segments"]
        assert world.engine.prompts["p2"] != expected_graph
        assert proxy._video_chain_units["p2"] != expected_units

        gate = asyncio.Event()
        history_entered = asyncio.Event()
        async def handle(request):
            if request.url.path == "/history/p3":
                history_entered.set()
                await gate.wait()
            return world.engine(request)
        world.rig.monkeypatch.setitem(world.rig.fakes.router._routes, HOST, HandlerTransport(handle))
        runner = asyncio.create_task(proxy._run_workflow_job(queued[0]))
        try:
            await submitted(world, 3)
            async def registered_units():
                while "p3" not in proxy._video_chain_units:
                    await asyncio.sleep(0)
            await asyncio.wait_for(registered_units(), timeout=5)
            assert json.dumps(world.engine.prompts["p3"], sort_keys=True) == json.dumps(expected_graph, sort_keys=True)
            assert proxy._video_chain_units["p3"] == expected_units

            # C is published before either public GET or the blocked engine
            # history observation. Both the pending and finished views keep A.
            plan_c = copy.deepcopy(plan_b)
            plan_c["workflows"][name].update(
                builder=builder_a, frame_rate=30, default_size="320x192", chain_segment_seconds=2)
            publish_section(world.rig.monkeypatch, "videos", plan_c)
            await asyncio.wait_for(history_entered.wait(), timeout=5)
            assert not runner.done()
            pending = await world.rig.call("GET", "/v1/videos/" + accepted["id"])
            assert pending.status_code == 200, pending.text
            assert pending.json()["status"] in {"queued", "in_progress"}
            assert {key: pending.json()[key] for key in fields} == {key: accepted[key] for key in fields}
            gate.set()
            await asyncio.wait_for(runner, timeout=10)
            finished = await world.rig.call("GET", "/v1/videos/" + accepted["id"])
            assert finished.status_code == 200, finished.text
            assert finished.json()["status"] == "completed"
            assert {key: finished.json()[key] for key in fields} == {key: accepted[key] for key in fields}
        finally:
            gate.set()
            if not runner.done():
                await asyncio.wait_for(runner, timeout=10)

    asyncio.run(drive())
