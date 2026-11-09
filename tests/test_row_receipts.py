"""Builder RH: a frontend row opens one request receipt and gives History the run facts of the kept request.

The rows run over the run core; the old 3D route opened a receipt and told History what it ran, and the run core path did
neither. These tests run the real app, run core and worker over the fake engine of ``tests.support.mesh_harness`` (the
rig of the S7 mesh goldens) and read the receipt store and the kept-request table. The hook is generic: the last test
requires that ``gateway/row_routes.py`` names no domain word.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

import proxy
from gateway import admin_store, media_jobs, receipt_store, retention
from gateway.receipt_store import GATEWAY_RESTARTED
from gateway.receipts import RouteReceipt
from tests.support.access import authenticated, identity, plan_for
from tests.support.gateway import gateway_client
from tests.test_adapter_differential_s7_mesh_rows import (  # noqa: F401  (rig is a fixture)
    ASYNC, RAW, SYNC, Gate, form, job_id_of, rig, settle)
from tests.test_request_history_capture import _kept_rows, _mint_member_key

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = "mesh.generate.trellis-2"
TITLE = "TRELLIS.2 image to 3D"
GATE_BOUND_S = 15.0


def run_async(scenario):
    async def go():
        async with gateway_client() as client, asyncio.timeout(GATE_BOUND_S * 2):
            return await scenario(client)
    return asyncio.run(go())


_BEFORE: set[str] = set()


@pytest.fixture(autouse=True)
def _only_this_tests_receipts(request):
    """The receipt store outlives a test (its path is bound at import); count only what this test wrote."""
    _BEFORE.clear()
    if "rig" in request.fixturenames:
        request.getfixturevalue("rig")
        _BEFORE.update(r["request_id"] for r in receipt_store.recent(limit=5000))


def receipts(path: str | None = None) -> list[dict[str, Any]]:
    return [r for r in receipt_store.recent(limit=5000)
            if r["request_id"] not in _BEFORE and (path is None or r.get("path") == path)]


async def closed(request_id: str, bound_s: float = GATE_BOUND_S) -> dict[str, Any]:
    """The receipt once it is no longer in progress."""
    deadline = time.monotonic() + bound_s
    while True:
        found = receipt_store.get(request_id)
        if found is not None and found["status"] != "in_progress":
            return found
        assert time.monotonic() < deadline, f"receipt {request_id} stayed open: {found}"
        await asyncio.sleep(0.02)


def reason(receipt: dict[str, Any]) -> str | None:
    return (receipt.get("metadata") or {}).get("terminal_reason")


# ------------------------------------------------------------------------------------------------ (a) History
@pytest.fixture
def remembered(rig, monkeypatch):
    """The App's person remembers their requests, so the capture keeps what they send."""
    person = admin_store.upsert_oidc_user("https://fixture.test", "fixture-person", path=proxy.ADMIN_DB_PATH)
    admin_store.put_prompt_memory_preference(
        person["id"], remember=True,
        retention=retention.bundled().member_defaults(retention.REQUEST_HISTORY)["retention"],
        group=retention.GROUP_REQUEST_HISTORY, path=proxy.ADMIN_DB_PATH)
    _mint_member_key(proxy.ADMIN_DB_PATH, person["id"])  # a personal App key: the only kind whose requests have a person
    return rig


def kept_summary(bound_s: float = GATE_BOUND_S) -> dict[str, Any]:
    deadline = time.monotonic() + bound_s
    while True:
        rows = _kept_rows(media_jobs.DB_PATH)
        if rows:
            assert len(rows) == 1
            return json.loads(rows[0]["summary_json"])
        assert time.monotonic() < deadline, "no kept request"
        time.sleep(0.05)


def test_a_kept_raw_row_request_records_the_run_facts_and_the_stored_model(remembered):
    async def go(c):
        response = await c.post(RAW, **form())
        assert response.status_code == 200, response.text
        return response

    response = run_async(go)
    run = kept_summary()["run"]
    assert run["asset_id"].startswith("ast_")
    assert run == {"workflow": WORKFLOW, "title": TITLE, "capability": "mesh", "capability_label": "3D models",
                   "asset_id": run["asset_id"]}
    # The asset is the stored model: the Library holds the bytes the caller received.
    assert response.headers["x-request-id"].startswith("req_")


def test_a_kept_async_row_request_records_the_run_facts_without_an_asset(remembered):
    async def go(c):
        created = await c.post(SYNC, headers=ASYNC, **form())
        await settle(c, job_id_of(created))
        return created

    created = run_async(go)
    assert created.status_code == 202
    assert kept_summary()["run"] == {"workflow": WORKFLOW, "title": TITLE, "capability": "mesh",
                                     "capability_label": "3D models"}


# ------------------------------------------------------------------------------------------------ (b) one receipt each
def test_a_sync_success_opens_one_receipt_and_closes_it_succeeded(rig):
    async def go(c):
        response = await c.post(RAW, **form())
        return response.status_code, await closed(response.headers["x-request-id"])

    status, receipt = run_async(go)
    assert status == 200
    assert [r["request_id"] for r in receipts(RAW)] == [receipt["request_id"]]
    assert (receipt["status"], reason(receipt)) == ("succeeded", "succeeded")
    # The receipt names the record the row chose, and the row's wire.
    assert receipt["resolved_workflow"] == WORKFLOW
    assert receipt["protocol"]
    # The intent is the route's declared one (the old 3D route recorded mesh.generate), not the core's run intent:
    # usage reads the capability from it.
    assert receipt["intent"] == "mesh.generate"


def test_an_async_success_is_closed_by_the_core_when_the_job_ends(rig):
    gate = Gate(rig)

    async def go(c):
        created = await c.post(SYNC, headers=ASYNC, **form())
        request_id = created.headers["x-request-id"]
        await gate.started.wait()
        # The job runs and the request is long gone: the receipt is still open, held by the core.
        assert receipt_store.get(request_id)["status"] == "in_progress"
        gate.release.set()
        await settle(c, job_id_of(created))
        return await closed(request_id)

    receipt = run_async(go)
    assert (receipt["status"], reason(receipt)) == ("succeeded", "succeeded")
    assert len(receipts(SYNC)) == 1


def test_an_engine_failure_closes_one_receipt_failed(rig):
    rig.downstream_mode = 502

    async def go(c):
        response = await c.post(RAW, **form())
        return response.status_code, await closed(response.headers["x-request-id"])

    status, receipt = run_async(go)
    assert status == 502
    assert receipt["status"] == "failed"
    assert reason(receipt) == "upstream_error"
    assert len(receipts(RAW)) == 1


def test_an_async_engine_failure_closes_its_receipt_failed_at_the_job_end(rig):
    rig.downstream_mode = 502

    async def go(c):
        created = await c.post(SYNC, headers=ASYNC, **form())
        await settle(c, job_id_of(created))
        return await closed(created.headers["x-request-id"])

    receipt = run_async(go)
    assert (receipt["status"], reason(receipt)) == ("failed", "upstream_error")


def test_a_busy_pool_answers_503_and_closes_one_receipt_failed(rig):
    rig.run_slots(4)
    gate = Gate(rig)

    async def go(c):
        first = await c.post(SYNC, headers=ASYNC, **form())
        await gate.started.wait()
        second = await c.post(RAW, **form())
        busy = (second.status_code, await closed(second.headers["x-request-id"]))
        gate.release.set()
        await settle(c, job_id_of(first))
        return busy

    status, receipt = run_async(go)
    assert status == 503
    assert (receipt["status"], reason(receipt)) == ("failed", "resource_unavailable")
    assert len(receipts(RAW)) == 1


@pytest.mark.parametrize("name", ["seed_negative", "missing_image", "not_multipart", "not_a_picture"])
def test_a_refused_body_closes_one_receipt_failed(rig, name):
    bodies = {
        "seed_negative": dict(form("-1")),
        "missing_image": dict(files={"seed": (None, "1")}),
        "not_multipart": dict(content=b"not a multipart body", headers={"content-type": "multipart/form-data; boundary=zz"}),
        "not_a_picture": dict(files={"image": ("photo.png", b"plain text", "image/png")}),
    }

    async def go(c):
        response = await c.post(RAW, **bodies[name])
        return response.status_code, await closed(response.headers["x-request-id"])

    status, receipt = run_async(go)
    assert status in (400, 422)
    assert receipt["status"] == "failed"      # the mesh row declares no refusal vocabulary: the old route closed these failed
    assert len(receipts(RAW)) == 1
    assert rig.downstream == []


def test_a_client_that_leaves_while_the_engine_works_closes_one_receipt_cancelled(rig):
    gate = Gate(rig)

    async def go():
        async with gateway_client() as client:
            task = asyncio.ensure_future(client.post(RAW, **form()))
            await gate.started.wait()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            [open_one] = receipts(RAW)
            receipt = await closed(open_one["request_id"])
            gate.release.set()
            while rig.leases.active_claims():
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)
            return receipt, receipt_store.get(receipt["request_id"])

    receipt, later = asyncio.run(go())
    assert receipt["status"] == "cancelled"
    # The job ends afterwards; it does not rewrite the closed receipt.
    assert (later["status"], reason(later)) == ("cancelled", "cancelled")
    assert len(receipts(RAW)) == 1


def test_a_route_policy_denial_opens_no_receipt_and_calls_no_engine(rig):
    async def go(c):
        with authenticated(identity(plan_for(caps=("chat",)))):
            return await c.post(RAW, **form())

    response = run_async(go)
    assert response.status_code == 403
    assert receipts(RAW) == []
    assert rig.downstream == []


def test_a_replayed_async_request_closes_its_own_receipt_and_the_job_closes_the_first(rig):
    async def go(c):
        headers = {**ASYNC, "Idempotency-Key": "k-1"}
        first = await c.post(SYNC, headers=headers, **form())
        await settle(c, job_id_of(first))
        second = await c.post(SYNC, headers=headers, **form())
        assert job_id_of(second) == job_id_of(first)
        return await closed(first.headers["x-request-id"]), await closed(second.headers["x-request-id"])

    first, second = run_async(go)
    assert first["request_id"] != second["request_id"]
    assert first["status"] == "succeeded"
    assert second["status"] == "succeeded"
    assert len(receipts(SYNC)) == 2


def test_async_replay_of_a_running_job_closes_its_receipt_as_accepted(rig):
    """K3-4: a respond-async replay of a job that still runs answers 202; its own receipt closes accepted, not as a wait
    timeout (no wait happened). The first request's receipt stays held by the core until the job ends."""
    gate = Gate(rig)

    async def go(c):
        headers = {**ASYNC, "Idempotency-Key": "K-run"}
        first = await c.post(SYNC, headers=headers, **form())
        await gate.started.wait()
        second = await c.post(SYNC, headers=headers, **form())
        assert second.status_code == 202 and job_id_of(second) == job_id_of(first)
        replay = await closed(second.headers["x-request-id"])
        assert receipt_store.get(first.headers["x-request-id"])["status"] == "in_progress"
        gate.release.set()
        await settle(c, job_id_of(first))
        return replay, await closed(first.headers["x-request-id"])

    replay, first = run_async(go)
    assert (replay["status"], reason(replay)) == ("succeeded", "accepted")
    assert (first["status"], reason(first)) == ("succeeded", "succeeded")


class _Edited:
    """A snapshot provider that, once ``edited`` is set, serves the workflow as a later revision with another title."""

    def __init__(self, inner):
        self._inner, self.edited = inner, False

    def current(self):
        snap = self._inner.current()
        if not self.edited:
            return snap
        edited = self

        class Snap:
            def get(self, workflow_id):
                entry = snap.get(workflow_id)
                if entry is None:
                    return None
                return dataclasses.replace(entry, record_revision=entry.record_revision + 1,
                                           contract_digest="sha256:" + "e" * 64,
                                           contract={**entry.contract, "display_name": "Edited title"})

            def __getattr__(self, name):
                return getattr(snap, name)

        return Snap()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_replay_receipt_and_history_name_the_pinned_revision(remembered, monkeypatch):
    """K3-5: after the workflow is edited, a replay of the finished job names the revision, digest and title the job
    pinned, not the current entry's."""
    live = _Edited(proxy.WORKFLOW_SNAPSHOTS)
    monkeypatch.setattr(proxy, "WORKFLOW_SNAPSHOTS", live)

    async def go(c):
        headers = {**ASYNC, "Idempotency-Key": "K-rev"}
        first = await c.post(SYNC, headers=headers, **form())
        await settle(c, job_id_of(first))
        first_receipt = await closed(first.headers["x-request-id"])
        live.edited = True
        second = await c.post(SYNC, headers=headers, **form())
        assert job_id_of(second) == job_id_of(first)
        return first_receipt, await closed(second.headers["x-request-id"])

    first, replay = run_async(go)
    assert replay["manifest_version"] == first["manifest_version"]
    assert replay["manifest_hash"] == first["manifest_hash"] and replay["resolved_workflow"] == WORKFLOW
    rows = _kept_rows(media_jobs.DB_PATH)
    assert len(rows) == 2
    titles = {json.loads(r["summary_json"])["run"]["title"] for r in rows}
    assert titles == {TITLE}


# ------------------------------------------------------------------------------------------------ S7 parity 6.2, 6.4
def test_s7_same_key_replays_after_backend_becomes_unavailable(rig):
    async def go(c):
        headers = {**ASYNC, "Idempotency-Key": "K"}
        first = await c.post(SYNC, headers=headers, **form())
        await settle(c, job_id_of(first))
        calls = len(rig.downstream)
        rig.services["trellis"]["enabled"] = False
        again = await c.post(SYNC, headers=headers, **form())
        fresh = await c.post(SYNC, headers={**ASYNC, "Idempotency-Key": "K2"}, **form())
        return first, again, fresh, calls

    first, again, fresh, calls = run_async(go)
    assert again.status_code in (200, 202)
    assert job_id_of(again) == job_id_of(first)
    assert len(rig.downstream) == calls
    assert fresh.status_code == 503


def test_s7_combined_refusals_follow_the_row_order(rig):
    """Authentication, route policy, body validation, service state: the first that applies answers."""
    rig.services["trellis"]["enabled"] = False
    bad_body = dict(files={"seed": (None, "1")})

    async def go(c):
        with authenticated(identity(plan_for(caps=("chat",)))):
            policy = await c.post(RAW, **bad_body)
        body = await c.post(RAW, **bad_body)
        service = await c.post(RAW, **form())
        return policy, body, service

    policy, body, service = run_async(go)
    assert policy.status_code == 403
    assert body.status_code in (400, 422)
    assert service.status_code == 503
    assert rig.downstream == []


# ------------------------------------------------------------------------------------------------ (c) recovery
def test_a_run_receipt_left_open_by_a_stopped_gateway_is_closed_as_restarted(rig):
    request_id = "req_" + "e" * 32
    receipt_store.begin(RouteReceipt(protocol="p", intent="i", path=RAW, request_id=request_id))
    media_jobs.create(media_jobs.MediaJob(intent="workflow.run", workflow=WORKFLOW, backend="b", request_id=request_id,
                                          metadata={"mode": "workflow.run"}), path=media_jobs.DB_PATH)
    assert receipt_store.get(request_id)["status"] == "in_progress"

    proxy._recover_workflow_runs()

    after = receipt_store.get(request_id)
    assert (after["status"], reason(after)) == ("failed", GATEWAY_RESTARTED)


def test_restart_closes_open_row_receipts_without_an_unfinished_job(rig):
    """K3-6: a receipt opened before its job exists (a restart during the body upload) and a receipt whose job ended
    before the receipt closed are both closed as restarted at startup; a receipt on another path is left alone."""
    no_job, ended, other = ("req_" + c * 32 for c in "abc")
    receipt_store.begin(RouteReceipt(protocol="p", intent="i", path=RAW, request_id=no_job))
    receipt_store.begin(RouteReceipt(protocol="p", intent="i", path=SYNC, request_id=ended))
    receipt_store.begin(RouteReceipt(protocol="p", intent="i", path="/v1/not-a-row", request_id=other))
    job = media_jobs.create(media_jobs.MediaJob(intent="workflow.run", workflow=WORKFLOW, backend="b", request_id=ended,
                                                metadata={"mode": "workflow.run"}), path=media_jobs.DB_PATH)
    media_jobs.update_status(job.job_id, "running", path=media_jobs.DB_PATH)
    media_jobs.update_status(job.job_id, "succeeded", path=media_jobs.DB_PATH)

    proxy._recover_workflow_runs()

    for request_id in (no_job, ended):
        after = receipt_store.get(request_id)
        assert (after["status"], reason(after)) == ("failed", GATEWAY_RESTARTED)
    assert receipt_store.get(other)["status"] == "in_progress"


def test_restart_leaves_a_receipt_a_live_request_opened_after_recovery_began(rig):
    """K3 recheck P2: the sweep closes receipts the stopped gateway left, not one a live request opens. Here the request
    is accepted a minute after recovery starts, so it must stay open and finalize as succeeded."""
    stopped, live = ("req_" + c * 32 for c in "fg")
    now_ms = int(time.time() * 1000)
    receipt_store.begin(RouteReceipt(protocol="p", intent="i", path=RAW, request_id=stopped), now_ms=now_ms - 5000)
    receipt_store.begin(RouteReceipt(protocol="p", intent="i", path=RAW, request_id=live), now_ms=now_ms + 60_000)

    proxy._recover_workflow_runs()

    after = receipt_store.get(stopped)
    assert (after["status"], reason(after)) == ("failed", GATEWAY_RESTARTED)
    assert receipt_store.get(live)["status"] == "in_progress"
    done = {"request_id": live, "status": "succeeded", "updated_at_ms": now_ms + 61_000,
            "metadata": {"terminal_reason": "succeeded"}}
    assert receipt_store.finalize(done) is True
    assert receipt_store.get(live)["status"] == "succeeded"


# ------------------------------------------------------------------------------------------------ (d) no domain words
def test_the_row_module_names_no_domain_literal():
    source = (ROOT / "gateway" / "row_routes.py").read_text(encoding="utf-8")
    words = r"\b(image|video|audio|sfx|music|mesh|trellis|flux|comfy|llama|vllm|ollama|whisper|tts|stt)\b"
    assert re.findall(words, source, flags=re.I) == []


def test_route_intent_is_the_one_value_for_receipt_and_job_row():
    """The receipt and the job row file under the matched route's declared intent; a route with none keeps the run intent."""
    import dataclasses
    from gateway import frontend_shapes, row_routes, workflow_run
    rows = {row.id: row for row in frontend_shapes.bundled_rows()}
    jobs_route = rows["image-jobs"].routes[0]
    assert row_routes.route_intent(rows["image-jobs"], jobs_route.canonical) == "image.generate"
    bare = dataclasses.replace(jobs_route, runtime_policy={})
    assert row_routes.route_intent(dataclasses.replace(rows["image-jobs"], routes=(bare,)), jobs_route.canonical) == workflow_run.RUN_INTENT
    # A run job is recognised by its mode, whatever intent its route filed it under; other jobs are not.
    assert workflow_run.is_run_job({"intent": "image.generate", "metadata": {"mode": workflow_run.RUN_INTENT}})
    assert workflow_run.is_run_job({"intent": workflow_run.RUN_INTENT})
    assert not workflow_run.is_run_job({"intent": "image.generate", "metadata": {"mode": "async_image_generation"}})


def test_the_refusal_vocabulary_is_row_data_and_only_the_image_rows_declare_the_image_words():
    """A row says how a refusal of the request (a 4xx) closes its receipt. The old image routes wrote ``rejected``; every
    other row keeps ``failed`` unless its data says otherwise."""
    from gateway.frontend_shapes import bundled_rows
    words = {row.id: row.refusal_receipt for row in bundled_rows()}
    image_rows = {"images-generations", "images-edits", "images-variations", "image-jobs"}
    assert {rid for rid, word in words.items() if word == "rejected"} == image_rows
    assert {word for word in words.values()} <= {"rejected", "failed"}


def test_a_run_receipt_path_is_row_data_kept_in_the_snapshot_and_only_the_image_job_row_declares_it():
    """A job whose old handler ran the sync route inside the job files a second receipt for the run under that route's path
    (``run_receipt_path``). The path is row data: the snapshot reads it, and it changes the row's digest only when set."""
    from gateway import registry_snapshot
    from gateway.frontend_shapes import bundled_rows
    declared = {row.id: row.run_receipt_path for row in bundled_rows() if row.run_receipt_path}
    assert declared == {"image-jobs": "/v1/images/generations"}
    raw = {"id": "x", "shape": "openai.images.jobs", "mode": "job", "receipt_protocol": "openai.images",
           "routes": [{"methods": ["POST"], "canonical": "/x", "runtime_policy": {}}]}
    plain = registry_snapshot.frontend_row(raw)
    kept = registry_snapshot.frontend_row({**raw, "run_receipt_path": "/y"})
    assert plain.run_receipt_path == "" and kept.run_receipt_path == "/y"
    assert registry_snapshot._row_value(plain) != registry_snapshot._row_value(kept)
    assert "run_receipt_path" not in registry_snapshot._row_value(plain)
