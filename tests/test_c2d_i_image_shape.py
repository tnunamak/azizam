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
    CALLER, ROWS, no_deployment_default, FakeJobService, FakeSnapshot, FakeRunCore, entry, finish, json_request, multipart_request, run_request,
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
    assert low.inputs == high.inputs == {"prompt": "p"}                    # the options are not inputs
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
    for form, expected in (("b64_json", "b64_json"), ("url", "url"), (None, "url"))   # none asked: the renderer the deployment gave:
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
