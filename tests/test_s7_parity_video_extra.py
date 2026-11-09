"""S7 parity tests for the video group, R16 (ids, access, storage) and R3 (multipart create).

Black-box: each test drives the real app through ``GatewayRig`` (the fake ComfyUI is the only fake) and asserts what a
caller and the engine observe. Items are the numbered rows of ``docs/design/c2d-s7-parity/video.md``; the name of each
test carries its item id. A test that pins today's behaviour passes on the base; a test for a behaviour the row path
changes is written first and fails until the move.

Rewritten for the video move (docs/design/CODE_TO_DATA_S7_VIDEO_MOVE.md section 5.1): a video made by create is the job
``video_mj_<hex>``; ids are read from responses, and the stores a test reads are the public routes and ``JobService``.
Chain extension stays on the old handler, so an extension is still ``video_<prompt id>`` and its record is read from the
legacy table. The tests that pinned the old route's ignoring of ``Idempotency-Key`` are gone: delta
``idempotency_acceptance`` replaces them, and video-05 (strict xfail in the create file) carries the new contract.

Receipt items (video-03, 04, 14) pin what the old route writes today, so they pass on the base; the row path's
receipt hook (RH, ``RowRouteDeps.open_receipt``) is what could move them. No item here is xfail: a strict xfail on an
item that already matches would fail as XPASS.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import sqlite3
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import pytest

import proxy
from tests.support.video_rig import video_job_rows
from tests.support.access import authenticated, everything, identity, plan_for
from tests.support.adapter_differential_rig import GatewayRig
from tests.support.config_snapshot import publish_section
from tests.test_adapter_differential import _png, _video_comfy

MARKER = "prompt-marker-7f3a9c"
JOB_ID = re.compile(r"^video_mj_[0-9a-f]{32}$")
RIG_OWNER = identity(everything(media_metadata="none", receipt_detail="full"))     # the rig's default caller


def _job_view(video_id):
    """The job behind a job-backed video id, read as its maker through the public job service."""
    view = proxy._workflow_job_service().get(video_id.removeprefix("video_"), proxy.AuthContext(identity=RIG_OWNER))
    assert view is not None, video_id
    return view


@lru_cache(maxsize=None)
def long_mp4_bytes(color: str = "black") -> bytes:
    """A 5 s clip. A remix or edit names a source, and a workflow refuses a request longer than its source; two
    colours give two clips whose bytes can be told apart."""
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "long.mp4"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
             f"color=c={color}:s=16x16:d=5:r=24", "-c:v", "mpeg4", str(output)],
            capture_output=True, check=True,
        )
        return output.read_bytes()


def _run(tmp_path: Path, name: str, setup, drive) -> GatewayRig:
    with GatewayRig(name, tmp_path) as rig:
        # The run core keeps its job spool and slots between tests; each test gets its own, built from its own directories.
        rig.monkeypatch.setattr(proxy, "_WORKFLOW_JOB_SPOOL", None)
        rig.monkeypatch.setattr(proxy, "_workflow_core_cache", None)
        rig.monkeypatch.setattr(proxy, "_workflow_run_semaphore", None)
        setup(rig)
        asyncio.run(drive(rig))
        return rig


_RUNNING_QUEUE = {"queue_running": [[0, "p1", {}, {}, []]], "queue_pending": []}


def _comfy(rig: GatewayRig, **kw) -> None:
    # Not every test reaches the engine's end (a refusal stops before it): nothing is required.
    kw.setdefault("strict", False)
    if kw.get("running_polls"):                 # the engine reports the prompt as running: the run core reads the queue too
        kw.setdefault("queue", _RUNNING_QUEUE)
    _video_comfy(rig, **kw)


def _engine_paths(rig: GatewayRig) -> list[str]:
    return [f"{entry['method']} {entry['path']}" for entry in rig.recorder.engine]


async def _await_submit(rig: GatewayRig) -> None:
    """The job's runner submits the prompt after the create answered: wait (real time, bounded) until it has."""
    for _ in range(500):
        if "POST /prompt" in _engine_paths(rig):
            return
        await asyncio.sleep(0.01)


# --------------------------------------------------------------------------- R16: ids, access, storage

def test_video_16_121_wire_id_is_prefixed_and_stable_on_lookup(tmp_path):
    """video-121: the wire id carries the video prefix; a GET of the id just returned answers for the same video."""
    seen = {}

    def setup(rig):
        _comfy(rig, running_polls=1)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        video_id = created.json()["id"]
        seen["id"] = video_id
        status = await rig.call("GET", f"/v1/videos/{video_id}")
        seen["status"] = status.status_code
        seen["same"] = status.json().get("id") == video_id

    _run(tmp_path, "r16-121", setup, drive)
    assert JOB_ID.match(seen["id"]), seen["id"]
    assert seen["status"] == 200
    assert seen["same"]


@pytest.mark.parametrize("bad", ["nonsense", "vid_123", "video_", "videoX1"])
def test_video_16_122_malformed_id_is_400_invalid_video_id(tmp_path, bad):
    """video-122, video-65, video-128: an id that is not `video_<id>` is refused with 400 `invalid video_id`."""
    seen = {}

    async def drive(rig):
        response = await rig.call("GET", f"/v1/videos/{bad}")
        seen["status"], seen["body"] = response.status_code, response.text

    _run(tmp_path, "r16-122", lambda rig: None, drive)
    assert "invalid video_id" in seen["body"]
    assert seen["status"] == 400


def test_video_16_128_delete_refuses_a_non_video_id(tmp_path):
    """video-128, video-89: DELETE of an id that is not a video id is 400 `invalid video_id`."""
    seen = {}

    async def drive(rig):
        response = await rig.call("DELETE", "/v1/videos/not-a-video")
        seen["status"], seen["body"] = response.status_code, response.text

    _run(tmp_path, "r16-128", lambda rig: None, drive)
    assert seen["status"] == 400
    assert "invalid video_id" in seen["body"]


def test_video_16_64_non_owner_gets_404_for_status(tmp_path):
    """video-64, video-123: a video made by one key is 404 to another key (status hidden, not refused)."""
    seen = {}

    def setup(rig):
        _comfy(rig, running_polls=1)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        video_id = created.json()["id"]
        other = identity(everything(), name="other-app", key_hash="sha256:other", app_id="app_other")
        with authenticated(other):
            response = await rig.call("GET", f"/v1/videos/{video_id}", label="status as another key")
        seen["status"] = response.status_code

    _run(tmp_path, "r16-64", setup, drive)
    assert seen["status"] == 404


def test_video_16_124_prompt_text_is_not_stored_in_the_video_record(tmp_path):
    """video-124 (delta record_store_moved): the old record row held no prompt text. The facts the job keeps, and every
    answer about the video, hold none either."""
    def setup(rig):
        _comfy(rig)

    seen = {}

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body={"prompt": MARKER, "model": "ltx-2.5-t2v"})
        video_id = created.json()["id"]
        status = await rig.call("GET", f"/v1/videos/{video_id}")
        facts = proxy._workflow_job_service().request_facts(video_id.removeprefix("video_"), proxy.AuthContext(identity=RIG_OWNER))
        seen.update(created=created.text, status=status.text, facts=facts)

    _run(tmp_path, "r16-124", setup, drive)
    assert seen["facts"] is not None, "the create recorded no request facts"
    assert MARKER not in repr(dict(seen["facts"])) and MARKER not in seen["status"]


def test_video_16_125_request_facts_replace_the_migration_marker(tmp_path):
    """video-125 (delta record_store_moved): the migration `video_job_records_v1` marked the old record table. A created
    video now has its facts on the job: the model, the resolved model, the size and the seconds."""
    def setup(rig):
        _comfy(rig)

    seen = {}

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        video_id = created.json()["id"]
        seen["facts"] = proxy._workflow_job_service().request_facts(
            video_id.removeprefix("video_"), proxy.AuthContext(identity=RIG_OWNER))

    _run(tmp_path, "r16-125", setup, drive)
    facts = dict(seen["facts"])
    assert facts["model"] == "ltx-2.5-t2v" and facts["size"] and facts["seconds"]


# --------------------------------------------------------------------------- R3: multipart create

def test_video_3_36_multipart_text_form_creates_a_queued_video(tmp_path):
    """video-36: a multipart form of text fields is the same create as the JSON body: 200, queued, a video id."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos",
                                  files={"prompt": (None, "waves"), "model": (None, "ltx-2.5-t2v")})
        seen["status"] = response.status_code
        seen["body"] = response.json()

    _run(tmp_path, "r3-36", setup, drive)
    assert seen["status"] == 200
    assert seen["body"]["status"] == "queued"
    assert seen["body"]["id"].startswith("video_")


def test_video_3_36_malformed_multipart_is_400(tmp_path):
    """video-36: a body that claims multipart but is not one is refused with 400 `Invalid multipart body`."""
    seen = {}

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", content=b"--xx\r\nnot a part\r\n",
                                  headers={"content-type": "multipart/form-data; boundary=xx"})
        seen["status"], seen["body"] = response.status_code, response.text

    _run(tmp_path, "r3-36-bad", lambda rig: None, drive)
    assert seen["status"] == 400
    assert "Invalid multipart body" in seen["body"] or "multipart" in seen["body"].lower()


def test_video_3_37_input_reference_is_uploaded_to_the_engine_before_the_prompt(tmp_path):
    """video-37: an uploaded input_reference is sent to the engine (POST /upload/image) before the prompt is queued."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", files={
            "prompt": (None, "waves"), "model": (None, "ltx-2.5-t2v"),
            "input_reference": ("ref.png", _png(), "image/png")})
        seen["status"] = response.status_code

    rig = _run(tmp_path, "r3-37", setup, drive)
    assert seen["status"] == 200
    paths = _engine_paths(rig)
    assert "POST /upload/image" in paths
    assert paths.index("POST /upload/image") < paths.index("POST /prompt")


def test_video_3_38_invalid_input_reference_is_a_400_with_the_param(tmp_path):
    """video-38, video-75: a reference that is none of asset id, URL or data URI is a 400 that names the param, and the
    engine is not called."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={
            "prompt": "waves", "model": "ltx-2.5-t2v", "input_reference": "definitely not a reference"})
        seen["status"], seen["body"] = response.status_code, response.text

    rig = _run(tmp_path, "r3-38", setup, drive)
    assert seen["status"] == 400
    assert "input_reference" in seen["body"]
    assert not any(p == "POST /prompt" for p in _engine_paths(rig))


# --------------------------------------------------------------------------- receipts
# Today's route writes these receipts, so these are pins: they pass on the base and must keep passing on the row path.
# The row path's receipt (RH open_receipt, receipt_opens) is what could break them.

def test_video_3_03_validation_refusal_writes_a_receipt(tmp_path):
    """video-03: a refused create leaves one closed receipt row (the golden s7_vc_validation_refusal)."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": 12, "model": "ltx-2.5-t2v"})
        seen["status"] = response.status_code
        seen["receipts"] = rig.trace()["receipts"]

    _run(tmp_path, "r3-03", setup, drive)
    assert seen["status"] == 400
    assert len(seen["receipts"].get("receipts") or []) == 1


def test_video_3_04_policy_refusal_is_403_before_the_engine_and_leaves_no_receipt(tmp_path):
    """video-04: a caller whose plan grants no video is refused 403 `not_in_access_plan` before any engine request. The
    old route writes no receipt for this refusal (golden s7_vc_policy_refusal); the row keeps that
    (receipt_opens: after_policy)."""
    seen = {}

    def setup(rig):
        _comfy(rig)
        rig._exit.enter_context(authenticated(identity(plan_for(caps=("chat",)))))

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        seen["status"], seen["code"] = response.status_code, response.json()["error"]["code"]
        seen["receipts"] = rig.trace()["receipts"]
        seen["engine"] = _engine_paths(rig)

    _run(tmp_path, "r3-04", setup, drive)
    assert seen["status"] == 403
    assert seen["code"] == "not_in_access_plan"
    assert "POST /prompt" not in seen["engine"]
    assert not (seen["receipts"].get("receipts") or [])


def test_video_3_14_success_writes_one_closed_receipt(tmp_path):
    """video-14: a successful create writes one receipt (golden video_lifecycle)."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        seen["status"] = response.status_code
        seen["receipts"] = rig.trace()["receipts"]

    _run(tmp_path, "r3-14", setup, drive)
    assert seen["status"] == 200
    rows = seen["receipts"].get("receipts") or []
    assert len(rows) == 1


def test_video_3_20_response_carries_x_request_id(tmp_path):
    """video-20: the create answer carries the request id header."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        seen["headers"] = dict(response.headers)

    _run(tmp_path, "r3-20", setup, drive)
    assert "x-request-id" in seen["headers"]




# --------------------------------------------------------------------------- R1: create (direct and template)

def test_video_1_01_capability_off_is_503_before_any_engine_call(tmp_path):
    """video-01: with the media backend disabled the create answers 503 `service_unavailable` and calls no engine."""
    seen = {}

    def setup(rig):
        _comfy(rig)
        rig.monkeypatch.setitem(proxy.runtime().derived, "COMFY_ENABLED", False)
        record = dict(proxy.config["backends"]["comfy"], enabled=False)       # the service record is off, as an operator turns it off
        rig.monkeypatch.setitem(proxy.config, "backends", {**proxy.config["backends"], "comfy": record})

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        seen["status"], seen["body"] = response.status_code, response.text
        seen["engine"] = _engine_paths(rig)

    _run(tmp_path, "r1-01", setup, drive)
    assert seen["status"] == 503
    assert "Video generation not enabled" in seen["body"]
    assert seen["engine"] == []


def test_video_1_02_non_string_prompt_is_refused_before_the_engine(tmp_path):
    """video-02: a non-string prompt on the direct create is refused before the engine is called."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": ["waves"], "model": "ltx-2.5-t2v"})
        seen["status"] = response.status_code
        seen["engine"] = _engine_paths(rig)

    _run(tmp_path, "r1-02", setup, drive)
    assert seen["status"] == 400
    assert "POST /prompt" not in seen["engine"]


def test_video_1_05_same_idempotency_key_twice_submits_once(tmp_path):
    """video-05 (decided: the `idempotency_acceptance` delta, S7 parity section 4): the old direct route ignored the key
    and sent two engine prompts. The row path dedupes: one engine prompt, and the replay answers the same video id.
    Red on the base; the move makes it pass."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        body = {"prompt": "waves", "model": "ltx-2.5-t2v"}
        first = await rig.call("POST", "/v1/videos", json_body=body, headers={"Idempotency-Key": "k-video-05"})
        second = await rig.call("POST", "/v1/videos", json_body=body, headers={"Idempotency-Key": "k-video-05"})
        assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
        seen["ids"] = (first.json().get("id"), second.json().get("id"))
        seen["prompts"] = sum(1 for p in _engine_paths(rig) if p == "POST /prompt")

    _run(tmp_path, "r1-05", setup, drive)
    assert seen["ids"][0] is not None and seen["ids"][0] == seen["ids"][1]
    assert seen["prompts"] == 1


@pytest.mark.xfail(strict=True, reason="open item P8: a create answers 200 queued before the engine is asked, and the body must not name "
                   "the engine address (see test_video_11b in test_s7_parity_video_create.py)")
def test_video_1_11_engine_unreachable_is_502_with_the_engine_url(tmp_path):
    """video-11: an unreachable engine answers 502 `api_error` and names the engine URL (golden video_create_unreachable)."""
    seen = {}

    def setup(rig):
        from tests.test_adapter_differential import _comfy_down
        _comfy_down(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        seen["status"], seen["body"] = response.status_code, response.text

    _run(tmp_path, "r1-11", setup, drive)
    assert seen["status"] == 502
    assert "ComfyUI unavailable at" in seen["body"]


def test_video_1_14_create_answer_is_queued_with_no_location_and_the_model(tmp_path):
    """video-19: the create answer is 200 with the video object, `status: queued`, progress 0, the requested model and
    the resolved model; no Location header (golden s7_vc_replay_same)."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        seen["status"], seen["body"], seen["headers"] = response.status_code, response.json(), dict(response.headers)

    _run(tmp_path, "r1-19", setup, drive)
    body = seen["body"]
    assert seen["status"] == 200
    assert body["object"] == "video" and body["status"] == "queued" and body["progress"] == 0
    assert body["model"] == "ltx-2.5-t2v"
    assert body["azizam_resolved_model"] == "video-ltx-2.5-t2v"
    assert "location" not in seen["headers"]


def test_video_1_30_size_is_clamped_not_rejected(tmp_path):
    """video-07: an oversize `size` is clamped to the workflow's bounds, not refused."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v",
                                                                   "size": "9000x9000"})
        seen["status"] = response.status_code

    _run(tmp_path, "r1-07", setup, drive)
    assert seen["status"] == 200


# --------------------------------------------------------------------------- R7-R10: lifecycle views

def _created(rig_setup_kwargs=None):
    return {"prompt": "waves", "model": "ltx-2.5-t2v"}


def test_video_7_59_status_maps_running_to_in_progress_and_history_success_to_completed(tmp_path):
    """video-59, 67, 68: status answers the OpenAI vocabulary: queued/in_progress while the engine works, completed
    (azizam_status succeeded) once it finished, with the progress value."""
    seen = {}

    def setup(rig):
        _comfy(rig, running_polls=6)      # the runner polls on its own now; the engine stays busy long enough to be seen

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
        video_id = created.json()["id"]
        seen["pending"] = (await rig.call("GET", f"/v1/videos/{video_id}")).json()
        seen["done"] = await _audit_wait(rig, video_id)     # the job's runner reaches the engine's end

    _run(tmp_path, "r7-59", setup, drive)
    assert seen["pending"]["status"] in ("queued", "in_progress")
    assert seen["pending"]["azizam_status"] in ("queued", "running")
    assert seen["done"]["status"] == "completed", json.dumps(seen["done"])
    assert seen["done"]["azizam_status"] == "succeeded"


def test_video_7_69_failed_job_status_carries_the_error(tmp_path):
    """video-69, video-23: a failed render's status is `failed` with an `error` naming the engine failure."""
    seen = {}
    from tests.test_adapter_differential_s7 import FAILED_HISTORY

    def setup(rig):
        from tests.test_adapter_differential import _comfy as engine
        from tests.support.adapter_differential_rig import comfy_exchanges
        engine(rig, comfy_exchanges(history=FAILED_HISTORY, strict=False))

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
        video_id = created.json()["id"]
        seen["status"] = await _audit_wait(rig, video_id)

    _run(tmp_path, "r7-69", setup, drive)
    assert seen["status"]["status"] == "failed"
    assert seen["status"]["error"] is not None


def test_video_7_64_status_of_an_unknown_video_is_404(tmp_path):
    """video-65 (unknown id): a well-formed id nobody made is 404 for an authenticated caller."""
    seen = {}

    async def drive(rig):
        response = await rig.call("GET", "/v1/videos/video_nosuchjob")
        seen["status"] = response.status_code

    _run(tmp_path, "r7-65", lambda rig: None, drive)
    assert seen["status"] == 404


def test_video_8_70_content_of_an_unfinished_video_is_not_ready(tmp_path):
    """video-70: content of a known, unfinished video is not 200."""
    seen = {}

    def setup(rig):
        _comfy(rig, running_polls=5)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
        seen["status"] = (await rig.call("GET", f"/v1/videos/{created.json()['id']}/content")).status_code

    _run(tmp_path, "r8-70", setup, drive)
    assert seen["status"] != 200


def test_video_8_74_content_of_a_finished_video_is_mp4_bytes(tmp_path):
    """video-73, video-74: the finished video's content is served as video/mp4 bytes from the engine's view, and the
    file is indexed as a Library asset on first fetch."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
        video_id = created.json()["id"]
        await _audit_wait(rig, video_id)
        response = await rig.call("GET", f"/v1/videos/{video_id}/content")
        seen["status"], seen["type"], seen["bytes"] = response.status_code, response.headers.get("content-type"), \
            response.content
        seen["assets"] = rig.trace()["assets"]

    _run(tmp_path, "r8-74", setup, drive)
    assert seen["status"] == 200
    assert seen["type"] == "video/mp4"
    assert seen["bytes"][4:8] == b"ftyp"
    assert seen["assets"]


def test_video_8_71_content_is_hidden_from_another_key(tmp_path):
    """video-71: another key gets 404 for the content of a video."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
        video_id = created.json()["id"]
        other = identity(everything(), name="other-app", key_hash="sha256:other", app_id="app_other")
        with authenticated(other):
            seen["status"] = (await rig.call("GET", f"/v1/videos/{video_id}/content")).status_code

    _run(tmp_path, "r8-71", setup, drive)
    assert seen["status"] == 404


def test_video_9_76_bad_list_parameters_are_400(tmp_path):
    """video-76: a bad limit, order or after cursor is each a 400."""
    seen = {}

    async def drive(rig):
        seen["limit"] = (await rig.call("GET", "/v1/videos", params={"limit": "0"})).status_code
        seen["order"] = (await rig.call("GET", "/v1/videos", params={"order": "sideways"})).status_code
        seen["after"] = (await rig.call("GET", "/v1/videos", params={"after": "video_nope"})).status_code

    _run(tmp_path, "r9-76", lambda rig: None, drive)
    assert (seen["limit"], seen["order"], seen["after"]) == (400, 400, 400)


def test_video_9_77_list_page_has_the_entry_and_no_more_rows(tmp_path):
    """video-77, video-80: the list page holds the caller's video with its entry fields and `has_more` false. (The
    newest-first order and `has_more` need several distinct engine prompts; the fake engine answers one id, so those
    two are not reachable here.)"""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        seen["id"] = created.json()["id"]
        seen["page"] = (await rig.call("GET", "/v1/videos", params={"limit": "2"})).json()

    _run(tmp_path, "r9-77", setup, drive)
    listed = seen["page"]["data"]
    assert [entry["id"] for entry in listed] == [seen["id"]]
    assert listed[0]["object"] == "video"
    assert seen["page"]["has_more"] is False


def test_video_9_81_list_is_scoped_to_the_owner(tmp_path):
    """video-81: another key's list does not include a video this key made."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
        video_id = created.json()["id"]
        other = identity(everything(), name="other-app", key_hash="sha256:other", app_id="app_other")
        with authenticated(other):
            listed = (await rig.call("GET", "/v1/videos")).json()
        seen["ids"] = [entry["id"] for entry in listed["data"]]
        seen["video"] = video_id

    _run(tmp_path, "r9-81", setup, drive)
    assert seen["video"] not in seen["ids"]


def test_video_10_90_delete_answers_the_deleted_object(tmp_path):
    """video-90: a successful delete answers 200 `{id, object: video, deleted: true}`."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
        video_id = created.json()["id"]
        response = await rig.call("DELETE", f"/v1/videos/{video_id}")
        seen["status"], seen["body"], seen["id"] = response.status_code, response.json(), video_id

    _run(tmp_path, "r10-90", setup, drive)
    assert seen["status"] == 200
    assert seen["body"] == {"id": seen["id"], "object": "video", "deleted": True}


def test_video_10_83_deleted_video_is_gone_from_status_and_list(tmp_path):
    """video-66, video-81: a deleted video is 404 on status and absent from the list."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
        video_id = created.json()["id"]
        await rig.call("DELETE", f"/v1/videos/{video_id}")
        seen["status"] = (await rig.call("GET", f"/v1/videos/{video_id}")).status_code
        seen["listed"] = [entry["id"] for entry in (await rig.call("GET", "/v1/videos")).json()["data"]]
        seen["video"] = video_id

    _run(tmp_path, "r10-83", setup, drive)
    assert seen["status"] == 404
    assert seen["video"] not in seen["listed"]


def test_video_10_85_deleting_a_queued_video_cancels_it_on_the_engine(tmp_path):
    """video-85: a queued video is cancelled on the engine before it is tombstoned."""
    seen = {}

    def setup(rig):
        _comfy(rig, running_polls=10 ** 6)      # the runner polls on its own: the engine stays busy until it is cancelled

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
        await _await_submit(rig)
        await rig.call("DELETE", f"/v1/videos/{created.json()['id']}")
        seen["engine"] = _engine_paths(rig)

    _run(tmp_path, "r10-85", setup, drive)
    assert any(path.startswith("POST /api/jobs/p1/cancel") or path == "POST /interrupt" for path in seen["engine"])


# --------------------------------------------------------------------------- R4-R6: remix, edit, extend

def test_video_4_39_remix_is_501_while_no_remix_workflow_is_configured(tmp_path):
    """video-39: the shipped config has no remix workflow; the remix answers 501 `video_remix_not_configured`."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
        response = await rig.call("POST", f"/v1/videos/{created.json()['id']}/remix", json_body={"prompt": "again"})
        seen["status"], seen["body"] = response.status_code, response.text

    _run(tmp_path, "r4-39", setup, drive)
    assert seen["status"] == 501
    assert "video_remix_not_configured" in seen["body"]


def test_video_4_45_remix_of_an_unknown_source_is_404(tmp_path):
    """video-45: an unknown source id is 404 (checked before the configuration answer is needed)."""
    seen = {}

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos/video_nosuchjob/remix", json_body={"prompt": "again"})
        seen["status"] = response.status_code

    _run(tmp_path, "r4-45", lambda rig: None, drive)
    assert seen["status"] == 404


def test_video_5_46_edit_is_501_while_no_remix_workflow_is_configured(tmp_path):
    """video-46 (golden s7_ve_success and s7_ve_upstream_failure both record 501): the edit answers 501
    `video_remix_not_configured` on a release config. M8 keeps this as row data."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
        video_id = created.json()["id"]
        response = await rig.call("POST", "/v1/videos/edits",
                                  json_body={"prompt": "make it blue", "video": {"id": video_id}})
        seen["status"], seen["body"] = response.status_code, response.text

    _run(tmp_path, "r5-46", setup, drive)
    assert seen["status"] == 501
    assert "video_remix_not_configured" in seen["body"]


def test_video_5_47_edit_without_a_prompt_or_source_is_400(tmp_path):
    """video-47: an edit with no prompt, or no video.id, is a 400 (golden s7_ve_validation_refusal)."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        seen["no_prompt"] = (await rig.call("POST", "/v1/videos/edits", json_body={"video": {"id": "video_x"}})).status_code
        seen["no_source"] = (await rig.call("POST", "/v1/videos/edits", json_body={"prompt": "blue"})).status_code

    _run(tmp_path, "r5-47", setup, drive)
    assert seen["no_prompt"] == 400
    assert seen["no_source"] == 400


def test_video_6_54_extension_of_another_keys_video_is_refused(tmp_path):
    """video-54, video-41: a source video the caller does not own is not extended; the answer is 404, not a chain error."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
        video_id = created.json()["id"]
        other = identity(everything(), name="other-app", key_hash="sha256:other", app_id="app_other")
        with authenticated(other):
            response = await rig.call("POST", "/v1/videos/extensions", json_body={"prompt": "more", "video": {"id": video_id}})
        seen["status"] = response.status_code

    _run(tmp_path, "r6-54", setup, drive)
    assert seen["status"] == 404


# --------------------------------------------------------------------------- R11: the media-job video intent

def test_video_11_96_accepted_video_media_job_is_202_with_location(tmp_path):
    """video-96: a video.generate media job is accepted with 202, a Location header and a vidjob_ id."""
    seen = {}

    def setup(rig):
        _comfy(rig)
        rig.set(proxy, "_schedule_video_media_job", lambda job_id: rig.scheduled.append(job_id) or True)

    async def drive(rig):
        response = await rig.call("POST", "/gateway/v1/media/jobs", json_body={
            "intent": "video.generate", "request": {"prompt": "waves", "model": "ltx-2.5-t2v"}})
        seen["status"], seen["headers"], seen["body"] = response.status_code, dict(response.headers), response.text

    _run(tmp_path, "r11-96", setup, drive)
    assert seen["status"] == 202, seen["body"]
    assert "location" in seen["headers"]
    assert "vidjob_" in seen["body"]


# --------------------------------------------------------------------------- coverage gaps (c2d-av-golden-coverage-1108)
#
# Items from the A/V golden coverage review. Each pins what the route answers today; a row-path move that changes one
# of these answers needs a manifest entry, not an edit to the expectation.

def _comfy_with_failing_cancel(rig: GatewayRig, *, running_polls: int = 0) -> None:
    """A fake engine whose cancel answers 500; the other exchanges are the video ones."""
    from tests.support.adapter_differential_rig import comfy_exchanges, exchange, history_done
    from tests.media_test_bytes import mp4_bytes
    from tests.test_adapter_differential import _comfy
    exchanges = comfy_exchanges(
        views=[(mp4_bytes(), "video/mp4")], history=history_done("images", "clip.mp4", node="60"),
        running_polls=running_polls, queue=_RUNNING_QUEUE if running_polls else None, strict=False)
    _comfy(rig, [exchange("cancel", "POST", "/api/jobs/p1/cancel", {"status": 500, "json": {"error": "fake"}},
                          repeat=True, required=False) if item["id"] == "cancel" else item for item in exchanges])


def test_video_pin_media_job_polls_the_engine_every_5_seconds(tmp_path):
    """S7 6.8: the video media job keeps its 5 s poll; audio and image keep their own schedules."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def record_sleep(delay, *args, **kwargs):
        delays.append(delay)
        await real_sleep(0)

    seen = {}

    def setup(rig):
        _comfy(rig, running_polls=2)
        rig.set(proxy, "_schedule_video_media_job", lambda job_id: rig.scheduled.append(job_id) or True)

    async def drive(rig):
        # The rig taps asyncio.sleep too; patch through the rig so both undo in order (a test-level monkeypatch
        # would restore the rig's tap after the rig had removed it, leaking it into every later test).
        rig.set(proxy.asyncio, "sleep", record_sleep)
        response = await rig.call("POST", "/gateway/v1/media/jobs", json_body={
            "intent": "video.generate", "request": {"prompt": "waves", "model": "ltx-2.5-t2v"}})
        seen["accepted"] = response.status_code
        await proxy._run_video_media_job(rig.scheduled[0])
        seen["polls"] = [d for d in delays if d != 0]

    _run(tmp_path, "s7-68-poll", setup, drive)
    assert seen["accepted"] == 202
    assert seen["polls"], "the job never polled the engine"
    assert set(seen["polls"]) == {5.0}


def test_video_delete_with_a_failing_cancel_is_409_and_keeps_the_video(tmp_path):
    """S7 6.7: a delete whose cancel fails answers 409 and keeps the video. (The restart half of the old test never
    restarted, and its legacy row belonged to a principal nobody authenticated as; it proved nothing and is gone. A
    real restart test needs the process-restart fixture the lifecycle file lists as missing.)"""
    seen = {}

    def setup(rig):
        _comfy_with_failing_cancel(rig, running_polls=10 ** 6)      # the engine stays busy: the video is still rendering
        # The test clock does not wait: a 10 s cancel wait would outlast the runner's whole poll budget. A short wait keeps
        # the run alive while the delete asks (the product wait is the row's 10 s).
        import dataclasses
        row = proxy._video_row()
        rig.set(proxy, "_video_row", lambda: dataclasses.replace(row, cancel_wait_s=0.01))

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        video_id = created.json()["id"]
        await _await_submit(rig)
        failed = await rig.call("DELETE", f"/v1/videos/{video_id}", label="delete with a failing cancel")
        seen["failed_status"] = failed.status_code
        seen["kept_after_failure"] = (await rig.call("GET", f"/v1/videos/{video_id}")).status_code
        seen["paths"] = _engine_paths(rig)

    _run(tmp_path, "s7-7-cancel-failure", setup, drive)
    assert seen["failed_status"] == 409, seen["failed_status"]
    assert seen["kept_after_failure"] == 200
    assert "POST /api/jobs/p1/cancel" in seen["paths"]


def test_video_source_owner_boundary_answers_as_an_unknown_id(tmp_path):
    """A source another key owns is refused as an unknown id: edit, remix and extension give the same 404 body
    as for an id that does not exist, and no engine call is made for the foreign source."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        source = created.json()["id"]
        before = len(rig.recorder.engine)
        unknown = await rig.call("POST", "/v1/videos/edits", json_body={"prompt": "y", "video": {"id": "video_nope"}})
        seen["unknown"] = (unknown.status_code, unknown.json())
        other = identity(everything(), name="other-app", key_hash="sha256:other", app_id="app_other")
        with authenticated(other):
            edit = await rig.call("POST", "/v1/videos/edits", json_body={"prompt": "y", "video": {"id": source}})
            remix = await rig.call("POST", f"/v1/videos/{source}/remix", json_body={"prompt": "y"})
            extend = await rig.call("POST", "/v1/videos/extensions", json_body={"prompt": "y", "video": {"id": source}})
        seen["edit"], seen["remix"], seen["extend"] = (
            (edit.status_code, edit.json()), (remix.status_code, remix.json()), (extend.status_code, extend.json()))
        seen["engine_calls_for_foreign"] = len(rig.recorder.engine) - before

    _run(tmp_path, "av-owner", setup, drive)
    assert seen["edit"] == seen["unknown"]
    assert seen["remix"] == seen["unknown"]
    assert seen["extend"] == seen["unknown"]
    assert seen["engine_calls_for_foreign"] == 0


@pytest.mark.parametrize(("video", "status", "param"), [
    (None, 400, "video"),            # no video field at all
    ({"id": "img_1"}, 400, None),    # a wire id that is not a video id
])
def test_video_edit_source_refusals_before_the_remix_check(tmp_path, video, status, param):
    """The source is checked before the remix configuration: a missing or non-video source is a 400 even while
    remix is unconfigured."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        body = {"prompt": "y"} if video is None else {"prompt": "y", "video": video}
        response = await rig.call("POST", "/v1/videos/edits", json_body=body)
        seen["status"], seen["error"] = response.status_code, response.json()["error"]

    _run(tmp_path, "av-edit-source", setup, drive)
    assert seen["status"] == status
    if param:
        assert seen["error"].get("param") == param


@pytest.mark.parametrize("empty", [{"id": ""}, {"id": 5}])
def test_video_edit_malformed_source_reaches_the_remix_check_today(tmp_path, empty):
    """Pins an ordering the row path must keep or change deliberately: an empty or non-string ``video.id`` is not
    validated as a source before the remix configuration answers 501."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos/edits", json_body={"prompt": "y", "video": empty})
        seen["status"], seen["code"] = response.status_code, response.json()["error"].get("code")

    _run(tmp_path, "av-edit-malformed", setup, drive)
    assert seen["status"] == 501
    assert seen["code"] == "video_remix_not_configured"


@pytest.mark.parametrize(("body", "status", "message_part"), [
    ({"seconds": "2"}, 200, None),                    # numeric string is coerced
    ({"seconds": 0}, 400, "between 1.0 and 10.0"),
    ({"seconds": -1}, 400, "between 1.0 and 10.0"),
    ({"seconds": "bad"}, 400, "must be a number"),
    ({"prompt": "   "}, 400, "prompt is required"),
    ({"prompt": ["a"]}, 400, "prompt must be a string"),
])
def test_video_create_field_validation_today(tmp_path, body, status, message_part):
    """Each field refusal or coercion the old create route makes today (golden-coverage gap 3)."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": "x", "model": "ltx-2.5-t2v", **body})
        seen["status"], seen["text"] = response.status_code, response.text
        seen["engine"] = len(rig.recorder.engine)

    _run(tmp_path, "av-create-validation", setup, drive)
    assert seen["status"] == status
    if message_part:
        assert message_part in seen["text"]
        assert seen["engine"] == 0, "a refused create reached the engine"


def test_video_create_size_is_clamped_to_the_nearest_supported_size(tmp_path):
    """An unparsable size is either refused 400 `invalid_request_error` (allowed by video-texts.md) or answered 200 with the
    default-clamped size (1024x704, the old behaviour); it is never a 500."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": "x", "model": "ltx-2.5-t2v", "size": "bad"})
        seen["status"], seen["size"] = response.status_code, response.json().get("size")
        seen["error_type"] = (response.json().get("error") or {}).get("type")

    _run(tmp_path, "av-create-size", setup, drive)
    assert seen["status"] in (200, 400)
    if seen["status"] == 200:
        assert seen["size"] == "1024x704"
    else:
        assert seen["error_type"] == "invalid_request_error"


def test_video_create_non_string_model_is_refused_without_an_engine_call(tmp_path):
    """A numeric ``model`` is either refused 400 `invalid_request_error` (allowed by video-texts.md) or, as the old create
    route did, answered by an unhandled 500. Either way no engine call is made and no video is created."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": "x", "model": 17})
        seen["status"], seen["text"] = response.status_code, response.text
        seen["engine"] = len(rig.recorder.engine)

    _run(tmp_path, "av-create-model-int", setup, drive)
    assert seen["status"] in (400, 500)
    if seen["status"] == 400:
        assert '"invalid_request_error"' in seen["text"]
    assert seen["engine"] == 0


def test_video_create_default_and_canonical_model_names(tmp_path):
    """No model resolves to the canonical name; the short name is echoed; an unknown name is 404 model_not_found."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        omitted = await rig.call("POST", "/v1/videos", json_body={"prompt": "x"})
        short = await rig.call("POST", "/v1/videos", json_body={"prompt": "x", "model": "ltx-2.5-t2v"})
        unknown = await rig.call("POST", "/v1/videos", json_body={"prompt": "x", "model": "nope-model"})
        seen["omitted"] = (omitted.status_code, omitted.json()["model"])
        seen["short"] = (short.status_code, short.json()["model"])
        seen["unknown"] = (unknown.status_code, unknown.json()["error"].get("code"), unknown.json()["error"].get("param"))

    _run(tmp_path, "av-create-models", setup, drive)
    assert seen["omitted"] == (200, "video-ltx-2.5-t2v")
    assert seen["short"] == (200, "ltx-2.5-t2v")
    assert seen["unknown"] == (404, "model_not_found", "model")


def test_openai_alias_create_answers_as_the_canonical_route(tmp_path):
    """The /openai/v1 alias of create answers 200 with a video id and the same model echo as the canonical route."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        canonical = await rig.call("POST", "/v1/videos", json_body={"prompt": "x", "model": "ltx-2.5-t2v"})
        alias = await rig.call("POST", "/openai/v1/videos", json_body={"prompt": "x", "model": "ltx-2.5-t2v"})
        seen["canonical"] = (canonical.status_code, canonical.json()["model"], canonical.json()["id"].startswith("video_"))
        seen["alias"] = (alias.status_code, alias.json()["model"], alias.json()["id"].startswith("video_"))

    _run(tmp_path, "av-alias", setup, drive)
    assert seen["alias"] == seen["canonical"]


@pytest.mark.xfail(strict=True, reason="open item P8: the engine is asked after the create has answered, so a refused or id-less prompt fails the "
                   "job; it no longer answers 502 at create and stores no video")
@pytest.mark.parametrize("failure", ["refused", "no_prompt_id"])
def test_video_engine_submit_failures_are_502_and_store_nothing(tmp_path, failure):
    """Engine prompt refusals and a prompt answer without an id: 502, and no video is stored."""
    from tests.support.adapter_differential_rig import comfy_exchanges, exchange, history_done
    from tests.test_adapter_differential import _comfy
    from tests.media_test_bytes import mp4_bytes
    seen = {}

    def setup(rig):
        prompt = (exchange("prompt", "POST", "/prompt", {"status": 500, "json": {"error": "fake"}}, repeat=True, required=False)
                  if failure == "refused" else
                  exchange("prompt", "POST", "/prompt", {"json": {"noid": True}}, repeat=True, required=False))
        exchanges = comfy_exchanges(views=[(mp4_bytes(), "video/mp4")], history=history_done("images", "clip.mp4", node="60"),
                                    strict=False)
        _comfy(rig, [prompt if item["id"] == "prompt" else item for item in exchanges])

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": "x", "model": "ltx-2.5-t2v"})
        seen["status"], seen["message"] = response.status_code, response.json()["error"]["message"]
        seen["listed"] = (await rig.call("GET", "/v1/videos")).json()["data"]

    _run(tmp_path, f"av-submit-{failure}", setup, drive)
    assert seen["status"] == 502
    assert seen["listed"] == []
    if failure == "refused":
        assert seen["message"] == "ComfyUI: The engine refused this request."
    else:
        assert seen["message"] == "No prompt_id returned"


def test_video_completed_status_with_missing_output_still_answers_completed(tmp_path):
    """Pins a mismatch: status says completed (100%) while the engine's /view answers 404, so content answers 502.
    The row path must keep it or change it with a manifest entry, never by editing this expectation silently."""
    from tests.support.adapter_differential_rig import comfy_exchanges, exchange, history_done
    from tests.test_adapter_differential import _comfy
    seen = {}

    def setup(rig):
        exchanges = comfy_exchanges(history=history_done("images", "clip.mp4", node="60"), strict=False)
        exchanges.append(exchange("view", "GET", "/view", {"status": 404, "json": {"error": "gone"}}, repeat=True, required=False))
        _comfy(rig, exchanges)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body={"prompt": "x", "model": "ltx-2.5-t2v"})
        video_id = created.json()["id"]
        status = await rig.call("GET", f"/v1/videos/{video_id}")
        seen["status"] = (status.status_code, status.json()["status"], status.json()["progress"])
        content = await rig.call("GET", f"/v1/videos/{video_id}/content")
        seen["content"] = (content.status_code, content.json())
        seen["engine"] = _engine_paths(rig)

    _run(tmp_path, "av-missing-output", setup, drive)
    assert seen["status"] == (200, "completed", 100)
    assert "GET /view" in seen["engine"], "the engine's /view refusal was never made"
    # The whole row of video-texts.md (content_view_error): status, type and message; no param and no code.
    assert seen["content"] == (502, {"error": {"message": "ComfyUI /view error: 404", "type": "api_error"}})


# --------------------------------------------------------------------------- gap 1: configured edit and remix

REFERENCE_IMAGE = "data:image/png;base64," + base64.b64encode(_png()).decode()
REMIX_EXTENSIONS = {"com.azizam.workflow": {"reference_image": REFERENCE_IMAGE}}
SOURCE_CLIP = long_mp4_bytes("black")
CHILD_CLIP = long_mp4_bytes("white")


def _configured_remix(rig: GatewayRig) -> None:
    """Configure remix the way an owner does: ``videos.remix_workflow`` names a workflow, and the ``video-remix`` model
    resolves to a shipped workflow with a source-video input. The shipped ``video-scail-2-replace`` is that workflow;
    it needs a reference image and at most 4 s, which the remix request supplies."""
    values = dict(proxy.runtime().config.get("videos") or {})
    publish_section(rig.monkeypatch, "videos", {**values, "remix_workflow": "video-remix"})
    rig.set(proxy, "VIDEO_ALIASES", {**proxy.VIDEO_ALIASES, "video-remix": "video-scail-2-replace"})


def _scripted_engine(rig: GatewayRig, steps, *, upload: dict | None = None, node: str = "60", unread=()) -> None:
    """A fake engine that answers each submission in order with its own prompt id, history and download. ``node`` is the
    graph's SaveVideo node: ``60`` for the t2v graph, ``save`` for the long (chain) graph that extensions run.

    ``steps``: ``(prompt_id, clip, outcome)``, outcome ``"done"`` (history succeeds, ``/view`` answers ``clip``) or
    ``"failed"`` (history fails; no download is made). ``unread`` names steps whose output the test never downloads.
    Housekeeping and uploads come from the standard script.
    """
    from tests.support.adapter_differential_rig import comfy_exchanges, exchange, history_done, hex_body
    from tests.test_adapter_differential import _comfy
    from tests.test_adapter_differential_s7 import FAILED_HISTORY
    base = [item for item in comfy_exchanges(strict=False) if item["id"] not in {"prompt", "history", "view"}]
    if upload is not None:
        base = [exchange("upload", "POST", "/upload/image", upload, repeat=True, required=False)
                if item["id"] == "upload" else item for item in base]
    ordered = []
    for index, (prompt_id, clip, outcome) in enumerate(steps):
        # The first step is the source, a t2v graph; the rest are the graph the test runs (``node``).
        step_node = "60" if index == 0 else node
        ordered.append(exchange(f"prompt-{prompt_id}", "POST", "/prompt", {"json": {"prompt_id": prompt_id}}))
        history = (history_done("images", f"{prompt_id}.mp4", node=step_node, prompt_id=prompt_id) if outcome == "done"
                   else {prompt_id: FAILED_HISTORY["p1"]})
        ordered.append(exchange(f"history-{prompt_id}", "GET", f"/history/{prompt_id}", {"json": history},
                                repeat=True, required=False))
        if outcome == "done":
            # Matched by file name, and repeatable: a content read downloads again from the engine.
            ordered.append(exchange(f"view-{prompt_id}", "GET", "/view", hex_body(clip, "video/mp4"),
                                    query={"filename": f"{prompt_id}.mp4", "type": "output"}, repeat=True,
                                    required=prompt_id not in unread))
    _comfy(rig, base + ordered)


@pytest.mark.parametrize("route", ["edit", "remix"])
def test_video_configured_edit_and_remix_make_a_distinct_child_and_keep_the_source(tmp_path, route):
    """Gap 1 (success): with remix configured, an edit or remix of an owned source submits a real child (its own id),
    the child's bytes are the engine's second output, and the source's bytes and lineage are unchanged."""
    seen = {}

    def setup(rig):
        _configured_remix(rig)
        _scripted_engine(rig, [("p1", SOURCE_CLIP, "done"), ("p2", CHILD_CLIP, "done")])

    async def drive(rig):
        source = (await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})).json()["id"]
        body = {"prompt": "make it dusk", "seconds": 4, "extensions": REMIX_EXTENSIONS}
        if route == "edit":
            answer = await rig.call("POST", "/v1/videos/edits", json_body={**body, "video": {"id": source}})
        else:
            answer = await rig.call("POST", f"/v1/videos/{source}/remix", json_body=body)
        seen["answer"] = (answer.status_code, answer.json())
        child = answer.json()["id"]
        seen["child_status"] = (await rig.call("GET", f"/v1/videos/{child}")).json()["status"]
        seen["child_bytes"] = (await rig.call("GET", f"/v1/videos/{child}/content")).content
        seen["source_bytes"] = (await rig.call("GET", f"/v1/videos/{source}/content")).content
        seen["source_id"] = source

    _run(tmp_path, f"av-configured-{route}", setup, drive)
    status, answer = seen["answer"]
    assert status == 200, answer
    assert answer["id"] != seen["source_id"]
    assert answer["remixed_from_video_id"] == seen["source_id"]
    assert seen["child_status"] == "completed"
    # The delivered bytes are MP4s written by the gateway (the engine's clip, re-muxed; provenance is gap 9), and the
    # child's are not the source's.
    assert seen["child_bytes"][4:8] == b"ftyp" and seen["source_bytes"][4:8] == b"ftyp"
    assert seen["child_bytes"] != seen["source_bytes"]


@pytest.mark.parametrize("route", ["edit", "remix"])
def test_video_configured_edit_and_remix_child_failure_is_a_failed_child_not_a_source(tmp_path, route):
    """Gap 1 (failure): the child's engine history fails. The child is failed, has no content, and the source is still
    playable. A failed source generation would not satisfy this test: the source completed first."""
    seen = {}

    def setup(rig):
        _configured_remix(rig)
        _scripted_engine(rig, [("p1", SOURCE_CLIP, "done"), ("p2", None, "failed")])

    async def drive(rig):
        source = (await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})).json()["id"]
        body = {"prompt": "make it dusk", "seconds": 4, "extensions": REMIX_EXTENSIONS}
        if route == "edit":
            answer = await rig.call("POST", "/v1/videos/edits", json_body={**body, "video": {"id": source}})
        else:
            answer = await rig.call("POST", f"/v1/videos/{source}/remix", json_body=body)
        seen["answer"] = answer.status_code
        child = answer.json().get("id")
        child_status = await rig.call("GET", f"/v1/videos/{child}")
        seen["child"] = (child_status.status_code, child_status.json().get("status"),
                         bool(child_status.json().get("error")))
        seen["child_content"] = (await rig.call("GET", f"/v1/videos/{child}/content")).status_code
        seen["source_content"] = (await rig.call("GET", f"/v1/videos/{source}/content")).content

    _run(tmp_path, f"av-configured-fail-{route}", setup, drive)
    assert seen["answer"] == 200
    assert seen["child"] == (200, "failed", True)
    assert seen["child_content"] != 200
    assert seen["source_content"][4:8] == b"ftyp"


# --------------------------------------------------------------------------- gap 4: extension completion

SOURCE_FOR_EXTENSION = long_mp4_bytes("black")
FIRST_EXTENSION = long_mp4_bytes("white")
SECOND_EXTENSION = long_mp4_bytes("red")
THIRD_EXTENSION = long_mp4_bytes("green")


def test_video_extend_complete_chain_keeps_lineage_and_the_source(tmp_path):
    """Gap 4: source p1 -> extension p2 (7 s, one segment) -> an extension of p2 asked before p2's output is read (p3) ->
    an extension of p2 after its output is read (p4). Every extension answers 200 with lineage to p2 and completes; the
    source's bytes are the same at the end. Each submission has its own scripted engine step."""
    seen = {}

    def setup(rig):
        _scripted_engine(rig, [("p1", SOURCE_FOR_EXTENSION, "done"), ("p2", FIRST_EXTENSION, "done"),
                               ("p3", SECOND_EXTENSION, "done"), ("p4", THIRD_EXTENSION, "done")],
                         node="save", unread=("p3",))

    async def drive(rig):
        source = (await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})).json()["id"]
        before = (await rig.call("GET", f"/v1/videos/{source}/content")).content
        first = await rig.call("POST", "/v1/videos/extensions",
                               json_body={"video": {"id": source}, "prompt": "more waves", "seconds": "2"})
        first_id = first.json()["id"]
        seen["first"] = (first.status_code, first.json().get("extended_from_video_id"), first.json().get("total_seconds"),
                         first.json().get("segments"))
        seen["first_status"] = (await rig.call("GET", f"/v1/videos/{first_id}")).json().get("status")
        posts_before = _engine_paths(rig).count("POST /prompt")
        early = await rig.call("POST", "/v1/videos/extensions",
                               json_body={"video": {"id": first_id}, "prompt": "before reading", "seconds": "2"})
        early_id = early.json().get("id")
        seen["early"] = (early.status_code, early.json().get("extended_from_video_id"))
        seen["early_submissions"] = _engine_paths(rig).count("POST /prompt") - posts_before
        first_content = (await rig.call("GET", f"/v1/videos/{first_id}/content")).content
        seen["first_content_is_mp4"] = first_content[4:8] == b"ftyp"
        second = await rig.call("POST", "/v1/videos/extensions",
                                json_body={"video": {"id": first_id}, "prompt": "even more", "seconds": "2"})
        second_id = second.json().get("id")
        seen["second"] = (second.status_code, second.json().get("extended_from_video_id"))
        seen["second_status"] = (await rig.call("GET", f"/v1/videos/{second_id}")).json().get("status")
        second_content = (await rig.call("GET", f"/v1/videos/{second_id}/content")).content
        seen["second_content_is_mp4"] = second_content[4:8] == b"ftyp"
        seen["ids"] = (source, first_id, early_id, second_id)
        seen["source_after"] = (await rig.call("GET", f"/v1/videos/{source}/content")).content
        seen["before"] = before

    _run(tmp_path, "av-extend-chain", setup, drive)
    source, first_id, early_id, second_id = seen["ids"]
    assert seen["first"] == (200, source, 7.0, 1)
    assert seen["first_status"] == "completed"
    assert seen["early"] == (200, first_id)
    assert seen["early_submissions"] == 1
    assert seen["first_content_is_mp4"]
    assert seen["second"] == (200, first_id)
    assert seen["second_status"] == "completed"
    assert seen["second_content_is_mp4"]
    assert len({source, first_id, early_id, second_id}) == 4
    assert seen["source_after"] == seen["before"]


def test_video_extend_prepare_failure_submits_no_child(tmp_path):
    """Gap 4: the source is fetched and staged for the engine, and the engine refuses the upload. No child is submitted
    and no child is stored."""
    seen = {}

    def setup(rig):
        _scripted_engine(rig, [("p1", SOURCE_FOR_EXTENSION, "done")],
                         upload={"status": 500, "json": {"error": "fake upload refused"}})

    async def drive(rig):
        source = (await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})).json()["id"]
        before = len([p for p in _engine_paths(rig) if p == "POST /prompt"])
        answer = await rig.call("POST", "/v1/videos/extensions",
                                json_body={"video": {"id": source}, "prompt": "more", "seconds": "2"})
        # Defect 4 (pinned): an engine upload refusal surfaces as a bare 500 text, not the JSON error envelope.
        seen["answer"] = (answer.status_code, answer.text)
        seen["child_prompts"] = len([p for p in _engine_paths(rig) if p == "POST /prompt"]) - before
        seen["listed"] = [item["id"] for item in (await rig.call("GET", "/v1/videos")).json()["data"]]
        seen["source_content"] = (await rig.call("GET", f"/v1/videos/{source}/content")).status_code

    _run(tmp_path, "av-extend-prepare", setup, drive)
    assert seen["answer"] == (500, "Internal Server Error")
    assert seen["child_prompts"] == 0
    assert len(seen["listed"]) == 1
    assert seen["source_content"] == 200


def test_video_extend_child_failure_keeps_the_source_playable(tmp_path):
    """Gap 4: the extension's engine history fails. The child is failed with an error and no content, and the source
    still plays."""
    seen = {}

    def setup(rig):
        _scripted_engine(rig, [("p1", SOURCE_FOR_EXTENSION, "done"), ("p2", None, "failed")], node="save")

    async def drive(rig):
        source = (await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})).json()["id"]
        answer = await rig.call("POST", "/v1/videos/extensions",
                                json_body={"video": {"id": source}, "prompt": "more", "seconds": "2"})
        child = answer.json().get("id")
        status = await rig.call("GET", f"/v1/videos/{child}")
        seen["child"] = (answer.status_code, status.json().get("status"), bool(status.json().get("error")))
        seen["child_content"] = (await rig.call("GET", f"/v1/videos/{child}/content")).status_code
        seen["source_content"] = (await rig.call("GET", f"/v1/videos/{source}/content")).content[4:8]

    _run(tmp_path, "av-extend-child-fail", setup, drive)
    assert seen["child"] == (200, "failed", True)
    assert seen["child_content"] != 200
    assert seen["source_content"] == b"ftyp"


def _route_shape(answer):
    """What a client can compare across the canonical and the /openai/v1 alias: the status, and for an extension the
    lineage and length fields (ids and seeds differ per submission, so they are not compared)."""
    if answer.status_code != 200:
        return (answer.status_code, answer.text)
    body = answer.json()
    return (200, body.get("object"), body.get("extended_from_video_id"), body.get("total_seconds"), body.get("segments"),
            body.get("azizam_resolved_model"), body.get("status"))


@pytest.mark.parametrize("route", ["edit", "remix", "extend"])
def test_video_canonical_and_alias_routes_answer_alike(tmp_path, route):
    """Gap 10: each video route answers the same through its canonical path and its /openai/v1 alias. Edit and remix are
    unconfigured here (501 both ways); extend completes (200 both ways) from the same source."""
    seen = {}
    paths = {
        "edit": ("/v1/videos/edits", "/openai/v1/videos/edits"),
        "remix": ("/v1/videos/{source}/remix", "/openai/v1/videos/{source}/remix"),
        "extend": ("/v1/videos/extensions", "/openai/v1/videos/extensions"),
    }
    bodies = {
        "edit": lambda source: {"prompt": "make it blue", "video": {"id": source}},
        "remix": lambda source: {"prompt": "again"},
        "extend": lambda source: {"video": {"id": source}, "prompt": "more", "seconds": "2"},
    }

    def setup(rig):
        if route == "extend":
            _scripted_engine(rig, [("p1", SOURCE_FOR_EXTENSION, "done"), ("p2", FIRST_EXTENSION, "done"),
                                   ("p3", SECOND_EXTENSION, "done")], node="save", unread=("p2", "p3"))
        else:
            _comfy(rig)

    async def drive(rig):
        source = (await rig.call("POST", "/v1/videos", json_body=_created())).json()["id"]
        canonical, alias = (path.format(source=source) for path in paths[route])
        seen["canonical"] = _route_shape(await rig.call("POST", canonical, json_body=bodies[route](source)))
        seen["alias"] = _route_shape(await rig.call("POST", alias, json_body=bodies[route](source)))

    _run(tmp_path, f"av-parity-{route}", setup, drive)
    assert seen["canonical"] == seen["alias"]
    if route == "extend":
        assert seen["canonical"][0] == 200
        assert seen["canonical"][2] is not None  # extended_from_video_id: the lineage answer on both paths
    else:
        assert seen["canonical"][0] == 501
        assert "video_remix_not_configured" in seen["canonical"][1]


@pytest.mark.parametrize("plan", ["full", "none"])
def test_video_provenance_embeds_the_prompt_only_when_the_plan_allows(tmp_path, plan):
    """Gap 9 (provenance output): full metadata needs the maker, a kept prompt and no ``never_keep``. With all three, the
    delivered MP4 carries the generation's prompt. With media metadata off, the bytes carry no prompt text."""
    import json
    import re
    from gateway import admin_store
    seen = {}

    def setup(rig):
        _comfy(rig)
        if plan == "full":
            admin_db = rig.tmp / "admin.db"
            admin_store.init_db(admin_db)
            person = admin_store.upsert_oidc_user("issuer", "provenance-maker", path=admin_db)
            admin_store.put_operator_setting(
                "prompt_memory", {"default_remember": True, "never_keep": False, "retention_cap": "3_months"}, path=admin_db)
            rig.set(proxy.ADMIN_STORE, "path", admin_db)
            rig._exit.enter_context(authenticated(identity(
                everything(media_metadata="full", receipt_detail="full"), person_id=person["id"])))

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body={"prompt": "make it dusk", "model": "ltx-2.5-t2v"})
        seen["video_id"] = created.json()["id"]
        seen["status"] = (await _audit_wait(rig, seen["video_id"]))["status"]
        content = await rig.call("GET", f"/v1/videos/{seen['video_id']}/content")
        seen["content"] = (content.status_code, content.content, content.text[:200])

    _run(tmp_path, f"av-provenance-{plan}", setup, drive)
    code, body, text = seen["content"]
    video_id = seen["video_id"]
    assert seen["status"] == "completed"
    assert code == 200, text
    assert body[4:8] == b"ftyp"
    if plan == "full":
        record = re.search(rb"\{[^\x00]*azizam\.provenance[^\x00]*\}", body)
        assert record is not None
        provenance = json.loads(record.group(0))
        assert provenance["schema"] == "azizam.provenance/1"
        assert provenance["kind"] == "video"
        assert provenance["request_id"] == provenance["receipt"].rsplit("/", 1)[1] == video_id
        # The provenance record is inside the delivered file, so the move keeps its values. Design 1.2 would change
        # resolved_model and the workflow id to the manifest id; reported as a wire change no section 5.4 kind covers.
        assert provenance["requested_model"] == "ltx-2.5-t2v"
        assert provenance["resolved_model"] == "video-ltx-2.5-t2v"
        assert provenance["workflow_id"] == "video.generate.video-ltx-2.5-t2v"
        assert provenance["parameters"]["size"] == "960x512"
        assert isinstance(provenance["seed"], int)
        assert b"make it dusk" not in body  # the prompt is kept for the maker, not written into the file
    else:
        assert b"azizam.provenance" not in body
        assert b"make it dusk" not in body


# ---- V3b contract tests from the video parity oracle audit ----


def _audit_record(rig, video_id):
    """What the old record held that a test compares: the Library asset of the video. A job-backed video is read through
    the public job service. An extension is still an old record; its row is the only place its asset id lives."""
    import json
    if JOB_ID.match(video_id):
        view = _job_view(video_id)
        results = tuple(view.results or ())
        if view.status == "succeeded":
            assert len(results) == 1, f"a finished video has exactly one result, not {len(results)}: {results}"
            return {"asset_id": results[0].asset_id}
        assert results == (), f"a {view.status} job-backed video has no results, not {results}"
        return {"asset_id": None}
    with sqlite3.connect(rig.media_jobs_db) as db:
        row = db.execute("SELECT record_json FROM video_job_records WHERE prompt_id = ?",
                         (video_id.removeprefix("video_"),)).fetchone()
    assert row is not None, video_id
    return json.loads(row[0])


async def _audit_wait(rig, vid, *, timeout_s=10.0):
    """Poll the public status route until the video is finished or failed; return the body."""
    import time
    deadline = time.monotonic() + timeout_s
    while True:
        response = await asyncio.wait_for(rig.call("GET", f"/v1/videos/{vid}"), timeout=20)
        assert response.status_code == 200, response.text
        if response.json()["status"] in {"completed", "failed"}:
            return response.json()
        assert time.monotonic() < deadline, f"{vid} did not finish: {response.json()['status']}"
        await asyncio.sleep(0)


@lru_cache(maxsize=None)
def _audit_frames(clip):
    with tempfile.TemporaryDirectory() as directory:
        source, output = Path(directory) / "in.mp4", Path(directory) / "out.rgb"
        source.write_bytes(clip)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(source), "-map", "0:v:0", "-f", "rawvideo",
                        "-pix_fmt", "rgb24", str(output)], capture_output=True, check=True, timeout=30)
        frames = output.read_bytes()
    assert frames, "No decoded fixture frames"
    return frames


@lru_cache(maxsize=None)
def _audit_clip(color, seconds):
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "clip.mp4"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c={color}:s=16x16:d={seconds}:r=24", "-c:v", "mpeg4", str(output)],
                       capture_output=True, check=True, timeout=30)
        return output.read_bytes()


async def _audit_call(rig, method, path, **kwargs):
    return await asyncio.wait_for(rig.call(method, path, **kwargs), timeout=20)


async def _audit_source(rig):
    response = await _audit_call(rig, "POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
    assert response.status_code == 200, response.text
    assert JOB_ID.match(response.json()["id"]), response.json()["id"]
    return response.json()["id"]


async def _audit_content(rig, vid):
    body = await _audit_wait(rig, vid)
    assert (body["id"], body["status"], body["azizam_status"]) == (vid, "completed", "succeeded")
    content = await _audit_call(rig, "GET", f"/v1/videos/{vid}/content")
    assert content.status_code == 200, content.text[:200]
    assert content.headers["content-type"] == "video/mp4"
    assert content.content[4:8] == b"ftyp"
    return content.content


async def _audit_child(rig, route, source, prompt="make it dusk", **kwargs):
    if route == "extend":
        return await _audit_call(rig, "POST", "/v1/videos/extensions",
                                 json_body={"video": {"id": source}, "prompt": prompt, "seconds": "2"}, **kwargs)
    body = {"prompt": prompt, "seconds": 4, "extensions": REMIX_EXTENSIONS}
    if route == "edit":
        return await _audit_call(rig, "POST", "/v1/videos/edits", json_body={**body, "video": {"id": source}}, **kwargs)
    assert route == "remix"
    return await _audit_call(rig, "POST", f"/v1/videos/{source}/remix", json_body=body, **kwargs)


def _audit_asset(rig, vid, delivered):
    import json
    record = _audit_record(rig, vid)
    aid = record["asset_id"]
    assert aid.startswith("vid_")
    sidecars = [json.loads(p.read_text()) for p in rig.cache.glob("*.meta.json")]
    assert len([s for s in sidecars if s.get("asset_id") == aid]) == 1
    assert delivered in [p.read_bytes() for p in rig.cache.glob("*.mp4")]
    if JOB_ID.match(vid):
        view = _job_view(vid)       # the job is the delivery row, and the only one for this video
        mine = [job for job in video_job_rows(rig) if aid in [a.get("asset_id") for a in job.get("assets") or ()]]
        assert len(mine) == 1 and mine[0]["status"] == "succeeded", mine
        assert view.status == "succeeded" and [(r.media_type, r.asset_id) for r in view.results] == [("video/mp4", aid)]
        return aid
    with sqlite3.connect(rig.media_jobs_db) as db:
        raw = db.execute("SELECT intent, job_json FROM media_jobs WHERE request_id = ?", (vid,)).fetchall()
    jobs = [json.loads(value) for intent, value in raw if intent == "video.generate"]
    assert len(jobs) == 1 and jobs[0]["status"] == "succeeded"
    assert any(a.get("asset_id") == aid and a.get("kind") == "video" for a in jobs[0]["assets"])
    return aid


FOREIGN = identity(everything(), name="foreign-video-maker", key_hash="sha256:foreign", app_id="app_foreign")
_LINEAGE = ("remixed_from_video_id", "extended_from_video_id")


async def _public_snapshot(rig, vid):
    """What the maker's public surface says about a video: its status word, lineage fields and (for a job) request
    facts. Compared before and after a child, so a child that rewrites the source's lineage shows up."""
    response = await _audit_call(rig, "GET", f"/v1/videos/{vid}")
    assert response.status_code == 200, response.text
    body = response.json()
    snapshot = {"status": body["status"], "lineage": {key: body.get(key) for key in _LINEAGE}}
    if JOB_ID.match(vid):
        facts = proxy._workflow_job_service().request_facts(vid.removeprefix("video_"), proxy.AuthContext(identity=RIG_OWNER))
        assert facts is not None
        snapshot["facts"] = json.dumps(dict(facts), sort_keys=True, default=str)
    return snapshot


async def _assert_foreign_caller_sees_nothing(rig, vid):
    """Design 5.1: a caller who is not the maker gets 404 on a video's status and its content."""
    with authenticated(FOREIGN):
        for suffix in ("", "/content"):
            response = await _audit_call(rig, "GET", f"/v1/videos/{vid}{suffix}")
            assert response.status_code == 404, (vid, suffix, response.status_code)


@pytest.mark.parametrize("route", ["edit", "remix", "extend"])
@pytest.mark.parametrize("outcome", ["done", "failed"])
def test_video_child_preserves_exact_source_and_owns_only_its_result(tmp_path, route, outcome):
    """video-23/43/44/46/55: a child is its own video with its own clip and asset; the source clip, record and asset
    do not change, whether the child finishes or fails."""
    source_clip = _audit_clip("black", 5)
    child_clip = _audit_clip("white", 7 if route == "extend" else 4)

    def setup(rig):
        if route != "extend":
            _configured_remix(rig)
        _scripted_engine(rig, [("p1", source_clip, "done"), ("p2", child_clip if outcome == "done" else None, outcome)],
                         node="save" if route == "extend" else "60")

    async def drive(rig):
        source = await _audit_source(rig)
        before = await _audit_content(rig, source)
        assert _audit_frames(before) == _audit_frames(source_clip)
        old_record = _audit_record(rig, source)
        old_public = await _public_snapshot(rig, source)
        await _assert_foreign_caller_sees_nothing(rig, source)
        files_before = {p.name: p.read_bytes() for p in rig.cache.glob("*.mp4")}
        response = await _audit_child(rig, route, source)
        assert response.status_code == 200, response.text
        body = response.json()
        child_id = body["id"]
        # An edit, remix or extension is an old record (`video_<prompt id>`): only the create route is on the run core.
        assert child_id == "video_p2"
        assert (body["object"], body["status"], body["progress"]) == ("video", "queued", 0)
        lineage = "extended_from_video_id" if route == "extend" else "remixed_from_video_id"
        assert body[lineage] == source
        if outcome == "done":
            child = await _audit_content(rig, child_id)
            assert _audit_frames(child) == _audit_frames(child_clip)
            assert _audit_frames(child) != _audit_frames(before)
            assert _audit_asset(rig, child_id, child) != _audit_asset(rig, source, before)
        else:
            failure = await _audit_wait(rig, child_id)
            assert (failure["id"], failure["status"], failure["azizam_status"]) == (child_id, "failed", "failed")
            assert failure["error"]["code"] == "video_generation_failed"
            assert failure["error"]["message"]
            content = await _audit_call(rig, "GET", f"/v1/videos/{child_id}/content")
            assert content.status_code >= 400
            assert isinstance(content.json().get("error"), dict)
            if JOB_ID.match(child_id):
                assert tuple(_job_view(child_id).results or ()) == (), "a failed job-backed child has no results"
            assert not _audit_record(rig, child_id).get("asset_id")
            assert {p.name: p.read_bytes() for p in rig.cache.glob("*.mp4")} == files_before
        assert await _audit_content(rig, source) == before
        new_record = _audit_record(rig, source)
        assert new_record.get("asset_id") == old_record.get("asset_id")
        assert await _public_snapshot(rig, source) == old_public
        # Neither the source nor the child (done or failed) is visible to a caller who did not make it.
        await _assert_foreign_caller_sees_nothing(rig, source)
        await _assert_foreign_caller_sees_nothing(rig, child_id)
        assert _engine_paths(rig).count("POST /prompt") == 2

    _run(tmp_path, f"audit-child-{route}-{outcome}", setup, drive)


def test_video_all_extension_children_complete_with_consistent_real_durations(tmp_path):
    """video-32/35/53/56/58: an extension of an extension keeps its source, whether or not the first child was read first.
    The source is a job; each extension is an old record named `video_<prompt id>` (chains stay on the old handler)."""
    clips = {"p1": _audit_clip("black", 5), "p2": _audit_clip("white", 7),
             "p3": _audit_clip("red", 9), "p4": _audit_clip("green", 9)}

    def setup(rig):
        _scripted_engine(rig, [(pid, clip, "done") for pid, clip in clips.items()], node="save")

    async def drive(rig):
        source = await _audit_source(rig)
        source_before = await _audit_content(rig, source)
        first = await _audit_child(rig, "extend", source, "more waves")
        assert first.status_code == 200
        first_body = first.json()
        assert (first_body["id"], first_body["extended_from_video_id"], first_body["total_seconds"],
                first_body["segments"]) == ("video_p2", source, 7.0, 1)
        ready = await _audit_call(rig, "GET", "/v1/videos/video_p2")
        assert ready.status_code == 200 and ready.json()["status"] == "completed"
        early = await _audit_child(rig, "extend", "video_p2", "before reading")
        assert early.status_code == 200
        first_bytes = await _audit_content(rig, "video_p2")
        late = await _audit_child(rig, "extend", "video_p2", "after reading")
        assert late.status_code == 200
        for response, expected in ((early, "video_p3"), (late, "video_p4")):
            body = response.json()
            assert (body["id"], body["extended_from_video_id"], body["total_seconds"], body["segments"]) == (
                expected, "video_p2", 9.0, 1)
        delivered = {source: ("p1", source_before), "video_p2": ("p2", first_bytes),
                     "video_p3": ("p3", await _audit_content(rig, "video_p3")),
                     "video_p4": ("p4", await _audit_content(rig, "video_p4"))}
        for vid, (pid, content) in delivered.items():
            assert _audit_frames(content) == _audit_frames(clips[pid])
        assert await _audit_content(rig, source) == source_before
        assert len({_audit_asset(rig, vid, data) for vid, (_, data) in delivered.items()}) == 4
        assert _engine_paths(rig).count("POST /prompt") == 4

    _run(tmp_path, "audit-every-extension-child", setup, drive)


@pytest.mark.parametrize("route", ["edit", "remix", "extend"])
def test_video_source_ownership_refusal_has_an_independent_404_control(tmp_path, route):
    """video-41/54: a foreign source answers exactly as an unknown source does, and the unknown source is itself a 404."""
    def setup(rig):
        _configured_remix(rig)
        _scripted_engine(rig, [("p1", SOURCE_CLIP, "done")], unread=("p1",))

    async def drive(rig):
        source = await _audit_source(rig)
        own = await _audit_call(rig, "GET", f"/v1/videos/{source}")
        assert own.status_code == 200
        before = len(_engine_paths(rig))
        unknown = await _audit_child(rig, route, "video_does_not_exist")
        assert unknown.status_code == 404
        assert isinstance(unknown.json().get("error"), dict)
        other = identity(everything(), name="foreign-video-maker", key_hash="sha256:foreign", app_id="app_foreign")
        with authenticated(other):
            foreign = await _audit_child(rig, route, source)
        assert foreign.status_code == 404
        assert foreign.json() == unknown.json()
        assert len(_engine_paths(rig)) == before
        assert (await _audit_call(rig, "GET", f"/v1/videos/{source}")).status_code == 200

    _run(tmp_path, f"audit-source-boundary-{route}", setup, drive)
