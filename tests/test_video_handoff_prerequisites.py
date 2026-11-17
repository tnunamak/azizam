"""Executable evidence for the remaining video-create migration prerequisites.

The variant checks cover removal of the unreachable missing-facts handoff. The chain
comparison deliberately compiles the static declared workflow independently of
routing, to expose the planner/composer integration still needed by the run path.
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
        if body["model"] == "ltx-2.5-t2v":
            assert response.json()["id"].startswith("video_mj_")
        await submitted(world, 2)
        assert world.engine.prompts["p1"] == world.engine.prompts["p2"]

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

        # legacy_route is normally consulted even for supported requests; return
        # None after checking selection rather than bypassing the selection.
        async def checked_selection(shape, request, row, snapshot):
            body_read = await video_create.read_body(shape, request)
            from gateway.frontend_shapes import shapes
            picked = video_create.select(shape, body_read, row, snapshot, shapes.VIDEO_HOOKS)
            from gateway.frontend_shapes._base import LegacyRoute
            assert not isinstance(picked, LegacyRoute)
            return None

        world.rig.set(video_create, "legacy_route", checked_selection)
        response = await world.rig.call("POST", "/v1/videos", json_body=body)
        assert response.status_code == 200, response.text
        assert response.json()["id"].startswith("video_mj_")
        await submitted(world, 2)
        assert world.engine.prompts["p1"] == world.engine.prompts["p2"]

    asyncio.run(drive())
