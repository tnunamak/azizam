"""Health and observer reads are bounded in bytes and in total time (review c2d-D4-rows-review-1108, finding 2)."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from gateway import multipart_adapter
from gateway.exclusive_pool import ExclusivePoolSpec, PoolObserverError

MIB = 1 << 20


class _Body(httpx.AsyncByteStream):
    def __init__(self, chunks, gap: float, counter: list[int]) -> None:
        self._chunks = chunks
        self._gap = gap
        self._counter = counter

    async def __aiter__(self):
        for chunk in self._chunks:
            if self._gap:
                await asyncio.sleep(self._gap)
            self._counter[0] += len(chunk)
            yield chunk


class _Transport(httpx.AsyncBaseTransport):
    def __init__(self, status: int, chunks, gap: float = 0.0) -> None:
        self.status, self.chunks, self.gap = status, chunks, gap
        self.read = [0]

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self.status, stream=_Body(self.chunks, self.gap, self.read))


def _big(total: int):
    return [b"x" * (64 * 1024)] * (total // (64 * 1024))


def _spec(timeout: int = 5) -> ExclusivePoolSpec:
    return ExclusivePoolSpec(pool_id="p", max_claim_bytes=MIB, reserve_bytes=0, retry_after_seconds=1,
                             timeout_seconds=timeout, observer_service="obs", observer_path="/f",
                             free_pointer="/free", total_pointer="/total")


def _run(coro):
    return asyncio.run(coro)


def test_a_health_answer_is_judged_by_its_status_without_reading_the_body() -> None:
    transport = _Transport(503, _big(8 * MIB))

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await multipart_adapter._healthy(client, "http://svc", {})

    assert _run(go()) is False
    assert transport.read[0] <= MIB


def test_a_health_answer_that_trickles_is_cut_at_the_total_bound(monkeypatch) -> None:
    monkeypatch.setattr(multipart_adapter, "_HEALTH_TIMEOUT_S", 0.3)
    transport = _Transport(200, [b"x"] * 100, gap=0.1)  # every read arrives inside the per-read timeout

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await multipart_adapter._healthy(client, "http://svc", {})

    start = time.monotonic()
    _run(go())
    assert time.monotonic() - start < 1.5


def test_an_observer_answer_over_the_byte_cap_is_refused_without_reading_it_all() -> None:
    transport = _Transport(200, _big(8 * MIB))

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await multipart_adapter.read_observer(client, _spec(), "http://obs/f")

    with pytest.raises(PoolObserverError):
        _run(go())
    assert transport.read[0] <= 2 * MIB


def test_an_observer_error_body_is_not_buffered() -> None:
    transport = _Transport(503, _big(8 * MIB))

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await multipart_adapter.read_observer(client, _spec(), "http://obs/f")

    with pytest.raises(PoolObserverError):
        _run(go())
    assert transport.read[0] <= MIB


def test_an_observer_that_trickles_is_cut_at_the_total_bound() -> None:
    transport = _Transport(200, [b" "] * 100, gap=0.1)

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await multipart_adapter.read_observer(client, _spec(timeout=1), "http://obs/f")

    start = time.monotonic()
    with pytest.raises(PoolObserverError):
        _run(go())
    assert time.monotonic() - start < 2.5


def test_a_good_observer_answer_still_reads() -> None:
    transport = _Transport(200, [b'{"free": 5, "total": 9}'])

    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            return await multipart_adapter.read_observer(client, _spec(), "http://obs/f")

    assert _run(go()) == {"free": 5, "total": 9}


def test_probe_rejects_compressed_expansion_before_buffering_excess() -> None:
    """An 8 KiB gzip reply that expands to 8 MiB is refused, and no decoded buffer passes the fixed cap (review F2, P1)."""
    import gzip
    import tracemalloc

    packed = gzip.compress(b"\x00" * (8 * MIB))
    assert len(packed) < 16 * 1024
    seen: list[httpx.Request] = []

    class _Gzip(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, headers={"content-encoding": "gzip"}, stream=_Body([packed], 0.0, [0]))

    async def go():
        async with httpx.AsyncClient(transport=_Gzip()) as client:
            return await multipart_adapter.read_observer(client, _spec(), "http://obs/f")

    tracemalloc.start()
    try:
        with pytest.raises(PoolObserverError):
            _run(go())
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 2 * MIB, f"a decoded buffer of {peak} bytes was held"
    assert seen[0].headers.get("accept-encoding") == "identity"
