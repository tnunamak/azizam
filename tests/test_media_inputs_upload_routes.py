"""Uploaded and referenced image inputs on the media input resolver (S2.6b).

Three routes are covered: the multipart branch of the image extension inputs, the image edit route's ``image[]``
uploads, and the video route's ``input_reference`` (an asset id, a data: URI, an upload or a URL).

The golden is what the old ingestion sent to the engine: the uploaded bytes as they came, the file name the caller
gave (or the route's default), and the content type the route fixes or the caller claimed. Each case holds the
resolver path to that. A lease row that exists after the request, and is released, shows the input went through the
resolver: the old path wrote none.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import proxy
from gateway import media_fetch, media_inputs
from tests.support.access import access, access_context, authenticated, identity, plan_for
from tests.support.video_row_engine import declare_graph_engine
from tests.support.video_row_files import patch_video_row_files_in_a_temporary_tree
from tests.test_image_extensions import CONTROLNET_EDIT_MANIFEST
from tests.test_image_route_receipts import _MultipartRequest, _MultipartUpload
from tests.test_media_inputs_image_route import (  # noqa: F401  (rig is a fixture)
    NS, OWNER_APP, image_bytes, lease_rows, live_rows, make_engine_remote, rig, stored,
)

OWNER = {"app_id": OWNER_APP["app_id"], "key_hash": OWNER_APP["key_hash"]}

FORMATS = ["PNG", "JPEG", "WEBP", "GIF"]
SUFFIX = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}
NOT_AN_IMAGE = b"this is plain text, not a picture " * 4


@pytest.fixture
def resolver_calls(monkeypatch):
    """The resolver entry points a request used, in order."""
    calls: list[str] = []
    for name in ("check_upload", "resolve_upload", "resolve_asset", "resolve_url"):
        original = getattr(media_inputs.MediaInputResolver, name)

        def spy(self, *args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(self, *args, **kwargs)

        if asyncio.iscoroutinefunction(original):
            async def spy(self, *args, _name=name, _original=original, **kwargs):  # noqa: F811
                calls.append(_name)
                return await _original(self, *args, **kwargs)

        monkeypatch.setattr(media_inputs.MediaInputResolver, name, spy)
    return calls


def ended(rig) -> bool:
    """The request is over: no live lease, and a file an upload made has been removed."""
    return live_rows() == 0 and lease_rows() == 0 and stored_files(rig) == []


def stored_files(rig) -> list[str]:
    return sorted(p.name for p in rig.cache.glob("*") if p.is_file()) if rig.cache.exists() else []


# -- the multipart branch of the image extension inputs ------------------------------------------------------------


class _Upload:
    def __init__(self, data: bytes, filename: str | None = None, content_type: str | None = None) -> None:
        self._data = data
        self.filename = filename
        self.content_type = content_type

    async def read(self) -> bytes:
        return self._data


def run_multipart_extension(upload: _Upload, *, plan_or_access=None):
    auth = access_context(plan_or_access, app_id=OWNER_APP["app_id"], key_hash=OWNER_APP["key_hash"])
    body = {"prompt": "x", "extensions": {NS: {"control_image": {"multipart_field": "control"}}}}
    form = SimpleNamespace(get=lambda name: upload if name == "control" else None)
    return asyncio.run(proxy._upload_image_workflow_extension_assets(
        form=form, request_body=body, manifest=CONTROLNET_EDIT_MANIFEST, client=object(), headers={}, receipt=None,
        auth_context=auth,
    ))


@pytest.mark.parametrize("fmt", FORMATS)
def test_extension_upload_sends_the_engine_the_bytes_name_and_type_it_always_sent(rig, fmt, resolver_calls):
    data = image_bytes(fmt)
    updated, error = run_multipart_extension(_Upload(data, "ctl-name.bin", "image/x-claimed"))
    assert error is None
    assert rig.uploads == [("ctl-name.bin", data, "image/x-claimed")]  # the claim is only what the engine is told
    assert updated["extensions"][NS]["control_image"] == "engine-name.png"
    assert resolver_calls == ["resolve_upload"] and ended(rig)


def test_extension_upload_without_a_name_or_type_gets_the_route_defaults(rig):
    data = image_bytes("PNG")
    run_multipart_extension(_Upload(data))
    assert rig.uploads == [("control.png", data, "image/png")]


# Revert: the ``resolve_upload`` call in ``_upload_image_workflow_extension_assets``.
def test_extension_upload_of_bytes_that_are_not_an_image_is_refused_before_any_effect(rig):
    _, error = run_multipart_extension(_Upload(NOT_AN_IMAGE, "x.png", "image/png"))  # a lying name and type
    assert error is not None and error.status_code == 400
    assert b"the input is not a file type this model accepts" in error.body
    assert rig.uploads == [] and lease_rows() == 0 and stored_files(rig) == []


def test_extension_upload_to_an_engine_the_plan_does_not_list_is_refused_before_any_effect(rig):
    make_engine_remote(rig)
    _, error = run_multipart_extension(_Upload(image_bytes("PNG")), plan_or_access=access(plan_for(paid=[])))
    assert error is not None and b"does not allow sending files to the provider" in error.body
    assert rig.uploads == [] and lease_rows() == 0 and stored_files(rig) == []


def test_extension_upload_lease_is_released_when_the_engine_upload_fails(rig, monkeypatch, resolver_calls):
    async def failing(client, headers, content, *, filename, media_type="image/png"):
        raise RuntimeError("engine down")

    monkeypatch.setattr(proxy, "_upload_graph_image_bytes", failing)
    with pytest.raises(RuntimeError):
        run_multipart_extension(_Upload(image_bytes("PNG")))
    assert resolver_calls == ["resolve_upload"] and ended(rig)


# -- the image edit route's image[] uploads ------------------------------------------------------------------------


@pytest.fixture
def edit_rig(rig):
    from tests._fake_comfy_video import FakeComfy
    comfy = FakeComfy()
    stack = ExitStack()
    # The engine is declared as a deployment declares one: a service record that names its protocol, which the run core finds.
    service = rig.registry["services"][proxy._GRAPH_BACKEND_ID]["config"]
    service["connection"] = {"protocol": "node-graph", "base_url": service["base_url"]}
    service["enabled"] = True
    for p in comfy.patches():
        stack.enter_context(p)
    generate = AsyncMock(return_value=proxy.Response(status_code=200))
    stack.enter_context(patch.object(proxy, "_generate_images_with_adaptive_batching", generate))
    stack.enter_context(patch.object(
        proxy, "resolve_image_background_decision", AsyncMock(return_value=proxy.ImageBackgroundRemovalDecision(False))))
    rig.generate = generate
    rig.comfy = comfy
    yield rig
    stack.close()


def run_edit(images: list[tuple[str, bytes]], *, plan_or_access=None):
    items = [("model", "flux2-klein-4b-edit"), ("prompt", "a tree")]
    items.extend(("image[]", _MultipartUpload(name, data)) for name, data in images)
    auth = access_context(plan_or_access, app_id=OWNER_APP["app_id"], key_hash=OWNER_APP["key_hash"])
    return asyncio.run(proxy.proxy_graph_image_edit_request(_MultipartRequest(items), auth))


@pytest.mark.parametrize("fmt", FORMATS)
def test_edit_route_sends_the_engine_the_upload_as_it_always_did(edit_rig, fmt, resolver_calls):
    data = image_bytes(fmt)
    response = run_edit([(f"source{SUFFIX[fmt]}", data)])
    assert response.status_code == 200, response.body
    assert edit_rig.comfy.uploads == [(f"source{SUFFIX[fmt]}", data, "image/png")]
    assert resolver_calls == ["check_upload"]  # a check only: the route keeps each image as a source of its result itself
    assert lease_rows() == 0 and stored_files(edit_rig) == []
    assert edit_rig.generate.call_args.kwargs["made_from"]  # the result still names the caller's input


def test_edit_route_second_reference_image_goes_up_in_order(edit_rig, resolver_calls):
    workflow = {"_meta": {"image_nodes": ["1", "2"]}, "1": {"inputs": {"image": ""}}, "2": {"inputs": {"image": ""}}}
    first, second = image_bytes("PNG"), image_bytes("JPEG")
    with patch.object(proxy, "_select_edit_workflow", lambda model, **kw: model), patch.object(
        proxy, "load_graph_workflow", return_value=workflow,
    ), patch.object(proxy, "_find_image_manifest", return_value=None):
        response = run_edit([("a.png", first), ("b.jpg", second)])
    assert response.status_code == 200, response.body
    assert edit_rig.comfy.uploads == [("a.png", first, "image/png"), ("b.jpg", second, "image/png")]
    assert resolver_calls == ["check_upload"] * 2 and lease_rows() == 0 and stored_files(edit_rig) == []


# Revert: the ``check_upload`` calls in ``proxy_graph_image_edit_request``.
def test_edit_route_refuses_bytes_that_are_not_an_image_before_any_effect(edit_rig):
    response = run_edit([("source.png", NOT_AN_IMAGE)])  # named like a picture
    assert response.status_code == 400
    error = json.loads(response.body)["error"]
    assert error["param"] == "image[]" and error["code"] == media_inputs.UNSUPPORTED
    assert error["message"] == "the input is not a file type this model accepts"
    assert edit_rig.comfy.uploads == [] and lease_rows() == 0 and stored_files(edit_rig) == []
    edit_rig.generate.assert_not_awaited()


def test_edit_route_one_refused_image_leaves_no_effect_from_the_others(edit_rig):
    workflow = {"_meta": {"image_nodes": ["1", "2"]}, "1": {"inputs": {"image": ""}}, "2": {"inputs": {"image": ""}}}
    with patch.object(proxy, "_select_edit_workflow", lambda model, **kw: model), patch.object(
        proxy, "load_graph_workflow", return_value=workflow,
    ), patch.object(proxy, "_find_image_manifest", return_value=None):
        response = run_edit([("a.png", image_bytes("PNG")), ("b.png", NOT_AN_IMAGE)])
    assert response.status_code == 400
    assert edit_rig.comfy.uploads == [] and lease_rows() == 0 and stored_files(edit_rig) == []


def test_edit_route_to_an_engine_the_plan_does_not_list_sends_nothing(edit_rig):
    make_engine_remote(edit_rig)
    response = run_edit([("source.png", image_bytes("PNG"))], plan_or_access=access(plan_for(paid=[])))
    assert response.status_code == 400
    assert b"does not allow sending files to the provider" in response.body
    assert edit_rig.comfy.uploads == [] and lease_rows() == 0 and stored_files(edit_rig) == []


# -- the video route's input_reference -----------------------------------------------------------------------------


@pytest.fixture
def video_rig(rig, isolated_receipts, monkeypatch):
    """A video create over the run core: the engine is declared as a service record that names its protocol, and the create's
    hold for the engine is cut to a moment. The fake engine never finishes the prompt it is given."""
    from tests._fake_comfy_video import FakeComfy
    comfy = FakeComfy()
    stack = ExitStack()
    declare_graph_engine(rig.registry)
    for p in comfy.patches():
        stack.enter_context(p)
    stack.enter_context(authenticated(identity(**OWNER)))
    stack.enter_context(patch.object(proxy, "_enforce_runtime_route_policy", return_value=None))
    patch_video_row_files_in_a_temporary_tree(stack)
    rig.comfy = comfy

    @contextlib.asynccontextmanager
    async def no_lifespan(_app):
        yield

    stack.enter_context(patch.object(proxy.app.router, "lifespan_context", no_lifespan))
    rig.client = stack.enter_context(TestClient(proxy.app))      # entered: the portal (and a run in flight) outlives each request
    yield rig
    stack.close()


VIDEO_MODEL = "video-minimax-h3-i2v"


def post_video(rig, reference=None, *, files=None, settle: bool = True):
    """Create a video and, unless ``settle`` is off, wait for the run core to stage the file: the create answers at once and the
    run goes on in the app's loop (the fixture keeps that loop alive between requests)."""
    body = {"model": VIDEO_MODEL, "prompt": "a lighthouse", "seconds": 5, "seed": 7}
    if files is not None:
        response = rig.client.post("/v1/videos", data={k: str(v) for k, v in body.items()}, files=files)
    else:
        response = rig.client.post("/v1/videos", json={**body, "input_reference": reference})
    if settle and response.status_code == 200:
        settled_uploads(rig)
    return response


def settled_uploads(rig, count: int = 1, *, wait_s: float = 5.0) -> list:
    """The files the engine was sent: the run core stages the file a moment after the create answered."""
    deadline = time.monotonic() + wait_s
    while len(rig.comfy.uploads) < count and time.monotonic() < deadline:
        time.sleep(0.02)
    return rig.comfy.uploads


def data_uri(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


@pytest.mark.parametrize("fmt", FORMATS)
def test_video_reference_data_uri_reaches_the_engine_as_before(video_rig, fmt):
    data = image_bytes(fmt)
    response = post_video(video_rig, data_uri(data, f"image/{fmt.lower()}"))
    assert response.status_code == 200, response.text
    ext = {"JPEG": "jpg"}.get(fmt, fmt.lower())
    # Delta video_reference_declared_type: the engine is told the type the caller declared, not a fixed image/png.
    assert video_rig.comfy.uploads == [(f"input.{ext}", data, f"image/{fmt.lower()}")]
    assert ended(video_rig)


@pytest.mark.parametrize("fmt", FORMATS)
def test_video_reference_upload_reaches_the_engine_as_before(video_rig, fmt):
    data = image_bytes(fmt)
    response = post_video(video_rig, files={"input_reference": (f"ref{SUFFIX[fmt]}", data, f"image/{fmt.lower()}")})
    assert response.status_code == 200, response.text
    ext = {"JPEG": "jpg"}.get(fmt, fmt.lower())
    # Delta video_reference_declared_type: the engine is told the type the caller declared, not a fixed image/png.
    assert video_rig.comfy.uploads == [(f"input.{ext}", data, f"image/{fmt.lower()}")]
    assert ended(video_rig)


@pytest.mark.parametrize("fmt", FORMATS)
def test_video_reference_asset_id_reaches_the_engine_as_before(video_rig, fmt):
    data = image_bytes(fmt)
    asset_id = stored(data)
    suffix = proxy.asset_cache_path(asset_id).suffix  # type: ignore[union-attr]
    response = post_video(video_rig, asset_id)
    assert response.status_code == 200, response.text
    old_bytes, old_name = proxy._resolve_cached_image_asset(
        asset_id, access_context(app_id=OWNER_APP["app_id"], key_hash=OWNER_APP["key_hash"]))
    assert video_rig.comfy.uploads == [(old_name, old_bytes, "image/png")] and old_name == f"{asset_id}{suffix}"
    assert live_rows() == 0


@pytest.mark.parametrize("fmt", FORMATS)
def test_video_reference_url_goes_through_the_fetcher_and_reaches_the_engine_as_before(video_rig, fmt):
    data = image_bytes(fmt)
    fetched = media_fetch.FetchedMedia(data, f"remote{SUFFIX[fmt]}", f"image/{fmt.lower()}")
    with patch.object(proxy, "_fetch_media_url", AsyncMock(return_value=fetched)) as fetch:
        response = post_video(video_rig, "https://example.test/pic")
    assert response.status_code == 200, response.text
    fetch.assert_awaited_once()
    assert video_rig.comfy.uploads == [(f"remote{SUFFIX[fmt]}", data, "image/png")]
    assert ended(video_rig)


# Revert: the ``resolve_upload`` call in ``_resolve_input_reference``.
def test_video_reference_that_is_not_an_image_is_refused_before_any_effect(video_rig):
    for reference in (data_uri(NOT_AN_IMAGE), base64.b64encode(NOT_AN_IMAGE).decode()):
        response = post_video(video_rig, reference)
        assert response.status_code == 400, response.text
        assert "the input is not a file type this model accepts" in response.text
    assert video_rig.comfy.uploads == [] and video_rig.comfy.submitted == []
    assert lease_rows() == 0 and stored_files(video_rig) == []


def test_video_reference_url_that_is_not_an_image_is_refused_before_any_effect(video_rig):
    fetched = media_fetch.FetchedMedia(NOT_AN_IMAGE, "remote.png", "image/png")
    with patch.object(proxy, "_fetch_media_url", AsyncMock(return_value=fetched)):
        response = post_video(video_rig, "https://example.test/pic")
    assert response.status_code == 400 and "the input is not a file type this model accepts" in response.text
    assert video_rig.comfy.uploads == [] and lease_rows() == 0 and stored_files(video_rig) == []


def test_video_reference_url_failure_keeps_its_text_and_makes_no_effect(video_rig):
    with patch.object(proxy, "_fetch_media_url", AsyncMock(side_effect=ValueError("blocked origin"))):
        response = post_video(video_rig, "https://example.test/pic")
    assert response.status_code == 400
    assert "failed to download input_reference: blocked origin" in response.text
    assert video_rig.comfy.uploads == [] and lease_rows() == 0


def test_video_reference_to_an_engine_the_plan_does_not_list_is_refused_before_any_effect(video_rig):
    make_engine_remote(video_rig)
    with authenticated(identity(plan_for(paid=[]), **OWNER)):
        response = post_video(video_rig, data_uri(image_bytes("PNG")))
    assert response.status_code == 400 and "does not allow sending files to the provider" in response.text
    assert video_rig.comfy.uploads == [] and lease_rows() == 0 and stored_files(video_rig) == []


def test_video_reference_lease_is_released_when_the_engine_upload_fails(video_rig):
    """The picture is resolved, then the request fails at the engine: the lease still ends with the request."""
    video_rig.comfy.upload_status = 500
    response = post_video(video_rig, data_uri(image_bytes("PNG")))
    assert response.status_code == 502
    assert ended(video_rig)
