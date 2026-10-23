"""Home reads the engine rows, not the whole status page.

Home needs one thing from the status build: the engine rows its capability tiles are
judged by. The full build also reads GPUs, disks, logs, git and the Daisy workers,
which Home never shows. A cold status cache used to make every Home read wait for all
of them, so a read after the cache expired cost about a second on the perf fixture and
more on a loaded host. These tests pin that Home takes the engine rows alone, and that a
burst of Home reads shares one probe pass.

A slow engine must not hold Home up either. Home is served the last rows while one shielded
refresh runs, and when it has none it waits briefly and then gets checking rows. Each engine
probe also has a deadline, so a probe that keeps its connection busy cannot stall the refresh.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

import proxy
import status_endpoint
from tests.support.fake_upstream.loopback import LoopbackServer


def _rows() -> list[dict]:
    return [{"key": "chat", "service_id": "chat", "name": "Chat", "status": "up", "live_status": "up",
             "observed_at_s": 100}]


def _registry() -> dict:
    return {"services": {"chat": {"enabled": True, "config": {"base_url": "http://chat.test"}}}}


def _engine_config() -> dict:
    return {"backends": {"chat": {"name": "Chat", "health_endpoint": "http://chat.test/health", "role": "required"}}}


async def _until_stored(previous) -> None:
    """Wait until a refresh has stored rows other than ``previous``."""
    while proxy._ENGINE_ROWS.get("payload") is previous:
        await asyncio.sleep(0.01)


def _cold_home(monkeypatch, grid) -> None:
    async def no_catalog_evidence(*args, **kwargs):
        return {}

    def full_build_must_not_run(config):
        raise AssertionError("Home read the full status page")

    monkeypatch.setattr(proxy.status_endpoint, "build_status_payload", full_build_must_not_run)
    monkeypatch.setattr(proxy.status_endpoint, "_CACHE", {})
    monkeypatch.setattr(proxy.status_endpoint, "_backend_grid", grid)
    monkeypatch.setattr(proxy, "_ENGINE_ROWS", {})
    monkeypatch.setattr(proxy, "_ENGINE_ROWS_TASK", None)
    monkeypatch.setattr(proxy, "_models_catalog_health_evidence", no_catalog_evidence)
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: _registry())
    monkeypatch.setattr(proxy, "_gateway_capabilities_for_auth_context", lambda context: [])
    monkeypatch.setattr(proxy, "service_for_capability", lambda capability: None)
    monkeypatch.setattr(proxy, "_highest_priority_chat_backend", lambda: None)
    monkeypatch.setattr(proxy, "_runtime_availability_evidence", lambda *args, **kwargs: {})
    monkeypatch.setattr(proxy, "config", {})


def test_home_status_with_a_cold_cache_probes_engines_and_skips_the_status_page(monkeypatch):
    grid_calls: list[dict] = []

    async def grid(config):
        grid_calls.append(config)
        return _rows()

    _cold_home(monkeypatch, grid)

    payload = asyncio.run(proxy._home_status_payload())

    assert grid_calls, "a cold Home read must probe the engine rows"
    assert payload["backends"] == _rows()
    assert "_availability_evidence" in payload


def test_concurrent_home_reads_share_one_engine_probe(monkeypatch):
    grid_calls: list[dict] = []

    async def grid(config):
        grid_calls.append(config)
        await asyncio.sleep(0.02)
        return _rows()

    _cold_home(monkeypatch, grid)

    async def burst():
        return await asyncio.gather(*(proxy._engine_rows_payload() for _ in range(6)))

    results = asyncio.run(burst())

    assert len(grid_calls) == 1
    assert all(result["backends"] == _rows() for result in results)


def test_home_engine_rows_use_a_fresh_status_page_when_one_is_cached(monkeypatch):
    async def grid(config):
        raise AssertionError("a fresh status page already holds the engine rows")

    _cold_home(monkeypatch, grid)
    cached_rows = [{"key": "chat", "service_id": "chat", "status": "down", "observed_at_s": 90}]
    monkeypatch.setattr(proxy, "_cached_status_payload_for_availability",
                        lambda: {"backends": cached_rows, "generated_at": 90})

    payload = asyncio.run(proxy._engine_rows_payload())

    assert payload == {"backends": cached_rows, "generated_at": 90}


def test_a_burst_during_a_slow_probe_is_served_the_last_rows_at_once(monkeypatch):
    stored = {"backends": _rows(), "generated_at": 100}
    grid_calls: list[dict] = []

    async def scenario():
        release = asyncio.Event()

        async def grid(config):
            grid_calls.append(config)
            await release.wait()  # the probe stays slow until the burst has returned
            return _rows()

        _cold_home(monkeypatch, grid)
        # Rows from a pass 20 s ago: older than the status TTL, so a refresh starts, but within the stale limit.
        monkeypatch.setattr(proxy, "_ENGINE_ROWS", {"ts": time.monotonic() - 20.0, "payload": stored})

        # A read that waited for the blocked probe would time out here.
        results = await asyncio.wait_for(asyncio.gather(*(proxy._engine_rows_payload() for _ in range(6))), timeout=1.0)
        assert all(result == stored for result in results)
        assert len(grid_calls) == 1, "the burst shares one refresh"

        release.set()
        await asyncio.wait_for(_until_stored(stored), timeout=2.0)
        assert proxy._ENGINE_ROWS["payload"]["backends"] == _rows()

    asyncio.run(scenario())


@pytest.mark.parametrize("stored_age_s", [None, 120.0], ids=["nothing-stored", "stored-past-the-stale-limit"])
def test_a_burst_with_no_rows_to_serve_gets_checking_rows_after_the_first_wait(monkeypatch, stored_age_s):
    grid_calls: list[dict] = []

    async def scenario():
        release = asyncio.Event()

        async def grid(config):
            grid_calls.append(config)
            await release.wait()
            return _rows()

        _cold_home(monkeypatch, grid)
        monkeypatch.setattr(proxy, "config", _engine_config())
        if stored_age_s is not None:
            monkeypatch.setattr(proxy, "_ENGINE_ROWS", {"ts": time.monotonic() - stored_age_s,
                                                       "payload": {"backends": _rows(), "generated_at": 100}})
        previous = proxy._ENGINE_ROWS.get("payload")

        results = await asyncio.wait_for(asyncio.gather(*(proxy._engine_rows_payload() for _ in range(6))), timeout=2.0)
        for result in results:
            assert [row["status"] for row in result["backends"]] == ["unknown"], "nothing was measured yet"
            assert all("observed_at_s" not in row for row in result["backends"])
        assert len(grid_calls) == 1

        release.set()
        await asyncio.wait_for(_until_stored(previous), timeout=2.0)

    asyncio.run(scenario())


def test_a_cancelled_first_reader_does_not_abandon_the_refresh(monkeypatch):
    grid_calls: list[dict] = []

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def grid(config):
            grid_calls.append(config)
            started.set()
            await release.wait()
            return _rows()

        _cold_home(monkeypatch, grid)
        first = asyncio.create_task(proxy._engine_rows_payload())
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        release.set()
        # No reader is waiting now, yet the refresh still stores its rows.
        await asyncio.wait_for(_until_stored(None), timeout=2.0)
        second = await proxy._engine_rows_payload()
        assert second["backends"] == _rows()
        assert len(grid_calls) == 1

    asyncio.run(scenario())


def test_a_failed_refresh_reads_as_checking_rows_and_is_logged(monkeypatch, caplog):
    async def grid(config):
        raise RuntimeError("probe pass broke")

    async def scenario():
        _cold_home(monkeypatch, grid)
        monkeypatch.setattr(proxy, "config", _engine_config())
        payload = await proxy._engine_rows_payload()
        await asyncio.sleep(0.01)  # the failure is logged by a callback on the finished refresh
        return payload

    payload = asyncio.run(scenario())

    assert [row["status"] for row in payload["backends"]] == ["unknown"]
    assert "Home's engine rows refresh failed" in caplog.text


async def _trickling_body(scope, receive, send):
    """A health endpoint that answers 200 and then sends its body a few bytes at a time, slowly."""
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    for _ in range(200):
        await send({"type": "http.response.body", "body": b" ", "more_body": True})
        await asyncio.sleep(0.05)
    await send({"type": "http.response.body", "body": b"{}", "more_body": False})


async def _ready(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": b'{"status": "ready"}', "more_body": False})


def test_the_grid_deadline_ends_a_trickling_probe_and_keeps_the_other_rows():
    slow = LoopbackServer(_trickling_body).start()
    fast = LoopbackServer(_ready).start()
    try:
        config = {"backends": {
            "slow": {"name": "Slow", "health_endpoint": f"{slow.base_url}/health", "role": "required"},
            "fast": {"name": "Fast", "health_endpoint": f"{fast.base_url}/health", "role": "required"},
        }}
        started = time.monotonic()
        rows = asyncio.run(status_endpoint._backend_grid(config, deadline_s=0.3))
        elapsed = time.monotonic() - started
    finally:
        slow.stop()
        fast.stop()

    by_key = {row["key"]: row for row in rows}
    assert elapsed < 2.0, "the deadline, not the 10 s body, ends the slow probe"
    assert (by_key["slow"]["status"], by_key["slow"]["last_error"]) == ("degraded", "timeout")
    assert by_key["fast"]["status"] == "up"


def test_the_grid_deadline_reports_a_lifecycle_check_that_does_not_finish_as_unknown(monkeypatch):
    async def never_finishes(target, backends, state=None):  # state: the systemd batch's answer for this engine
        await asyncio.Event().wait()

    monkeypatch.setattr(status_endpoint, "_lifecycle_probe", never_finishes)
    config = {"backends": {"engine": {"name": "Engine", "role": "required", "lifecycle": {"driver": "systemd"}}}}

    (row,) = asyncio.run(status_endpoint._backend_grid(config, deadline_s=0.1))

    assert (row["status"], row["last_error"]) == ("unknown", "status check did not finish")


def _health_entry(service: dict, *, age_s: float, evidence: dict) -> tuple:
    """A health evidence cache entry, as the catalog probe writes it."""
    return (time.monotonic() - age_s, json.dumps(service, sort_keys=True, default=str), evidence)


def test_home_health_evidence_is_served_from_the_cache_while_a_probe_runs(monkeypatch):
    stored = {"available": True, "status": "ready"}

    async def scenario():
        release = asyncio.Event()
        calls: list[list[str]] = []

        async def probe(service_records, *, service_ids):
            calls.append(list(service_ids))
            await release.wait()  # slow until the read has returned
            return {}

        monkeypatch.setattr(proxy, "_models_catalog_health_evidence", probe)
        monkeypatch.setattr(proxy, "_HOME_HEALTH_TASK", None)
        monkeypatch.setattr(proxy, "_HEALTH_EVIDENCE_CACHE",
                            {"chat": _health_entry(_registry()["services"]["chat"], age_s=30.0, evidence=stored)})

        evidence = await asyncio.wait_for(proxy._home_health_evidence(_registry(), service_ids=["chat"]), timeout=1.0)
        await asyncio.sleep(0)  # the refresh task starts on the next turn of the loop

        assert evidence == {"chat": stored}
        assert calls == [["chat"]]
        release.set()
        await asyncio.wait_for(proxy._HOME_HEALTH_TASK, timeout=1.0)

    asyncio.run(scenario())


def test_home_health_evidence_with_nothing_cached_does_not_wait_for_the_probe(monkeypatch):
    async def scenario():
        release = asyncio.Event()

        async def probe(service_records, *, service_ids):
            await release.wait()  # the probe is still running when the read returns
            return {}

        monkeypatch.setattr(proxy, "_models_catalog_health_evidence", probe)
        monkeypatch.setattr(proxy, "_HOME_HEALTH_TASK", None)
        monkeypatch.setattr(proxy, "_HEALTH_EVIDENCE_CACHE", {})

        started = time.monotonic()
        evidence = await asyncio.wait_for(proxy._home_health_evidence(_registry(), service_ids=["chat"]), timeout=2.0)

        assert evidence == {}
        assert time.monotonic() - started < 0.2
        release.set()
        await asyncio.wait_for(proxy._HOME_HEALTH_TASK, timeout=1.0)

    asyncio.run(scenario())


def test_home_health_evidence_that_is_fresh_starts_no_probe(monkeypatch):
    stored = {"available": True, "status": "ready"}

    async def probe(service_records, *, service_ids):
        raise AssertionError("fresh evidence needs no probe")

    monkeypatch.setattr(proxy, "_models_catalog_health_evidence", probe)
    monkeypatch.setattr(proxy, "_HOME_HEALTH_TASK", None)
    monkeypatch.setattr(proxy, "_HEALTH_EVIDENCE_CACHE",
                        {"chat": _health_entry(_registry()["services"]["chat"], age_s=1.0, evidence=stored)})

    evidence = asyncio.run(proxy._home_health_evidence(_registry(), service_ids=["chat"]))

    assert evidence == {"chat": stored}
    assert proxy._HOME_HEALTH_TASK is None
