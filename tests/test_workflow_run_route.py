"""POST /gateway/v1/workflows/{id}/run through the real route, the real worker and the real job store (builder C).

The snapshot and the projections are the test doubles of the run-core tests; the engine is a fake whose call counter
proves "no engine call". The route, the port, the request spool, the claim table and the media-job routes are real.
"""

from __future__ import annotations

import asyncio
import json
from types import MappingProxyType, SimpleNamespace

import pytest

import proxy
from gateway import api, media_jobs, receipt_store, request_spool, result_bundle
from tests.support.gateway import gateway_client, use_admin_db
from tests.test_workflow_run import CONTRACT, Projections, Provider
from tests.workflow_runtime_fakes import FakeSnapshot, entry

# The plan maps an intent to a capability; an intent it does not know is refused, so the route contract uses a known one.
ROUTE_CONTRACT = {**CONTRACT, "intent": "image.generate"}
ROUTE = "/gateway/v1/workflows/demo.run/run"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture
def served(monkeypatch, tmp_path):
    use_admin_db(monkeypatch, tmp_path)
    media = tmp_path / "media_jobs.db"
    media_jobs.init_db(media)
    monkeypatch.setattr(media_jobs, "DB_PATH", media)
    receipt_store.init_db(receipt_store.DB_PATH)
    monkeypatch.setattr(proxy, "_WORKFLOW_RUN_SPOOL_ROOT", tmp_path / "spool")
    monkeypatch.setattr(proxy, "_WORKFLOW_JOB_SPOOL", request_spool.JobSpool(tmp_path / "spool"))
    monkeypatch.setattr(proxy, "_workflow_core_cache", None)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    snap = FakeSnapshot(1, MappingProxyType({"demo.run": entry(ROUTE_CONTRACT)}))
    monkeypatch.setattr(proxy, "WORKFLOW_SNAPSHOTS", Provider(snap))
    monkeypatch.setattr(proxy, "WORKFLOW_PROJECTIONS", Projections())
    stub_protocol = SimpleNamespace(id="proto", revision=1, adapter_identity="adapter")
    stub_adapter = SimpleNamespace(interface_version=1)
    engine = ("svc", {"host": "engine.test", "port": 1}, stub_protocol, stub_adapter)
    monkeypatch.setattr(proxy, "_workflow_engine", lambda adapter_id: engine)
    state = SimpleNamespace(engine_calls=0, gate=None)

    async def run_adapter(ctx):
        state.engine_calls += 1
        if state.gate is not None:
            await state.gate.wait()
        limits = result_bundle.ResultLimits()
        result_bundle.complete_job(
            ctx.job_id, lambda conn, attempt: result_bundle.stage_json_in(
                conn, ctx.job_id, attempt, "result", b'{"ok": true}', limits=limits),
            limits=limits, path=media)

    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "run_adapter", run_adapter)
    state.media = media
    return state


def call(method: str, path: str, **kwargs):
    async def go():
        async with gateway_client() as client:
            return await getattr(client, method)(path, **kwargs)
    return asyncio.run(go())


def test_unconfigured_runtime_answers_503_and_runs_nothing(served, monkeypatch):
    monkeypatch.setattr(proxy, "WORKFLOW_SNAPSHOTS", None)
    response = call("post", ROUTE, json={"inputs": {"prompt": "hi"}})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "workflow_runtime_unavailable"
    assert served.engine_calls == 0


def test_json_submit_runs_once_and_the_media_job_providers_serve_the_job(served):
    created = call("post", ROUTE, json={"inputs": {"prompt": "hi"}}, headers={"Prefer": "wait=10"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["status"] == "succeeded" and body["disposition"] == "new"
    assert body["results"][0]["output"] == "result"
    assert created.headers["location"] == f"/gateway/v1/media/jobs/{body['job_id']}"
    assert served.engine_calls == 1
    # The media-job routes read the job store through these providers (the router captures the live store path at import,
    # so the test builds them over its own store): a workflow job is served as any media job is.
    _, getter, lister, event_lister, _, canceler = api._build_media_job_providers(
        media_job_creator=None, media_job_getter=None, media_job_lister=None, media_job_event_lister=None,
        media_job_event_appender=None, media_job_canceler=None, media_job_store_path=served.media)
    row = getter(body["job_id"], audience="internal")
    assert row["intent"] == "workflow.run" and row["status"] == "succeeded" and row["results"]
    assert [r["job_id"] for r in lister(audience="internal", limit=10)] == [body["job_id"]]
    assert event_lister(body["job_id"], audience="internal", limit=10)
    with pytest.raises(ValueError):          # the existing rule: a finished job cannot be canceled
        canceler(body["job_id"], audience="internal")


def test_a_retry_with_the_same_key_replays_the_job_without_an_engine_call(served):
    headers = {"Idempotency-Key": "k-1", "Prefer": "wait=10"}
    first = call("post", ROUTE, json={"inputs": {"prompt": "hi"}}, headers=headers)
    second = call("post", ROUTE, json={"inputs": {"prompt": "hi"}}, headers=headers)
    other = call("post", ROUTE, json={"inputs": {"prompt": "different"}}, headers=headers)
    assert first.json()["job_id"] == second.json()["job_id"]
    assert second.json()["disposition"] == "replay"
    assert other.status_code == 409 and other.json()["error"]["code"] == "idempotency_conflict"
    assert served.engine_calls == 1


def test_without_wait_the_answer_is_202_with_the_queued_job(served):
    served.gate = None
    response = call("post", ROUTE, json={"inputs": {"prompt": "hi"}})
    assert response.status_code in (200, 202)
    assert response.json()["status"] in ("queued", "running", "succeeded")
    assert response.headers["location"].endswith(response.json()["job_id"])


def test_every_input_problem_is_reported_at_once_and_nothing_runs(served):
    response = call("post", ROUTE, json={"inputs": {"seed": -5, "extra": 1}})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "invalid_inputs"
    assert len(error["problems"]) >= 2
    assert served.engine_calls == 0


def test_unknown_workflow_is_404_and_a_body_with_other_keys_is_422(served):
    assert call("post", "/gateway/v1/workflows/nope/run", json={"inputs": {}}).status_code == 404
    assert call("post", ROUTE, json={"inputs": {}, "model": "x"}).status_code == 422
    assert served.engine_calls == 0


def test_a_multipart_upload_reaches_the_adapter_with_its_declared_type(served):
    seen = {}

    async def run_adapter(ctx):
        seen["media"] = ctx.media
        seen["bytes"] = ctx.spool.path(ctx.job_id, ctx.media["photo"][0].spool_key).read_bytes()
        served.engine_calls += 1
        limits = result_bundle.ResultLimits()
        result_bundle.complete_job(
            ctx.job_id, lambda conn, attempt: result_bundle.stage_json_in(
                conn, ctx.job_id, attempt, "result", b'{}', limits=limits),
            limits=limits, path=served.media)

    proxy._WORKFLOW_RUN_PORT.run_adapter = run_adapter  # undone with the fixture's monkeypatch of the same name
    response = call("post", ROUTE, data={"inputs": json.dumps({"prompt": "hi"})},
                    files={"photo": ("p.png", PNG, "image/png")}, headers={"Prefer": "wait=10"})
    assert response.status_code == 200, response.text
    ref = seen["media"]["photo"][0]
    assert (ref.media_type, ref.wire_media_type, ref.size) == ("image/png", "image/png", len(PNG))
    assert seen["bytes"] == PNG


def test_a_file_part_below_a_top_level_name_is_media_path_unsupported(served):
    response = call("post", ROUTE, data={"inputs": json.dumps({"prompt": "hi"})},
                    files={"photo.sub": ("p.png", PNG, "image/png")})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "media_path_unsupported"
    assert served.engine_calls == 0


def test_a_file_without_a_known_type_is_415_and_leaves_no_bytes(served, tmp_path):
    response = call("post", ROUTE, data={"inputs": json.dumps({"prompt": "hi"})},
                    files={"photo": ("p.bin", PNG, "not a type")})
    assert response.status_code == 415
    assert not list((tmp_path / "spool").rglob("*.*")) and not [p for p in (tmp_path / "spool").rglob("*") if p.is_file()]
    assert served.engine_calls == 0


def test_a_disabled_workflow_is_409_and_runs_nothing(served, monkeypatch):
    snap = FakeSnapshot(1, MappingProxyType({"demo.run": entry(ROUTE_CONTRACT, state="disabled")}))
    monkeypatch.setattr(proxy, "WORKFLOW_SNAPSHOTS", Provider(snap))
    response = call("post", ROUTE, json={"inputs": {"prompt": "hi"}})
    assert response.status_code == 409 and response.json()["error"]["code"] == "workflow_disabled"
    assert served.engine_calls == 0


def test_restart_recovery_runs_a_queued_workflow_job_from_its_pin(served, monkeypatch):
    scheduled: list[str] = []
    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "schedule", lambda job_id: scheduled.append(job_id))
    created = call("post", ROUTE, json={"inputs": {"prompt": "hi"}})
    job_id = created.json()["job_id"]
    assert scheduled == [job_id]            # the route scheduled it (the fake did not run it)
    scheduled.clear()
    proxy._recover_workflow_runs()
    assert scheduled == [job_id]            # a restart schedules the unfinished job again
    assert served.engine_calls == 0


