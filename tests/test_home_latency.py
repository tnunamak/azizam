"""Home must not repeat SQLite opens per capability or block the event loop."""
import asyncio
import time
from unittest.mock import patch

import httpx
import pytest

from gateway import admin_store, home_api
from tests.test_home_api import home_case  # noqa: F401 - fixture


def test_connections_secure_a_state_path_once(tmp_path):
    db = tmp_path / "state" / "admin.db"
    admin_store.init_db(db)
    with admin_store._conn(db):
        pass
    with patch.object(admin_store, "secure_state_path", wraps=admin_store.secure_state_path) as secure:
        for _ in range(5):
            with admin_store._conn(db):
                pass
    assert secure.call_count == 0


def test_secure_state_path_still_retightens_on_demand(tmp_path):
    db = tmp_path / "admin.db"
    admin_store.init_db(db)
    db.chmod(0o666)
    admin_store.secure_state_path(db)
    assert db.stat().st_mode & 0o777 == 0o600


def _capability_connections(extra_models: int) -> int:
    import proxy
    registry = proxy.ADMIN_STORE.get_service_registry()
    context = proxy._catalog_auth_context_for_profile(proxy.DEFAULT_PROFILE, "default-local")
    opens = 0
    real = admin_store._conn

    def counting(path=admin_store.DB_PATH):
        nonlocal opens
        opens += 1
        return real(path)

    with patch.object(admin_store, "_conn", counting):
        caps = proxy._gateway_capabilities_for_auth_context(context)
    assert len(caps) >= 5, "fixture gateway must publish several capabilities"
    return opens, len(caps)


def test_capability_resolution_opens_a_constant_number_of_connections():
    opens, count = _capability_connections(0)
    # One registry read for the live view, one for registry capabilities, one
    # provider-admin read: independent of how many capabilities are published.
    assert opens <= 4 < count, (opens, count)


def test_home_does_not_block_the_event_loop(home_case):
    c = home_case
    original_snapshot = home_api.build_user_portal_snapshot

    def slow_snapshot(*args, **kwargs):
        time.sleep(0.2)
        return original_snapshot(*args, **kwargs)

    async def run() -> float:
        app = c.http.app
        worst = 0.0
        stop = False

        async def ticker():
            nonlocal worst
            last = time.perf_counter()
            while not stop:
                await asyncio.sleep(0.01)
                now = time.perf_counter()
                worst = max(worst, now - last)
                last = now

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
            task = asyncio.create_task(ticker())
            await asyncio.sleep(0.05)  # let the probe start ticking before Home runs
            response = await client.get("/me/api/home")
            stop = True
            await task
        assert response.status_code == 200
        return worst

    with patch("gateway.home_api.build_user_portal_snapshot", slow_snapshot):
        worst = asyncio.run(run())
    assert worst < 0.1, f"event loop stalled {worst:.3f}s while Home ran 0.2s of blocking work"
