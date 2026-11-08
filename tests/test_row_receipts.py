"""Builder RH: a frontend row opens one request receipt and gives History the run facts of the kept request.

The rows run over the run core; the old 3D route opened a receipt and told History what it ran, and the run core path did
neither. These tests run the real app, run core and worker over the fake engine of ``tests.support.mesh_harness`` (the
rig of the S7 mesh goldens) and read the receipt store and the kept-request table. The hook is generic: the last test
requires that ``gateway/row_routes.py`` names no domain word.
"""
from __future__ import annotations

import asyncio
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
def _only_this_tests_receipts():
    """The receipt store outlives a test (its path is bound at import); count only what this test wrote."""
    _BEFORE.clear()
    yield


@pytest.fixture(autouse=True)
def _note_receipts_before(request):
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
    assert receipt["status"] == "failed"
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


# ------------------------------------------------------------------------------------------------ (d) no domain words
def test_the_row_module_names_no_domain_literal():
    source = (ROOT / "gateway" / "row_routes.py").read_text(encoding="utf-8")
    words = r"\b(image|video|audio|sfx|music|mesh|trellis|flux|comfy|llama|vllm|ollama|whisper|tts|stt)\b"
    assert re.findall(words, source, flags=re.I) == []
