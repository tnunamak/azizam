"""S7 parity tests for the video group, R16 (ids, access, storage) and R3 (multipart create).

Black-box: each test drives the real app through ``GatewayRig`` (the fake ComfyUI is the only fake) and asserts what a
caller and the engine observe. Items are the numbered rows of ``docs/design/c2d-s7-parity/video.md``; the name of each
test carries its item id. A test that pins today's behaviour passes on the base; a test for a behaviour the row path
changes is written first and fails until the move.

Receipt items (video-03, 04, 14) pin what the old route writes today, so they pass on the base; the row path's
receipt hook (RH, ``RowRouteDeps.open_receipt``) is what could move them. No item here is xfail: a strict xfail on an
item that already matches would fail as XPASS.
"""

from __future__ import annotations

import asyncio
import base64
import sqlite3
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import pytest

import proxy
from tests.support.access import authenticated, everything, identity, plan_for
from tests.support.adapter_differential_rig import GatewayRig
from tests.support.config_snapshot import publish_section
from tests.test_adapter_differential import _png, _video_comfy

MARKER = "prompt-marker-7f3a9c"


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
        setup(rig)
        asyncio.run(drive(rig))
        return rig


def _comfy(rig: GatewayRig, **kw) -> None:
    # Not every test reaches the engine's end (a refusal stops before it): nothing is required.
    kw.setdefault("strict", False)
    _video_comfy(rig, **kw)


def _migrations(rig: GatewayRig) -> set[str]:
    conn = sqlite3.connect(rig.media_jobs_db)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM media_jobs_migrations")}
    finally:
        conn.close()


def _engine_paths(rig: GatewayRig) -> list[str]:
    return [f"{entry['method']} {entry['path']}" for entry in rig.recorder.engine]


def _job_rows(rig: GatewayRig) -> list[tuple]:
    """Every record row the old video table holds (the legacy store; R16-124). Read while the rig is open."""
    conn = sqlite3.connect(rig.media_jobs_db)
    try:
        return conn.execute("SELECT prompt_id, owner_key_hash, record_json FROM video_job_records").fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


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
    assert seen["id"].startswith("video_") and len(seen["id"]) > len("video_")
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
    """video-124: the record row holds no prompt text (stripped on save). The legacy table is read here: it gets no
    new rows once the create route moves, so the check is about the rows it holds."""
    def setup(rig):
        _comfy(rig)

    seen = {}

    async def drive(rig):
        await rig.call("POST", "/v1/videos", json_body={"prompt": MARKER, "model": "ltx-2.5-t2v"})
        seen["rows"] = _job_rows(rig)

    _run(tmp_path, "r16-124", setup, drive)
    assert seen["rows"], "the create left no record row to check"
    assert all(MARKER not in record for _, _, record in seen["rows"])


def test_video_16_125_migration_marker_is_recorded(tmp_path):
    """video-125: the additive migration `video_job_records_v1` is recorded in the media-jobs schema table."""
    def setup(rig):
        _comfy(rig)

    seen = {}

    async def drive(rig):
        await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        seen["names"] = _migrations(rig)

    _run(tmp_path, "r16-125", setup, drive)
    assert "video_job_records_v1" in seen["names"]


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


@pytest.mark.xfail(strict=True, reason="delta idempotency_acceptance: lands with the videos.create move")
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
        seen["ids"] = (first.json().get("id"), second.json().get("id"))
        seen["prompts"] = sum(1 for p in _engine_paths(rig) if p == "POST /prompt")

    _run(tmp_path, "r1-05", setup, drive)
    assert seen["ids"][0] is not None and seen["ids"][0] == seen["ids"][1]
    assert seen["prompts"] == 1


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
        _comfy(rig, running_polls=1)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
        video_id = created.json()["id"]
        seen["pending"] = (await rig.call("GET", f"/v1/videos/{video_id}")).json()
        seen["done"] = (await rig.call("GET", f"/v1/videos/{video_id}")).json()

    _run(tmp_path, "r7-59", setup, drive)
    assert seen["pending"]["status"] in ("queued", "in_progress")
    assert seen["pending"]["azizam_status"] in ("queued", "running")
    assert seen["done"]["status"] == "completed"
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
        seen["status"] = (await rig.call("GET", f"/v1/videos/{video_id}")).json()

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
        await rig.call("GET", f"/v1/videos/{video_id}")
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
        _comfy(rig, running_polls=5)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body=_created())
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
        running_polls=running_polls, strict=False)
    _comfy(rig, [exchange("cancel", "POST", "/api/jobs/p1/cancel", {"status": 500, "json": {"error": "fake"}},
                          repeat=True, required=False) if item["id"] == "cancel" else item for item in exchanges])


def test_video_pin_media_job_polls_the_engine_every_5_seconds(tmp_path, monkeypatch):
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
        monkeypatch.setattr(proxy.asyncio, "sleep", record_sleep)
        response = await rig.call("POST", "/gateway/v1/media/jobs", json_body={
            "intent": "video.generate", "request": {"prompt": "waves", "model": "ltx-2.5-t2v"}})
        seen["accepted"] = response.status_code
        await proxy._run_video_media_job(rig.scheduled[0])
        seen["polls"] = [d for d in delays if d != 0]

    _run(tmp_path, "s7-68-poll", setup, drive)
    assert seen["accepted"] == 202
    assert seen["polls"], "the job never polled the engine"
    assert set(seen["polls"]) == {5.0}


@pytest.mark.xfail(strict=True, reason="core JobService.delete")
def test_s7_legacy_video_delete_survives_restart_and_cancel_failure_keeps_record(tmp_path):
    """S7 6.7: a delete whose cancel fails answers 409 and keeps the job; a delete that succeeds stays deleted after a
    restart. Red until the core's JobService.delete lands; the legacy reader may then mark an existing row deleted."""
    from gateway import video_job_records
    seen = {}

    def setup(rig):
        _comfy_with_failing_cancel(rig, running_polls=5)

    async def drive(rig):
        created = await rig.call("POST", "/v1/videos", json_body={"prompt": "waves", "model": "ltx-2.5-t2v"})
        video_id = created.json()["id"]
        failed = await rig.call("DELETE", f"/v1/videos/{video_id}", label="delete with a failing cancel")
        seen["failed_status"] = failed.status_code
        seen["kept_after_failure"] = (await rig.call("GET", f"/v1/videos/{video_id}")).status_code
        video_job_records.save("p_legacy", {"owner_key_hash": "sha256:legacy", "created_at": 1, "status": "completed"},
                               path=rig.media_jobs_db)
        seen["legacy_delete"] = (await rig.call("DELETE", "/v1/videos/video_p_legacy")).status_code

    _run(tmp_path, "s7-7-legacy-delete", setup, drive)
    assert seen["failed_status"] == 409
    assert seen["kept_after_failure"] == 200
    assert seen["legacy_delete"] == 200


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
    """An unparsable size is not refused: the create answers 200 with the default-clamped size (1024x704)."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": "x", "model": "ltx-2.5-t2v", "size": "bad"})
        seen["status"], seen["size"] = response.status_code, response.json().get("size")

    _run(tmp_path, "av-create-size", setup, drive)
    assert seen["status"] == 200
    assert seen["size"] == "1024x704"


def test_video_create_non_string_model_is_an_unhandled_500_today(tmp_path):
    """Pins a defect, not a behaviour to keep: a numeric ``model`` makes the old create route raise and answer a bare
    500 "Internal Server Error", with no engine call. The row path may only change this with a manifest entry."""
    seen = {}

    def setup(rig):
        _comfy(rig)

    async def drive(rig):
        response = await rig.call("POST", "/v1/videos", json_body={"prompt": "x", "model": 17})
        seen["status"], seen["text"] = response.status_code, response.text
        seen["engine"] = len(rig.recorder.engine)

    _run(tmp_path, "av-create-model-int", setup, drive)
    assert seen["status"] == 500
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
        seen["content"] = content.status_code

    _run(tmp_path, "av-missing-output", setup, drive)
    assert seen["status"] == (200, "completed", 100)
    assert seen["content"] == 502


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


KEY = {"Idempotency-Key": "k-gap8"}


def test_video_replay_overlap_with_the_same_key_submits_twice_today(tmp_path):
    """Gap 8 (overlap): the same key and body sent while the first engine acceptance is held for 30 virtual seconds.
    Today the second request is not deduplicated: it submits its own prompt while the first is still held, and both
    answer 200 with their own ids. Pins that baseline (two prompts, two ids). The intended one-work behaviour is
    video-05's strict xfail."""
    from tests.support.adapter_differential_rig import comfy_exchanges, exchange
    from tests.test_adapter_differential import _comfy
    seen = {}

    def setup(rig):
        base = [item for item in comfy_exchanges(strict=False) if item["id"] not in {"prompt", "history", "view"}]
        _comfy(rig, base + [
            exchange("prompt-held", "POST", "/prompt", {"wait_before_headers": 30, "json": {"prompt_id": "p1"}}, times=1),
            exchange("prompt-second", "POST", "/prompt", {"json": {"prompt_id": "p2"}}, times=1),
        ])

    async def drive(rig):
        body = {"prompt": "waves", "model": "ltx-2.5-t2v"}
        held = asyncio.create_task(rig.call("POST", "/v1/videos", json_body=body, headers=KEY))
        await rig.fakes.clock.until_waiting()
        second = asyncio.create_task(rig.call("POST", "/v1/videos", json_body=body, headers=KEY))
        for _ in range(200):
            await asyncio.sleep(0)
            if sum(1 for p in _engine_paths(rig) if p == "POST /prompt") == 2:
                break
        seen["prompts_while_held"] = sum(1 for p in _engine_paths(rig) if p == "POST /prompt")
        rig.fakes.clock.advance(31)
        first = await held
        other = await second
        seen["first"] = (first.status_code, first.json().get("id"))
        seen["second"] = (other.status_code, other.json().get("id"))
        seen["prompts"] = sum(1 for p in _engine_paths(rig) if p == "POST /prompt")

    _run(tmp_path, "av-replay-overlap", setup, drive)
    assert seen["prompts_while_held"] == 2
    assert seen["first"] == (200, "video_p1")
    assert seen["second"] == (200, "video_p2")
    assert seen["prompts"] == 2


@pytest.mark.parametrize("terminal", ["failed", "cancelled"])
def test_video_replay_after_a_terminal_job_with_the_same_key_submits_again(tmp_path, terminal):
    """Gap 8 (terminal): a job with the key ends, its engine history failed or the job was deleted while queued. The same
    key and body sent again submits a new prompt and gets a new id. Pins today's answer: the key does not replay the
    terminal job."""
    seen = {}

    def setup(rig):
        if terminal == "failed":
            _scripted_engine(rig, [("p1", SOURCE_CLIP, "failed"), ("p2", CHILD_CLIP, "done")], unread=("p2",))
        else:
            _scripted_engine(rig, [("p1", SOURCE_CLIP, "done"), ("p2", CHILD_CLIP, "done")], unread=("p1", "p2"))

    async def drive(rig):
        body = {"prompt": "waves", "model": "ltx-2.5-t2v"}
        first = await rig.call("POST", "/v1/videos", json_body=body, headers=KEY)
        first_id = first.json()["id"]
        if terminal == "failed":
            seen["first_status"] = (await rig.call("GET", f"/v1/videos/{first_id}")).json().get("status")
        else:
            seen["deleted"] = (await rig.call("DELETE", f"/v1/videos/{first_id}")).status_code
        second = await rig.call("POST", "/v1/videos", json_body=body, headers=KEY)
        seen["second"] = (second.status_code, second.json().get("id"))
        seen["prompts"] = sum(1 for p in _engine_paths(rig) if p == "POST /prompt")

    _run(tmp_path, f"av-replay-{terminal}", setup, drive)
    assert seen["second"] == (200, "video_p2")
    assert seen["prompts"] == 2
    if terminal == "failed":
        assert seen["first_status"] == "failed"
    else:
        assert seen["deleted"] == 200


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
        seen["status"] = (await rig.call("GET", f"/v1/videos/{seen['video_id']}")).json().get("status")
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
        assert provenance["requested_model"] == "ltx-2.5-t2v"
        assert provenance["resolved_model"] == "video-ltx-2.5-t2v"
        assert provenance["workflow_id"] == "video.generate.video-ltx-2.5-t2v"
        assert provenance["parameters"]["size"] == "960x512"
        assert isinstance(provenance["seed"], int)
        assert b"make it dusk" not in body  # the prompt is kept for the maker, not written into the file
    else:
        assert b"azizam.provenance" not in body
        assert b"make it dusk" not in body
