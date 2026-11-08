"""S7 acceptance goldens for the 3D rows (``POST /v1/3d/generations`` and ``POST /v1/3d/generations/glb``).

Smoke version; replaced below.
"""
from __future__ import annotations

import asyncio
import dataclasses

import pytest

import proxy
from gateway import media_jobs, multipart_adapter, receipt_store, request_spool, workflow_adapters
from tests.support.gateway import gateway_client, use_admin_db
from tests.support.mesh_harness import GLB, PNG, Harness

SYNC = "/v1/3d/generations"
RAW = "/v1/3d/generations/glb"


@pytest.fixture
def rig(monkeypatch, tmp_path):
    use_admin_db(monkeypatch, tmp_path)
    jobs = tmp_path / "media_jobs.db"
    media_jobs.init_db(jobs)
    monkeypatch.setattr(media_jobs, "DB_PATH", jobs)
    receipt_store.init_db(receipt_store.DB_PATH)
    monkeypatch.setattr(proxy, "_WORKFLOW_RUN_SPOOL_ROOT", tmp_path / "spool")
    monkeypatch.setattr(proxy, "_WORKFLOW_JOB_SPOOL", request_spool.JobSpool(tmp_path / "spool"))
    monkeypatch.setattr(proxy, "_workflow_core_cache", None)
    monkeypatch.setattr(proxy, "IMAGE_MEDIA_JOB_PAYLOAD_DIR", tmp_path / "payloads")
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", cache)
    harness = Harness()
    monkeypatch.setattr(proxy.MESH_DEPS, "services", lambda: harness.services)
    monkeypatch.setattr(proxy.MESH_DEPS, "client_factory", harness.deps().client_factory)
    host = dataclasses.replace(proxy.MULTIPART_HOST, leases=harness.leases, require_egress=lambda _s: None)
    runner = multipart_adapter.runner(host)
    monkeypatch.setitem(workflow_adapters._RUNNERS, runner.binding_key, runner)
    harness.jobs = jobs
    return harness


def post(path, **kwargs):
    async def go():
        async with gateway_client() as client:
            return await client.post(path, **kwargs)
    return asyncio.run(go())


def test_smoke(rig):
    r = post(SYNC, files={"image": ("p.png", PNG, "image/png")}, data={"seed": "42"})
    print(r.status_code, r.headers, r.text[:500])
    assert r.status_code == 200
