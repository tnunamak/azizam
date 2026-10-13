"""Work budgets: a request's SQLite statements and subprocesses must not grow with the data.

Counts, not seconds: a count is the same on a loaded host and on a fast one. Each
test runs one request at a small and a large data size. Equal counts mean no
per-item query or probe; a bounded count keeps the constant part honest.
(docs/design/TEST_STRATEGY.md; scripts/perf/ measures the same counts on real data.)
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway import admin_store, me_media, media_jobs, ownership, receipt_store
from gateway.admin_oidc import AdminSession
from gateway.receipts import RouteReceipt
from tests.support.work import count_work


class UsageFixture:
    """Alice's apps and audio assets: each asset has a job, a receipt, a file and a sidecar."""

    def __init__(self, root: Path) -> None:
        self.cache = root / "cache"
        self.cache.mkdir()
        self.admin, self.jobs, self.receipts = root / "admin.db", root / "jobs.db", root / "receipts.db"
        admin_store.init_db(self.admin)
        media_jobs.init_db(self.jobs)
        receipt_store.init_db(self.receipts)
        self.alice = admin_store.upsert_oidc_user("iss", "alice", email="alice@example.test", path=self.admin)
        self.apps = [admin_store.create_app(f"App {n}", kind="personal", owner_user_id=self.alice["id"], path=self.admin)
                     for n in range(3)]
        self.count = 0
        self.assets: list[str] = []
        app = FastAPI()
        app.include_router(me_media.create_me_media_router(
            session_provider=lambda request: AdminSession(
                subject=self.alice["id"], email=self.alice["email"], role="none",
                issued_at=time.time(), expires_at=time.time() + 3600),
            signed_url_builder=lambda request, asset_id, mode: f"http://t/{mode}/{asset_id}",
            asset_path_resolver=lambda asset_id: self.cache / f"{asset_id.split('_', 1)[1]}.mp3",
            asset_media_type_resolver=lambda asset_id, path: "audio/mpeg",
            asset_deleter=lambda asset_id: False,
            asset_catalog_provider=lambda: list(self.assets),
            asset_metadata_reader=lambda asset_id: {
                "owner_key_hashes": [ownership.principal_key("app", self.apps[0]["id"])]},
            admin_db_path=self.admin, media_jobs_path=self.jobs,
            receipts_path=self.receipts, prompt_memory_path=self.jobs,
        ))
        self.client = TestClient(app)

    def add_audio(self, *, with_job: bool = True) -> str:
        self.count += 1
        asset_id = f"aud_{self.count:064x}"
        request_id = f"req_{self.count}"
        now = int(time.time())
        (self.cache / f"{self.count:064x}.mp3").write_bytes(b"not really audio")
        self.assets.append(asset_id)
        if with_job:
            app = self.apps[self.count % len(self.apps)]
            receipt_store.record(RouteReceipt(
                protocol="openai.audio", intent="audio.generate", path="/v1/audio/generate",
                request_id=request_id, app_id=app["id"], requested_model="music", resolved_model="music",
                status="succeeded", accepted_at_ms=now * 1000,
            ), path=self.receipts)
            media_jobs.create(media_jobs.MediaJob(
                request_id=request_id, intent="audio.generate", status=media_jobs.STATUS_SUCCEEDED,
                created_at=now, updated_at=now, progress=100,
                assets=[{"asset_id": asset_id, "kind": "audio", "model": "music"}],
            ), path=self.jobs)
        return asset_id

    def usage(self):
        response = self.client.get("/me/api/usage/summary")
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture()
def usage(tmp_path: Path) -> UsageFixture:
    me_media._EMBEDDED_MEMO.clear()
    return UsageFixture(tmp_path)


def test_usage_summary_work_does_not_grow_with_the_assets(usage: UsageFixture) -> None:
    for _ in range(6):
        usage.add_audio()
    with count_work() as small:
        body = usage.usage()
    for _ in range(54):
        usage.add_audio()
    with count_work() as large:
        grown = usage.usage()
    assert body["media_count"] == 6 and grown["media_count"] == 60
    assert small.statements > 10, small  # the counter saw the request
    assert large.statements == small.statements, f"{small.statements} statements for 6 assets, {large.statements} for 60"
    assert large.processes == small.processes == 0, (small.commands, large.commands)


def test_usage_summary_does_not_probe_files_that_a_job_already_describes(usage: UsageFixture) -> None:
    for _ in range(5):
        usage.add_audio()
    with count_work() as work:
        usage.usage()
    assert work.processes == 0, work.commands


def test_usage_summary_probes_a_file_no_job_knows_once(usage: UsageFixture) -> None:
    usage.add_audio(with_job=False)
    with count_work() as first:
        usage.usage()
    with count_work() as again:
        usage.usage()
    assert first.processes == 1 and again.processes == 0, (first.commands, again.commands)
