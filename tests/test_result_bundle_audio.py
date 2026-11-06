"""The audio job route publishes a result bundle that mirrors its assets (S2.3).

The rig is the adapter-differential one: the real app, a fake engine and a temporary jobs database. The route keeps its
``assets`` list; its client output does not change (the wire goldens are in tests/test_adapter_differential.py). The
bundle holds the same files under one name, and the job's receipt carries its summary.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import proxy
from gateway import media_jobs, receipt_store, result_bundle
from tests.support.adapter_differential_rig import GatewayRig, comfy_exchanges, history_done, scenario
from tests.support.config_snapshot import publish_section
from tests.test_adapter_differential import _wav

COMFY_HOST = "comfy.fk.test"
CAPABILITY = proxy._AUDIO_GRAPH_CAPABILITY


@pytest.fixture
def rig(tmp_path: Path):
    with GatewayRig("result-bundle-audio", tmp_path) as made:
        made.set(proxy, "_schedule_audio_media_job", lambda job_id: made.scheduled.append(job_id) or True)
        fake = made.engine("comfy", scenario("comfyui", "result-bundle-audio", comfy_exchanges(
            history=history_done("audio", "tone_00001_.wav"), views=[(_wav(), "audio/wav")], strict=False)),
            host=COMFY_HOST)
        made.set(proxy, "COMFY_HOST", fake.base_url)
        yield made


def settings(rig: GatewayRig, **limits: int) -> None:
    publish_section(rig.monkeypatch, "workflow_execution",
                    {"compile_from_pin": {CAPABILITY: True}, **({"result_limits": limits} if limits else {})})


async def accept(rig: GatewayRig, prompt: str = "a door creaks") -> str:
    made = await rig.call("POST", "/gateway/v1/media/jobs", label="submit the audio job", unordered_json=True,
                          json_body={"intent": "audio.generate",
                                     "request": {"prompt": prompt, "model": "tangoflux", "duration": 4}})
    assert made.status_code == 202, made.text
    return made.json()["job_id"]


def job(job_id: str) -> dict[str, Any]:
    found = media_jobs.get(job_id, audience="internal", path=media_jobs.DB_PATH)
    assert found is not None
    return found


def rows(sql: str, *args: Any) -> list[tuple]:
    with sqlite3.connect(media_jobs.DB_PATH) as conn:
        return conn.execute(sql, args).fetchall()


def run_job(rig: GatewayRig, prompt: str = "a door creaks") -> str:
    async def go() -> str:
        job_id = await accept(rig, prompt)
        await proxy._run_audio_media_job(job_id)
        return job_id

    return asyncio.run(go())


def test_a_finished_audio_job_publishes_a_bundle_that_mirrors_its_assets(rig: GatewayRig):
    settings(rig)
    job_id = run_job(rig)
    done = job(job_id)
    assert done["status"] == media_jobs.STATUS_SUCCEEDED, done.get("error")
    (asset,) = done["assets"]
    summary = result_bundle.bundle_summary(job_id, path=media_jobs.DB_PATH)
    assert summary["results"] == [{"name": "assets", "kind": "assets", "count": 1, "size_bytes": asset["size_bytes"]}]
    assert rows("SELECT name, state, items_json FROM result_stage WHERE job_id = ?", job_id) == [
        ("assets", "published", json.dumps([{"asset_ref": asset["asset_id"], "media_type": asset["media_type"],
                                             "size_bytes": asset["size_bytes"]}], sort_keys=True, separators=(",", ":")))]
    # The pin is recorded in the envelope the summary names.
    assert summary["envelope"]["source_kind"] == "workflow" and summary["envelope"]["adapter_id"]


def test_the_job_view_is_what_it_was_and_gains_no_results_field(rig: GatewayRig):
    """The D4 ``results`` field is a later step. The client view of a finished job keeps its shape."""
    settings(rig)
    job_id = run_job(rig)
    client = media_jobs.get(job_id, audience="client", path=media_jobs.DB_PATH)
    assert set(client) == {"request_id", "job_id", "intent", "status", "progress", "assets", "error", "created_at",
                           "updated_at", "parent_request_id", "accepted_at", "queued_at", "started_at",
                           "first_output_at", "finished_at", "error_code", "error_category", "retryable",
                           "upstream_http_status", "attempt_number", "previous_attempts", "cancel_requested"}
    assert set(client["assets"][0]) == {"asset_id", "kind", "media_type", "size_bytes", "index"}
    admin = media_jobs.get(job_id, audience="admin", path=media_jobs.DB_PATH)
    assert "results" not in admin and "bundle" not in json.dumps(admin)


def test_the_receipt_carries_the_summary_for_the_operator_only(rig: GatewayRig):
    settings(rig)
    job_id = run_job(rig)
    request_id = job(job_id)["request_id"]
    internal = receipt_store.get(request_id, audience="internal", path=receipt_store.DB_PATH)
    assert internal["metadata"]["result_bundle"] == result_bundle.bundle_summary(job_id, path=media_jobs.DB_PATH)
    for audience in ("client", "admin"):
        shown = receipt_store.get(request_id, audience=audience, path=receipt_store.DB_PATH)
        assert "result_bundle" not in json.dumps(shown), audience
    assert rows("SELECT receipt_linked_at IS NOT NULL FROM result_bundle WHERE job_id = ?", job_id) == [(1,)]


def test_a_crash_before_the_receipt_link_is_repaired_by_the_startup_scan(rig: GatewayRig):
    settings(rig)

    def crash(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("the process died before the link")

    link = result_bundle.link_receipt
    rig.set(result_bundle, "link_receipt", crash)
    job_id = run_job(rig)
    assert job(job_id)["status"] == media_jobs.STATUS_SUCCEEDED, "the job is done; the link is the scan's"
    request_id = job(job_id)["request_id"]
    assert "result_bundle" not in receipt_store.get(request_id, path=receipt_store.DB_PATH)["metadata"]
    rig.set(result_bundle, "link_receipt", link)  # the real link again, as after a restart
    # The scan runs from the recovery that every start runs.
    proxy._recover_media_jobs()
    linked = receipt_store.get(request_id, path=receipt_store.DB_PATH)["metadata"]["result_bundle"]
    assert linked["results"][0]["name"] == "assets"


def test_recovery_deletes_the_stage_of_a_dead_attempt_before_any_job_resumes(rig: GatewayRig):
    settings(rig)
    media_jobs.create(media_jobs.MediaJob(job_id="dead", intent="thing.make", status="running", app_id="a",
                                          owner_key_hash="k"), path=media_jobs.DB_PATH, maker_from_request=False)
    with media_jobs.connection(media_jobs.DB_PATH) as conn:
        conn.execute("BEGIN IMMEDIATE")
        result_bundle.stage_json_in(conn, "dead", 1, "x", b'"canary"', limits=result_bundle.ResultLimits())
        conn.commit()
    assert rows("SELECT COUNT(*) FROM result_json") == [(1,)]
    proxy._recover_media_jobs()
    assert rows("SELECT COUNT(*) FROM result_stage") == [(0,)] and rows("SELECT COUNT(*) FROM result_json") == [(0,)]


def test_a_result_over_a_limit_fails_the_job_and_saves_no_file(rig: GatewayRig):
    settings(rig, max_asset_bytes=10)
    saved: list[bytes] = []
    real = proxy.store_asset_bytes
    rig.set(proxy, "store_asset_bytes", lambda content, **options: saved.append(content) or real(content, **options))
    job_id = run_job(rig)
    failed = job(job_id)
    assert failed["status"] == media_jobs.STATUS_FAILED
    assert failed["error_code"] == "result_refused" and failed["retryable"] is False
    assert failed["assets"] == [] and saved == [], "the limit is checked before the file is saved"
    assert rows("SELECT COUNT(*) FROM result_stage") == [(0,)] and rows("SELECT COUNT(*) FROM result_bundle") == [(0,)]


def test_a_total_over_the_bundle_limit_at_publication_fails_the_job_and_aborts_the_stage(rig: GatewayRig):
    """The files pass their own checks; the bundle check at publication refuses. Nothing is published."""
    settings(rig)
    real = result_bundle.publish_in

    def tighter(conn, job_id, attempt, *, limits, now=None):
        return real(conn, job_id, attempt, limits=result_bundle.ResultLimits(max_total_bytes=1), now=now)

    rig.set(result_bundle, "publish_in", tighter)
    job_id = run_job(rig)
    failed = job(job_id)
    assert failed["status"] == media_jobs.STATUS_FAILED and failed["error_code"] == "result_refused"
    assert rows("SELECT state FROM result_stage WHERE job_id = ?", job_id) == [("aborted",)]
    assert rows("SELECT COUNT(*) FROM result_bundle") == [(0,)]


def test_a_cancel_that_has_not_settled_publishes_nothing(rig: GatewayRig):
    settings(rig)

    async def go() -> str:
        job_id = await accept(rig)
        media_jobs.update_status(job_id, media_jobs.STATUS_RUNNING, metadata_patch={"cancel_requested_at": 1},
                                 path=media_jobs.DB_PATH)
        await proxy._run_audio_media_job(job_id)
        return job_id

    job_id = asyncio.run(go())
    assert job(job_id)["status"] != media_jobs.STATUS_SUCCEEDED
    assert rows("SELECT COUNT(*) FROM result_bundle") == [(0,)]
    assert all(state != "published" for (state,) in rows("SELECT state FROM result_stage"))
