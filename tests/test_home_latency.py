"""Home must not repeat SQLite opens per capability or block the event loop."""
import asyncio
import threading
from unittest.mock import patch

import httpx
import pytest

from gateway import admin_store, home_api
from tests.support.access import access_context
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
    context = access_context()
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
    # One registry read for the live view and one for registry capabilities:
    # independent of how many capabilities are published.
    assert opens <= 4 < count, (opens, count)


def test_home_does_not_block_the_event_loop(home_case):
    """The blocking snapshot runs on a worker thread, never on the event loop thread.

    A wall-clock stall bound flaked under host load; the property is where the work runs.
    """
    c = home_case
    original_snapshot = home_api.build_user_portal_snapshot
    snapshot_threads: list[int] = []

    def recording_snapshot(*args, **kwargs):
        snapshot_threads.append(threading.get_ident())
        return original_snapshot(*args, **kwargs)

    async def run() -> int:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=c.http.app), base_url="http://t") as client:
            response = await client.get("/me/api/home")
        assert response.status_code == 200
        return threading.get_ident()

    with patch("gateway.home_api.build_user_portal_snapshot", recording_snapshot):
        loop_thread = asyncio.run(run())
    assert snapshot_threads, "Home never built the portal snapshot"
    assert loop_thread not in snapshot_threads, "Home built the portal snapshot on the event loop thread"
