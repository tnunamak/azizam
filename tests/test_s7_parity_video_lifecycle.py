"""S7 video lifecycle parity: HTTP callers, persisted rows, assets and engine wire.

Items video-59..92 and video-121..128 from docs/design/c2d-s7-parity/video.md.
The gateway is real; only the engine's HTTP boundary is fake. No handler,
progress dictionary, scheduling function or result mapper is replaced here.
The tests intentionally pin today's public behaviour, including list/status
vocabulary differences and the fact that DELETE keeps the Library file.

Rewritten for the video move (docs/design/CODE_TO_DATA_S7_VIDEO_MOVE.md section 5.1): a video is the job
``video_mj_<hex>``; ids are read from responses; state is read through the public routes and ``JobService``;
the engine is driven with ``tests.support.video_rig.drive`` and the runner writes the status. A record of the old kind
(``video_<engine prompt id>``) is made with ``legacy_video_record`` and still answers from the old reader.

Not claimed by this file (not skips or expected failures):
* video-60: the allowed HTTP rig has no live engine progress-event source;
  completed/history transitions do not prove live sampling progress is monotonic.
* video-82: a Python exception-class catch boundary is not a public wire contract.
* video-92, video-126, video-127: the rig does not restart the process or expose a
  public retention-clock fixture. Clearing private dictionaries/calling hydration
  or GC helpers would not be a black-box restart/retention test.
* video-124: the save/change/no-inline-prompt portion is tested, not startup
  hydration or scrubbing an old inline prompt on load.
* video-91: retained Library bytes are tested; maker prompt-row deletion needs a
  populated prompt-memory fixture (the supplied rig does not retain prompts).
None of these gaps is covered by a section-4 allowed delta, so none is xfailed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re

import pytest

from tests.support.access import identity
from tests.support.legacy_video import legacy_video_record
from tests.support.video_rig import ALICE, BOB, MODEL, PLAN, drive, engine_prompt_of, run, settled, video_job_rows

JOB_ID = re.compile(r"^video_mj_[0-9a-f]{32}$")


def test_video_59_running_and_success_use_openai_status_words(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "running")
        running = await w.status(vid)
        assert running.status_code == 200
        assert running.json()["status"] == "in_progress"
        assert running.json()["azizam_status"] == "running"
        await drive(w, vid, "completed")
        done = await w.status(vid)
        assert done.status_code == 200
        assert done.json()["status"] == "completed"
        assert done.json()["azizam_status"] == "succeeded"
    run(tmp_path, exercise)


def test_video_61_terminal_error_is_served_while_history_answers(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "failed")
        first = await w.status(vid)
        assert first.status_code == 200
        assert first.json()["status"] == "failed"
        assert first.json()["error"]["code"] == "video_generation_failed"
        again_while_answering = await w.status(vid)
        assert again_while_answering.json() == first.json()
        # The job store holds the terminal error (design 3.2: the status route makes no engine call), so the terminal
        # answer survives an engine that stops answering. The old route answered 404 here, because it read /history on
        # every GET; inventory row video-61 says the terminal error "wins over fallback". Reported as an unlisted
        # difference to the design owner (it is none of the five section 5.4 kinds).
        w.engine.history_mode = "unavailable"
        requests = len(w.engine.requests)
        again = await w.status(vid)
        assert again.status_code == 200
        assert again.json() == first.json()
        assert len(w.engine.requests) == requests
    run(tmp_path, exercise)


def test_video_62_absent_engine_job_is_orphaned(tmp_path):
    async def exercise(w):
        vid = await w.create()
        view = await drive(w, vid, "missing")        # needs V5b: the runner marks the orphan (design 3.6)
        assert view.status == "failed"
        response = await w.status(vid)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed"
        assert body["error"]["message"] == "orphaned: ComfyUI does not report this prompt_id"
        assert w.engine.calls("GET", "/queue")
    run(tmp_path, exercise)


def bad_history_count(w):
    """How many history answers the engine has sent in a malformed or wrong-shape mode (recorded by the rig)."""
    return len(w.engine.bad_history)


async def bad_history_polls(w, count, *, before, timeout_s=5.0):
    """Wait until the engine has answered ``count`` more history reads with the bad body than it had at ``before``."""
    import time
    deadline = time.monotonic() + timeout_s
    while bad_history_count(w) < before + count:
        assert time.monotonic() < deadline, f"the runner never read the {w.engine.history_mode} history again"
        await asyncio.sleep(0.005)


@pytest.mark.parametrize("mode", ["malformed", "wrong_shape"])
def test_video_63_bad_history_does_not_orphan_live_job(tmp_path, mode):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "running")
        first = await w.status(vid)
        assert first.status_code == 200 and first.json()["status"] == "in_progress"
        # The engine now answers with a body the runner cannot read, for a prompt it also no longer lists. A response
        # that is malformed or of the wrong shape never counts toward orphaning, however many grace periods pass.
        w.engine.history_mode = mode
        await drive(w, vid, "missing", wait=False)
        await bad_history_polls(w, 1, before=0)                # a runner poll read the bad body before the grace ends
        w.now += 4 * 15.0
        seen_before = bad_history_count(w)
        await drive(w, vid, "missing", wait=False)
        await bad_history_polls(w, 1, before=seen_before)      # and another one after the grace has long passed
        response = await w.status(vid)
        assert response.status_code == 200
        assert response.json()["status"] == "in_progress"
        assert response.json()["error"] is None
        assert response.json()["progress"] >= first.json()["progress"]
    run(tmp_path, exercise)


def test_video_64_status_is_hidden_from_other_owner(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "completed")
        before = len(w.engine.requests)
        other = await w.status(vid, owner=BOB)
        assert other.status_code == 404
        assert other.json()["error"]["message"] == "video_id not found"
        assert len(w.engine.requests) == before
        own = await w.status(vid)
        assert own.status_code == 200 and own.json()["status"] == "completed"
    run(tmp_path, exercise)


@pytest.mark.parametrize("video_id,status", [("not-a-video", 400), ("video_", 400),
                                                   ("video_untracked", 404), ("video_mj_" + "0" * 32, 404)])
def test_video_65_invalid_and_untracked_status_ids_are_refused(tmp_path, video_id, status):
    async def exercise(w):
        # Even a real engine job is not visible without an owned gateway record or job.
        w.engine.states["untracked"] = "completed"
        response = await w.status(video_id)
        assert response.status_code == status
        if status == 400:
            assert response.json()["error"]["message"] == "invalid video_id"
        assert not w.engine.requests
    run(tmp_path, exercise)


def test_video_66_deleted_video_is_hidden_from_status_and_content(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "completed")
        assert (await w.delete(vid)).status_code == 200
        assert (await w.status(vid)).status_code == 404
        assert (await w.content(vid)).status_code == 404
    run(tmp_path, exercise)


def test_video_67_completed_status_has_terminal_fields(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "completed")
        response = await w.status(vid)
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == vid and body["object"] == "video"
        assert body["status"] == "completed" and body["azizam_status"] == "succeeded"
        assert body["progress"] == 100 and body["completed_at"] is not None
    run(tmp_path, exercise)


def test_video_68_running_status_exposes_progress(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "running")
        response = await w.status(vid)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "in_progress" and body["azizam_status"] == "running"
        assert body["progress"] == 50  # the recorded history-running wire value
        assert body["completed_at"] is None
    run(tmp_path, exercise)


def test_video_69_failed_status_carries_sanitized_error(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "failed")
        response = await w.status(vid)
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "failed" and body["azizam_status"] == "failed"
        assert body["error"] == {"code": "video_generation_failed",
                                 "message": "ComfyUI LTXVConcatAVLatent node 12: execution failed"}
        assert "oom" not in response.text
    run(tmp_path, exercise)


def test_video_70_unfinished_content_is_not_ready(tmp_path):
    async def exercise(w):
        vid = await w.create()
        response = await w.content(vid)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "video_not_ready"
        assert "queued" in response.json()["error"]["message"]
        assert not w.engine.calls("GET", "/view")
        assert w.video_files() == {}
    run(tmp_path, exercise)


def test_video_71_content_is_hidden_from_other_owner(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "completed")
        before, files = len(w.engine.requests), w.video_files()
        response = await w.content(vid, owner=BOB)
        assert response.status_code == 404 and len(w.engine.requests) == before
        assert w.video_files() == files
    run(tmp_path, exercise)


def test_video_72_cancel_during_the_engine_fetch_keeps_no_asset(tmp_path):
    """video-72: a video deleted while its result is being fetched stores no asset. The fetch of the clip is the runner's
    (design 3.3): the job is canceled before the adapter commits, so nothing is stored after the fetch returns."""
    async def exercise(w):
        vid = await w.create()
        w.engine.view_started, w.engine.view_release = asyncio.Event(), asyncio.Event()
        await drive(w, vid, "completed", wait=False)
        await asyncio.wait_for(w.engine.view_started.wait(), timeout=5)
        assert (await w.delete(vid)).status_code == 200
        w.engine.view_release.set()
        for _ in range(20):
            await asyncio.sleep(0.005)
        assert w.deleted(vid)
        assert (await w.status(vid)).status_code == 404
        assert w.video_files() == {}
        assert not list(w.rig.cache.glob("*.meta.json"))
    run(tmp_path, exercise)


def test_video_73_first_content_fetch_indexes_library_asset(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "completed")
        aid = w.asset_id(vid)
        assert aid and aid.startswith("vid_")
        response = await w.content(vid)
        assert response.status_code == 200
        files = w.video_files()
        assert len(files) == 1
        stored = next(iter(files.values()))
        # The cached copy is not the engine's bytes: the caller's plan (media_metadata none) strips the encoder tag.
        assert b"Lavf" in w.engine.clip and b"Lavf" not in stored
        assert stored == response.content
        metadata = [json.loads(p.read_text()) for p in w.rig.cache.glob("*.meta.json")]
        assert any(row.get("asset_id") == aid for row in metadata)
        view = w.view(vid)
        assert view.workflow_id.startswith("video.generate.") and view.status == "succeeded"
        assert [(r.media_type, r.asset_id) for r in view.results] == [("video/mp4", aid)]
    run(tmp_path, exercise)


def test_video_74_content_serves_the_stored_mp4_with_delivery_headers(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "completed")
        response = await w.content(vid)
        assert response.status_code == 200
        # Served bytes are an mp4 remux of the engine clip with its encoder tag removed (media_metadata none).
        assert response.content[4:8] == b"ftyp" and b"Lavf" not in response.content
        assert response.content != w.engine.clip
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["content-disposition"] == 'inline; filename="clip.mp4"'
        assert int(response.headers["content-length"]) == len(response.content)
        views = w.engine.calls("GET", "/view")
        assert len(views) == 1
        # The gateway sends subfolder only when the output has one (proxy.py _load_completed_video_bytes; the
        # content route builds its params the same way); this output has none.
        assert dict(views[0].url.params) == {"filename": "clip.mp4", "type": "output"}
    run(tmp_path, exercise)


def test_video_75_missing_output_is_a_video_asset_error(tmp_path):
    async def exercise(w):
        vid = await w.create()
        w.engine.view_error = True                  # the engine lists the output but /view refuses it
        await drive(w, vid, "completed", wait=False)
        assert (await settled(w, vid)).status == "failed"
        response = await w.content(vid)
        # The whole row of video-texts.md (content_view_error): the /view refusal is seen below, and the content route
        # answers 502 with the engine's status, nothing else.
        assert response.status_code == 502
        assert response.json() == {"error": {"message": "ComfyUI /view error: 404", "type": "api_error"}}
        assert w.video_files() == {}
        assert w.asset_id(vid) is None
        refused = w.engine.calls("GET", "/view")
        assert refused, "the engine's /view refusal was never made"
    run(tmp_path, exercise)


@pytest.mark.parametrize("params,param", [({"limit": "0"}, "limit"), ({"limit": "many"}, "limit"),
    ({"order": "sideways"}, "order"), ({"after": "not-a-video"}, "after")])
def test_video_76_bad_list_parameters_are_400(tmp_path, params, param):
    async def exercise(w):
        response = await w.listing(**params)
        assert response.status_code == 400
        assert response.json()["error"]["param"] == param
        assert not w.engine.requests
    run(tmp_path, exercise)


async def three_videos(w, states=("completed", "completed", "completed")):
    """Three independent jobs, each driven to its own state (a terminal job is never driven to another state)."""
    ids = []
    for state in states:
        vid = await w.create()
        await drive(w, vid, state)
        ids.append(vid)
        w.now += 10
    return ids


def test_video_77_list_order_and_has_more(tmp_path):
    async def exercise(w):
        ids = await three_videos(w)
        newest = await w.listing(limit="2")
        assert newest.status_code == 200
        assert [x["id"] for x in newest.json()["data"]] == ids[::-1][:2]
        assert newest.json()["has_more"] is True
        assert newest.json()["first_id"] == ids[-1] and newest.json()["last_id"] == ids[1]
        oldest = await w.listing(order="asc", limit="3")
        assert oldest.status_code == 200
        assert [x["id"] for x in oldest.json()["data"]] == ids
        assert oldest.json()["has_more"] is False
    run(tmp_path, exercise)


def test_video_78_after_excludes_cursor_and_earlier_page(tmp_path):
    async def exercise(w):
        ids = await three_videos(w)
        response = await w.listing(after=ids[-1])
        assert response.status_code == 200
        assert [x["id"] for x in response.json()["data"]] == ids[-2::-1]
        ascending = await w.listing(order="asc", after=ids[0])
        assert ascending.status_code == 200
        assert [x["id"] for x in ascending.json()["data"]] == ids[1:]
    run(tmp_path, exercise)


def test_video_79_missing_cursor_does_not_restart_pagination(tmp_path):
    async def exercise(w):
        await three_videos(w)
        response = await w.listing(after="video_evicted")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "cursor_not_found"
        assert response.json()["error"]["param"] == "after"
    run(tmp_path, exercise)


def test_video_80_list_entry_fields_error_and_queue_fallback(tmp_path):
    async def exercise(w):
        done, running, failed = await three_videos(w, ("completed", "queued", "failed"))
        requests = len(w.engine.requests)
        response = await w.listing()
        assert response.status_code == 200
        entries = {x["id"]: x for x in response.json()["data"]}
        assert set(entries) == {done, running, failed}
        entry = entries[done]
        assert entry["object"] == "video" and entry["model"] == MODEL
        assert entry["size"] == "960x512" and entry["seconds"] == "5"
        assert entry["status"] == "succeeded" and entry["completed_at"] is not None
        assert entry["created_at"] == 1_800_000_000
        assert entries[running]["status"] == "queued"
        assert entries[failed]["status"] == "failed"
        assert entries[failed]["error"]
        # The list reads the job store; it asks the engine nothing. (The list wire keeps its own status words,
        # `succeeded` for a finished video, beside the status route's `completed`: design 3.4 renders list items "with
        # the status body of its own store", which would change that word; reported to the design owner.)
        assert len(w.engine.requests) == requests
    run(tmp_path, exercise)


def test_video_81_list_is_owner_scoped_and_excludes_deleted(tmp_path):
    async def exercise(w):
        alice = await w.create()
        bob = await w.create(owner=BOB)
        deleted = await w.create()
        for vid in (alice, bob, deleted):
            await drive(w, vid, "completed")
        assert (await w.delete(deleted)).status_code == 200
        own = await w.listing()
        other = await w.listing(owner=BOB)
        assert own.status_code == other.status_code == 200
        assert [x["id"] for x in own.json()["data"]] == [alice]
        assert [x["id"] for x in other.json()["data"]] == [bob]
    run(tmp_path, exercise)


def test_video_83_terminal_delete_tombstones_without_cancel(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "completed")
        assert (await w.status(vid)).json()["status"] == "completed"
        response = await w.delete(vid)
        assert response.status_code == 200
        assert w.deleted(vid)
        assert not [r for r in w.engine.requests if r.url.path.endswith("/cancel")]
        deletes = w.engine.calls("POST", "/history")
        assert len(deletes) == 1 and json.loads(deletes[0].content) == {"delete": [engine_prompt_of(w, vid)]}
    run(tmp_path, exercise)


def test_video_84_known_job_without_history_is_deletable(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "queued")        # the runner has submitted the prompt
        response = await w.delete(vid)
        assert response.status_code == 200 and response.json()["deleted"] is True
        assert w.deleted(vid)
    run(tmp_path, exercise)


def test_video_85_cancel_precedes_durable_tombstone(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "queued")        # the runner has submitted the prompt
        observed = []
        w.engine.before_cancel = lambda: observed.append(w.deleted(vid))
        response = await w.delete(vid)
        assert response.status_code == 200
        assert observed == [False]
        assert w.deleted(vid)
        posts = [r.url.path for r in w.engine.requests if r.method == "POST"]
        assert posts.index(f"/api/jobs/{engine_prompt_of(w, vid)}/cancel") < posts.index("/history")
    run(tmp_path, exercise)


def test_video_86_failed_cancel_is_409_and_not_deleted(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "queued")        # the runner has submitted the prompt
        w.engine.cancel_error = True
        response = await w.delete(vid)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "video_cancel_failed"
        assert not w.deleted(vid)
        assert not w.engine.calls("POST", "/history")
        assert (await w.status(vid)).status_code == 200
    run(tmp_path, exercise)


def test_video_87_cancel_false_still_tombstones(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "queued")        # the runner has submitted the prompt
        w.engine.cancelled = False
        response = await w.delete(vid)
        assert response.status_code == 200 and response.json()["deleted"] is True
        assert w.engine.calls("POST", f"/api/jobs/{engine_prompt_of(w, vid)}/cancel")
        assert w.deleted(vid)
    run(tmp_path, exercise)


def test_video_88_other_owner_cannot_delete(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "queued")        # the runner has submitted the prompt
        before = len(w.engine.requests)
        denied = await w.delete(vid, owner=BOB)
        assert denied.status_code == 404 and len(w.engine.requests) == before
        assert not w.deleted(vid)
        own = await w.delete(vid)
        assert own.status_code == 200 and w.deleted(vid)
    run(tmp_path, exercise)


@pytest.mark.parametrize("video_id,status", [("other_id", 400), ("video_", 400), ("video_missing", 404)])
def test_video_89_delete_rejects_invalid_and_unknown_ids(tmp_path, video_id, status):
    async def exercise(w):
        response = await w.delete(video_id)
        assert response.status_code == status
        if status == 400:
            assert response.json()["error"]["message"] == "invalid video_id"
        assert not w.engine.requests
    run(tmp_path, exercise)


def test_video_90_delete_success_body_is_exact(tmp_path):
    async def exercise(w):
        vid = await w.create()
        response = await w.delete(vid)
        assert response.status_code == 200
        assert response.json() == {"id": vid, "object": "video", "deleted": True}
    run(tmp_path, exercise)


def test_video_91_delete_keeps_library_file_bytes(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "completed")
        assert (await w.content(vid)).status_code == 200
        before = w.video_files()
        assert len(before) == 1 and b"Lavf" not in next(iter(before.values()))
        assert (await w.delete(vid)).status_code == 200
        assert w.video_files() == before  # writers-54: prompt removal is not file deletion
        assert (await w.content(vid)).status_code == 404
    run(tmp_path, exercise)


def test_video_121_id_is_video_prefix_and_job_id(tmp_path):
    async def exercise(w):
        vid = await w.create()
        assert JOB_ID.match(vid), vid
        await drive(w, vid, "completed")
        response = await w.status(vid)
        assert response.status_code == 200 and response.json()["id"] == vid
        pid = engine_prompt_of(w, vid)
        assert w.engine.calls("GET", f"/history/{pid}")
        assert not w.engine.calls("GET", f"/history/{vid}")
    run(tmp_path, exercise)


@pytest.mark.parametrize("video_id", ["p1", "video_", "image_p1"])
def test_video_122_malformed_prefix_is_invalid_video_id(tmp_path, video_id):
    async def exercise(w):
        response = await w.status(video_id)
        assert response.status_code == 400
        assert response.json()["error"]["message"] == "invalid video_id"
        assert not w.engine.requests
    run(tmp_path, exercise)


def test_video_123_owner_identity_is_kept_without_raw_credential(tmp_path):
    """video-123 (delta record_store_moved): the owner key hash has no public surface, so the owner is asserted by the
    behaviour it exists for: the maker reads the video, another key gets 404, and no response carries the credential."""
    async def exercise(w):
        raw = "parity-only-synthetic-credential"
        hashed = "sha256:" + hashlib.sha256(raw.encode()).hexdigest()
        principal = identity(PLAN, name="hashed-video", app_id=None, key_hash=hashed)
        vid = await w.create(owner=principal)
        own = await w.status(vid, owner=principal)
        assert own.status_code == 200
        assert (await w.status(vid, owner=BOB)).status_code == 404
        assert raw not in own.text and hashed not in own.text
        assert raw not in json.dumps(dict(w.facts(vid)), default=str)
    run(tmp_path, exercise)


def test_video_124_http_changes_persist_without_inline_prompt(tmp_path):
    """video-124 (delta record_store_moved): the fields the old record held are read from the status body and the request
    facts; the prompt is in neither (the rig retains no prompts)."""
    async def exercise(w):
        secret = "parity prompt that must not be inline in the video record"
        vid = await w.create(prompt=secret)
        created = await w.status(vid)
        facts = dict(w.facts(vid))
        assert "prompt" not in facts and secret not in json.dumps(facts, default=str) and secret not in created.text
        assert created.json()["completed_at"] is None and not w.deleted(vid)
        await drive(w, vid, "completed")
        completed = await w.status(vid)
        assert completed.status_code == 200 and completed.json()["completed_at"] is not None
        assert dict(w.facts(vid)) == facts           # the facts are written once, with the job
        assert secret not in completed.text
        assert (await w.delete(vid)).status_code == 200
        assert w.deleted(vid)
        assert (await w.status(vid)).status_code == 404
    run(tmp_path, exercise)


def test_video_125_request_facts_are_recorded_with_each_job(tmp_path):
    """video-125 (delta record_store_moved): the migration row `video_job_records_v1` is replaced by `request_facts` on the
    job metadata. Every created video has its facts, with the keys the design freezes (section 1.2)."""
    async def exercise(w):
        first, second = await w.create(), await w.create()
        for vid in (first, second):
            facts = dict(w.facts(vid))
            assert facts["model"] == MODEL and facts["resolved_model"] == MODEL
            assert facts["size"] == "960x512" and facts["seconds"] == "5"
            assert set(facts) <= {"model", "resolved_model", "size", "seconds", "actual_seconds", "length_frames",
                                  "frame_rate", "seed", "audio", "metadata_mode", "provenance", "remixed_from_video_id",
                                  "extended_from_video_id", "source_asset_id", "total_seconds", "warnings", "prompt",
                                  "maker_person_id"}
    run(tmp_path, exercise)


@pytest.mark.parametrize("value", ["image_p1", "p1", "video_"])
def test_video_128_list_cursor_and_delete_require_video_id(tmp_path, value):
    async def exercise(w):
        response = await w.listing(after=value)
        assert response.status_code == 400 and response.json()["error"]["param"] == "after"
        deleted = await w.delete(value)
        assert deleted.status_code == 400 and deleted.json()["error"]["message"] == "invalid video_id"
        assert not w.engine.requests
    run(tmp_path, exercise)


# ---- V3b contract tests from the video parity oracle audit ----


@pytest.mark.parametrize("cancel_error,cancelled", [(False, True), (False, False), (True, True)])
def test_video_83_delete_while_running_cancels_before_the_tombstone(tmp_path, cancel_error, cancelled):
    """video-83..87: a running video is cancelled on the engine before the tombstone; a failed cancel is 409 and keeps it."""
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "running")
        before = await w.status(vid)
        assert before.status_code == 200
        assert before.json()["status"] == "in_progress"
        assert not w.deleted(vid)
        w.engine.cancel_error = cancel_error
        w.engine.cancelled = cancelled
        observed = []
        w.engine.before_cancel = lambda: observed.append(w.deleted(vid))
        answer = await asyncio.wait_for(w.delete(vid), timeout=5)
        assert observed == [False]
        assert len(w.engine.calls("POST", f"/api/jobs/{engine_prompt_of(w, vid)}/cancel")) == 1
        if cancel_error:
            assert answer.status_code == 409
            assert answer.json()["error"]["code"] == "video_cancel_failed"
            assert not w.deleted(vid)
            assert not w.engine.calls("POST", "/history")
            assert (await w.status(vid)).status_code == 200
        else:
            assert answer.status_code == 200
            assert answer.json() == {"id": vid, "object": "video", "deleted": True}
            assert w.deleted(vid)
            assert (await w.status(vid)).status_code == 404
            assert (await w.content(vid)).status_code == 404
            listing = await w.listing()
            assert vid not in {x["id"] for x in listing.json()["data"]}
    run(tmp_path, exercise)


@pytest.mark.parametrize("state,wire", [("completed", "completed"), ("failed", "failed")])
def test_video_83_each_terminal_state_deletes_without_trying_cancel(tmp_path, state, wire):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, state)
        status = await w.status(vid)
        assert status.status_code == 200 and status.json()["status"] == wire
        w.engine.cancel_error = True
        answer = await asyncio.wait_for(w.delete(vid), timeout=5)
        assert answer.status_code == 200
        assert not [r for r in w.engine.requests if r.url.path.endswith("/cancel")]
        assert w.deleted(vid)
        assert (await w.status(vid)).status_code == 404
    run(tmp_path, exercise)


def test_video_73_content_keeps_the_decoded_frames_and_the_exact_library_identity(tmp_path):
    """video-73/74/100, writers-33/34/40: the served clip decodes to the engine's frames, is stored under the hash of its
    bytes, and the delivery row, the sidecar and the record agree on the asset."""
    import shutil
    import subprocess

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("content parity requires the hermetic ffmpeg fixture")

    def decoded_video(data):
        result = subprocess.run(
            [ffmpeg, "-nostdin", "-v", "error", "-i", "pipe:0", "-map", "0:v:0", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "pipe:1"], input=data, capture_output=True, check=True, timeout=30)
        if not result.stdout:
            raise RuntimeError("fixture had no decoded video frames")
        return result.stdout

    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "completed")
        source_frames = decoded_video(w.engine.clip)
        response = await asyncio.wait_for(w.content(vid), timeout=40)
        assert response.status_code == 200
        assert response.headers["content-type"] == "video/mp4"
        assert response.headers["content-disposition"] == 'inline; filename="clip.mp4"'
        assert "x-request-id" not in response.headers
        assert int(response.headers["content-length"]) == len(response.content)
        assert decoded_video(response.content) == source_frames
        files = w.video_files()
        assert len(files) == 1
        filename, stored = next(iter(files.items()))
        assert stored == response.content
        digest = hashlib.sha256(stored).hexdigest()
        aid = "vid_" + digest
        assert filename == digest + ".mp4"
        assert w.asset_id(vid) == aid
        sidecars = [json.loads(p.read_text()) for p in w.rig.cache.glob("*.meta.json")]
        matching = [m for m in sidecars if m.get("asset_id") == aid]
        assert len(matching) == 1
        assert not matching[0].get("origin")  # the old video stores no workflow-output origin
        # The job is the video's one durable row (it was a record plus a sync delivery job). It shows the one asset.
        view = w.view(vid)
        assert view.status == "succeeded" and view.workflow_id.startswith("video.generate.")
        assert [(r.media_type, r.asset_id) for r in view.results] == [("video/mp4", aid)]
        again = await asyncio.wait_for(w.content(vid), timeout=40)
        assert again.status_code == 200 and again.content == response.content
        assert w.video_files() == files
        # Exactly one row for the video after both fetches: no delivery job beside the run job (video-18, design 5.4).
        rows = video_job_rows(w.rig)
        assert [(job["request_id"], job["status"]) for job in rows] == [(rows[0]["request_id"], "succeeded")], rows
        assert [a["asset_id"] for a in rows[0]["assets"]] == [aid]
        assert [x["id"] for x in (await w.listing()).json()["data"]] == [vid]
    run(tmp_path, exercise)


def test_video_77_list_default_limit_is_20_and_the_largest_page_100(tmp_path):
    async def exercise(w):
        ids = []
        for index in range(101):
            vid = await w.create(prompt=f"page item {index}")
            await drive(w, vid, "completed")
            ids.append(vid)
            w.now += 1
        default = await w.listing()
        assert default.status_code == 200
        assert [x["id"] for x in default.json()["data"]] == ids[::-1][:20]
        assert default.json()["has_more"] is True
        maximum = await w.listing(limit="100")
        assert maximum.status_code == 200
        assert [x["id"] for x in maximum.json()["data"]] == ids[::-1][:100]
        assert maximum.json()["has_more"] is True
    run(tmp_path, exercise)


def test_video_80_list_error_names_the_failed_node(tmp_path):
    async def exercise(w):
        vid = await w.create()
        await drive(w, vid, "failed")
        listed = await w.listing()
        assert listed.status_code == 200
        entries = {entry["id"]: entry for entry in listed.json()["data"]}
        assert set(entries) == {vid}
        assert entries[vid]["status"] == "failed"
        rendered = json.dumps(entries[vid]["error"])
        assert "ComfyUI LTXVConcatAVLatent node 12: execution failed" in rendered or "node 12" in rendered
        assert "oom" not in rendered
    run(tmp_path, exercise)


# ---- records of the old kind: the M7 legacy reader still serves them, with the old output ----


def test_video_legacy_record_status_follows_the_engine_on_each_get(tmp_path):
    async def exercise(w):
        vid = legacy_video_record(w, "old-p1")
        assert vid == "video_old-p1"
        w.state(vid, "running")
        running = (await w.status(vid)).json()
        assert running["status"] == "in_progress" and running["azizam_status"] == "running"
        w.state(vid, "completed")
        done = await w.status(vid)
        assert done.status_code == 200
        assert done.json()["status"] == "completed" and done.json()["id"] == vid
        assert done.json()["model"] == MODEL and done.json()["size"] == "960x512" and done.json()["seconds"] == "5"
        assert w.engine.calls("GET", "/history/old-p1")
    run(tmp_path, exercise)


def test_video_legacy_record_is_hidden_from_another_key_and_deletable_by_its_owner(tmp_path):
    async def exercise(w):
        vid = legacy_video_record(w, "old-p2")
        w.state(vid, "completed")
        before = len(w.engine.requests)
        assert (await w.status(vid, owner=BOB)).status_code == 404
        assert (await w.delete(vid, owner=BOB)).status_code == 404
        assert len(w.engine.requests) == before
        answer = await w.delete(vid)
        assert answer.status_code == 200
        assert answer.json() == {"id": vid, "object": "video", "deleted": True}
        assert (await w.status(vid)).status_code == 404
    run(tmp_path, exercise)


def test_video_list_merges_old_records_and_jobs_under_one_cursor(tmp_path):
    """design 3.4: one order key (created_at, raw id) across both stores; `after` is the last wire id of the previous page."""
    async def exercise(w):
        old = legacy_video_record(w, "old-p3")
        w.now += 10
        first = await w.create()
        w.now += 10
        second = await w.create()
        page = await w.listing(limit="2")
        assert page.status_code == 200
        assert [x["id"] for x in page.json()["data"]] == [second, first]
        assert page.json()["has_more"] is True and page.json()["last_id"] == first
        rest = await w.listing(limit="2", after=first)
        assert [x["id"] for x in rest.json()["data"]] == [old]
        assert rest.json()["has_more"] is False
        ascending = await w.listing(order="asc", limit="2")
        assert [x["id"] for x in ascending.json()["data"]] == [old, first]
        tail = await w.listing(order="asc", after=old)
        assert [x["id"] for x in tail.json()["data"]] == [first, second]
        # A deleted boundary is the evicted-cursor error in both stores.
        assert (await w.delete(first)).status_code == 200
        gone = await w.listing(after=first)
        assert gone.status_code == 400 and gone.json()["error"]["code"] == "cursor_not_found"
    run(tmp_path, exercise)


def test_video_130_create_answers_queued_while_the_engine_never_answers(tmp_path):
    """Contract: a create answers as soon as the job is durably queued, as the old route did. It does not wait for the
    engine's reply, so an engine that never answers costs the caller no time."""
    async def exercise(w):
        w.engine.prompt_gate = asyncio.Event()           # never set: the engine holds the submission for good
        started = asyncio.get_running_loop().time()
        response = await asyncio.wait_for(
            w.call("POST", "/v1/videos", owner=ALICE, json_body={"prompt": "never answered", "model": MODEL}), timeout=1.0)
        assert asyncio.get_running_loop().time() - started < 1.0
        assert response.status_code == 200, response.text
        body = response.json()
        vid = w.track(body["id"])
        assert body["status"] == "queued" and body["object"] == "video" and JOB_ID.match(vid)
        assert w.engine.prompt_ids == []
    run(tmp_path, exercise)
