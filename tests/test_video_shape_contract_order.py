"""Frozen video shape contracts: path sources, response privacy, duration and check order."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from starlette.responses import Response

from gateway import frontend_shapes
from gateway.frontend_shapes import _wire, shapes
from gateway.video_source import VideoSource
from gateway.workflow_runtime_types import MediaRef, RunJobView, RunRequest
from tests import test_video_create_shape as fixtures


@pytest.fixture(autouse=True)
def no_deployment_hooks(monkeypatch):
    monkeypatch.setattr(shapes, "DEFAULT_WORKFLOWS", {})
    monkeypatch.setattr(shapes, "VIDEO_HOOKS", None)


def _view(run_request):
    return RunJobView(
        job_id="mj_new", status="queued", disposition="new", workflow_id=run_request.workflow_id,
        record_revision=1, binding_key="comfyui", created_at_s=1000, updated_at_s=1000)


def _create(body, *, person_id=None):
    caller = SimpleNamespace(person_id=person_id)
    request = fixtures.json_request({"prompt": "x", "model": "ltx-2.5-t2v", **body})
    request.state.caller = caller
    shape = frontend_shapes.build_shape(fixtures.ROWS["videos-create"])
    run_request = asyncio.run(shape.to_run_request(
        request, fixtures.ROWS["videos-create"], fixtures.SNAP, spool=fixtures.FakeSpool()))
    assert isinstance(run_request, RunRequest), getattr(run_request, "body", run_request)
    response = asyncio.run(shape.to_response(
        _view(run_request), fixtures.ROWS["videos-create"], jobs=fixtures._NoFacts(), caller=caller, request=request))
    return shape, run_request, response


def test_remix_reads_the_path_source_and_binds_its_role_with_lineage(monkeypatch):
    seen = []
    caller = SimpleNamespace(person_id="person-1")

    class Hooks(shapes.VideoHooks):
        async def source(self, job_id, wire_id, request_caller):
            seen.append((job_id, wire_id, request_caller))
            return VideoSource(
                wire_id=wire_id, asset_id="asset-source", model="m", resolved_model="video-m",
                fps=30.0, size="1280x704", seconds=4.2, has_audio=True)

    monkeypatch.setattr(shapes, "VIDEO_HOOKS", Hooks())
    contract = fixtures.video_contract("remix")
    # An image before the source must not receive the stored video.
    contract["inputs"]["poster"] = {"type": "asset", "accepts": ["image/*"]}
    contract["inputs"]["video"] = {"type": "asset", "accepts": ["video/*"], "required": True}
    contract["intent"] = "video.edit"
    contract["video"]["roles"]["source"] = "video"
    snapshot = fixtures.snap(contract)
    row = fixtures.ROWS["videos-remix"]
    shape = frontend_shapes.build_shape(row)
    request = fixtures.json_request({"prompt": "x", "model": contract["id"]})
    request.scope["path_params"] = {"video_id": "video_mj_source"}
    request.state.caller = caller

    assert asyncio.run(shape.legacy_route(request, row, snapshot)) is None
    run_request = asyncio.run(shape.to_run_request(request, row, snapshot, spool=fixtures.FakeSpool()))
    assert isinstance(run_request, RunRequest), getattr(run_request, "body", run_request)
    assert seen == [("mj_source", "video_mj_source", caller)]
    assert tuple(run_request.media) == ("video",)
    ref, = run_request.media["video"]
    assert isinstance(ref, MediaRef) and ref.asset_id == "asset-source"
    assert run_request.inputs["frame_rate"] == 30
    assert run_request.facts["remixed_from_video_id"] == "video_mj_source"
    assert run_request.facts["source_asset_id"] == "asset-source"
    response = asyncio.run(shape.to_response(
        _view(run_request), row, jobs=fixtures._NoFacts(), caller=caller, request=request))
    assert json.loads(response.body)["remixed_from_video_id"] == "video_mj_source"
    assert json.loads(response.body)["prompt"] is None


def test_remix_without_a_configured_workflow_keeps_its_501(monkeypatch):
    class Hooks(shapes.VideoHooks):
        async def source(self, *args):
            raise AssertionError("an unconfigured remix must not look up its source")

    monkeypatch.setattr(shapes, "VIDEO_HOOKS", Hooks())
    row = fixtures.ROWS["videos-remix"]
    shape = frontend_shapes.build_shape(row)
    request = fixtures.json_request({"prompt": "x"})
    request.scope["path_params"] = {"video_id": "video_mj_source"}
    response = asyncio.run(shape.to_run_request(request, row, fixtures.snap(), spool=fixtures.FakeSpool()))
    # The 501 itself is the row layer's answer (row_routes, before the shape runs): it fires when the row declares
    # ``unconfigured`` and selects no workflow. Here the shape must refuse without a source lookup (the hook raises).
    assert row.unconfigured["status"] == 501 and row.unconfigured["code"] == "video_remix_not_configured"
    assert not _wire.candidates(fixtures.snap(), row)
    assert isinstance(response, Response) and response.status_code == 404


@pytest.mark.parametrize("person_id,expected_prompt", [(None, "x"), ("person-1", None)])
def test_create_only_echoes_the_validated_prompt_for_an_anonymous_caller(person_id, expected_prompt):
    shape, run_request, response = _create({"prompt": "  x  "}, person_id=person_id)
    assert json.loads(response.body)["prompt"] == expected_prompt
    assert "prompt" not in run_request.facts
    assert "  x  " not in json.dumps(run_request.facts)
    # Later status reads use recorded facts, not the create request's transient prompt.
    assert shape.status_body(_view(run_request), run_request.facts)["prompt"] is None


@pytest.mark.parametrize("seconds,adjusted", [(3, True), (73 / 24, False), (3.0417, True)])
def test_duration_header_compares_exact_frames_and_rate_not_a_tolerance_or_rounded_fact(seconds, adjusted):
    _, run_request, response = _create({"seconds": seconds})
    assert run_request.facts["length_frames"] == 73
    assert run_request.facts["frame_rate"] == 24
    assert ("X-Duration-Adjusted" in response.headers) is adjusted
    if adjusted:
        assert response.headers["X-Duration-Adjusted"] == (
            f"requested={seconds:g} actual={73 / 24:.4f} reason=frame_count_stride")


@pytest.mark.parametrize("extra,message", [
    ({"seconds": "bad"}, "seconds must be a number"),
    ({"size": []}, "size must be a string like 1280x720"),
    ({}, "last_image is required for this model"),
])
def test_seconds_and_size_are_checked_before_required_secondary_image_extensions(extra, message):
    contract = fixtures.video_contract("keyframes")
    contract["inputs"]["last_image"] = {"type": "asset", "accepts": ["image/*"], "required": True}
    contract["video"]["roles"]["last_image"] = "last_image"
    response = fixtures.read({"prompt": "x", "model": "keyframes", **extra}, fixtures.snap(contract))
    assert response.status_code == 400
    assert fixtures.error(response)["message"] == message
