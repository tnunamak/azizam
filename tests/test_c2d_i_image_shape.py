"""Image wire shapes (S7 parity M2 and the answer form; dot review c2d-whole-rows-shapes-review-1108, findings 1, 2, 5).

The options a caller sets on the picture reach the run request, the answer form is read from each wire request, and a
source file with an optional mask, each at the file limit, fits the multipart body budget."""
from __future__ import annotations

import asyncio
import base64
import json
from types import MappingProxyType

import pytest
from starlette.responses import Response

from gateway.request_spool import FileRequestSpool
from tests.test_frontend_shapes import (  # noqa: F401 - the autouse fixture
    CALLER, ROWS, FakeSpool, no_deployment_default, FakeJobService, FakeSnapshot, FakeRunCore, entry, finish, json_request, multipart_request, run_request,
    shape_of, snapshot, submit)

MIB = 1024 * 1024
EDIT_WITH_MASK = {"id": "image.edit.masked", "version": "1", "intent": "image.edit", "schema_version": 2,
                  "inputs": {"prompt": {"type": "string", "required": True},
                             "source_image": {"type": "asset", "accepts": ["image/png"], "required": True},
                             "mask": {"type": "asset", "accepts": ["image/png"]}},
                  "outputs": {"image": {"media_type": "image/png"}}, "bindings": {"fake": {}}}


def ok(run):
    assert not isinstance(run, Response), run.body
    return run


def test_image_wire_options_reach_run_request() -> None:
    snap = snapshot("flux2-klein")
    low = ok(run_request("images-generations", json_request({"prompt": "p", "n": 1, "quality": "low", "output_format": "png"}), snap))
    high = ok(run_request("images-generations", json_request({"prompt": "p", "n": 4, "quality": "high", "output_format": "jpg",
                                                           "output_compression": 70}), snap))
    assert low.options == {"n": 1, "quality": "low", "output_format": "png"}
    assert high.options == {"n": 4, "quality": "high", "output_format": "jpeg", "output_compression": 70}
    assert low.inputs == high.inputs == {"prompt": "p", "width": 1024, "height": 1024}   # the options are not inputs; the size is the route's 1024 square
    assert "response_format" not in high.options                            # an answer form is not an option
    form = run_request("images-generations", json_request({"prompt": "p"}), snap)
    assert form.options == {}                                               # nothing sent, nothing added
    multi = run_request("images-edits", multipart_request({"prompt": "p", "n": "3"}, [("image", "a.png", "image/png", b"x")]),
                        snapshot("qwen-image-2.1-edit"))
    assert multi.options == {"n": 3}


@pytest.mark.parametrize("payload,param", [({"n": 0}, "n"), ({"n": 11}, "n"), ({"n": "x"}, "n"), ({"n": True}, "n"),
                                           ({"quality": "ultra"}, "quality"), ({"output_format": "gif"}, "output_format"),
                                           ({"background": "red"}, "background"),
                                           ({"background": "transparent", "output_format": "jpeg"}, "background")])
def test_image_wire_options_are_checked(payload, param) -> None:
    response = run_request("images-generations", json_request({"prompt": "p", **payload}), snapshot("flux2-klein"))
    assert isinstance(response, Response) and response.status_code == 400
    assert json.loads(response.body)["error"]["param"] == param


def test_image_answer_form_is_read_from_each_wire_request() -> None:
    snap = snapshot("flux2-klein")
    core, jobs = FakeRunCore(), None
    for form, expected in (("b64_json", "b64_json"), ("url", "url"), (None, "b64_json")):  # none asked: the row's default_answer
        body = {"prompt": "p", **({"response_format": form} if form else {})}
        request = json_request(body)
        run = run_request_with(request, snap)
        view = submit(core, run, snap)
        finish(core, view.job_id, "image", "image/png", b"\x89PNG")
        response = asyncio.run(shape_of("images-generations", asset_url=lambda ref, caller: "https://x/" + ref.asset_id).to_response(
            core.jobs[view.job_id], ROWS["images-generations"], jobs=FakeJobService(core), caller=CALLER, request=request))
        item = json.loads(response.body)["data"][0]
        assert ("url" in item) == (expected == "url") and ("b64_json" in item) == (expected == "b64_json"), form
        if expected == "b64_json":
            assert base64.b64decode(item["b64_json"]) == b"\x89PNG"
    refused = run_request("images-generations", json_request({"prompt": "p", "response_format": "gif"}), snap)
    assert isinstance(refused, Response) and refused.status_code == 400
    # No URL renderer on this shape: a request for URLs is refused when it is answered, never answered with bytes.
    run = run_request("images-generations", json_request({"prompt": "p", "response_format": "url"}), snap)
    view = submit(core, run, snap)
    finish(core, view.job_id, "image", "image/png", b"x")
    bare = asyncio.run(shape_of("images-generations").to_response(
        core.jobs[view.job_id], ROWS["images-generations"], jobs=FakeJobService(core), caller=CALLER,
        request=_carry({"response_format": "url"})))
    assert bare.status_code == 400


def _carry(answer):
    request = json_request({})
    request.state.answer_form = answer
    return request


def run_request_with(request, snap, row_id="images-generations"):
    from tests.test_frontend_shapes import FakeSpool
    return asyncio.run(shape_of(row_id, asset_url=lambda ref, caller: "u").to_run_request(request, ROWS[row_id], snap, spool=FakeSpool()))


def test_optional_asset_files_each_at_limit_are_accepted(tmp_path) -> None:
    spool = FileRequestSpool(tmp_path, max_bytes=4 * MIB)
    snap = FakeSnapshot(1, MappingProxyType({EDIT_WITH_MASK["id"]: entry(EDIT_WITH_MASK)}))
    request = multipart_request({"prompt": "p"}, [("image", "a.png", "image/png", b"a" * (4 * MIB)),
                                                  ("mask", "m.png", "image/png", b"m" * (4 * MIB))])
    run = asyncio.run(shape_of("images-edits").to_run_request(request, ROWS["images-edits"], snap, spool=spool))
    assert not isinstance(run, Response), getattr(run, "status_code", None)
    assert set(run.media) == {"source_image", "mask"}


def test_default_answer_is_route_data_checked_at_load() -> None:
    from dataclasses import replace
    from gateway.frontend_shapes import RowError, parse_rows
    snap = snapshot("flux2-klein")
    assert ROWS["images-generations"].default_answer == "b64_json"
    # A row that declares one answers b64_json to a request that names none, with a URL renderer configured.
    request = json_request({"prompt": "p"})
    run = run_request_with(request, snap)
    core = FakeRunCore()
    view = submit(core, run, snap)
    finish(core, view.job_id, "image", "image/png", b"\x89PNG")
    answer = asyncio.run(shape_of("images-generations", asset_url=lambda ref, caller: "https://x/").to_response(
        core.jobs[view.job_id], ROWS["images-generations"], jobs=FakeJobService(core), caller=CALLER, request=request))
    item = json.loads(answer.body)["data"][0]
    assert "b64_json" in item and "url" not in item
    # A row that declares none keeps the shape's rule: the renderer's URL.
    bare = replace(ROWS["images-generations"], default_answer=None)
    request = json_request({"prompt": "p"})
    asyncio.run(shape_of("images-generations", asset_url=lambda ref, caller: "u").to_run_request(
        request, bare, snap, spool=__import__("tests.test_frontend_shapes", fromlist=["FakeSpool"]).FakeSpool()))
    assert request.state.answer_form == {"response_format": None, "output_format": "png", "size": "1024x1024"}
    # A form the shape does not accept is refused at load, and so is a value on a shape with no answer forms.
    good = {"id": "r", "shape": "openai.images.json", "mode": "sync", "selects": {"ids": ["a"]},
            "routes": [{"methods": ["POST"], "canonical": "/v1/x", "runtime_policy": {"default_answer": "url"}}]}
    assert parse_rows({"frontends": [good]})[0].default_answer == "url"
    for shape, form in (("openai.images.json", "gif"), ("openai.audio.json", "url")):
        bad = {**good, "shape": shape, "routes": [{**good["routes"][0], "runtime_policy": {"default_answer": form}}]}
        with pytest.raises(RowError):
            parse_rows({"frontends": [bad]})


# -- the deployment's default model for edits and variations ----------------------------------------------------------
def _defaults(monkeypatch, **by_key):
    from gateway import frontend_shapes
    monkeypatch.setattr(frontend_shapes.shapes, "DEFAULT_WORKFLOWS", {key: (lambda value=value: value) for key, value in by_key.items()})


def test_edit_with_no_model_runs_the_deployment_default_and_several_pictures_the_multi_picture_default(monkeypatch) -> None:
    from gateway.frontend_shapes.shapes import OpenAIImagesMultipart as Edit, SECOND_CHOICE
    _defaults(monkeypatch, **{Edit.shape_id: "qwen-image-2.1-edit", Edit.shape_id + SECOND_CHOICE: "qwen-image-2.1-edit-multiref"})
    snap = snapshot("qwen-image-2.1-edit", "qwen-image-2.1-edit-multiref")
    one = ok(run_request("images-edits", multipart_request({"prompt": "p"}, [("image", "a.png", "image/png", b"A")]), snap, FakeSpool()))
    two = ok(run_request("images-edits", multipart_request({"prompt": "p"}, [("image", "a.png", "image/png", b"A"),
                                                                              ("image", "b.png", "image/png", b"B")]), snap, FakeSpool()))
    assert one.workflow_id == "image.edit.qwen-image-2.1-edit" and two.workflow_id == "image.edit.qwen-image-2.1-edit-multiref"


def test_variations_with_no_model_run_the_variations_default_with_its_fixed_prompt(monkeypatch) -> None:
    from gateway.frontend_shapes import shapes
    from gateway.frontend_shapes.shapes import OpenAIImagesVariations as Variations
    _defaults(monkeypatch, **{Variations.shape_id: "qwen-image-2.1"})
    monkeypatch.setattr(shapes, "FIXED_INPUTS", {Variations.shape_id: lambda: {"prompt": "vary it", "denoise": 0.5}})
    run = ok(run_request("images-variations", multipart_request({}, [("image", "a.png", "image/png", b"A")]),
                         snapshot("qwen-image-2.1-edit"), FakeSpool()))
    assert run.workflow_id == "image.edit.qwen-image-2.1-edit" and run.inputs["prompt"] == "vary it"


def test_the_old_refusal_words_for_a_missing_prompt_a_missing_image_and_an_unknown_model() -> None:
    snap = snapshot("flux2-klein", "qwen-image-2.1-edit")
    texts = lambda response: json.loads(response.body)["error"]["message"]  # noqa: E731
    assert texts(run_request("images-generations", json_request({"model": "flux2-klein"}), snap)) == "prompt is required"
    assert texts(run_request("images-edits", multipart_request({"prompt": "p"}, []), snap, FakeSpool())) == "image is required"
    assert texts(run_request("images-edits", multipart_request({"prompt": "p", "model": "nope"}, [("image", "a.png", "image/png", b"A")]),
                             snap, FakeSpool())) == "Model 'nope-edit' is not available for image editing."
