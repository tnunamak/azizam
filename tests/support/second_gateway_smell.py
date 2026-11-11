"""A second gateway process for tests/test_owner_engine_chain_smell.py (the no-restart check).

Run as ``python -m tests.support.second_gateway_smell <admin.db>`` from the repository root, with the stores' environment
variables the parent test has (so the receipts, the jobs and the asset cache are the parent's). It imports the real
``proxy`` app and points it at the parent's admin database, as the ``gateway`` fixture does, then serves the one
smell job that the parent asks for. It never runs the lifespan, so it holds no instance lock and starts no sampler.

Protocol: the gateway prints boot lines of its own on stdout, so the child's messages carry the prefix ``SECOND `` and
the parent ignores every other line. One JSON object per line:
  child  -> {"event": "ready", "knows_engine": false}   the app is built; its own config has no smell engine
  parent -> {"key": "<app key>"}                          the parent has enabled the engine in the shared database
  child  -> {"event": "done", "status": ..., "content": ..., "posts": [...], "job_id": ...}
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

from tests.support.engine_dns import use_fake_engine_dns
from tests.support.fake_upstream.transport import HostRouter, StreamingASGITransport
from tests.test_owner_engine_chain_smell import HOST, SmellEngine


def _say(**line) -> None:
    print("SECOND " + json.dumps(line), flush=True)


async def _job(proxy, key: str) -> dict:
    headers = {"authorization": f"Bearer {key}"}
    async with httpx.AsyncClient(transport=StreamingASGITransport(proxy.app), base_url="https://ai.example") as client:
        submitted = await client.post("/gateway/v1/media/jobs", headers=headers, json={
            "intent": "smell.infer", "model": "smell-1", "input": {"vector": [0.25, -1.5, 3.0, 0.0]}})
        if submitted.status_code != 202:
            return {"status": f"submit {submitted.status_code}", "content": None, "job_id": None}
        job = submitted.json()
        status = "queued"
        for _ in range(100):
            status = (await client.get(f"/gateway/v1/media/jobs/{job['id']}", headers=headers)).json()["status"]
            if status in ("succeeded", "failed"):
                break
            await asyncio.sleep(0.1)
        content = await client.get(job["content_url"], headers=headers) if status == "succeeded" else None
        return {"status": status, "job_id": job["id"], "content": content.text if content is not None else None}


def main(admin_db: str) -> None:
    import proxy
    from gateway import receipt_store
    receipt_store.init_db()  # the lifespan does this at start; the child does not run it
    path = Path(admin_db)
    proxy.ADMIN_STORE.path = path
    proxy.ADMIN_DB_PATH = path
    with pytest.MonkeyPatch.context() as patch:
        use_fake_engine_dns(patch)
        engine = SmellEngine()
        HostRouter(patch).add(HOST, engine)
        _say(event="ready", knows_engine="smell-box" in json.dumps(proxy.config.get("backends") or {}))
        key = json.loads(sys.stdin.readline())["key"]
        outcome = asyncio.run(_job(proxy, key))
        _say(event="done", posts=engine.posts, **outcome)


if __name__ == "__main__":
    main(sys.argv[1])
