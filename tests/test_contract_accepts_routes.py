"""The routes hand the resolver the ``accepts`` list of the contract input a file is for (CODE_TO_DATA_S3, D4, S3.7).

Each case sends real picture bytes and lets the real resolver measure them, so a list that is not the contract's shows
as a different outcome: a PNG under ``video/*`` is refused where ``image/*`` would let it through, and a JPEG under
``image/png`` is refused where ``image/*`` would too. Every route that reaches the resolver has its own case, because
each one passes the list in its own call.
"""

from __future__ import annotations

import asyncio
import base64
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import proxy
from gateway import media_fetch
from tests.support.access import access_context
from tests.test_image_extensions import CONTROLNET_EDIT_MANIFEST
from tests.test_media_inputs_image_route import (  # noqa: F401  (rig is a fixture)
    NS, OWNER_APP, image_bytes, lease_rows, live_rows, rig, stored,
)
from tests.test_media_inputs_upload_routes import (  # noqa: F401  (edit_rig and video_rig are fixtures)
    _Upload, edit_rig, run_edit, video_rig)

PNG, JPEG = image_bytes("PNG"), image_bytes("JPEG")
REFUSED = "the input is not a file type this model accepts"


def manifest_accepting(*accepts: str):
    doc = copy.deepcopy(CONTROLNET_EDIT_MANIFEST)
    doc["inputs"]["control_image"]["accepts"] = list(accepts)
    return doc


def auth():
    return access_context(app_id=OWNER_APP["app_id"], key_hash=OWNER_APP["key_hash"])


def upload_to_extension(data: bytes, doc):
    body = {"prompt": "x", "extensions": {NS: {"control_image": {"multipart_field": "control"}}}}
    form = SimpleNamespace(get=lambda name: _Upload(data) if name == "control" else None)
    return asyncio.run(proxy._upload_image_workflow_extension_assets(
        form=form, request_body=body, manifest=doc, client=object(), headers={}, receipt=None, auth_context=auth()))


def asset_to_extension(asset_id: str, doc):
    body = {"prompt": "x", "extensions": {NS: {"control_image": {"asset_id": asset_id}}}}
    return asyncio.run(proxy._upload_image_workflow_extension_assets(
        form=None, request_body=body, manifest=doc, client=object(), headers={}, receipt=None, auth_context=auth()))


# -- the image extension inputs ------------------------------------------------------------------------------------


def test_extension_upload_is_held_to_the_inputs_own_list(rig):
    _, error = upload_to_extension(PNG, manifest_accepting("video/*"))
    assert error is not None and REFUSED.encode() in error.body and rig.uploads == []
    _, error = upload_to_extension(JPEG, manifest_accepting("image/png"))
    assert error is not None and REFUSED.encode() in error.body and rig.uploads == []
    _, error = upload_to_extension(PNG, manifest_accepting("image/png"))
    assert error is None and len(rig.uploads) == 1
    _, error = upload_to_extension(JPEG, manifest_accepting("image/*"))
    assert error is None and len(rig.uploads) == 2


def test_extension_stored_asset_is_held_to_the_inputs_own_list(rig):
    asset_id = stored(PNG)
    _, error = asset_to_extension(asset_id, manifest_accepting("video/*"))
    assert error is not None and rig.uploads == [] and lease_rows() == 0
    _, error = asset_to_extension(asset_id, manifest_accepting("image/jpeg"))
    assert error is not None and rig.uploads == []
    _, error = asset_to_extension(asset_id, manifest_accepting("image/png"))
    assert error is None and len(rig.uploads) == 1


# -- the OpenAI-shaped reference (video input_reference and image references) ----------------------------------------


def reference(value, accepts, *, fetched=None):
    async def run():
        if fetched is None:
            return await proxy._resolve_input_reference(value, auth(), accepts=accepts)
        with patch.object(proxy, "_fetch_media_url", AsyncMock(return_value=fetched)):
            return await proxy._resolve_input_reference(value, auth(), accepts=accepts)
    return asyncio.run(run())


DATA_URI = "data:image/png;base64," + base64.b64encode(PNG).decode()


@pytest.mark.parametrize("kind", ["data_uri", "base64", "asset", "url"])
def test_input_reference_is_held_to_the_list_it_is_given(rig, kind):
    fetched = media_fetch.FetchedMedia(PNG, "remote.png", "image/png")
    value = {"data_uri": DATA_URI, "base64": base64.b64encode(PNG).decode(), "asset": stored(PNG),
             "url": "https://example.test/pic"}[kind]
    extra = {"fetched": fetched} if kind == "url" else {}
    with pytest.raises(proxy._InputReferenceError):
        reference(value, ("video/*",), **extra)
    with pytest.raises(proxy._InputReferenceError):
        reference(value, ("image/jpeg",), **extra)
    assert reference(value, ("image/png",), **extra)[0] == PNG


def test_the_wire_list_applies_only_to_a_picture_field_the_contract_does_not_declare():
    declared = {"inputs": {"image": {"type": "asset", "accepts": ["video/*"]}}}
    assert proxy._input_accepts(declared, "image") == ("video/*",)
    assert proxy._input_accepts({"inputs": {}}, "image") == ("image/*",)
    assert proxy._input_accepts(None, "image") == ("image/*",)
    assert proxy._input_accepts({"inputs": {"image": {"type": "asset"}}}, "image") == ()   # an input with no list accepts nothing


# -- the edit route's image[] parts ------------------------------------------------------------------------------------


def test_each_edit_part_takes_the_list_of_its_own_contract_input():
    doc = {"inputs": {"source_image": {"type": "asset", "accepts": ["image/png"]},
                      "style": {"type": "asset", "accepts": ["image/jpeg"]},
                      "other": {"type": "asset", "accepts": ["video/*"]},
                      "prompt": {"type": "string"}},
           "bindings": {proxy.DERIVED._GRAPH_BINDING: {"route_bound_inputs": ["style"]}}}
    assert proxy._edit_part_accepts(doc, 3) == [("image/png",), ("image/jpeg",), ("image/*",)]
    assert proxy._edit_part_accepts(None, 2) == [("image/*",), ("image/*",)]


def test_edit_route_refuses_a_picture_its_source_input_does_not_accept(edit_rig):
    doc = copy.deepcopy(proxy._find_image_manifest("image.edit", "flux2-klein-4b-edit"))
    doc["inputs"]["source_image"]["accepts"] = ["image/png"]
    with patch.object(proxy, "_find_image_manifest", return_value=doc):
        refused = run_edit([("a.jpg", JPEG)])
        allowed = run_edit([("a.png", PNG)])
    assert refused.status_code == 400 and REFUSED.encode() in refused.body
    assert allowed.status_code != 400 or REFUSED.encode() not in allowed.body
    assert edit_rig.comfy.uploads == [] or all(upload[1] == PNG for upload in edit_rig.comfy.uploads)


# -- the video route's extension pictures ------------------------------------------------------------------------------


def test_video_extension_picture_is_resolved_with_the_inputs_own_list():
    doc = {"inputs": {"end": {"type": "asset", "accepts": ["image/webp"], "required": False}},
           "bindings": {proxy.DERIVED._GRAPH_BINDING: {"map": {"end": [{"node": "1", "input": "image"}]}}}}
    body = {"extensions": {proxy.WORKFLOW_EXTENSION_NAMESPACE: {"end": "asset:img_x"}}}
    seen = {}

    async def resolve(value, **kwargs):
        seen.update(kwargs)
        return PNG, "end.png", "image/png"

    with patch.object(proxy, "_resolve_video_workflow_asset", resolve):
        asyncio.run(proxy._resolve_video_workflow_assets(body, doc, {}, None))
    assert seen["accepts"] == ("image/webp",)


# -- every extension input is checked before the first one reaches the engine ---------------------------------------------


def two_input_manifest(second_accepts: str):
    doc = manifest_accepting("image/png")
    doc["inputs"]["style_image"] = {"type": "asset", "accepts": [second_accepts]}
    doc["bindings"]["comfyui"]["map"]["style_image"] = [{"node": "43", "input": "image"}]
    return doc


def two_assets_to_extension(first: str, second: str, doc):
    body = {"prompt": "x", "extensions": {NS: {"control_image": {"asset_id": first}, "style_image": {"asset_id": second}}}}
    return asyncio.run(proxy._upload_image_workflow_extension_assets(
        form=None, request_body=body, manifest=doc, client=object(), headers={}, receipt=None, auth_context=auth()))


def test_a_refused_second_extension_input_leaves_no_upload_and_no_live_lease_behind_for_the_first(rig):
    first, second = stored(PNG), stored(PNG)
    _, error = two_assets_to_extension(first, second, two_input_manifest("image/jpeg"))
    assert error is not None and b"style_image" in error.body
    assert rig.uploads == [] and live_rows() == 0
    updated, error = two_assets_to_extension(first, second, two_input_manifest("image/png"))
    assert error is None and len(rig.uploads) == 2 and live_rows() == 0


def test_a_refused_second_multipart_extension_input_leaves_no_upload_behind_for_the_first(rig):
    doc = two_input_manifest("image/jpeg")
    body = {"prompt": "x", "extensions": {NS: {"control_image": {"multipart_field": "control"}, "style_image": {"multipart_field": "style"}}}}
    form = SimpleNamespace(get=lambda name: _Upload(PNG) if name in ("control", "style") else None)
    _, error = asyncio.run(proxy._upload_image_workflow_extension_assets(
        form=form, request_body=body, manifest=doc, client=object(), headers={}, receipt=None, auth_context=auth()))
    assert error is not None and rig.uploads == [] and lease_rows() == 0


# -- a picture field the contract declares with another shape is refused, not given the wire's list -----------------------


@pytest.mark.parametrize("spec", [{"type": "string"}, {"type": "array", "items": {"type": "asset", "accepts": ["image/*"]}},
                                  {"type": "object", "properties": {}}, "not a mapping", None])
def test_a_picture_field_declared_as_something_else_is_refused(spec):
    doc = {"inputs": {"image": spec}}
    with pytest.raises(proxy._InputReferenceError):
        proxy._input_accepts(doc, "image")
    with pytest.raises(proxy._InputReferenceError):
        proxy._edit_part_accepts({"inputs": {"source_image": spec}}, 1)


# -- the video create route reaches the reference helper with the contract's list ---------------------------------------


def declaring_reference(*accepts: str):
    """``_find_video_manifest`` with the picture input declared as the contract of the test says."""
    real = proxy._find_video_manifest

    def find(workflow_name, workflow_cfg):
        doc = copy.deepcopy(real(workflow_name, workflow_cfg))
        doc["inputs"][proxy.VIDEO_REFERENCE_INPUT] = {"type": "asset", "accepts": list(accepts)}
        return doc
    return patch.object(proxy, "_find_video_manifest", find)


def test_video_create_route_holds_input_reference_to_the_contracts_list(video_rig):
    # A request that names no workflow extension never loads the contract, so it keeps the wire's list; this one does.
    reference_png = DATA_URI
    with declaring_reference("video/*"):
        refused = video_rig.client.post("/v1/videos", json={
            "model": "video-minimax-h3-i2v", "prompt": "a lighthouse", "seconds": 5, "seed": 7, "input_reference": reference_png,
            "extensions": {NS: {"steps": 8}}})
    assert refused.status_code == 400 and REFUSED in refused.text, refused.text
    assert video_rig.comfy.uploads == [] and video_rig.comfy.submitted == []
    with declaring_reference("image/png"):
        allowed = video_rig.client.post("/v1/videos", json={
            "model": "video-minimax-h3-i2v", "prompt": "a lighthouse", "seconds": 5, "seed": 7, "input_reference": reference_png,
            "extensions": {NS: {"steps": 8}}})
    assert allowed.status_code == 200, allowed.text
