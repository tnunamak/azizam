"""Every GET route: SQLite statements and subprocesses do not grow with the data.

Each route is called with a small household (4 apps, 8 keys, 10 finished jobs and
receipts) and again with a larger one (24 apps, 48 keys, 60 jobs and receipts).
The statement count must be the same, and no request may start a process. A route
that fails here reads a row at a time (an N+1) or probes the host on each call.
Counts do not depend on host load. ``scripts/perf/api_bench.py`` measures the same
counts on a copy of the live databases.

A route that cannot meet this yet is listed in ``KNOWN_TO_GROW`` with the reason;
the list may only shrink (tests/budgets.toml, ratchets.route_work_exceptions).
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import proxy
from gateway import admin_store, media_jobs, receipt_store
from gateway.receipts import RouteReceipt
from gateway.runtime_auth import hash_api_key
from tests.support.work import count_work
from tests.test_event_loop_guard import ROUTES, _concrete, gateway  # noqa: F401 - fixture

SMALL, LARGE = 4, 24
MAX_STATEMENTS = 100  # one request, any route, at LARGE; the constant part must stay small too

# route -> why its statement count still grows with the data
KNOWN_TO_GROW: dict[str, str] = {}


@pytest.fixture(autouse=True)
def media_store(tmp_path, monkeypatch):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    monkeypatch.setattr(media_jobs, "DB_PATH", db)
    return db


class Household:
    def __init__(self, jobs_db):
        self.db = proxy.ADMIN_DB_PATH
        self.jobs_db = jobs_db
        self.owner = next(u for u in admin_store.list_users(path=self.db))
        self.apps = 1  # the fixture's personal app
        self.jobs = 0

    def grow_to(self, apps: int) -> None:
        while self.apps < apps:
            n = self.apps
            app = admin_store.create_app(f"App {n}", kind="personal", owner_user_id=self.owner["id"], path=self.db)
            for key in range(2):
                admin_store.create_credential_hash(
                    subject_kind="app", subject_id=app["id"], app_id=app["id"],
                    hash=hash_api_key(f"key-{n}-{key}"), prefix="masked", path=self.db)
            self.apps += 1
        target_jobs = (apps * 5 // 2)
        while self.jobs < target_jobs:
            n = self.jobs
            request_id = f"req_budget_{n}"
            now = int(time.time()) - n
            receipt_store.record(RouteReceipt(
                protocol="openai.videos", intent="video.generate", path="/v1/videos", request_id=request_id,
                requested_model="m", resolved_model="m", status="succeeded", accepted_at_ms=now * 1000,
            ), path=receipt_store.DB_PATH)
            media_jobs.create(media_jobs.MediaJob(
                request_id=request_id, intent="video.generate", status=media_jobs.STATUS_SUCCEEDED,
                created_at=now, updated_at=now, progress=100, assets=[],
            ), path=self.jobs_db)
            self.jobs += 1


def _get(route: str, gateway) -> tuple[int, object]:
    async def call():
        transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 50000), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", cookies=gateway["cookies"],
                                     timeout=60) as client:
            await client.get(_concrete(route), headers=gateway["bearer"])  # fills the once-per-process caches
            with count_work() as work:
                response = await client.get(_concrete(route), headers=gateway["bearer"])
            return response.status_code, work

    return asyncio.run(call())


@pytest.mark.parametrize("route", ROUTES)
def test_route_work_does_not_grow_with_the_data(gateway, media_store, route):  # noqa: F811
    household = Household(media_store)
    household.grow_to(SMALL)
    status, small = _get(route, gateway)
    if status != 200:
        pytest.skip(f"{route} answers {status} without a seeded id; the route walk needs a 200")
    household.grow_to(LARGE)
    _, large = _get(route, gateway)
    assert small.processes == large.processes == 0, f"{route} started {small.commands + large.commands}"
    assert large.statements <= MAX_STATEMENTS, f"{route} ran {large.statements} statements at {LARGE} apps"
    if route in KNOWN_TO_GROW:
        return
    assert large.statements == small.statements, (
        f"{route}: {small.statements} statements for {SMALL} apps, {large.statements} for {LARGE}")
