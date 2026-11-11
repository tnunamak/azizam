"""S7 video-create parity through public HTTP and observable stores only.

The real app runs over GatewayRig and a HandlerTransport engine. No route,
scheduler, runner, graph-builder or ownership function is called or patched here.
The section-4 idempotency_acceptance delta (the same key deduplicates) holds on the row path, so its test is a plain pass.

Rewritten for the video move (docs/design/CODE_TO_DATA_S7_VIDEO_MOVE.md section 5.1): a video made by create is a job,
``video_mj_<hex>``. Ids are read from responses; the engine prompt id is ``p<n>`` in submit order, and a create waits
for its runner to submit the prompt before a test reads the engine's graph. Chain extension stays on the old handler, so
an extension is still ``video_<prompt id>``.

Some inventory items lack a usable black-box fixture or contradict writers.md.
Those gaps are reported with the delivery; they are not hidden by skips.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import sqlite3
import subprocess
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

import proxy
from tests.media_test_bytes import mp4_bytes
from tests.support.access import authenticated, everything, identity, plan_for
from tests.support.adapter_differential_rig import GatewayRig
from tests.support.config_snapshot import publish_section
from tests.support.fake_upstream import HandlerTransport
from tests.support import video_rig
from tests.support.video_rig import video_job_rows

HOST = "comfy.fk.test"
MODEL = "ltx-2.5-t2v"
CREATE = {"prompt": "waves at dusk", "model": MODEL}
REAL_SLEEP = asyncio.sleep
JOB_ID = re.compile(r"^video_mj_[0-9a-f]{32}$")


class VideoEngine:
    """Stateful protocol peer: every accepted prompt gets a distinct upstream id."""

    def __init__(self):
        self.clip = mp4_bytes()
        self.calls = []
        self.prompts = {}
        self.pending = False
        self.failed = False
        self.down = False
        self.submit_release = None

    def __call__(self, request):
        path = request.url.path
        self.calls.append((request.method, path, bytes(request.content)))
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if request.method == "POST" and path == "/prompt":
            prompt_id = f"p{len(self.prompts) + 1}"
            self.prompts[prompt_id] = json.loads(request.content)
            return httpx.Response(200, json={"prompt_id": prompt_id})
        if request.method == "GET" and path.startswith("/history/"):
            prompt_id = path.rsplit("/", 1)[-1]
            if prompt_id not in self.prompts or self.pending:
                return httpx.Response(200, json={})
            if self.failed:
                entry = {"status": {"status_str": "error", "completed": False, "messages": [
                    ["execution_error", {"node_type": "LTXVSampler", "node_id": "12", "exception_message": "oom"}]]},
                    "outputs": {}}
            else:
                graph = self.prompts[prompt_id]["prompt"]
                outputs = {node_id: {"images": [{"filename": "clip.mp4", "subfolder": "", "type": "output"}]}
                           for node_id, node in graph.items()
                           if isinstance(node, dict) and node.get("class_type") in {"SaveVideo", "VHS_VideoCombine"}}
                if not outputs:
                    raise AssertionError("the submitted graph has no video output node")
                entry = {"status": {"status_str": "success", "completed": True, "messages": []}, "outputs": outputs}
            return httpx.Response(200, json={prompt_id: entry})
        if request.method == "GET" and path == "/queue":
            pending = [[index, pid, {}, {}, []] for index, pid in enumerate(self.prompts)] if self.pending else []
            return httpx.Response(200, json={"queue_running": [], "queue_pending": pending})
        if request.method == "GET" and path == "/view":
            # A stream, not content=: httpx reads a content= response at construction, and the gateway reads
            # /view as a stream (media_fetch.read_bounded_media_response), as it does with a real engine.
            return httpx.Response(200, stream=httpx.ByteStream(self.clip), headers={"content-type": "video/mp4"})
        if request.method == "POST" and path == "/upload/image":
            name = "uploaded.mp4" if b"video/mp4" in request.content else "uploaded.png"
            return httpx.Response(200, json={"name": name, "subfolder": "", "type": "input"})
        if request.method == "GET" and path == "/system_stats":
            return httpx.Response(200, json={"system": {}, "devices": []})
        if request.method == "POST" and (path in {"/free", "/history", "/interrupt"} or path.endswith("/cancel")):
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected engine request: {request.method} {path}")

    def traffic(self):
        return [(method, path) for method, path, _ in self.calls]


@pytest.fixture
def world(tmp_path):
    with GatewayRig("s7-video-create-parity", tmp_path) as rig:
        engine = VideoEngine()
        async def handle(request):
            response = engine(request)
            if request.method == "POST" and request.url.path == "/prompt" and engine.submit_release is not None:
                await engine.submit_release.wait()
            return response

        rig.fakes.router.add_transport(HOST, HandlerTransport(handle))
        rig.set(proxy, "COMFY_HOST", f"http://{HOST}")
        rig.monkeypatch.setitem(proxy.config, "backends", {
            **(proxy.config.get("backends") or {}),
            "comfy": {"enabled": True, "host": HOST, "port": 80, "protocol": "node-graph"},
        })
        yield SimpleNamespace(rig=rig, engine=engine, tmp=tmp_path)


def configure_remix(world, workflow):
    # publish_section carries the rig's derived fake-engine values across a non-backend config change.
    publish_section(world.rig.monkeypatch, "videos",
                    {**(proxy.config.get("videos") or {}), "remix_workflow": workflow})


def run(coro):
    return asyncio.run(coro)


async def submitted(world, count, *, timeout_s=5.0):
    """Wait (bounded) until the engine has accepted ``count`` prompts: a job's runner submits after the create answers."""
    deadline = time.monotonic() + timeout_s
    while len(world.engine.prompts) < count:
        if time.monotonic() > deadline:
            pytest.fail(f"the engine has {len(world.engine.prompts)} prompts after {timeout_s}s, expected {count}")
        await REAL_SLEEP(0.005)


async def create(world, **options):
    before = len(world.engine.prompts)
    response = await world.rig.call("POST", "/v1/videos", json_body={**CREATE, **options})
    # A broken setup must not become an expected failure in the allowed-delta test.
    if response.status_code != 200:
        raise RuntimeError(f"source create failed: {response.status_code} {response.text}")
    if JOB_ID.match(response.json()["id"]):
        await submitted(world, before + 1)
    return response


async def await_status(world, video_id, status="completed", *, timeout_s=10.0):
    """Poll the public status route until the video shows ``status``; return the body."""
    deadline = time.monotonic() + timeout_s
    while True:
        response = await world.rig.call("GET", f"/v1/videos/{video_id}")
        assert response.status_code == 200, response.text
        if response.json()["status"] == status:
            return response.json()
        assert time.monotonic() < deadline, f"{video_id} stayed {response.json()['status']!r}, expected {status!r}"
        await REAL_SLEEP(0.01)


def prompt_posts(world):
    return [call for call in world.engine.traffic() if call == ("POST", "/prompt")]


def rows(path, table):
    assert table in {"receipts", "video_job_records", "media_jobs", "result_bundle", "result_stage"}
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return []
        return [dict(row) for row in conn.execute(f'SELECT * FROM "{table}"')]


def receipt(world, request_id):
    found = [row for row in rows(world.rig.receipts_db, "receipts") if row["request_id"] == request_id]
    assert len(found) == 1, found
    return json.loads(found[0]["receipt_json"])


async def source(world):
    made = await create(world)
    video_id = made.json()["id"]
    await await_status(world, video_id)
    return video_id


def long_clip(world):
    """The same 2-second, 10-fps source as the recorded video_extend golden."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("video extension parity requires the hermetic test environment's ffmpeg")
    path = world.tmp / "source.mp4"
    subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=64x64:d=2:r=10",
                    "-pix_fmt", "yuv420p", "-c:v", "mpeg4", "-fflags", "+bitexact", "-flags:v", "+bitexact",
                    "-map_metadata", "-1", str(path)], capture_output=True, check=True)
    world.engine.clip = path.read_bytes()


async def extension(world, **options):
    long_clip(world)
    video_id = await source(world)
    before = len(world.engine.calls)
    response = await world.rig.call("POST", "/v1/videos/extensions",
                                   json_body={"video": {"id": video_id}, "prompt": "more waves", "seconds": "2", **options})
    return video_id, before, response


async def submit_job(world, **request):
    response = await world.rig.call("POST", "/gateway/v1/media/jobs",
                                   json_body={"intent": "video.generate", "request": {**CREATE, **request}},
                                   unordered_json=True)
    if response.status_code != 202:
        raise RuntimeError(f"media-job submit failed: {response.status_code} {response.text}")
    return response


async def terminal_job(world, job_id):
    """Let the real background scheduler run; never invoke a private runner."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        response = await world.rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", unordered_json=True)
        assert response.status_code == 200, response.text
        if response.json()["status"] in {"succeeded", "failed", "canceled"}:
            return response.json()
        await REAL_SLEEP(0.01)
    pytest.fail("the public media-job status did not settle within 15 seconds")


def test_video_01_disabled_capability_refuses_before_engine(world):
    async def drive():
        video_rig.disable_service(world.rig)         # the service record is off, as an operator turns it off
        response = await world.rig.call("POST", "/v1/videos", json_body=CREATE)
        assert response.status_code == 503
        assert response.json()["error"] == {"message": "Video generation not enabled", "type": "service_unavailable"}
        assert world.engine.calls == []
    run(drive())


@pytest.mark.parametrize("prompt", [7, [], {}], ids=["number", "list", "object"])
def test_video_02_non_string_prompt_never_reaches_engine(world, prompt):
    async def drive():
        response = await world.rig.call("POST", "/v1/videos", json_body={**CREATE, "prompt": prompt})
        assert response.status_code == 400, response.text
        assert response.json()["error"]["type"] == "invalid_request_error"
        assert world.engine.calls == []
    run(drive())


def test_video_03_validation_refusal_has_a_closed_receipt(world):
    async def drive():
        response = await world.rig.call("POST", "/v1/videos", content=b"{", headers={"content-type": "application/json"})
        assert response.status_code == 400
        assert response.json() == {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}}
        kept = receipt(world, response.headers["x-request-id"])
        assert kept["status"] == "failed" and kept["intent"] == "video.generate"
        assert kept["metadata"]["status_code"] == 400
        assert world.engine.calls == []
    run(drive())


def test_video_04_access_plan_refuses_before_engine(world):
    async def drive():
        with authenticated(identity(plan_for(caps=(), categories=()))):
            response = await world.rig.call("POST", "/v1/videos", json_body=CREATE)
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "not_in_access_plan"
        assert world.engine.calls == []
    run(drive())


def test_video_04b_a_plan_naming_the_catalog_workflow_id_runs_the_video_job(world):
    async def drive():
        plan = plan_for(caps=("video",), workflows=["video.generate.ltx-2.5-t2v"])
        with authenticated(identity(plan)):
            submitted = await submit_job(world)
            done = await terminal_job(world, submitted.json()["job_id"])
        assert done["status"] == "succeeded", done
    run(drive())


def test_video_05_same_key_deduplicates_under_the_allowed_delta(world):
    async def drive():
        responses = []
        for _ in range(2):
            response = await world.rig.call("POST", "/v1/videos", json_body=CREATE, headers={"Idempotency-Key": "video-parity"})
            if response.status_code != 200:
                raise RuntimeError(f"create setup failed: {response.status_code} {response.text}")
            responses.append(response.json())
        await submitted(world, 1)
        await REAL_SLEEP(0.05)
        assert (responses[0]["id"], len(world.engine.prompts)) == (responses[1]["id"], 1)
    run(drive())


def test_video_06_explicit_wire_fields_reach_the_engine_graph(world):
    async def drive():
        response = await create(world, size="960x512", seconds="5")
        graph = world.engine.prompts["p1"]["prompt"]
        assert graph["5"]["inputs"]["text"] == CREATE["prompt"]
        assert graph["20"]["inputs"]["value"] == 960 and graph["21"]["inputs"]["value"] == 512
        assert response.json()["size"] == "960x512" and response.json()["seconds"] == "5"
    run(drive())


def test_video_08_seconds_follow_the_recorded_frame_grid(world):
    async def drive():
        response = await create(world, seconds="5")
        graph = world.engine.prompts["p1"]["prompt"]
        assert graph["22"]["inputs"]["value"] == 121
        assert graph["23"]["inputs"]["value"] == 24
        assert response.json()["actual_seconds"] == 5.0417
    run(drive())


def test_video_10_create_submits_exactly_one_engine_prompt(world):
    async def drive():
        await create(world)
        # The job's runner also polls the engine for the result; the submit is the one POST of a prompt.
        assert prompt_posts(world) == [("POST", "/prompt")]
        assert [call for call in world.engine.traffic() if call[0] == "POST"] == [("POST", "/prompt")]
    run(drive())


def test_video_11b_an_unreachable_engine_fails_the_job_and_no_body_names_its_address(world):
    """P8 (new behaviour, a delta): the create is stored and answered first; the job then fails with a stable code, and neither the
    create answer nor the job's status nor its content names the engine's address."""
    async def drive():
        world.engine.down = True
        made = await world.rig.call("POST", "/v1/videos", json_body=CREATE)
        assert made.status_code == 200 and made.json()["status"] == "queued", made.text
        video_id = made.json()["id"]
        failed = await await_status(world, video_id, "failed")
        content = await world.rig.call("GET", f"/v1/videos/{video_id}/content")
        assert failed["error"]["code"] == "video_generation_failed" and failed["error"]["message"] == "ComfyUI unavailable", failed
        assert [json.loads(r["job_json"]).get("error_code") for r in rows(world.rig.media_jobs_db, "media_jobs")] == ["upstream_unavailable"]
        for text in (made.text, json.dumps(failed), content.text):
            assert HOST not in text and "comfy.fk.test" not in text, text
        assert content.status_code >= 400
    run(drive())


def test_video_12_create_does_not_wait_for_render(world):
    async def drive():
        world.engine.pending = True
        response = await create(world)
        assert response.json()["status"] == "queued"
        # The answer came while the engine still held the render: the video is not finished.
        assert (await world.rig.call("GET", f"/v1/videos/{response.json()['id']}")).json()["status"] != "completed"
    run(drive())


def test_video_13_disconnect_does_not_cancel_the_accepted_engine_prompt(world):
    async def drive():
        world.engine.pending = True
        world.engine.submit_release = asyncio.Event()

        def disconnect_after_accept():
            if not world.engine.prompts:
                return False
            world.engine.submit_release.set()
            return True

        await world.rig.call_disconnecting("POST", "/v1/videos", label="disconnect after engine accepts",
                                          json_body=CREATE, disconnect_when=disconnect_after_accept)
        await submitted(world, 1)
        assert prompt_posts(world) == [("POST", "/prompt")]
        assert not [call for call in world.engine.traffic() if call[1].endswith("/cancel")]
        assert world.rig.calls[-1]["response"]["status"] == 200
    run(drive())


def test_video_14_create_receipt_names_the_job(world):
    """video-14 (delta receipt_names_job): the receipt carries the job id; the old receipt had `null`."""
    async def drive():
        response = await create(world)
        kept = receipt(world, response.headers["x-request-id"])
        assert (kept["status"], kept["intent"], kept["protocol"], kept["job_id"]) == (
            "succeeded", "video.generate", "openai.video", response.json()["id"].removeprefix("video_"))
        assert kept["finished_at_ms"] is not None
    run(drive())


def test_video_15_receipt_pins_writer_inventory_not_unwritten_fields(world):
    async def drive():
        response = await create(world)
        kept = receipt(world, response.headers["x-request-id"])
        assert kept["requested_model"] == MODEL and kept["backend"] == "comfyui"
        assert kept["policy_decision"] == "allowed"
        # Integrator decision: create keeps the old receipt, closed at submit with nothing resolved. Only the job_id
        # link (receipt_names_job, video-14) and the named contract_manifest_hash delta differ from the old receipt.
        assert kept["accepted_inputs"] == [] and kept["rejected_inputs"] == {}
        assert kept["resolved_model"] is None and kept["resolved_workflow"] is None
        assert kept["manifest_hash"] is None
        assert kept["metadata"]["status_code"] == 200
    run(drive())


def test_video_18_content_delivery_indexes_the_video_in_library(world):
    async def drive():
        video_id = await source(world)
        response = await world.rig.call("GET", f"/v1/videos/{video_id}/content")
        assert response.status_code == 200 and response.headers["content-type"].startswith("video/mp4")
        metadata = [json.loads(path.read_text()) for path in world.rig.cache.glob("*.meta.json")]
        assert any(row.get("asset_id", "").startswith("vid_") for row in metadata)
        # The job is the one delivery row (video-18). Its public job document shows the asset the Library indexed.
        job = await world.rig.call("GET", f"/gateway/v1/media/jobs/{video_id.removeprefix('video_')}", unordered_json=True)
        assert job.status_code == 200, job.text
        assert job.json()["intent"] == "video.generate" and job.json()["status"] == "succeeded"
        assert len(job.json()["assets"]) == 1 and job.json()["assets"][0]["media_type"].startswith("video/")   # delta: the run core lists no `kind` leaf (open item)
        assert any(row.get("asset_id") == job.json()["assets"][0]["asset_id"] for row in metadata)
        again = await world.rig.call("GET", f"/v1/videos/{video_id}/content")
        assert again.status_code == 200
        # Exactly one video row after both fetches: a delivery must not add a second job (design 5.4).
        listed = video_job_rows(world.rig)
        assert [(row["request_id"], row["status"]) for row in listed] == [(listed[0]["request_id"], "succeeded")], listed
    run(drive())


def test_video_19_success_keeps_the_queued_video_envelope_without_location(world):
    async def drive():
        response = await create(world)
        body = response.json()
        assert JOB_ID.match(body["id"])
        # `azizam_resolved_model` keeps the old wire value (the workflow key). Design 1.2 records the manifest id
        # without the intent prefix instead; reported to the design owner as a wire change no section 5.4 kind covers.
        assert {key: body[key] for key in ("object", "model", "azizam_resolved_model", "status", "progress")} == {
            "object": "video", "model": MODEL, "azizam_resolved_model": "video-ltx-2.5-t2v",
            "status": "queued", "progress": 0}
        assert response.status_code == 200 and "location" not in response.headers
    run(drive())


def test_video_20_create_response_request_id_names_its_receipt(world):
    async def drive():
        response = await create(world)
        request_id = response.headers["x-request-id"]
        assert request_id and receipt(world, request_id)["request_id"] == request_id
    run(drive())


def test_video_21_created_video_id_is_accepted_by_public_status(world):
    async def drive():
        made = await create(world)
        video_id = made.json()["id"]
        assert JOB_ID.match(video_id)
        status = await world.rig.call("GET", f"/v1/videos/{video_id}")
        assert status.status_code == 200 and status.json()["id"] == video_id
    run(drive())


def test_video_23_engine_failure_keeps_the_video_error_vocabulary(world):
    async def drive():
        world.engine.failed = True
        made = await create(world)
        body = await await_status(world, made.json()["id"], "failed")
        assert body["status"] == "failed"
        assert body["error"] == {"code": "video_generation_failed",
                                 "message": "ComfyUI LTXVConcatAVLatent node 12: execution failed"}
    run(drive())


def test_video_36_multipart_text_fields_match_json_fields(world):
    async def drive():
        fields = [("prompt", (None, CREATE["prompt"])), ("model", (None, MODEL)),
                  ("size", (None, "960x512")), ("seconds", (None, "5"))]
        response = await world.rig.call("POST", "/v1/videos", files=fields)
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["model"], body["size"], body["seconds"]) == (MODEL, "960x512", "5")
        assert world.engine.prompts["p1"]["prompt"]["5"]["inputs"]["text"] == CREATE["prompt"]
    run(drive())


def test_video_39_unconfigured_remix_is_stable_501(world):
    async def drive():
        video_id = await source(world)
        before = len(world.engine.calls)
        configure_remix(world, None)
        response = await world.rig.call("POST", f"/v1/videos/{video_id}/remix", json_body={"prompt": "new dusk"})
        assert response.status_code == 501 and response.json()["error"]["code"] == "video_remix_not_configured"
        assert len(world.engine.calls) == before
    run(drive())


def test_video_40_configured_remix_requires_a_prompt(world):
    async def drive():
        video_id = await source(world)
        before = len(world.engine.prompts)
        # A shipped workflow name makes the config non-empty; prompt validation precedes execution.
        configure_remix(world, "video-ltx-2.5-t2v")
        response = await world.rig.call("POST", f"/v1/videos/{video_id}/remix", json_body={})
        assert response.status_code == 400, response.text
        assert response.json()["error"]["type"] == "invalid_request_error"
        assert response.json()["error"]["param"] == "prompt"
        assert len(world.engine.prompts) == before
    run(drive())


def test_video_41_non_owner_remix_hides_the_source(world):
    async def drive():
        video_id = await source(world)
        before = len(world.engine.calls)
        with authenticated(identity(everything(), name="other", app_id="app_other", key_hash="sha256:other")):
            response = await world.rig.call("POST", f"/v1/videos/{video_id}/remix", json_body={"prompt": "new dusk"})
        assert response.status_code == 404
        assert len(world.engine.calls) == before
    run(drive())


def test_video_42_disabled_capability_refuses_remix(world):
    async def drive():
        video_id = await source(world)
        before = len(world.engine.calls)
        world.rig.set(proxy, "COMFY_ENABLED", False)
        response = await world.rig.call("POST", f"/v1/videos/{video_id}/remix", json_body={"prompt": "new dusk"})
        assert response.status_code == 503, response.text
        assert len(world.engine.calls) == before
    run(drive())


def test_video_45_unknown_remix_source_is_404(world):
    async def drive():
        response = await world.rig.call("POST", "/v1/videos/video_missing/remix", json_body={"prompt": "new dusk"})
        assert response.status_code == 404
        assert world.engine.calls == []
    run(drive())


def test_video_46_unconfigured_edit_is_stable_501(world):
    async def drive():
        video_id = await source(world)
        before = len(world.engine.calls)
        configure_remix(world, None)
        response = await world.rig.call("POST", "/v1/videos/edits",
                                       json_body={"prompt": "new dusk", "video": {"id": video_id}})
        assert response.status_code == 501 and response.json()["error"]["code"] == "video_remix_not_configured"
        assert len(world.engine.calls) == before
    run(drive())


def test_video_47_edit_requires_prompt_before_engine(world):
    async def drive():
        response = await world.rig.call("POST", "/v1/videos/edits", json_body={"video": {"id": "video_missing"}})
        assert response.status_code == 400, response.text
        assert response.json() == {"error": {"message": "prompt is required",
                                           "type": "invalid_request_error", "param": "prompt"}}
        assert world.engine.calls == []
    run(drive())


def test_video_48_edit_policy_refuses_before_engine(world):
    async def drive():
        with authenticated(identity(plan_for(caps=(), categories=()))):
            response = await world.rig.call("POST", "/v1/videos/edits",
                                           json_body={"prompt": "new dusk", "video": {"id": "video_missing"}})
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "not_in_access_plan"
        assert world.engine.calls == []
    run(drive())


def test_video_49_disabled_capability_refuses_edit(world):
    async def drive():
        world.rig.set(proxy, "COMFY_ENABLED", False)
        response = await world.rig.call("POST", "/v1/videos/edits",
                                       json_body={"prompt": "new dusk", "video": {"id": "video_missing"}})
        assert response.status_code == 503, response.text
        assert world.engine.calls == []
    run(drive())


def test_video_50_cancel_of_the_goldens_missing_job_does_not_cancel_a_video(world):
    async def drive():
        world.engine.pending = True
        made = await create(world)
        await submitted(world, 1)
        response = await world.rig.call("POST", "/gateway/v1/media/jobs/None/cancel", json_body={})
        assert response.status_code == 404
        # The fake clock lets a held render reach the runner's own timeout at once; what the cancel of a missing job must never
        # do is ask for a cancel of the video: the job records no cancel request and never ends canceled.
        job = await world.rig.call("GET", f"/gateway/v1/media/jobs/{made.json()['id'].removeprefix('video_')}", unordered_json=True)
        assert job.json().get("cancel_requested") in (False, None) and job.json()["status"] != "canceled", job.text
    run(drive())


def test_video_51_unconfigured_edit_does_not_enter_an_engine_wait(world):
    async def drive():
        video_id = await source(world)
        before = len(world.engine.calls)
        configure_remix(world, None)
        response = await world.rig.call("POST", "/v1/videos/edits",
                                       json_body={"prompt": "new dusk", "video": {"id": video_id}},
                                       headers={"Prefer": "wait=1"})
        assert response.status_code == 501 and response.json()["error"]["code"] == "video_remix_not_configured"
        assert len(world.engine.calls) == before
    run(drive())


def test_video_52_unconfigured_edit_replays_repeat_the_same_refusal(world):
    async def drive():
        video_id = await source(world)
        before = len(world.engine.calls)
        configure_remix(world, None)
        for _ in range(3):
            response = await world.rig.call("POST", "/v1/videos/edits",
                                           json_body={"prompt": "new dusk", "video": {"id": video_id}},
                                           headers={"Idempotency-Key": "edit-parity"})
            assert response.status_code == 501 and response.json()["error"]["code"] == "video_remix_not_configured"
        assert len(world.engine.calls) == before
    run(drive())


def test_video_53_extend_rejects_non_numeric_seconds_before_new_prompt(world):
    async def drive():
        long_clip(world)
        video_id = await source(world)
        before = len(world.engine.prompts)
        response = await world.rig.call("POST", "/v1/videos/extensions",
                                       json_body={"video": {"id": video_id}, "prompt": "more waves", "seconds": "not-a-number"})
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_value"
        assert len(world.engine.prompts) == before
    run(drive())


def test_video_54_non_owner_cannot_extend_the_source(world):
    async def drive():
        video_id = await source(world)
        before = len(world.engine.calls)
        with authenticated(identity(everything(), name="other", app_id="app_other", key_hash="sha256:other")):
            response = await world.rig.call("POST", "/v1/videos/extensions",
                                           json_body={"video": {"id": video_id}, "prompt": "more waves", "seconds": "2"})
        assert response.status_code == 404, response.text
        assert len(world.engine.calls) == before
    run(drive())


def test_video_55_extension_uploads_source_before_one_new_prompt(world):
    async def drive():
        _, before, response = await extension(world)
        assert response.status_code == 200, response.text
        traffic = world.engine.traffic()[before:]
        uploads = [i for i, call in enumerate(traffic) if call == ("POST", "/upload/image")]
        prompts = [i for i, call in enumerate(traffic) if call == ("POST", "/prompt")]
        assert len(uploads) == len(prompts) == 1 and uploads[0] < prompts[0]
        assert b"uploaded.mp4" in json.dumps(world.engine.prompts["p2"]).encode()
    run(drive())


def test_video_56_extended_video_is_pollable_and_delivers_content(world):
    async def drive():
        _, _, response = await extension(world)
        assert response.status_code == 200, response.text
        video_id = response.json()["id"]
        status = await world.rig.call("GET", f"/v1/videos/{video_id}")
        assert status.status_code == 200 and status.json()["status"] == "completed", status.text
        content = await world.rig.call("GET", f"/v1/videos/{video_id}/content")
        assert content.status_code == 200 and content.headers["content-type"].startswith("video/mp4")
        assert content.content
    run(drive())


def test_video_58_extension_keeps_the_video_envelope_and_lineage(world):
    async def drive():
        source_id, _, response = await extension(world)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == "video_p2" and body["id"] != source_id
        assert (body["object"], body["status"], body["progress"]) == ("video", "queued", 0)
        assert body["extended_from_video_id"] == source_id and body["remixed_from_video_id"] == source_id
        assert body["total_seconds"] == 3.6 and body["segments"] == 1
    run(drive())


def test_video_94_media_job_plan_refusal_creates_no_job(world):
    async def drive():
        with authenticated(identity(plan_for(caps=(), categories=()))):
            response = await world.rig.call("POST", "/gateway/v1/media/jobs",
                                           json_body={"intent": "video.generate", "request": CREATE})
        assert response.status_code == 403, response.text
        assert response.json()["error"]["code"] == "not_in_access_plan"
        assert rows(world.rig.media_jobs_db, "media_jobs") == [] and world.engine.calls == []
    run(drive())


def test_video_96_media_job_has_location_and_distinct_job_request_ids(world):
    async def drive():
        submitted = await submit_job(world)
        body = submitted.json()
        assert body["job_id"].startswith("mj_")           # writers-43 corrects video-96's conflated vidjob_ id.
        assert body["request_id"].startswith("vidjob_")
        assert submitted.headers["location"] == f"/gateway/v1/media/jobs/{body['job_id']}"
        done = await terminal_job(world, body["job_id"])
        assert done["status"] == "succeeded", done
    run(drive())


def test_video_98_engine_failure_is_a_terminal_failed_media_job(world):
    async def drive():
        world.engine.failed = True
        submitted = await submit_job(world)
        job_id = submitted.json()["job_id"]
        done = await terminal_job(world, job_id)
        assert len(world.engine.prompts) == 1 and ("GET", "/history/p1") in world.engine.traffic()
        assert done["status"] == "failed" and done["finished_at"] is not None, done
        persisted = [row for row in rows(world.rig.media_jobs_db, "media_jobs") if row["job_id"] == job_id]
        assert len(persisted) == 1 and persisted[0]["status"] == "failed"
    run(drive())


def test_video_100_media_job_mirrors_typed_library_assets(world):
    async def drive():
        submitted = await submit_job(world)
        job_id = submitted.json()["job_id"]
        done = await terminal_job(world, job_id)
        assert done["status"] == "succeeded", done
        assert done["results"]["video"] == {"kind": "assets", "count": 1}
        asset = done["assets"][0]
        assert asset["asset_id"].startswith("vid_") and asset["media_type"] == "video/mp4" and asset["kind"] == "video"
        content = await world.rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/content")
        assert content.status_code == 200, content.text
        link = urlsplit(content.json()["data"][0]["url"])
        downloaded = await world.rig.call("GET", link.path + ("?" + link.query if link.query else ""))
        assert downloaded.status_code == 200 and downloaded.headers["content-type"].startswith("video/mp4")
        assert any(json.loads(path.read_text()).get("asset_id") == asset["asset_id"]
                   for path in world.rig.cache.glob("*.meta.json"))
    run(drive())


# ---- V3b contract tests from the video parity oracle audit (pinned on the old route; the move pairs them with deltas) ----


def test_video_16_job_store_write_failure_is_a_visible_failure(world):
    """video-16 (delta persistence_failure_visible): the old create swallowed a failed record save and answered the video.
    The job is the record, so a job store that refuses the write is a 500 with no job and no engine request."""
    async def drive():
        with sqlite3.connect(world.rig.media_jobs_db) as db:
            db.execute("""
                CREATE TRIGGER parity_refuse_job_insert
                BEFORE INSERT ON media_jobs
                BEGIN SELECT RAISE(ABORT, 'parity job write failure'); END
            """)
        try:
            response = await world.rig.call("POST", "/v1/videos", json_body=CREATE)
            assert response.status_code == 500, response.text
            await REAL_SLEEP(0.05)
            assert world.engine.calls == []
        finally:
            with sqlite3.connect(world.rig.media_jobs_db) as db:
                db.execute("DROP TRIGGER IF EXISTS parity_refuse_job_insert")
        assert (await world.rig.call("GET", "/v1/videos")).json()["data"] == []
    run(asyncio.wait_for(drive(), timeout=20))


@pytest.mark.parametrize("seconds,frames", [("2", 49), ("5", 121), ("10", 241)])
def test_video_06_frame_grid_and_dimensions_are_in_the_submitted_graph(world, seconds, frames):
    """video-06/08/113: the engine receives the frame count and rate for the asked length, with no ``_meta`` left in."""
    async def drive():
        response = await create(world, seconds=seconds)
        assert list(world.engine.prompts) == ["p1"]
        graph = world.engine.prompts["p1"]["prompt"]
        assert "_meta" not in graph
        assert graph["5"]["inputs"]["text"] == CREATE["prompt"]
        assert graph["22"]["inputs"]["value"] == frames
        assert graph["23"]["inputs"]["value"] == 24
        assert response.json()["seconds"] == seconds
        assert response.json()["actual_seconds"] == pytest.approx(frames / 24, abs=0.00005)
    run(asyncio.wait_for(drive(), timeout=20))


def test_video_07_clamped_default_size_is_the_size_in_the_graph(world):
    """video-07: an unparsable size is either refused 400 `invalid_request_error` (the alternative video-texts.md allows)
    or answered with the default size, rendered at that size (the old behaviour). It is never a 500."""
    async def drive():
        response = await world.rig.call("POST", "/v1/videos", json_body={**CREATE, "size": "bad"})
        assert response.status_code in (200, 400), response.text
        if response.status_code == 400:
            assert response.json()["error"]["type"] == "invalid_request_error"
            assert not world.engine.prompts
            return
        await submitted(world, 1)
        assert response.json()["size"] == "1024x704"
        graph = world.engine.prompts["p1"]["prompt"]
        assert graph["20"]["inputs"]["value"] == 1024
        assert graph["21"]["inputs"]["value"] == 704
    run(asyncio.wait_for(drive(), timeout=20))


def test_video_17_request_receipt_closes_with_the_answer_not_with_the_engine(world):
    """writers-12/18/19, video-17: one receipt, opened with its facts and closed once with the create answer. The job's
    runner, not the request, waits for the engine, so a submit the engine holds does not hold the receipt open. The
    receipt is read while the engine holds the submit reply and read again after the runner finished the job; it is the
    same single row both times (the old request held it open: restart_closes_receipt family; a design question)."""
    async def drive():
        world.engine.submit_release = asyncio.Event()
        try:
            response = await asyncio.wait_for(create_unawaited(world), timeout=5)
            rid = response.headers["x-request-id"]
            await submitted(world, 1)                  # the engine has the prompt and holds its reply
            closed = receipt(world, rid)
            assert len(rows(world.rig.receipts_db, "receipts")) == 1
            assert closed["status"] == "succeeded" and closed["metadata"]["status_code"] == 200
            assert closed["intent"] == "video.generate" and closed["protocol"] == "openai.video"
            assert closed["requested_model"] == MODEL and closed["backend"] == "comfyui"
            assert closed["attempt_number"] == 1
            assert closed["finished_at_ms"] >= closed["accepted_at_ms"]
        finally:
            world.engine.submit_release.set()
        video_id = response.json()["id"]
        await await_status(world, video_id)
        final = receipt(world, rid)
        assert len(rows(world.rig.receipts_db, "receipts")) == 1
        # The receipt names its job (delta receipt_names_job), so the run core files the job's result summary on it, as it does
        # for every receipt that names a job. Nothing else changes: it is not reopened, rewritten or closed again.
        summary = final["metadata"].pop("result_bundle", None)
        assert summary is not None, "the job's result summary should reach the receipt that names the job"
        assert final == closed, "the runner's work rewrote or reopened the create receipt"
    run(asyncio.wait_for(drive(), timeout=20))


async def create_unawaited(world):
    """A create that does not wait for the runner's submit (the engine holds it)."""
    response = await world.rig.call("POST", "/v1/videos", json_body=CREATE)
    assert response.status_code == 200, response.text
    return response


def test_video_96_media_job_ids_are_unique_and_link_the_runner_receipt(world):
    """video-96, writers-13/43: job_id is ``mj_<uuid4 hex>``, request_id ``vidjob_<uuid4 hex>``; the row, the job document
    and the runner receipt agree."""
    import re
    import uuid

    async def drive():
        seen_job_ids, seen_request_ids = set(), set()
        for prompt in ("first maker request", "second maker request"):
            submitted = await submit_job(world, prompt=prompt)
            body = submitted.json()
            jid, rid = body["job_id"], body["request_id"]
            assert re.fullmatch(r"mj_[0-9a-f]{32}", jid)
            assert re.fullmatch(r"vidjob_[0-9a-f]{32}", rid)
            assert uuid.UUID(jid.split("_", 1)[1]).version == 4
            assert uuid.UUID(rid.split("_", 1)[1]).version == 4
            assert jid not in seen_job_ids and rid not in seen_request_ids
            seen_job_ids.add(jid)
            seen_request_ids.add(rid)
            assert submitted.headers["location"] == f"/gateway/v1/media/jobs/{jid}"
            done = await terminal_job(world, jid)
            assert done["status"] == "succeeded"
            matching = [r for r in rows(world.rig.media_jobs_db, "media_jobs") if r["job_id"] == jid]
            assert len(matching) == 1 and matching[0]["request_id"] == rid
            stored = json.loads(matching[0]["job_json"])
            assert stored["job_id"] == jid and stored["request_id"] == rid
            assert stored["metadata"]["mode"] == "async_video_generation"
            recorded = receipt(world, rid)
            assert recorded["job_id"] == jid
            assert recorded["request_id"] == rid
            assert recorded["path"] == "/gateway/v1/media/jobs"
            assert recorded["status"] == "succeeded"
    run(asyncio.wait_for(drive(), timeout=40))
