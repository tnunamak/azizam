"""Retire the old loader without changing the live idle-unload lifecycle."""

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

import proxy


@pytest.mark.parametrize("name", [
    "proxy_request", "load_model", "TABBY_LOAD_TIMEOUT", "MAX_LOAD_FAILURES",
    "LOAD_FAILURE_BACKOFF", "backend_for_host", "MODEL_REQUIRED_PATHS",
])
def test_old_executor_and_loader_are_absent(name):
    assert not hasattr(proxy, name)


@pytest.mark.parametrize("name", [
    "model_loading", "load_event", "unload_event", "load_failure_count",
    "last_load_failure_time", "last_load_error", "reset_load_failures",
    "record_load_failure", "can_attempt_load",
])
def test_loader_exclusive_state_is_absent(name):
    assert not hasattr(proxy.ProxyState(), name)


@pytest.fixture
def idle_state(monkeypatch):
    state = proxy.ProxyState()
    monkeypatch.setattr(proxy, "state", state)
    monkeypatch.setattr(proxy, "TABBY_HOST", "http://idle-engine.test")
    monkeypatch.setattr(proxy, "ADMIN_KEY", "fixture-admin")
    return state


def _mock_upstream(monkeypatch, handler):
    client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(proxy.httpx, "AsyncClient", lambda: client(transport=transport))


@pytest.mark.parametrize("status", [200, 503, None])
def test_idle_unload_preserves_upstream_request_and_failure_cleanup(
    idle_state, monkeypatch, status,
):
    calls = []

    async def upstream(request):
        calls.append(request)
        assert idle_state.model_unloading
        if status is None:
            raise httpx.ConnectError("fixture offline", request=request)
        return httpx.Response(status)

    _mock_upstream(monkeypatch, upstream)
    assert asyncio.run(proxy.unload_model()) is (status == 200)
    assert len(calls) == 1
    request = calls[0]
    assert request.method == "POST"
    assert str(request.url) == "http://idle-engine.test/v1/model/unload"
    assert request.headers["x-admin-key"] == "fixture-admin"
    assert request.content == b""
    assert request.extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), 60,
    )
    assert not idle_state.model_unloading
    assert not idle_state.load_lock.locked()


@pytest.mark.parametrize("cancel", [False, True])
def test_idle_unload_rejects_overlap_and_cleans_up_on_cancellation(
    idle_state, monkeypatch, cancel,
):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def upstream(request):
            calls.append(request)
            entered.set()
            await release.wait()
            return httpx.Response(200)

        _mock_upstream(monkeypatch, upstream)
        task = asyncio.create_task(proxy.unload_model())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            assert await proxy.unload_model() is False
            assert len(calls) == 1
            assert idle_state.model_unloading
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                assert await task is True
            assert not idle_state.model_unloading
            assert not idle_state.load_lock.locked()
            release.set()
            assert await proxy.unload_model() is True
            assert len(calls) == 2
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(scenario())


@pytest.mark.parametrize("unloading,idle,healthy,loaded,timeout,expected", [
    (False, 301, True, True, 300, (1, 1, 1)),
    (True, 301, True, True, 300, (0, 0, 0)),
    (False, 299, True, True, 300, (0, 0, 0)),
    (False, 301, False, True, 300, (1, 0, 0)),
    (False, 301, True, False, 300, (1, 1, 0)),
    (False, 301, True, True, 0, (0, 0, 0)),
])
def test_maintenance_keeps_idle_unload_conditions(
    idle_state, monkeypatch, unloading, idle, healthy, loaded, timeout, expected,
):
    idle_state.model_unloading = unloading
    monkeypatch.setattr(idle_state, "get_idle_seconds", lambda: idle)
    monkeypatch.setattr(proxy, "TABBY_IDLE_TIMEOUT", timeout)
    health = AsyncMock(return_value=healthy)
    probe = AsyncMock(return_value=loaded)
    unload = AsyncMock(return_value=True)
    monkeypatch.setattr(proxy, "check_tabby_health", health)
    monkeypatch.setattr(proxy, "is_model_loaded", probe)
    monkeypatch.setattr(proxy, "unload_model", unload)
    ticks = []

    async def tick(interval):
        ticks.append(interval)
        if len(ticks) == 2:
            idle_state.shutdown_requested = True

    monkeypatch.setattr(proxy.asyncio, "sleep", tick)
    asyncio.run(proxy.maintenance_loop())
    assert ticks == [proxy.CHECK_INTERVAL] * 2
    assert (health.await_count, probe.await_count, unload.await_count) == expected
