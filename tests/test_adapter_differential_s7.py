"""S7 goldens: the route x outcome cells the first differential set misses (CODE_TO_DATA_S4_S8 section 8).

Each cell is one scenario recorded against TODAY's route, before any route moves onto a frontend row. The cell list
is ``tests/fixtures/adapter_differential/s7_matrix.csv`` (route, outcome, status, file); this module builds one
scenario per recorded cell from a route descriptor and an outcome recipe. Where today's route has no behaviour for an
outcome (for example the sync image routes ignore ``Idempotency-Key``), the golden records exactly that: the two runs
the old route makes. The S7 acceptance oracle for those cells is a separate, later set (design 10.2 rule 4).

Cells that need a wire which today has no route (the LocalAI 3D shape) are not recorded here; see the matrix file.

Mask profile: the one the first set uses (``Normalizer``). Nothing in this module masks a status, an error code, a
header, an engine request or a byte.
"""
from __future__ import annotations

import asyncio
import csv
import json
from pathlib import Path

import pytest

import proxy
from tests.support.graph_service import set_derived
from tests import test_adapter_differential as base
from tests.support.access import authenticated, identity, plan_for
from tests.support.adapter_differential import FIXTURE_DIR
from tests.support.adapter_differential_rig import trace_property  # noqa: F401
from tests.support.adapter_differential_rig import comfy_exchanges, exchange, history_done, play

MATRIX = FIXTURE_DIR / "s7_matrix.csv"
FAILED_HISTORY = {"p1": {"status": {"status_str": "error", "completed": False, "messages": [
    ["execution_error", {"node_type": "GoldenNode", "node_id": "7", "exception_message": "golden upstream failure"}]]},
    "outputs": {}}}
NEVER_HISTORY = {"p1": {"status": {"status_str": "running", "completed": False, "messages": []}, "outputs": {}}}
KEY = "Idempotency-Key"
BAD_JSON = {"content": b"{", "headers": {"content-type": "application/json"}}


class Route:
    """One route's request recipes and engine recipes. ``mode``: ``sync`` (the answer carries the result),
    ``job`` (a queued media job the test runs through its executor) or ``video`` (created queued, advanced by polls)."""

    def __init__(self, code, method, path, *, mode, runner=None, multipart=False):
        self.code, self.method, self.path, self.mode, self.runner, self.multipart = code, method, path, mode, runner, multipart

    # request recipes: ``variant`` is "ok", "other" (identity differs) or "invalid"
    def request(self, variant="ok", key=None) -> dict:  # pragma: no cover - overridden
        raise NotImplementedError

    def engine_ok(self, rig):  # pragma: no cover
        raise NotImplementedError

    def engine_fail(self, rig):
        base._comfy(rig, comfy_exchanges(history=FAILED_HISTORY, strict=False))

    def engine_never(self, rig):
        rig.set(proxy, "GRAPH_TIMEOUT", 2)
        base._comfy(rig, comfy_exchanges(history=NEVER_HISTORY, strict=False,
                                         queue={"queue_running": [], "queue_pending": [[0, "p1", {}, {}, []]]}))

    def engine_down(self, rig):
        base._comfy_down(rig)

    def engine_hang(self, rig):
        """The engine accepts the prompt, then its history call never answers (a request task is cancelled there)."""
        base._comfy(rig, comfy_exchanges(views=[(base._png(), "image/png")], strict=False, extra=[
            exchange("history-hang", "GET", "/history/p1", {"wait_before_headers": "forever", "json": {}}, times=1)]))

    def extra_setup(self, rig):
        pass


def _headers(key):
    return {KEY: key} if key else None


class ImageGenerate(Route):
    def request(self, variant="ok", key=None):
        if variant == "invalid":
            return dict(BAD_JSON, headers={**BAD_JSON["headers"], **(_headers(key) or {})})
        prompt = "a red wolf" if variant == "other" else "a red fox"
        return {"json_body": {"prompt": prompt, "response_format": "b64_json", **base.BG}, "headers": _headers(key)}

    def engine_ok(self, rig):
        base._comfy(rig, comfy_exchanges(views=[(base._png(), "image/png")]))


class ImageEdit(Route):
    def request(self, variant="ok", key=None):
        data = {"prompt": "make the fox blue", "response_format": "b64_json", **base.BG}
        image = base._png((1, 2, 3)) if variant == "other" else base._png()
        files = {"mask": ("mask.png", base._png((255, 255, 255)), "image/png")}
        if variant != "invalid":
            files = {"image": ("in.png", image, "image/png"), **files}
        return {"data": data, "files": files, "headers": _headers(key)}

    def engine_ok(self, rig):
        base._comfy(rig, comfy_exchanges(views=[(base._png((20, 20, 20)), "image/png")], history=history_done(node="13")))

    def engine_fail(self, rig):
        base._comfy(rig, comfy_exchanges(history=FAILED_HISTORY, strict=False))


class ImageVariation(Route):
    def request(self, variant="ok", key=None):
        files = {} if variant == "invalid" else {
            "image": ("in.png", base._png((1, 2, 3)) if variant == "other" else base._png(), "image/png")}
        # a multipart body needs at least one file part; the invalid variant sends an unrelated one
        files = files or {"other": ("x.txt", b"x", "text/plain")}
        return {"data": {"response_format": "b64_json"}, "files": files, "headers": _headers(key)}

    def extra_setup(self, rig):
        async def no_cutout(*_a, **_k):
            return proxy.ImageBackgroundRemovalDecision(False)

        rig.set(proxy, "resolve_image_background_decision", no_cutout)

    def engine_ok(self, rig):
        base._comfy(rig, comfy_exchanges(views=[(base._png((70, 70, 160)), "image/png")], history=history_done(node="10")))


class ImageJob(Route):
    def request(self, variant="ok", key=None):
        if variant == "invalid":
            return dict(BAD_JSON, headers={**BAD_JSON["headers"], **(_headers(key) or {})})
        prompt = "a green window" if variant == "other" else "a green door"
        return {"json_body": {"prompt": prompt, "response_format": "url", **base.BG}, "headers": _headers(key),
                "unordered_json": True}

    def extra_setup(self, rig):
        base._capture_scheduling(rig)

    def engine_ok(self, rig):
        base._comfy(rig, comfy_exchanges(views=[(base._png((90, 160, 40)), "image/png")]))


class AudioSync(Route):
    def request(self, variant="ok", key=None):
        if variant == "invalid":
            return dict(BAD_JSON, headers={**BAD_JSON["headers"], **(_headers(key) or {})})
        prompt = "a door slams" if variant == "other" else "a door creaks"
        return {"json_body": {"prompt": prompt, "model": "tangoflux", "duration": 4}, "headers": _headers(key)}

    def engine_ok(self, rig):
        base._audio_comfy(rig)


class AudioJob(Route):
    def request(self, variant="ok", key=None):
        if variant == "invalid":
            return dict(BAD_JSON, headers={**BAD_JSON["headers"], **(_headers(key) or {})})
        prompt = "a door slams" if variant == "other" else "a door creaks"
        return {"json_body": {"intent": "audio.generate", "request": {"prompt": prompt, "model": "tangoflux", "duration": 4}},
                "headers": _headers(key), "unordered_json": True}

    def extra_setup(self, rig):
        base._capture_scheduling(rig)

    def engine_ok(self, rig):
        base._audio_comfy(rig)


class VideoCreate(Route):
    def request(self, variant="ok", key=None):
        if variant == "invalid":
            return dict(BAD_JSON, headers={**BAD_JSON["headers"], **(_headers(key) or {})})
        prompt = "waves at noon" if variant == "other" else "waves at dusk"
        return {"json_body": {"prompt": prompt, "model": "ltx-2.5-t2v"}, "headers": _headers(key)}

    def engine_ok(self, rig):
        base._video_comfy(rig, running_polls=1, strict=False)


class VideoEdit(Route):
    """Today's edit route answers 501 ``video_remix_not_configured`` on a release config (no remix workflow shipped);
    every request that passes the field checks ends there. See proxy.py ``_submit_video_remix``."""

    source_id = "video_p1"  # replaced by the id of a video the scenario created first (see ``_first``)

    def request(self, variant="ok", key=None):
        if variant == "invalid":
            return {"json_body": {"video": {"id": self.source_id}}, "headers": _headers(key)}
        prompt = "make it night" if variant == "other" else "make it dusk"
        return {"json_body": {"prompt": prompt, "video": {"id": self.source_id}}, "headers": _headers(key)}

    def engine_ok(self, rig):
        base._video_comfy(rig, strict=False)


ROUTES = {r.code: r for r in (
    ImageGenerate("ig", "POST", "/v1/images/generations", mode="sync"),
    ImageEdit("ie", "POST", "/v1/images/edits", mode="sync", multipart=True),
    ImageVariation("iv", "POST", "/v1/images/variations", mode="sync", multipart=True),
    ImageJob("ij", "POST", "/gateway/v1/media/jobs/image-generations", mode="job", runner="_run_image_media_job"),
    AudioSync("as", "POST", "/v1/audio/generate", mode="sync"),
    AudioJob("aj", "POST", "/gateway/v1/media/jobs", mode="job", runner="_run_audio_media_job"),
    VideoCreate("vc", "POST", "/v1/videos", mode="video"),
    VideoEdit("ve", "POST", "/v1/videos/edits", mode="video"),
)}


# --------------------------------------------------------------------------- drivers

async def _first(rig, route, label, **kw):
    if route.code == "ve" and kw.get("variant") != "invalid" and getattr(rig, "_ve_source", None) is None:
        # An edit needs a source video the caller owns: create one first, as a caller would.
        created = await rig.call("POST", "/v1/videos", label="create the source video",
                                 json_body={"prompt": "waves at dusk", "model": "ltx-2.5-t2v"})
        rig._ve_source = created.json().get("id") if created.status_code == 200 else "video_p1"
        route.source_id = rig._ve_source
    return await rig.call(route.method, route.path, label=label, **route.request(**kw))


async def _run_job(rig, route):
    """Run the executor of the job a job route queued (the scenario runs it explicitly)."""
    if route.mode == "job" and rig.scheduled:
        await getattr(proxy, route.runner)(rig.scheduled[-1])


def _body_id(response):
    try:
        data = response.json()
    except ValueError:
        return None
    return data.get("job_id") or data.get("id") if isinstance(data, dict) else None


async def _job_ids(rig):
    listed = await rig.call("GET", "/gateway/v1/media/jobs", label="list the caller's media jobs")
    data = listed.json() if listed.status_code == 200 else {}
    items = data.get("data") or data.get("jobs") or [] if isinstance(data, dict) else []
    return [item["job_id"] for item in items if isinstance(item, dict) and item.get("job_id")]


async def _settle(rig, route, response):
    """After a first call: a job route is run to its end; a video is polled until the engine finished it."""
    if route.mode == "job":
        await _run_job(rig, route)
    elif route.mode == "video" and route.code == "vc" and response is not None and response.status_code in (200, 202):
        video_id = response.json().get("id")
        if video_id:
            await rig.call("GET", f"/v1/videos/{video_id}", label="status while pending")
            await rig.call("GET", f"/v1/videos/{video_id}", label="status after the engine finished")


def build(route: Route, outcome: str):
    """Return (setup, drive, description) for one cell."""

    def prepare(rig, engine):
        route.extra_setup(rig)
        getattr(route, engine)(rig)

    if outcome == "success":
        def setup(rig): prepare(rig, "engine_ok")

        async def drive(rig):
            response = await _first(rig, route, "success")
            await _settle(rig, route, response)
            if route.mode == "job":
                job_id = _body_id(response)
                await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="poll the finished job")
                await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/content", label="job content")
                await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/events", label="job events")
    elif outcome == "upstream_failure":
        def setup(rig): prepare(rig, "engine_fail")

        async def drive(rig):
            response = await _first(rig, route, "upstream failure")
            await _settle(rig, route, response)
            if route.mode == "job":
                job_id = _body_id(response)
                await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="poll the failed job")
                await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/content", label="content of the failed job")
                await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/events", label="job events")
    elif outcome == "validation_refusal":
        def setup(rig): prepare(rig, "engine_down")  # a sentinel: any engine call is recorded

        async def drive(rig):
            await _first(rig, route, "malformed request", variant="invalid")
            if route.mode == "job":
                rig.note("scheduled_jobs", len(rig.scheduled))
    elif outcome == "policy_refusal":
        def setup(rig):
            prepare(rig, "engine_down")
            rig._exit.enter_context(authenticated(identity(plan_for(caps=("chat",)))))

        async def drive(rig):
            await _first(rig, route, "caller whose plan grants no media")
            if route.mode == "job":
                rig.note("scheduled_jobs", len(rig.scheduled))
    elif outcome == "admission_refusal":
        def setup(rig):
            prepare(rig, "engine_down")
            set_derived(rig.monkeypatch, "GRAPH_ENABLED", False)
            # The run core reads the engine's own record: a video route's engine is disabled there too.
            rig.monkeypatch.setitem(proxy.config, "backends", {**(proxy.config.get("backends") or {}), "comfy": {
                "enabled": False, "host": base.GRAPH_HOST, "port": 80, "protocol": "node-graph"}})

        async def drive(rig):
            await _first(rig, route, "media backend disabled")
            if route.mode == "job":
                rig.note("scheduled_jobs", len(rig.scheduled))
    elif outcome == "replay_same":
        def setup(rig): prepare(rig, "engine_ok")

        async def drive(rig):
            key = f"golden-{route.code}-same"
            first = await _first(rig, route, "first submit", key=key)
            second = await _first(rig, route, "same key, same request, before the run finished", key=key)
            await _settle(rig, route, first)
            third = await _first(rig, route, "same key, same request, after the run finished", key=key)
            rig.note("scheduled_jobs", len(rig.scheduled))
            rig.note("job_ids_equal", [_body_id(first) == _body_id(second), _body_id(first) == _body_id(third)]
                     if route.mode == "job" else "not a job route")
    elif outcome == "replay_conflict":
        def setup(rig): prepare(rig, "engine_ok")

        async def drive(rig):
            key = f"golden-{route.code}-conflict"
            first = await _first(rig, route, "first submit", key=key)
            await _first(rig, route, "same key, one input changed", key=key, variant="other")
            await _settle(rig, route, first)
            rig.note("scheduled_jobs", len(rig.scheduled))
    elif outcome == "wait_timeout":
        # Today no modality route holds a client for a bounded wait. A sync route waits for the engine until the
        # engine budget (GRAPH_TIMEOUT) ends and answers its timeout; a job route answers the queued job at once.
        # That is the contract recorded here; the generic route's Prefer: wait is the S6 contract, tested in C.
        def setup(rig): prepare(rig, "engine_never")

        async def drive(rig):
            response = await _first(rig, route, "request while the engine never finishes")
            if route.mode == "job":
                job_id = _body_id(response)
                await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="poll while the job is queued")
                await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/events", label="events while queued")
            elif route.mode == "video":
                await _settle(rig, route, response)
    elif outcome == "cancel_queued":
        def setup(rig): prepare(rig, {"sync": "engine_hang", "job": "engine_down"}.get(route.mode, "engine_ok"))

        async def drive(rig):
            if route.mode == "sync":
                # No queued state is visible to a sync caller; the old cancel path is the request task being cancelled
                # while the engine works.
                await rig.call(route.method, route.path, label="request cancelled while the engine works",
                               cancel_when=lambda: rig.fakes.clock.until_waiting(), **route.request())
                for _ in range(20):
                    await asyncio.sleep(0)
                return
            response = await _first(rig, route, "submit")
            job_ids = [_body_id(response)] if route.mode == "job" else await _job_ids(rig)
            job_id = job_ids[0] if job_ids else None
            canceled = await rig.call("POST", f"/gateway/v1/media/jobs/{job_id}/cancel", label="cancel while queued",
                                      json_body={})
            await rig.call("POST", f"/gateway/v1/media/jobs/{job_id}/cancel", label="cancel again", json_body={})
            await _run_job(rig, route)
            await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="poll after the executor ran")
            await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/events", label="events")
            rig.note("cancel_status", canceled.status_code)
    elif outcome == "cancel_finished":
        def setup(rig): prepare(rig, "engine_ok")

        async def drive(rig):
            response = await _first(rig, route, "submit")
            await _settle(rig, route, response)
            job_ids = [_body_id(response)] if route.mode == "job" else await _job_ids(rig)
            job_id = job_ids[0] if job_ids else None
            await rig.call("POST", f"/gateway/v1/media/jobs/{job_id}/cancel", label="cancel the finished job", json_body={})
            await rig.call("POST", f"/gateway/v1/media/jobs/{job_id}/cancel", label="cancel it again", json_body={})
            await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="poll after the cancels")
            await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/events", label="events after the cancels")
    elif outcome == "disconnect":
        def setup(rig): prepare(rig, "engine_hang" if route.multipart else "engine_never")

        async def drive(rig):
            sent = route.request()
            if route.multipart or "json_body" not in sent:
                # call_disconnecting speaks JSON only: a multipart route is walked away from by task cancellation.
                await rig.call(route.method, route.path, label="caller walks away (task cancelled)",
                               cancel_when=lambda: rig.fakes.clock.until_waiting(), **sent)
                for _ in range(20):
                    await asyncio.sleep(0)
                return
            polls = lambda: sum(1 for e in rig.recorder.engine if e["path"].startswith("/history/")) >= (
                2 if route.mode == "sync" else 0)
            await rig.call_disconnecting(route.method, route.path, label="http.disconnect during the request",
                                         json_body=sent["json_body"], disconnect_when=polls,
                                         unordered_json=route.mode == "job")
            if route.mode == "job":
                await rig.call(route.method, route.path, label="reconnect with the same request", **route.request())
    else:  # pragma: no cover
        raise ValueError(outcome)
    return setup, drive, f"S7 golden: {route.method} {route.path}, outcome {outcome}, recorded on the unchanged route"


# --------------------------------------------------------------------------- the cells

def recorded_cells() -> list[tuple[str, str]]:
    rows = csv.DictReader(MATRIX.open(encoding="utf-8"))
    return [(row["route"], row["outcome"]) for row in rows if row["status"] == "recorded" and row["route"] in ROUTES]


def fixture_name(route: str, outcome: str) -> str:
    return f"s7_{route}_{outcome}"


# The video cells whose create answers the engine's outcome (a timeout, a failure) where the recorded route answered the queued
# video. The first line the trace differs on is that answer; the cause is the known product item below.
CREATE_WAITS = {("vc", "wait_timeout"), ("vc", "disconnect"), ("ve", "wait_timeout"), ("ve", "upstream_failure")}
CREATE_WAITS_MARK = pytest.mark.xfail(
    strict=True, reason="V5d: a create waits for the engine round before answering (it answers the engine's outcome, not the queued job)")


def cell_params():
    return [pytest.param(route, outcome, id=f"{route}-{outcome}", marks=[CREATE_WAITS_MARK] if (route, outcome) in CREATE_WAITS else [])
            for route, outcome in recorded_cells()]


@pytest.mark.parametrize("route,outcome", cell_params())
def test_s7_cell(route, outcome, tmp_path, record_testsuite_property):
    # ROUTES lives for the worker process: a source video an earlier cell created must not reach this one.
    vars(ROUTES[route]).pop("source_id", None)
    setup, drive, description = build(ROUTES[route], outcome)
    play(fixture_name(route, outcome), tmp_path, record_testsuite_property, setup, drive, description=description)
