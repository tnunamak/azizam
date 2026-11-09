"""Literal winners for two-fault video creates; no live engine or legacy code."""

from contextlib import nullcontext
from copy import deepcopy

import pytest
from fastapi import FastAPI
from starlette.responses import Response
from starlette.testclient import TestClient

from gateway import row_routes
from gateway.frontend_shapes import _base, shapes
from tests.test_video_create_shape import FakeSpool, error, read, snap, video_contract


@pytest.fixture(autouse=True)
def no_deployment_hooks(monkeypatch):
    monkeypatch.setattr(shapes, "DEFAULT_WORKFLOWS", {})
    monkeypatch.setattr(shapes, "VIDEO_HOOKS", None, raising=False)


@pytest.fixture(params=["/v1/videos", "/openai/v1/videos"])
def path(request):
    return request.param


def snapshot():
    keyframes = video_contract("keyframes", requires=["extensions"])
    keyframes["inputs"]["frames"] = {"type": "integer", "minimum": 9, "maximum": 241}
    keyframes["inputs"]["last_image"] = {"type": "asset", "accepts": ["image/*"], "required": True}
    keyframes["video"]["roles"]["length"] = "frames"
    del keyframes["inputs"]["length"]
    return snap(
        video_contract("m"),
        video_contract("i2v", image=True, requires=["image"]),
        keyframes,
        video_contract("longable", variants={"long": "video.generate.long"}),
        video_contract("long", chain=True),
    )


def mounted(registry):
    calls = {key: [] for key in ("receipt", "submit", "legacy", "report", "policy")}
    sentinel = Response(
        b"legacy-fault-pair\n", status_code=207,
        headers={"Content-Type": "application/x-legacy", "X-Fault-Pair": "unchanged"},
    )

    async def legacy(request, caller, body):
        calls["legacy"].append((caller, deepcopy(body)))
        return sentinel

    class Core:
        async def submit_run(self, run_request, caller, *, snapshot):
            calls["submit"].append(run_request)
            raise AssertionError("fault pairs must not submit a run")

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
        authenticate=lambda request, path: "caller-pairs",
        enforce_policy=lambda caller, path: calls["policy"].append(path),
        core=lambda: Core(), snapshot=lambda: registry, jobs=lambda: None, new_spool=FakeSpool,
        scope=lambda request, spool: nullcontext(),
        refused=lambda refused: Response("unexpected core refusal", status_code=refused.http_status),
        new_request_id=lambda: "req-pairs",
        open_receipt=lambda *args: calls["receipt"].append(args) or Owner(),
        name_receipt=lambda *args: None, pinned=lambda job_id: None,
        legacy={"videos.create": legacy},
    )
    app = FastAPI()
    row_routes.mount_rows(app, deps, ("videos-create",))
    return app, calls, sentinel


def assert_refusal(body, status, expected_error, path):
    registry = snapshot()
    before = deepcopy(body)
    spool = FakeSpool()
    direct = read(deepcopy(body), registry, spool=spool)
    assert isinstance(direct, Response), direct
    assert direct.status_code == status and error(direct) == expected_error
    assert spool.staged == []
    app, calls, _ = mounted(registry)
    with TestClient(app, raise_server_exceptions=False) as client:
        answer = client.post(path, json=deepcopy(body))
    assert answer.status_code == status
    assert answer.json() == {"error": expected_error}
    assert calls["legacy"] == [] and calls["submit"] == []
    assert body == before


def test_unknown_top_level_field_wins_over_invalid_video_control(path):
    assert_refusal({
        "model": "m", "prompt": "x", "bogus": 1,
        "extensions": {"com.azizam.video": {"audio": "no"}},
    }, 400, {
        "message": "unknown top-level field; move gateway-native controls under extensions.com.azizam.workflow",
        "type": "invalid_request_error", "param": "bogus", "code": "unknown_parameter",
    }, path)


def test_prompt_type_wins_over_unknown_model(path):
    assert_refusal({"model": "missing", "prompt": 5}, 400, {
        "message": "prompt must be a string", "type": "invalid_request_error", "param": "prompt",
    }, path)


def test_unknown_model_wins_over_nonnumeric_seconds(path):
    assert_refusal({"model": "missing", "prompt": "x", "seconds": "bad"}, 404, {
        "message": "Model 'missing' is not available for video generation.",
        "type": "invalid_request_error", "param": "model", "code": "model_not_found",
    }, path)


def test_missing_reference_wins_over_nonnumeric_seconds(path):
    assert_refusal({"model": "i2v", "prompt": "x", "seconds": "bad"}, 400, {
        "message": "Model video-i2v is image-to-video and requires input_reference",
        "type": "invalid_request_error", "param": "input_reference",
    }, path)


def test_shots_without_long_variant_win_over_nonnumeric_seconds(path):
    assert_refusal({"model": "m", "prompt": "x", "shots": [{"prompt": "a"}], "seconds": "bad"}, 400, {
        "message": "Model video-m cannot render shots or clips longer than 10 s; use a long-video model",
        "type": "invalid_request_error", "param": "shots", "code": "long_video_not_supported",
    }, path)


def test_nonnumeric_seconds_win_over_non_string_size(path):
    assert_refusal({"model": "m", "prompt": "x", "seconds": "bad", "size": 42}, 400, {
        "message": "seconds must be a number", "type": "invalid_request_error",
    }, path)


def test_missing_picture_wins_over_nonnumeric_frames(path):
    assert_refusal({
        "model": "keyframes", "prompt": "x",
        "extensions": {"com.azizam.workflow": {"frames": "bad"}},
    }, 400, {
        "message": "last_image is required for this model", "type": "invalid_request_error",
        "param": "extensions.com.azizam.workflow.last_image",
    }, path)


def test_shots_with_long_variant_hand_off_even_with_nonnumeric_seconds(path):
    body = {"model": "longable", "prompt": "x", "shots": [{"prompt": "a"}], "seconds": "bad"}
    registry = snapshot()
    spool = FakeSpool()
    direct = read(deepcopy(body), registry, spool=spool)
    assert isinstance(direct, _base.LegacyRoute), direct
    assert direct.name == "videos.create" and direct.body == body
    assert spool.staged == []
    app, calls, sentinel = mounted(registry)
    with TestClient(app, raise_server_exceptions=False) as client:
        answer = client.post(path, json=deepcopy(body))
    assert calls["legacy"] == [("caller-pairs", body)]
    assert calls["receipt"] == [] and calls["submit"] == []
    assert calls["policy"] == [] and calls["report"] == []
    assert (answer.status_code, answer.content, dict(answer.headers)) == (
        sentinel.status_code, sentinel.body, dict(sentinel.headers),
    )
