"""The status fallback keeps a service's row for 12 seconds, and readers that find a service cold share one probe.

Capability and overview reads ask about the same few services. Before a read probes a service, it looks for a row that
was probed less than 12 seconds ago, or for a probe already in flight for that service. The status clock is faked, so
no test waits for the TTL.
"""
import asyncio

import pytest

import proxy
import status_endpoint

TTL_S = 12.0


@pytest.fixture
def clock(monkeypatch):
    fake = {"now": 1_000.0}
    monkeypatch.setattr(status_endpoint, "_now", lambda: fake["now"])
    monkeypatch.setattr(proxy.service_registry, "runtime_backends", lambda _records: {"chat": {"key": "chat"}})
    return fake


@pytest.fixture
def grid(monkeypatch):
    """A grid probe that answers every service it is asked about as up, and records each probe."""
    asked: list[list[str]] = []

    async def probe(config):
        services = sorted(config["backends"])
        asked.append(services)
        return [{"service_id": key, "key": key, "live_status": "up", "status": "up", "source": "http"}
                for key in services]

    monkeypatch.setattr(status_endpoint, "_backend_grid", probe)
    return asked


def read():
    return proxy._status_payload_with_service_rows({"backends": []}, {}, ["chat"])


def service_ids(payload):
    return [row["service_id"] for row in payload["backends"]]


@pytest.mark.usefixtures("clock")
def test_two_immediate_reads_make_one_probe(grid):
    async def scenario():
        return await read(), await read()

    first, second = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert grid == [["chat"]], "the second read should use the row the first read probed"
    assert service_ids(first) == service_ids(second) == ["chat"]


def test_a_read_at_exactly_the_ttl_probes_again(clock, grid):
    async def scenario():
        start = clock["now"]
        await read()
        clock["now"] = start + TTL_S - 0.001
        await read()
        clock["now"] = start + TTL_S
        await read()

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert grid == [["chat"], ["chat"]], "a read 11.999 s after the probe is cached; a read at 12 s probes again"


@pytest.mark.usefixtures("clock")
def test_concurrent_cold_readers_share_one_probe(monkeypatch):
    asked: list[list[str]] = []

    async def scenario():
        entered = asyncio.Event()
        gate = asyncio.Event()

        async def probe(config):
            # The probe holds until every reader has arrived, so each one finds the service cold or in flight.
            asked.append(sorted(config["backends"]))
            entered.set()
            await gate.wait()
            return [{"service_id": "chat", "key": "chat", "live_status": "up", "status": "up", "source": "http"}]

        monkeypatch.setattr(status_endpoint, "_backend_grid", probe)
        readers = asyncio.gather(*(read() for _ in range(5)))
        await asyncio.wait_for(entered.wait(), timeout=5)
        gate.set()
        return await readers

    results = asyncio.run(asyncio.wait_for(scenario(), timeout=30))

    assert asked == [["chat"]], "five cold readers at once should make one probe"
    assert all(service_ids(result) == ["chat"] for result in results)
