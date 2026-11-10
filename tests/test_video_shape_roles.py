"""Video media bind to manifest roles, and edits retain source lineage."""

from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

from gateway.frontend_shapes import _base, shapes
from gateway.video_source import VideoSource
from gateway.workflow_runtime_types import MediaRef, RunRequest
from tests.test_frontend_shapes import FakeSpool
from tests.test_video_create_shape import PNG, read, snap, video_contract


@pytest.fixture(autouse=True)
def isolated_video_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shapes, "DEFAULT_WORKFLOWS", {})
    monkeypatch.setattr(shapes, "VIDEO_HOOKS", None)


def image_contract(target: str | None = "input_reference") -> dict:
    contract = video_contract("roles-i2v", image=True, requires=["image"])
    image = contract["inputs"].pop("input_reference")
    image["required"] = True
    contract["inputs"][target or "input_reference"] = image
    if target is None:
        contract["video"]["roles"].pop("image")
    else:
        contract["video"]["roles"]["image"] = target
    return contract


def create_body(contract: dict) -> dict:
    return {"model": contract["id"], "prompt": "waves",
            "input_reference": "data:image/png;base64," + base64.b64encode(PNG).decode()}


@pytest.mark.parametrize("target", ["input_reference", "first_frame", None])
def test_a_supplied_primary_image_fills_its_required_role_input(target: str | None) -> None:
    contract = image_contract(target)
    spool = FakeSpool()
    result = read(create_body(contract), snap(contract), spool=spool)
    expected = target or "input_reference"
    assert isinstance(result, RunRequest), getattr(result, "body", result)
    assert list(result.media) == [expected]
    assert spool.staged == [(expected, PNG, "input_reference.png", "image/png")]
    assert result.media[expected][0].spool_key == "k1"


def test_an_absent_primary_image_is_not_exempted_from_required_assets() -> None:
    contract = image_contract()
    contract["video"]["requires"] = []
    result = read({"model": contract["id"], "prompt": "waves"}, snap(contract))
    assert result.status_code == 400
    assert json.loads(result.body)["error"]["message"] == "input_reference is required for this model"


def test_a_supplied_primary_image_does_not_satisfy_a_required_secondary_image() -> None:
    contract = image_contract()
    contract["inputs"]["guide"] = {"type": "asset", "accepts": ["image/*"], "required": True}
    result = read(create_body(contract), snap(contract))
    assert result.status_code == 400
    assert json.loads(result.body)["error"]["message"] == "guide is required for this model"


def edit_contract(*, source_role: bool = False) -> dict:
    contract = video_contract("roles-edit")
    contract["intent"] = "video.edit"
    contract["inputs"]["guide"] = {"type": "asset", "accepts": ["image/*"]}
    contract["inputs"]["video"] = {"type": "asset", "accepts": ["video/*"], "required": True}
    if source_role:
        contract["video"]["roles"]["source"] = "video"
    return contract


@pytest.fixture
def source(monkeypatch: pytest.MonkeyPatch) -> VideoSource:
    found = VideoSource(wire_id="video_mj_source", asset_id="asset-source", model="source-model",
                        resolved_model="video-source-model", fps=30.0, size="1280x704",
                        seconds=4.2, has_audio=True)

    class Hooks(shapes.VideoHooks):
        async def source(self, job_id, wire_id, caller):
            assert job_id == "mj_source"
            assert wire_id == found.wire_id
            return found

    monkeypatch.setattr(shapes, "VIDEO_HOOKS", Hooks())
    return found


def edit_request(contract: dict, source: VideoSource):
    return read({"model": contract["id"], "prompt": "night", "video": {"id": source.wire_id}},
                snap(contract), row="videos-edit")


@pytest.mark.parametrize("source_role", [False, True])
def test_edit_skips_an_optional_image_before_the_video_input(source: VideoSource, source_role: bool) -> None:
    result = edit_request(edit_contract(source_role=source_role), source)
    assert isinstance(result, RunRequest), getattr(result, "body", result)
    assert list(result.media) == ["video"]
    assert result.media["video"][0].asset_id == source.asset_id


def test_edit_prefers_the_source_role_over_an_earlier_video_input(source: VideoSource) -> None:
    contract = edit_contract()
    contract["inputs"]["video"]["required"] = False
    contract["inputs"]["source_clip"] = {"type": "asset", "accepts": ["video/mp4"], "required": True}
    contract["video"]["roles"]["source"] = "source_clip"
    result = edit_request(contract, source)
    assert isinstance(result, RunRequest), getattr(result, "body", result)
    assert list(result.media) == ["source_clip"]
    assert result.media["source_clip"][0].asset_id == source.asset_id


def test_edit_records_the_resolved_source_wire_and_asset_ids(source: VideoSource) -> None:
    result = edit_request(edit_contract(), source)
    assert isinstance(result, RunRequest), getattr(result, "body", result)
    assert result.facts.get("remixed_from_video_id") == source.wire_id
    assert result.facts.get("source_asset_id") == source.asset_id


def test_edit_merges_source_media_and_lineage_without_losing_existing_values(
    monkeypatch: pytest.MonkeyPatch, source: VideoSource,
) -> None:
    guide = MediaRef(digest="guide-digest", media_type="image/png", size=1, asset_id="asset-guide")
    original = _base.Shape.to_run_request
    seeded = []

    async def with_existing_values(self, *args, **kwargs):
        run = await original(self, *args, **kwargs)
        assert isinstance(run, RunRequest), getattr(run, "body", run)
        run = replace(run, media={"guide": (guide,)}, facts={"model": "kept-model"})
        seeded.append(run)
        return run

    monkeypatch.setattr(_base.Shape, "to_run_request", with_existing_values)
    result = edit_request(edit_contract(source_role=True), source)
    assert isinstance(result, RunRequest), getattr(result, "body", result)
    assert list(result.media) == ["guide", "video"]
    assert result.media["guide"][0] is guide
    assert result.media["video"][0].asset_id == source.asset_id
    assert result.facts == {"model": "kept-model", "remixed_from_video_id": source.wire_id,
                            "source_asset_id": source.asset_id}
    assert seeded[0].media == {"guide": (guide,)}
    assert seeded[0].facts == {"model": "kept-model"}
