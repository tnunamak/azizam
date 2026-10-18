"""ComfyUI-side behaviour capture: image, video and audio routes against a fake engine.

Each test is one scenario. ``play`` runs it twice in this process, requires the two
traces to be identical, records the trace for the junit dump, and compares it with
``tests/fixtures/adapter_differential/<name>.json``. See
``docs/verification/ENGINE_W6B_DIFFERENTIAL.md``.

The engine is the scenario-driven ``comfyui`` fake (tests/support/fake_upstream), served
in process at ``http://comfy.fk.test`` with ``proxy.COMFY_HOST`` pointed at it. A scenario
that needs the engine to be down routes the host to a transport that refuses.

Not reachable hermetically: the image background decision ``background: "auto"`` asks a
chat model, which is outside the ComfyUI client, so every scenario sends an explicit
background. Image bytes depend on the locked Pillow and video and audio bytes on this
machine's ffmpeg, so the goldens are comparable between checkouts on one machine.
"""
from __future__ import annotations

import asyncio
import io
import json
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

import proxy
from tests.media_test_bytes import mp4_bytes
from tests.support.adapter_differential_rig import (
    REAL_OBJECT_INFO_LOOKUP, comfy_exchanges, comfy_object_info, exchange, history_done, play, scenario,
)

COMFY_HOST = "comfy.fk.test"
HAVE_FFMPEG = shutil.which("ffmpeg") is not None


# --------------------------------------------------------------------------- fixtures

def _png(color=(200, 40, 40)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(out, format="PNG")
    return out.getvalue()


def _wav() -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x01\x00\x02\x00" * 800)
    return out.getvalue()


def _long_mp4() -> bytes:
    """A 2 second, 10 fps clip: long enough to extend (the source needs 9 frames of context)."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "clip.mp4"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=64x64:d=2:r=10",
             "-pix_fmt", "yuv420p", "-c:v", "mpeg4", "-fflags", "+bitexact", "-flags:v", "+bitexact",
             "-map_metadata", "-1", str(path)],
            capture_output=True, check=True)
        return path.read_bytes()


def _comfy(rig, exchanges, name="comfy"):
    fake = rig.engine(name, scenario("comfyui", f"{rig.name}-comfy", exchanges), host=COMFY_HOST)
    rig.set(proxy, "COMFY_HOST", fake.base_url)
    return fake


def _comfy_down(rig, error=None):
    rig.set(proxy, "COMFY_HOST", f"http://{COMFY_HOST}")
    rig.down_engine(COMFY_HOST, error)


def _capture_scheduling(rig) -> None:
    """Capture job ids the routes would hand to the background executors (``rig.scheduled``); the scenario runs them."""
    for name in ("_schedule_image_media_job", "_schedule_audio_media_job", "_schedule_video_media_job"):
        rig.set(proxy, name, lambda job_id: rig.scheduled.append(job_id) or True)


def _relative(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + ("?" + parts.query if parts.query else "")


BG = {"background": "opaque"}


# --------------------------------------------------------------------------- images, synchronous

def test_image_sync_b64(tmp_path, record_testsuite_property):
    def setup(rig):
        _comfy(rig, comfy_exchanges(views=[(_png(), "image/png")]))

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate one image as b64_json",
                       json_body={"prompt": "a red fox", "response_format": "b64_json", **BG})

    play("image_sync_b64", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/images/generations, one image, base64 in the response")


def test_image_sync_url(tmp_path, record_testsuite_property):
    def setup(rig):
        _comfy(rig, comfy_exchanges(views=[(_png((10, 120, 200)), "image/png")]))

    async def drive(rig):
        made = await rig.call("POST", "/v1/images/generations", label="generate one image as url",
                              json_body={"prompt": "a blue heron", "response_format": "url", "size": "512x512", **BG})
        url = made.json()["data"][0]["url"]
        await rig.call("GET", _relative(url), label="download the signed image url")

    play("image_sync_url", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/images/generations with response_format url, then GET the signed url")


def test_image_sync_batch(tmp_path, record_testsuite_property):
    def setup(rig):
        views = [(_png((i * 60, 30, 200 - i * 50)), "image/png") for i in range(3)]
        _comfy(rig, comfy_exchanges(views=views))

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate a batch of three",
                       json_body={"prompt": "three lanterns", "n": 3, "response_format": "b64_json", **BG})

    play("image_sync_batch", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/images/generations with n=3: adaptive batching, one round per image on the fake")


def test_image_sync_slow_completion(tmp_path, record_testsuite_property):
    def setup(rig):
        _comfy(rig, comfy_exchanges(views=[(_png(), "image/png")], running_polls=3))

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate while the history is not ready for 3 polls",
                       json_body={"prompt": "slow tide", "response_format": "b64_json", **BG})

    play("image_sync_slow_completion", tmp_path, record_testsuite_property, setup, drive,
         description="history answers 'not yet' three times: the poll cadence (gateway_sleeps) is part of the trace")


# --------------------------------------------------------------------------- images, media jobs

def test_image_job_succeeds(tmp_path, record_testsuite_property):
    def setup(rig):
        _capture_scheduling(rig)
        _comfy(rig, comfy_exchanges(views=[(_png((90, 160, 40)), "image/png")]))

    async def drive(rig):
        submit = await rig.call("POST", "/gateway/v1/media/jobs/image-generations", label="submit the image job",
                                json_body={"prompt": "a green door", "response_format": "url", **BG}, unordered_json=True)
        job_id = submit.json()["job_id"]
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="poll before the executor ran")
        rig.note("scheduled_jobs", len(rig.scheduled))
        await proxy._run_image_media_job(rig.scheduled[0])
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="poll after the executor ran")
        content = await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/content", label="job content")
        await rig.call("GET", _relative(content.json()["data"][0]["url"]), label="fetch the asset")
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/events", label="job events")

    play("image_job_succeeds", tmp_path, record_testsuite_property, setup, drive,
         description="queued image media job: submit, poll, executor, poll, content, asset, events")


def test_image_job_upstream_failure(tmp_path, record_testsuite_property):
    jobs: list[str] = []
    failed = {"p1": {"status": {"status_str": "error", "completed": False, "messages": [
        ["execution_error", {"node_type": "KSampler", "node_id": "7", "exception_message": "CUDA error"}]]},
        "outputs": {}}}

    def setup(rig):
        _capture_scheduling(rig)
        _comfy(rig, comfy_exchanges(views=[(_png(), "image/png")], history=failed, strict=False))

    async def drive(rig):
        submit = await rig.call("POST", "/gateway/v1/media/jobs/image-generations", label="submit the image job",
                                json_body={"prompt": "doomed", "response_format": "url", **BG}, unordered_json=True)
        job_id = submit.json()["job_id"]
        await proxy._run_image_media_job(rig.scheduled[0])
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="poll the failed job")
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/content", label="content of the failed job")
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/events", label="job events")

    play("image_job_upstream_failure", tmp_path, record_testsuite_property, setup, drive,
         description="queued image job whose ComfyUI history reports execution_error")


# --------------------------------------------------------------------------- image edit and variations

def test_image_edit(tmp_path, record_testsuite_property):
    def setup(rig):
        _comfy(rig, comfy_exchanges(views=[(_png((20, 20, 20)), "image/png")]))

    async def drive(rig):
        await rig.call("POST", "/v1/images/edits", label="edit with image and mask",
                       data={"prompt": "make the fox blue", "response_format": "b64_json", **BG},
                       files={"image": ("in.png", _png(), "image/png"), "mask": ("mask.png", _png((255, 255, 255)), "image/png")})

    play("image_edit", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/images/edits multipart with image and mask: upload then prompt")


def test_image_variation(tmp_path, record_testsuite_property):
    async def no_cutout(*_args, **_kwargs):
        # Variations take no `background` field, so the route asks a chat model whether to cut out
        # the subject. That decision is outside the ComfyUI client: answer "no" and keep the model out.
        return proxy.ImageBackgroundRemovalDecision(False)

    def setup(rig):
        rig.set(proxy, "resolve_image_background_decision", no_cutout)
        _comfy(rig, comfy_exchanges(views=[(_png((70, 70, 160)), "image/png")]))

    async def drive(rig):
        await rig.call("POST", "/v1/images/variations", label="variation of one image",
                       data={"response_format": "b64_json"}, files={"image": ("in.png", _png(), "image/png")})

    play("image_variation", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/images/variations multipart with one image")


# --------------------------------------------------------------------------- image failure paths

def test_image_comfy_unreachable(tmp_path, record_testsuite_property):
    def setup(rig):
        _comfy_down(rig)

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate while ComfyUI refuses connections",
                       json_body={"prompt": "nobody home", "response_format": "b64_json", **BG})

    play("image_comfy_unreachable", tmp_path, record_testsuite_property, setup, drive,
         description="ComfyUI refuses the connection (httpx.ConnectError)")


def test_image_prompt_http_500(tmp_path, record_testsuite_property):
    def setup(rig):
        _comfy(rig, comfy_exchanges(prompt={"status": 500, "json": {"error": {"type": "server_error", "message": "boom"}}}, strict=False))

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate, POST /prompt answers 500",
                       json_body={"prompt": "boom", "response_format": "b64_json", **BG})

    play("image_prompt_http_500", tmp_path, record_testsuite_property, setup, drive,
         description="POST /prompt returns HTTP 500 with a JSON body")


def test_image_prompt_without_id(tmp_path, record_testsuite_property):
    def setup(rig):
        _comfy(rig, comfy_exchanges(prompt={"json": {"node_errors": {}}}, strict=False))

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate, POST /prompt returns no prompt_id",
                       json_body={"prompt": "no id", "response_format": "b64_json", **BG})

    play("image_prompt_without_id", tmp_path, record_testsuite_property, setup, drive,
         description="POST /prompt returns 200 without a prompt_id")


def test_image_execution_error(tmp_path, record_testsuite_property):
    failed = {"p1": {"status": {"status_str": "error", "completed": False, "messages": [
        ["execution_error", {"node_type": "VAEDecode", "node_id": "8", "exception_message": "out of memory"}]]},
        "outputs": {}}}

    def setup(rig):
        _comfy(rig, comfy_exchanges(history=failed))

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate, history reports execution_error",
                       json_body={"prompt": "bad graph", "response_format": "b64_json", **BG})

    play("image_execution_error", tmp_path, record_testsuite_property, setup, drive,
         description="history reports an execution_error message")


def test_image_generation_timeout(tmp_path, record_testsuite_property):
    never = {"p1": {"status": {"status_str": "running", "completed": False, "messages": []}, "outputs": {}}}

    def setup(rig):
        rig.set(proxy, "COMFY_TIMEOUT", 2)  # the round budget is counted in skipped sleeps, not wall time
        _comfy(rig, comfy_exchanges(history=never, queue={"queue_running": [], "queue_pending": [[0, "p1", {}, {}, []]]}))

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate, ComfyUI never finishes",
                       json_body={"prompt": "forever", "response_format": "b64_json", **BG})

    play("image_generation_timeout", tmp_path, record_testsuite_property, setup, drive,
         description="history never completes: after the 2 second budget the gateway asks /queue and cancels the prompt")


# --------------------------------------------------------------------------- video

def _video_comfy(rig, **kw):
    return _comfy(rig, comfy_exchanges(
        history=history_done("videos", "clip.mp4", count=1), views=[(kw.pop("clip", mp4_bytes()), "video/mp4")], **kw))


def test_video_lifecycle(tmp_path, record_testsuite_property):
    def setup(rig):
        _video_comfy(rig, running_polls=1)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", label="create a video",
                                 json_body={"prompt": "waves at dusk", "model": "ltx-2.5-t2v"})
        video_id = created.json()["id"]
        await rig.call("GET", f"/v1/videos/{video_id}", label="status while ComfyUI has no history yet")
        await rig.call("GET", f"/v1/videos/{video_id}", label="status after ComfyUI finished")
        await rig.call("GET", f"/v1/videos/{video_id}/content", label="download the content")
        await rig.call("GET", "/v1/videos", label="list videos")
        await rig.call("DELETE", f"/v1/videos/{video_id}", label="delete the video")
        await rig.call("GET", "/v1/videos", label="list videos after the delete")

    play("video_lifecycle", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/videos, status while pending and when done, content, list, delete, list")


def test_video_delete_while_running(tmp_path, record_testsuite_property):
    running = {"p1": {"status": {"status_str": "running", "completed": False, "messages": []}, "outputs": {}}}
    busy = {"queue_running": [[0, "p1", {}, {}, []]], "queue_pending": []}

    def setup(rig):
        _comfy(rig, comfy_exchanges(history=running, queue=busy))

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", label="create a video",
                                 json_body={"prompt": "never ends", "model": "ltx-2.5-t2v"})
        video_id = created.json()["id"]
        await rig.call("GET", f"/v1/videos/{video_id}", label="status of a running video")
        await rig.call("DELETE", f"/v1/videos/{video_id}", label="delete a running video: cancel and clean up")

    play("video_delete_while_running", tmp_path, record_testsuite_property, setup, drive,
         description="DELETE of a video ComfyUI is still rendering issues the cancel and history-delete calls")


def test_video_execution_error(tmp_path, record_testsuite_property):
    failed = {"p1": {"status": {"status_str": "error", "completed": False, "messages": [
        ["execution_error", {"node_type": "LTXVSampler", "node_id": "12", "exception_message": "oom"}]]}, "outputs": {}}}

    def setup(rig):
        _comfy(rig, comfy_exchanges(history=failed))

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", label="create a video",
                                 json_body={"prompt": "will fail", "model": "ltx-2.5-t2v"})
        video_id = created.json()["id"]
        await rig.call("GET", f"/v1/videos/{video_id}", label="status of a failed video")
        await rig.call("GET", f"/v1/videos/{video_id}/content", label="content of a failed video")

    play("video_execution_error", tmp_path, record_testsuite_property, setup, drive,
         description="video whose ComfyUI history reports execution_error: status failed, content refused")


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is needed to build the source clip")
def test_video_remix(tmp_path, record_testsuite_property):
    def setup(rig):
        _video_comfy(rig, clip=_long_mp4())

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", label="create the source video",
                                 json_body={"prompt": "waves at dusk", "model": "ltx-2.5-t2v"})
        video_id = created.json()["id"]
        await rig.call("GET", f"/v1/videos/{video_id}", label="status of the source")
        await rig.call("POST", f"/v1/videos/{video_id}/remix", label="remix the source",
                       json_body={"prompt": "the same waves at night", "seconds": "1.5"})

    play("video_remix", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/videos/{id}/remix of a finished video: source download, upload, new prompt")


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is needed to build the source clip")
def test_video_extend(tmp_path, record_testsuite_property):
    def setup(rig):
        _video_comfy(rig, clip=_long_mp4())

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", label="create the source video",
                                 json_body={"prompt": "waves at dusk", "model": "ltx-2.5-t2v"})
        video_id = created.json()["id"]
        await rig.call("GET", f"/v1/videos/{video_id}", label="status of the source")
        await rig.call("POST", "/v1/videos/extensions", label="extend the source",
                       json_body={"video": {"id": video_id}, "prompt": "more waves", "seconds": "2"})

    play("video_extend", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/videos/extensions of a finished video")


@pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg is needed to build the source clip")
def test_video_job_succeeds(tmp_path, record_testsuite_property):
    def setup(rig):
        _capture_scheduling(rig)
        rig.set(proxy, "_VIDEO_MEDIA_JOB_POLL_INTERVAL_SECONDS", 0)
        _video_comfy(rig, clip=_long_mp4())

    async def drive(rig):
        submit = await rig.call("POST", "/gateway/v1/media/jobs", label="submit the video job",
                                json_body={"intent": "video.generate",
                                           "request": {"prompt": "waves at dusk", "model": "ltx-2.5-t2v"}},
                                unordered_json=True)
        job_id = submit.json()["job_id"]
        await proxy._run_video_media_job(rig.scheduled[0])
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="poll the finished job")
        content = await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/content", label="job content")
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/events", label="job events")
        rig.note("content_status", content.status_code)

    play("video_job_succeeds", tmp_path, record_testsuite_property, setup, drive,
         description="queued video media job through the generic submit route")


# --------------------------------------------------------------------------- audio

def _audio_comfy(rig, **kw):
    return _comfy(rig, comfy_exchanges(
        history=history_done("audio", "tone_00001_.wav"), views=[(_wav(), "audio/wav")], **kw))


def test_audio_generate_sync(tmp_path, record_testsuite_property):
    def setup(rig):
        _audio_comfy(rig)

    async def drive(rig):
        await rig.call("POST", "/v1/audio/generate", label="generate audio",
                       json_body={"prompt": "a door creaks", "model": "tangoflux", "duration": 4})

    play("audio_generate_sync", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/audio/generate against a fake audio file served by /view")


def test_audio_generate_job(tmp_path, record_testsuite_property):
    def setup(rig):
        _capture_scheduling(rig)
        _audio_comfy(rig)

    async def drive(rig):
        submit = await rig.call("POST", "/gateway/v1/media/jobs", label="submit the audio job",
                                json_body={"intent": "audio.generate",
                                           "request": {"prompt": "a door creaks", "model": "tangoflux", "duration": 4}},
                                unordered_json=True)
        job_id = submit.json()["job_id"]
        await proxy._run_audio_media_job(rig.scheduled[0])
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="poll the finished job")
        content = await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/content", label="job content")
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/events", label="job events")
        rig.note("content_status", content.status_code)

    play("audio_generate_job", tmp_path, record_testsuite_property, setup, drive,
         description="queued audio media job through the generic submit route, executor run explicitly")


def test_audio_generate_unreachable(tmp_path, record_testsuite_property):
    def setup(rig):
        _comfy_down(rig)

    async def drive(rig):
        await rig.call("POST", "/v1/audio/generate", label="generate audio while ComfyUI refuses",
                       json_body={"prompt": "a door creaks", "model": "tangoflux", "duration": 4})

    play("audio_generate_unreachable", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/audio/generate with ComfyUI refusing connections")


# --------------------------------------------------------------------------- more engine behaviours

def _object_info_handler(workflow: Path):
    """Answer GET /object_info/{class} for the node classes of one workflow file (generic slot definitions)."""
    graph = {k: v for k, v in json.loads(workflow.read_text()).items() if k != "_meta"}

    def is_link(value) -> bool:
        return isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int)

    outputs: dict[str, int] = {}
    for node in graph.values():
        for value in node["inputs"].values():
            if is_link(value):
                source = graph[value[0]]["class_type"]
                outputs[source] = max(outputs.get(source, 0), value[1] + 1)
    infos: dict[str, dict] = {}
    for node in graph.values():
        required = {}
        for name, value in node["inputs"].items():
            kind = ("*" if is_link(value) else "BOOLEAN" if isinstance(value, bool) else "INT" if isinstance(value, int)
                    else "FLOAT" if isinstance(value, float) else "STRING")
            required[name] = [kind, {}]
        count = outputs.get(node["class_type"], 0)
        infos[node["class_type"]] = {
            "input": {"required": required}, "input_order": {"required": list(required)},
            "output": ["*"] * count, "output_name": [f"OUT{i}" for i in range(count)],
            "name": node["class_type"], "display_name": node["class_type"]}

    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if request.url.path.startswith("/object_info/") and name in infos:
            return httpx.Response(200, json={name: infos[name]})
        return httpx.Response(404, json={})

    return handler


def test_audio_generate_with_object_info(tmp_path, record_testsuite_property):
    """The UI workflow embedded in the prompt is derived from ComfyUI's /object_info (a blocking client)."""
    def setup(rig):
        rig.set(comfy_object_info, "lookup", REAL_OBJECT_INFO_LOOKUP)
        rig.set(comfy_object_info, "_CACHE", {})
        rig.set(comfy_object_info, "_FAILED_AT", {})
        rig.sync_engine(COMFY_HOST, _object_info_handler(Path(proxy.AUDIO_WORKFLOWS_DIR) / "tangoflux.json"))
        _audio_comfy(rig)

    async def drive(rig):
        await rig.call("POST", "/v1/audio/generate", label="generate audio; the prompt carries a derived UI workflow",
                       json_body={"prompt": "a door creaks", "model": "tangoflux", "duration": 4})

    play("audio_generate_with_object_info", tmp_path, record_testsuite_property, setup, drive,
         description="node slot definitions come from GET /object_info/{class} on a blocking client; the prompt embeds the derived workflow")


def test_image_sync_oom_then_complete(tmp_path, record_testsuite_property):
    def setup(rig):
        _comfy(rig, comfy_exchanges(views=[(_png((120, 60, 160)), "image/png")], extra=[
            exchange("prompt-oom", "POST", "/prompt", {"status": 500, "body": [{"text": "CUDA out of memory. Tried to allocate 2.00 GiB"}]},
                     times=1)]))

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate, the first submit runs out of memory",
                       json_body={"prompt": "heavy", "response_format": "b64_json", **BG})

    play("image_sync_oom_then_complete", tmp_path, record_testsuite_property, setup, drive,
         description="out-of-memory on submit with an idle queue: the gateway frees models and submits again")


def test_image_job_oom_then_complete(tmp_path, record_testsuite_property):
    def setup(rig):
        _capture_scheduling(rig)
        _comfy(rig, comfy_exchanges(views=[(_png((60, 160, 120)), "image/png")], extra=[
            exchange("prompt-oom", "POST", "/prompt", {"status": 500, "body": [{"text": "CUDA out of memory. Tried to allocate 2.00 GiB"}]},
                     times=1)]))

    async def drive(rig):
        submit = await rig.call("POST", "/gateway/v1/media/jobs/image-generations", label="submit the image job",
                                json_body={"prompt": "heavy", "response_format": "url", **BG}, unordered_json=True)
        job_id = submit.json()["job_id"]
        await proxy._run_image_media_job(rig.scheduled[0])
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}", label="poll the finished job")
        await rig.call("GET", f"/gateway/v1/media/jobs/{job_id}/events", label="job events")

    play("image_job_oom_then_complete", tmp_path, record_testsuite_property, setup, drive,
         description="queued image job whose first submit runs out of memory: retry events and sleeps are part of the trace")


def test_image_history_transient_errors(tmp_path, record_testsuite_property):
    def setup(rig):
        _comfy(rig, comfy_exchanges(views=[(_png((10, 200, 10)), "image/png")], extra=[
            exchange("history-error", "GET", "/history/p1", {"status": 503, "json": {"error": "busy"}}, times=2)]))

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate, history answers 503 twice",
                       json_body={"prompt": "patient", "response_format": "b64_json", **BG})

    play("image_history_transient_errors", tmp_path, record_testsuite_property, setup, drive,
         description="GET /history answers 503 twice before the result: the poll continues")


def test_image_view_missing(tmp_path, record_testsuite_property):
    def setup(rig):
        _comfy(rig, comfy_exchanges(views=[(b"", "text/plain")], strict=False, extra=[
            exchange("view-missing", "GET", "/view", {"status": 404, "json": {"error": "not found"}}, times=1)]))

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate, the output file cannot be downloaded",
                       json_body={"prompt": "lost", "response_format": "b64_json", **BG})

    play("image_view_missing", tmp_path, record_testsuite_property, setup, drive,
         description="GET /view answers 404 for the finished output: no image, an error to the caller")


def test_image_edit_upload_refused(tmp_path, record_testsuite_property):
    def setup(rig):
        _comfy(rig, comfy_exchanges(strict=False, extra=[
            exchange("upload-refused", "POST", "/upload/image", {"status": 500, "json": {"error": "disk full"}}, times=1)]))

    async def drive(rig):
        await rig.call("POST", "/v1/images/edits", label="edit while ComfyUI refuses the upload",
                       data={"prompt": "make it blue", "response_format": "b64_json", **BG},
                       files={"image": ("in.png", _png(), "image/png")})

    play("image_edit_upload_refused", tmp_path, record_testsuite_property, setup, drive,
         description="POST /upload/image answers 500: the edit stops before any prompt is submitted")


def test_video_create_unreachable(tmp_path, record_testsuite_property):
    def setup(rig):
        _comfy_down(rig)

    async def drive(rig):
        await rig.call("POST", "/v1/videos", label="create a video while ComfyUI refuses",
                       json_body={"prompt": "nobody home", "model": "ltx-2.5-t2v"})

    play("video_create_unreachable", tmp_path, record_testsuite_property, setup, drive,
         description="POST /v1/videos with ComfyUI refusing connections")


def test_audio_generate_execution_error(tmp_path, record_testsuite_property):
    failed = {"p1": {"status": {"status_str": "error", "completed": False, "messages": [
        ["execution_error", {"node_type": "TangoFluxSampler", "node_id": "2", "exception_message": "oom"}]]}, "outputs": {}}}

    def setup(rig):
        _comfy(rig, comfy_exchanges(history=failed, strict=False))

    async def drive(rig):
        await rig.call("POST", "/v1/audio/generate", label="generate audio, history reports execution_error",
                       json_body={"prompt": "a door creaks", "model": "tangoflux", "duration": 4})

    play("audio_generate_execution_error", tmp_path, record_testsuite_property, setup, drive,
         description="audio generation whose ComfyUI history reports execution_error")


def test_image_request_task_cancelled(tmp_path, record_testsuite_property):
    """The request task is cancelled while ComfyUI is still working (a shutdown, a middleware cancel scope)."""
    def setup(rig):
        _comfy(rig, comfy_exchanges(views=[(_png(), "image/png")], strict=False, extra=[
            exchange("history-hang", "GET", "/history/p1", {"wait_before_headers": "forever", "json": {}}, times=1)]))

    async def drive(rig):
        await rig.call("POST", "/v1/images/generations", label="generate, then the request task is cancelled mid-render",
                       json_body={"prompt": "abandoned", "response_format": "b64_json", **BG},
                       cancel_when=lambda: rig.fakes.clock.until_waiting())
        for _ in range(20):  # the route's cancellation handler finishes on the loop
            await asyncio.sleep(0)

    play("image_request_task_cancelled", tmp_path, record_testsuite_property, setup, drive,
         description="the request task is cancelled while GET /history is pending: the cancel call starts, the receipt is cancelled")


def test_image_client_disconnect_during_poll(tmp_path, record_testsuite_property):
    """``http.disconnect`` reaches the app while the gateway polls ComfyUI; the trace records what the route does.

    The route checks ``request.is_disconnected()`` before every poll. The gateway's middleware stack
    wraps ``receive``, and the trace shows the poll loop running on to its time budget, so the
    disconnect is not seen on this path. The scenario pins that behaviour, whichever way it is.
    """
    running = {"p1": {"status": {"status_str": "running", "completed": False, "messages": []}, "outputs": {}}}

    def setup(rig):
        rig.set(proxy, "COMFY_TIMEOUT", 2)
        _comfy(rig, comfy_exchanges(history=running, strict=False))

    async def drive(rig):
        polls = lambda: sum(1 for e in rig.recorder.engine if e["path"].startswith("/history/")) >= 2
        await rig.call_disconnecting("POST", "/v1/images/generations", label="generate, the client disconnects after two polls",
                                     json_body={"prompt": "abandoned", "response_format": "b64_json", **BG},
                                     disconnect_when=polls)

    play("image_client_disconnect_during_poll", tmp_path, record_testsuite_property, setup, drive,
         description="http.disconnect is delivered to the app after two /history polls; the trace records the route's reaction")
