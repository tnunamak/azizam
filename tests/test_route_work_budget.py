"""Every GET route: SQLite statements and subprocesses do not grow with the data.

Each route is called with a small household (4 apps, 8 keys, 10 finished jobs and
receipts) and again with a larger one (24 apps, 48 keys, 60 jobs and receipts).
The statement count must not be higher, and no request may start a process. A route
that fails here reads a row at a time (an N+1) or probes the host on each call.
Counts do not depend on host load. ``scripts/perf/api_bench.py`` measures the same
counts on a copy of the live databases.

A route that cannot meet this yet is listed in ``KNOWN_TO_GROW`` with the reason; the list is
empty and may only shrink.
"""
from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import proxy
from gateway import accounts, admin_store, media_jobs, receipt_store, signin_trial
from gateway.receipts import RouteReceipt
from gateway.runtime_auth import hash_api_key
from gateway.signin_rules import Claims
from tests.support.work import count_work
from tests.test_event_loop_guard import ROUTES, _concrete, gateway  # noqa: F401 - fixture

SMALL, LARGE = 4, 24
MAX_STATEMENTS = 100  # one request, any route, at LARGE; the constant part must stay small too

# route -> why its statement count still grows with the data
KNOWN_TO_GROW: dict[str, str] = {}

# Plain routes this fixture cannot answer with 200, and why. Routes with a path parameter need a real id
# and are covered where the id exists (tests/test_library_perf.py, tests/test_me_media.py).
NOT_MEASURABLE = {
    "/admin/api/categories/active": "409: no category snapshot is active in the fixture",
    "/admin/api/operator/litellm/keys": "503: the LLM substrate is not configured in the gate",
    "/admin/api/settings/receipts/preview": "400: needs a retention argument",
    "/me/api/profile/avatar/picture": "404: the fixture's person has no sign-in picture, and the route fetches it from the provider",
    "/me/api/prompt-memory": "500: needs the prompt-memory store's own database and a person row",
    "/admin/api/activity/requests": "500 in some orders: its router keeps the receipts path the app was imported with, "
                                    "not the fixture's (scripts/perf/api_bench.py measures it on real data)",
}
# Routes with a path parameter that the walk can still answer, because the fixture
# makes the thing the parameter names (see ``_seed``).
SEEDED = ("/admin/api/signin/test/{test_id}",)
MEASURED = [route for route in ROUTES if ("{" not in route or route in SEEDED) and route not in NOT_MEASURABLE]


@pytest.fixture(autouse=True)
def no_test_runs():
    signin_trial.clear()
    yield
    signin_trial.clear()


@pytest.fixture(autouse=True)
def media_store(tmp_path, monkeypatch):
    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    monkeypatch.setattr(media_jobs, "DB_PATH", db)
    return db


class Household:
    def __init__(self, jobs_db):
        self.db = proxy.ADMIN_DB_PATH
        accounts.initialize(self.db)  # people, invites and account links live in the admin database
        self.jobs_db = jobs_db
        self.owner = next(u for u in admin_store.list_users(path=self.db))
        self.apps = 1  # the fixture's personal app
        self.jobs = 0

    def grow_to(self, apps: int) -> None:
        while self.apps < apps:
            n = self.apps
            app = admin_store.create_app(f"App {n}", kind="personal", owner_user_id=self.owner["id"],
                                         access_plan="person", path=self.db)
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


def _seed(route: str, household: "Household") -> str:
    """The concrete path of ``route``: made-up ids, or the id of a record the fixture creates."""
    if route == "/admin/api/signin/test/{test_id}":
        # A finished test sign-in by the owner: the status answer then asks the rules who would get in.
        run = signin_trial.create(household.owner["id"], proxy.ADMIN_OIDC_CONFIG, "sig")
        run.status, run.passed_at, run.subject = "passed", int(time.time()), "walk-subject"
        run.claims = Claims(email="walk@example.com")
        return route.replace("{test_id}", run.id)
    return _concrete(route)


def _get(route: str, gateway, path: str) -> tuple[int, object]:
    async def call():
        transport = httpx.ASGITransport(app=proxy.app, client=("127.0.0.1", 50000), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t", cookies=gateway["cookies"],
                                     timeout=60) as client:
            for _ in range(2):  # fill the once-per-process caches and the few-second ones
                await client.get(path, headers=gateway["bearer"])
            with count_work() as work:
                response = await client.get(path, headers=gateway["bearer"])
            return response.status_code, work

    return asyncio.run(call())


def test_the_walk_measures_most_of_the_read_routes() -> None:
    assert len(MEASURED) >= 50, f"only {len(MEASURED)} of {len(ROUTES)} GET routes are measured"
    assert set(NOT_MEASURABLE) <= set(ROUTES), "an unmeasurable route no longer exists: remove it from NOT_MEASURABLE"


@pytest.mark.parametrize("route", MEASURED)
def test_route_work_does_not_grow_with_the_data(gateway, media_store, route):  # noqa: F811
    household = Household(media_store)
    household.grow_to(SMALL)
    path = _seed(route, household)
    status, small = _get(route, gateway, path)
    assert status == 200, f"{route} answers {status}; fix the fixture or list the route in NOT_MEASURABLE with the reason"
    household.grow_to(LARGE)
    _, large = _get(route, gateway, path)
    assert small.processes == large.processes == 0, f"{route} started {small.commands + large.commands}"
    assert large.statements <= MAX_STATEMENTS, f"{route} ran {large.statements} statements at {LARGE} apps"
    if route in KNOWN_TO_GROW:
        return
    assert large.statements <= small.statements, (
        f"{route}: {small.statements} statements for {SMALL} apps, {large.statements} for {LARGE}")
