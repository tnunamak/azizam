"""Home reads the engine rows, not the whole status page.

Home needs one thing from the status build: the engine rows its capability tiles are
judged by. The full build also reads GPUs, disks, logs, git and the Daisy workers,
which Home never shows. A cold status cache used to make every Home read wait for all
of them, so a read after the cache expired cost about a second on the perf fixture and
more on a loaded host. These tests pin that Home takes the engine rows alone, and that a
burst of Home reads shares one probe pass.
"""
from __future__ import annotations

import asyncio

import proxy


def _rows() -> list[dict]:
    return [{"key": "chat", "service_id": "chat", "name": "Chat", "status": "up", "live_status": "up",
             "observed_at_s": 100}]


def _registry() -> dict:
    return {"services": {"chat": {"enabled": True, "config": {"base_url": "http://chat.test"}}}}


def _cold_home(monkeypatch, grid) -> None:
    async def no_catalog_evidence(*args, **kwargs):
        return {}

    def full_build_must_not_run(config):
        raise AssertionError("Home read the full status page")

    monkeypatch.setattr(proxy.status_endpoint, "build_status_payload", full_build_must_not_run)
    monkeypatch.setattr(proxy.status_endpoint, "_CACHE", {})
    monkeypatch.setattr(proxy.status_endpoint, "_backend_grid", grid)
    monkeypatch.setattr(proxy, "_ENGINE_ROWS", {})
    monkeypatch.setattr(proxy, "_ENGINE_ROWS_LOCK", asyncio.Lock())
    monkeypatch.setattr(proxy, "_models_catalog_health_evidence", no_catalog_evidence)
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: _registry())
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
