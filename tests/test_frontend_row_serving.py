"""A modality route served by its frontend row through the run core (CODE_TO_DATA_S4_S8 section 8, builder D5).

The row, the shape, the snapshot, the engine lookup, the request spool, the claim table and the job store are real. The
engine is a service record the deployment declares (the way a deployment declares one); the adapter that talks to it is a
double that records what the job pinned, so a test sees exactly what the run reached the engine with.
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

import proxy
from gateway import frontend_shapes, media_jobs, receipt_store, request_spool, result_bundle
from tests.support.gateway import gateway_client, use_admin_db

ROW = "images-generations"
def _png() -> bytes:
    import io

    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 40, 40)).save(out, format="PNG")
    return out.getvalue()


PNG = _png()
CONFIGURED_DEFAULT = "krea2-turbo"      # a deployment names its default by the short form of a workflow id


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
    # The deployment declares its engine as a service record; the run core finds it by the protocol the record names.
    monkeypatch.setitem(proxy.config, "backends", {**(proxy.config.get("backends") or {}), "engine-under-test": {
        "enabled": True, "host": "engine.test", "port": 80, "protocol": "node-graph"}})
    monkeypatch.setitem(proxy.runtime().derived, "COMFY_DEFAULT_MODEL", CONFIGURED_DEFAULT)
    state = SimpleNamespace(seen=[], media=media)

    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "cache")

    async def run_adapter(ctx):
        state.seen.append(SimpleNamespace(workflow=ctx.pin.envelope.source_id, inputs=dict(ctx.inputs)))
        asset_id = proxy._store_asset_bytes(PNG, owner_key_hash=f"app:{ctx.caller.identity.app_id}", keep_prompt=False)
        limits = result_bundle.ResultLimits()
        result_bundle.complete_job(
            ctx.job_id, lambda conn, attempt: result_bundle.stage_assets_in(
                conn, ctx.job_id, attempt, "image", [{"asset_ref": asset_id, "media_type": "image/png", "size_bytes": len(PNG)}],
                limits=limits),
            limits=limits, path=media)

    monkeypatch.setattr(proxy._WORKFLOW_RUN_PORT, "run_adapter", run_adapter)
    return state


def post(body: dict, monkeypatch: pytest.MonkeyPatch) -> httpx.Response:
    """POST through the real app, so the request runs under the middleware a modality route runs under; the route that
    serves it is a test route that hands the request to the helper the modality handlers use."""
    async def endpoint(request: Request) -> Response:
        auth = await asyncio.to_thread(proxy.authenticate_request, request, "/v1/images/generations")
        if isinstance(auth, Response):
            return auth
        return await proxy._serve_frontend_row(ROW, request, auth)

    monkeypatch.setattr(proxy.app.router, "routes", [Route("/__serve_row", endpoint, methods=["POST"]), *proxy.app.router.routes])

    async def go():
        async with gateway_client() as client:
            return await client.post("/__serve_row", json=body)
    return asyncio.run(go())


def test_the_row_runs_the_deployment_default_on_the_engine_the_deployment_declares(served, monkeypatch):
    response = post({"prompt": "a red fox", "size": "512x768", "background": "opaque", "n": 1, "response_format": "b64_json"}, monkeypatch)
    assert response.status_code == 200, response.text
    assert [(s.workflow, s.inputs["prompt"], s.inputs["width"], s.inputs["height"]) for s in served.seen] == [
        ("image.generate.krea2-turbo", "a red fox", 512, 768)]
    assert not {"n", "background", "response_format", "size"} & set(served.seen[0].inputs)     # wire fields, not inputs
    item = response.json()["data"][0]
    assert item["media_type"] == "image/png" and item["asset_id"].startswith("img_")
    assert base64.b64decode(item["b64_json"]) == PNG


def test_a_model_the_caller_names_wins_over_the_default(served, monkeypatch):
    response = post({"prompt": "a red fox", "model": "image.generate.flux2-klein"}, monkeypatch)
    assert response.status_code == 200, response.text
    assert [s.workflow for s in served.seen] == ["image.generate.flux2-klein"]


def test_a_deployment_that_declares_no_engine_refuses_before_a_job_exists(served, monkeypatch):
    monkeypatch.setitem(proxy.config, "backends", {})
    response = post({"prompt": "a red fox"}, monkeypatch)
    assert response.status_code == 503 and response.json()["error"]["code"] == "binding_unavailable"
    assert served.seen == []


def test_the_bundled_rows_are_served_by_this_helper_only_when_the_row_exists(served):
    assert "images-generations" in {row.id for row in frontend_shapes.bundled_rows()}
    async def go():
        request = Request({"type": "http", "method": "POST", "path": "/", "query_string": b"", "headers": []})
        return await proxy._serve_frontend_row("no-such-row", request, None)
    response = asyncio.run(go())
    assert response.status_code == 503 and json.loads(response.body)["error"]["code"] == "workflow_runtime_unavailable"


def test_a_caller_whose_plan_grants_no_media_is_refused_by_the_rows_policy_before_a_job_exists(served, monkeypatch):
    from tests.support.access import authenticated, identity, plan_for
    with authenticated(identity(plan_for(caps=("chat",)))):
        response = post({"prompt": "a red fox"}, monkeypatch)
    assert response.status_code == 403, response.text
    error = response.json()["error"]
    assert error["type"] == "access_error" and error["code"] == "not_in_access_plan"
    assert served.seen == []
