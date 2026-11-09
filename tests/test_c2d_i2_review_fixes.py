"""Contract tests for the dot review of the image rows (I2 fix 3).

Findings 1, 2 and 5 run a request on the real row server over the golden rig's fake engine (the row side of
tests/support/s7_parity_images_rig.py). Finding 6 drives the run core over the in-memory port of tests/test_workflow_run.py.
Findings 4 and 7 prove that the parity readers see what they once lost: each mutant changes one thing in an observation and
the comparison must fail.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import io
import json

import pytest
from PIL import Image

import proxy
from gateway import media_jobs
from gateway.workflow_run import WorkflowRunCore
from tests import test_adapter_differential as base
from tests.support.adapter_differential_rig import comfy_exchanges, exchange
from tests.support.s7_parity_images_facts import (
    asset_files, assert_same, canon_jobs, data_items, doc, job_document, job_rows, receipt_summary, status)
from tests.support.s7_parity_images_rig import ROW_PREFIX, Obs, observe
from tests.test_c2d_k4_core import ReceiptPort
from tests.test_s7_parity_images import EDIT, GEN, JOB, IMG, _half_mask, form, job_body, job_setup, ok
from tests.test_workflow_run import Caller, db, make_core, request, run, snapshot  # noqa: F401 - the db fixture is used by name


def _png(mode: str, color) -> bytes:
    out = io.BytesIO()
    Image.new(mode, (64, 64), color).save(out, "PNG")
    return out.getvalue()


TRANSPARENT = {"json_body": {"prompt": "a red fox", "response_format": "b64_json", "background": "transparent"}}


def _refused_once(views):
    def setup(rig):
        base._comfy(rig, comfy_exchanges(views=views, strict=False, extra=[
            exchange("prompt-fails", "POST", "/prompt", {"status": 500, "body": [{"text": "a node failed"}]}, times=1, required=False)]))
    return setup


def _cut_out(calls):
    async def stage(ctx, files):
        calls.append(len(files))
        out = []
        for raw in files:
            picture = Image.open(io.BytesIO(raw)).convert("RGBA")
            picture.putpixel((0, 0), (0, 0, 0, 0))
            data = io.BytesIO()
            picture.save(data, "PNG")
            out.append(data.getvalue())
        return out
    return stage


def _delivered(obs: Obs) -> list[Image.Image]:
    return [Image.open(io.BytesIO(base64.b64decode(item["b64_json"]))) for item in data_items(obs) if "b64_json" in item]


def _prompts(obs: Obs) -> int:
    return len([r for r in obs.engine_requests if r["path"] == "/prompt"])


# ------------------------------------------------------------------------------------------------------ finding 1


def test_1_a_failed_cutout_ends_transparent_with_one_fallback(tmp_path):
    calls: list[int] = []

    def setup(rig):
        _refused_once([(_png("RGB", (200, 40, 40)), "image/png")])(rig)
        rig.monkeypatch.setitem(proxy._RESULT_STAGES, "background_fallback", _cut_out(calls))
    row = observe(tmp_path, GEN, TRANSPARENT, setup, row=True)
    assert status(row) == 200 and "exception" not in row.extra
    assert calls == [1], "the fallback cutout ran once"
    assert _prompts(row) == 2, "one refused cutout graph, one fallback graph"
    pictures = _delivered(row)
    assert len(pictures) == 1 and "A" in pictures[0].getbands() and pictures[0].getchannel("A").getextrema()[0] == 0
    stored = [a for a in row.trace["assets"] if a["suffix"] == ".png"]
    assert len(stored) == 1


def test_1_no_cutout_to_make_fails_and_stores_no_opaque_picture(tmp_path):
    row = observe(tmp_path, GEN, TRANSPARENT, _refused_once([(_png("RGB", (200, 40, 40)), "image/png")]), row=True)
    assert status(row) >= 400, "an opaque picture is never an answer to background=transparent"
    assert not _delivered(row) and not [a for a in row.trace["assets"] if a["suffix"] == ".png"]


def test_1_a_stage_that_returns_an_opaque_picture_fails(tmp_path):
    async def opaque(ctx, files):
        return files

    def setup(rig):
        _refused_once([(_png("RGB", (200, 40, 40)), "image/png")])(rig)
        rig.monkeypatch.setitem(proxy._RESULT_STAGES, "background_fallback", opaque)
    row = observe(tmp_path, GEN, TRANSPARENT, setup, row=True)
    assert status(row) >= 400 and not _delivered(row)


def test_1_a_cutout_the_engine_made_needs_no_fallback(tmp_path):
    calls: list[int] = []

    def setup(rig):
        base._comfy(rig, comfy_exchanges(views=[(_png("RGBA", (200, 40, 40, 0)), "image/png")], strict=False))
        rig.monkeypatch.setitem(proxy._RESULT_STAGES, "background_fallback", _cut_out(calls))
    row = observe(tmp_path, GEN, TRANSPARENT, setup, row=True)
    assert status(row) == 200 and calls == [] and _prompts(row) == 1
    assert "A" in _delivered(row)[0].getbands()


def test_1_the_fallback_graph_has_its_cutout_turned_off_explicitly(tmp_path):
    def setup(rig):
        _refused_once([(_png("RGB", (200, 40, 40)), "image/png")])(rig)
        rig.monkeypatch.setitem(proxy._RESULT_STAGES, "background_fallback", _cut_out([]))
    row = observe(tmp_path, GEN, TRANSPARENT, setup, row=True)
    prompts = [r for r in row.engine_requests if r["path"] == "/prompt"]
    assert len(prompts) == 2
    first, second = (json.dumps(r["body"], sort_keys=True) for r in prompts)
    assert first != second, "the fallback submits a different graph: the cutout step is off"


# ------------------------------------------------------------------------------------------------------ finding 2


def test_2_a_resumed_masked_edit_is_masked(tmp_path):
    big = _png("RGB", (200, 40, 40))
    request_ = form(files={"image": ("in.png", big, "image/png"), "mask": _half_mask()}, model="flux2-klein")
    engine_output = _png("RGB", (20, 20, 20))

    def setup(rig):
        base._comfy(rig, comfy_exchanges(views=[(engine_output, "image/png")], strict=False))

    def plain(use_interruption):
        async def go(rig):
            real = proxy._RESULT_STAGES["post_composite_mask"]
            started = asyncio.Event()
            if use_interruption:
                async def hold(ctx, files):
                    started.set()
                    await asyncio.Event().wait()
                rig.monkeypatch.setitem(proxy._RESULT_STAGES, "post_composite_mask", hold)
            call = asyncio.ensure_future(rig.call("POST", ROW_PREFIX + EDIT, label="edit", **request_))
            if use_interruption:
                await asyncio.wait_for(started.wait(), 30)
                # The interruption: the run stops where the stages begin. Its job and engine prompt are saved.
                for task in list(proxy._workflow_run_tasks):
                    task.cancel()
                call.cancel()
                for _ in range(50):
                    await asyncio.sleep(0)
                assert not proxy._workflow_run_scheduled, proxy._workflow_run_scheduled
                rig.monkeypatch.setitem(proxy._RESULT_STAGES, "post_composite_mask", real)
                job = next(iter(media_jobs.unfinished(path=media_jobs.DB_PATH)))
                assert job["metadata"].get("protocol_job"), "the prompt id is saved before the stages"
                proxy._recover_media_jobs()
                for _ in range(3000):
                    await asyncio.sleep(0)
                    found = media_jobs.get(job["job_id"], audience="internal", path=media_jobs.DB_PATH)
                    if found and media_jobs.is_terminal(str(found["status"])):
                        break
                for _ in range(50):
                    await asyncio.sleep(0)
                return None
            return await call
        return go

    whole = observe(tmp_path / "whole", EDIT, plain(False), setup, row=True)
    resumed = observe(tmp_path / "resumed", EDIT, plain(True), setup, row=True)
    assert status(whole) == 200
    assert "exception" not in resumed.extra, resumed.extra
    assert [j["status"] for j in resumed.trace["media_jobs"]["media_jobs"]] == ["succeeded"], resumed.trace["media_jobs"]["media_jobs"][0]
    files_whole = [f for f in asset_files(whole) if f[0] == ".png"]
    files_resumed = [f for f in asset_files(resumed) if f[0] == ".png"]
    unmasked = __import__("hashlib").sha256(engine_output).hexdigest()
    assert files_whole and files_whole[0][2] != unmasked, "the masked result is not the engine's picture"
    assert files_resumed == files_whole, "a resumed run stores the same masked picture"


# ------------------------------------------------------------------------------------------------------ finding 5


def test_5_a_replay_of_a_failed_job_closes_its_submit_receipt_on_its_own_answer(tmp_path):
    keyed = dict(job_body(metadata="nonsense"), headers={"Idempotency-Key": "k-5"})

    def twice(rig):
        async def go(rig_):
            first = await rig_.call("POST", ROW_PREFIX + JOB, label="submit", **keyed)
            job_id = (first.json() or {}).get("job_id") or (first.json() or {}).get("id")
            for _ in range(3000):
                await asyncio.sleep(0)
                found = media_jobs.get(job_id, audience="internal", path=media_jobs.DB_PATH)
                if found and media_jobs.is_terminal(str(found["status"])):
                    break
            for _ in range(100):
                await asyncio.sleep(0)
            return await rig_.call("POST", ROW_PREFIX + JOB, label="replay", **keyed)
        return go
    row = observe(tmp_path, JOB, twice(None), job_setup(), row=True)
    assert [status(row, 0), status(row, 1)][0] == 202
    summary = receipt_summary(row)
    submits = [r for r in summary if r[3] == JOB]
    runs = [r for r in summary if r[3] != JOB]
    assert [r[0] for r in submits] == ["succeeded", "succeeded"], summary
    assert len(runs) == 1 and runs[0][0] == "rejected", summary
    assert [j["status"] for j in row.trace["media_jobs"]["media_jobs"]] == ["failed"]


# ------------------------------------------------------------------------------------------------------ finding 6


class RunReceiptPort(ReceiptPort):
    async def open_run_receipt(self, job, caller):
        return "run-receipt-1"


@pytest.mark.parametrize("vocabulary", [None, "failed", "rejected"])
@pytest.mark.parametrize("http_status", [400, 500])
def test_6_a_deferred_refusal_closes_with_the_rows_word_for_a_4xx_refusal_only(db, tmp_path, vocabulary, http_status):
    snap = snapshot()
    port = RunReceiptPort(db)
    core, port, _ = make_core(db, tmp_path, snap, port)
    refusal = {"status": http_status, "code": "unknown_parameter", "category": "invalid_input", "message": "no",
               **({"receipt": vocabulary} if vocabulary else {})}
    view = run(core.submit_run(request(request_id="req-6", run_receipt_path="/v1/x", run_refusal=refusal), Caller(), snapshot=snap))
    run(core.execute_run(view.job_id))
    expected = "rejected" if vocabulary == "rejected" and http_status == 400 else "failed"
    assert port.closed == [("run-receipt-1", expected, "unknown_parameter")]
    assert media_jobs.get(view.job_id, audience="internal", path=db)["status"] == "failed"


def test_6_an_engine_failure_is_a_failure_whatever_the_rows_word(db, tmp_path):
    snap = snapshot()
    port = RunReceiptPort(db)
    core, port, _ = make_core(db, tmp_path, snap, port)

    async def refused(ctx):
        raise RuntimeError("engine fault")
    port.run_adapter = refused
    view = run(core.submit_run(request(request_id="req-6", run_receipt_path="/v1/x"), Caller(), snapshot=snap))
    run(core.execute_run(view.job_id))
    assert [c[1] for c in port.closed] == ["failed"]


# ------------------------------------------------------------------------------------------------------ findings 4 and 7


def _obs(jobs, caller, assets=()):
    return Obs(trace={"caller": caller, "media_jobs": {"media_jobs": jobs}, "assets": list(assets), "receipts": {"receipts": []}})


def _job(job_id="job_aaa", **over):
    row = {"job_id": job_id, "status": "succeeded", "intent": "image.generate", "request_id": "<req:1>",
           "job_json": {"job_id": job_id, "status": "succeeded", "metadata": {"n": 1, "size": "512x512"}, "assets": [{"asset_id": "img_1"}]},
           "metadata": {"n": 1}}
    return {**row, **over}


def _answer(job_id="job_aaa", body=None):
    body = body or {"id": job_id, "status": "succeeded", "metadata": {"n": 1}, "assets": [{"a": 1}], "results_url": f"/jobs/{job_id}/results"}
    return {"response": {"status": 200, "body": {"kind": "json", "text": json.dumps(body)}}}


def _pair(old_id="job_aaa", row_id="job_bbb"):
    return _obs([_job(old_id)], [_answer(old_id), _answer(old_id)]), _obs([_job(row_id)], [_answer(row_id), _answer(row_id)])


def _same(old, row):
    assert_same(old, row, ("job rows", job_rows), ("job document", lambda o: job_document(o, 1)))


def test_7_the_same_trace_under_other_random_ids_is_the_same():
    _same(*_pair())


def test_7_a_job_id_that_differs_only_in_the_column_fails():
    old, row = _pair()
    row.trace["media_jobs"]["media_jobs"][0]["job_id"] = "job_other"
    with pytest.raises(AssertionError):
        _same(old, row)


def test_7_nested_job_metadata_that_differs_fails():
    old, row = _pair()
    row.trace["media_jobs"]["media_jobs"][0]["job_json"]["metadata"]["size"] = "1024x1024"
    with pytest.raises(AssertionError):
        _same(old, row)


def test_7_a_cross_reference_to_another_job_fails():
    old, row = _pair()
    row.trace["caller"][1] = _answer("job_bbb", {"id": "job_bbb", "status": "succeeded", "metadata": {"n": 1}, "assets": [{"a": 1}], "results_url": "/jobs/job_zzz/results"})
    with pytest.raises(AssertionError):
        _same(old, row)


def test_7_a_field_the_old_reader_skipped_fails():
    old, row = _pair()
    row.trace["caller"][1] = _answer("job_bbb", {"id": "job_bbb", "status": "succeeded", "metadata": {"n": 1}, "assets": [{"a": 2}], "results_url": "/jobs/job_bbb/results"})
    with pytest.raises(AssertionError):
        _same(old, row)


def test_7_a_changed_value_under_the_same_keys_fails():
    old, row = _pair()
    row.trace["caller"][1] = _answer("job_bbb", {"id": "job_bbb", "status": "succeeded", "metadata": {"n": 2}, "assets": [{"a": 1}], "results_url": "/jobs/job_bbb/results"})
    with pytest.raises(AssertionError):
        _same(old, row)


def test_4_stored_files_of_the_same_size_and_other_bytes_differ():
    one = _obs([], [], [{"suffix": ".png", "size": 10, "sha256": "aa"}])
    two = _obs([], [], [{"suffix": ".png", "size": 10, "sha256": "bb"}])
    assert asset_files(one) != asset_files(two)
    assert asset_files(one) == asset_files(copy.deepcopy(one))
