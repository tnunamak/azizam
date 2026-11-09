"""V5c selection and refusal order, using only synthetic video contracts.

No engine runs: the mounted row records any attempted submit and receipt open.
"""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import nullcontext

import pytest
from fastapi import FastAPI
from starlette.responses import Response
from starlette.testclient import TestClient

from gateway import frontend_shapes, row_routes
from gateway.frontend_shapes import _base, shapes, video_create
from tests.test_frontend_shapes import FakeSpool, json_request
from tests.test_video_create_shape import PNG, ROWS, snap, video_contract


@pytest.fixture(autouse=True)
def no_deployment_hooks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shapes, "DEFAULT_WORKFLOWS", {})
    monkeypatch.setattr(shapes, "VIDEO_HOOKS", None, raising=False)


def _chain_image_request():
    source = video_contract("demo", variants={"image": "video.generate.demo-image"})
    image = video_contract("demo-image", chain=True, image=True, requires=["image"])
    body = {
        "prompt": "x",
        "model": "demo",
        "seconds": 5,
        "input_reference": "data:image/png;base64," + base64.b64encode(PNG).decode(),
    }
    return snap(source, image), body


@pytest.mark.parametrize("entrypoint", ["legacy_route", "to_run_request"])
def test_an_image_variant_chain_is_legacy_at_both_shape_entrypoints(entrypoint: str) -> None:
    snapshot, body = _chain_image_request()
    row = ROWS["videos-create"]
    shape = frontend_shapes.build_shape(row)
    request, spool = json_request(body), FakeSpool()
    if entrypoint == "legacy_route":
        result = asyncio.run(video_create.legacy_route(shape, request, row, snapshot))
    else:
        result = asyncio.run(video_create.to_run_request(shape, request, row, snapshot, spool=spool))
    assert isinstance(result, _base.LegacyRoute), result
    assert result.name == "videos.create"
    assert result.body == body
    assert spool.staged == []


def test_an_image_variant_chain_opens_no_row_receipt_and_never_submits() -> None:
    snapshot, body = _chain_image_request()
    calls = {key: [] for key in ("auth", "policy", "receipt", "report", "submit", "legacy")}

    async def legacy(request, caller, parsed_body):
        calls["legacy"].append((caller, dict(parsed_body)))
        return Response("old", status_code=200)

    class Core:
        async def submit_run(self, run_request, caller, *, snapshot):
            calls["submit"].append(run_request)
            raise AssertionError("a selected chain must never reach the new runner")

    class Owner:
        def report(self, **outcome):
            calls["report"].append(outcome)

        def handoff(self):
            pass

        def dispatching(self, backend):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    deps = row_routes.RowRouteDeps(
        authenticate=lambda request, path: calls["auth"].append(path) or "caller-1",
        enforce_policy=lambda caller, path: calls["policy"].append(path),
        core=lambda: Core(), snapshot=lambda: snapshot, jobs=lambda: None, new_spool=FakeSpool,
        scope=lambda request, spool: nullcontext(), refused=lambda r: Response("refused", status_code=r.http_status),
        new_request_id=lambda: "req-order", open_receipt=lambda *a: calls["receipt"].append(a) or Owner(),
        name_receipt=lambda *a: None, pinned=lambda job_id: None, legacy={"videos.create": legacy})
    app = FastAPI()
    row_routes.mount_rows(app, deps, ("videos-create",))
    with TestClient(app, raise_server_exceptions=False) as client:
        answer = client.post("/v1/videos", json=body)
    assert calls["receipt"] == []
    assert calls["submit"] == []
    assert calls["policy"] == [] and calls["report"] == []
    assert calls["auth"] == ["/v1/videos"]
    assert calls["legacy"] == [("caller-1", body)]
    assert answer.status_code == 200 and answer.text == "old"


@pytest.mark.parametrize(("fields", "expected_error"), [
    pytest.param(
        {"seconds": "bad"},
        {"message": "seconds must be a number", "type": "invalid_request_error"},
        id="seconds-number-before-required-asset"),
    pytest.param(
        {"seconds": 20},
        {"message": "seconds must be between 1.0 and 10.0 for model video-demo", "type": "invalid_request_error"},
        id="seconds-range-before-required-asset"),
    pytest.param(
        {"seconds": 5, "size": 42},
        {"message": "size must be a string like 1280x720", "type": "invalid_request_error", "param": "size"},
        id="size-before-required-asset"),
    pytest.param(
        {"seconds": 5},
        {"message": "last_image is required for this model", "type": "invalid_request_error",
         "param": "extensions.com.azizam.workflow.last_image"},
        id="valid-seconds-still-requires-asset"),
])
def test_keyframe_required_asset_is_checked_after_seconds_and_size(fields: dict, expected_error: dict) -> None:
    contract = video_contract("demo", requires=["extensions"])
    contract["inputs"]["frames"] = {"type": "integer", "minimum": 9, "maximum": 241}
    contract["inputs"]["last_image"] = {"type": "asset", "accepts": ["image/*"], "required": True}
    contract["video"]["roles"]["length"] = "frames"
    del contract["inputs"]["length"]
    body = {"prompt": "x", "model": "demo", "extensions": {"com.azizam.workflow": {"frames": 49}}, **fields}
    row = ROWS["videos-create"]
    shape = frontend_shapes.build_shape(row)
    spool = FakeSpool()
    result = asyncio.run(video_create.to_run_request(shape, json_request(body), row, snap(contract), spool=spool))
    assert isinstance(result, Response), result
    assert result.status_code == 400
    assert json.loads(result.body) == {"error": expected_error}
    assert spool.staged == []
