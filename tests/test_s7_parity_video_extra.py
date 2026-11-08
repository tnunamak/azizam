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
import sqlite3
from pathlib import Path

import pytest

import proxy
from tests.support.access import authenticated, everything, identity, plan_for
from tests.support.adapter_differential_rig import GatewayRig
from tests.test_adapter_differential import _png, _video_comfy

MARKER = "prompt-marker-7f3a9c"


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
