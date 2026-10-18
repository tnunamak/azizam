"""Unit tests for gateway.swap_drain: serving leases and the bounded swap drain.

These tests drive the module directly with asyncio. The proxy integration
(ensure_target, the LiteLLM inner hop, the systemd driver) is covered in
tests/test_swap_drain_lifecycle.py.
"""

from __future__ import annotations

import asyncio
import gc
import sys
import time
from pathlib import Path

import pytest

from tests.support.virtual_loop import VirtualEventLoop

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.swap_drain import (  # noqa: E402
    SwapDeferred,
    SwapDrain,
    SwapDrainSettings,
)


def _settings(**overrides) -> SwapDrainSettings:
    values = {"enabled": True, "timeout_s": 0.2}
    values.update(overrides)
    return SwapDrainSettings(**values)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_default_off_and_bounded() -> None:
    settings = SwapDrainSettings.from_llm_config(None)
    assert settings.enabled is False
    assert settings.timeout_s == 120.0


def test_settings_read_from_llm_block() -> None:
    settings = SwapDrainSettings.from_llm_config(
        {"swap_drain": {"enabled": True, "timeout_s": 30}}
    )
    assert settings == SwapDrainSettings(enabled=True, timeout_s=30.0)


def test_settings_ignore_the_removed_lease_max_age() -> None:
    """A config written for the earlier lease_max_age_s still loads."""
    settings = SwapDrainSettings.from_llm_config({"swap_drain": {"enabled": True, "lease_max_age_s": 600}})
    assert settings == SwapDrainSettings(enabled=True)


@pytest.mark.parametrize(
    "block",
    [
        {"enabled": True, "timeout_s": "soon"},
        {"enabled": True, "timeout_s": -1},
        {"enabled": True, "timeout_s": 0},
    ],
)
def test_settings_reject_unbounded_values_by_falling_back_to_defaults(block) -> None:
    settings = SwapDrainSettings.from_llm_config({"swap_drain": block})
    assert settings.enabled is True
    assert settings.timeout_s == 120.0


# ---------------------------------------------------------------------------
# Lease counting
# ---------------------------------------------------------------------------


def test_lease_counts_until_released_and_release_is_idempotent() -> None:
    drain = SwapDrain()
    first = drain.lease("llm")
    second = drain.lease("llm")
    other = drain.lease("other-llm")
    assert drain.in_flight("llm") == 2
    assert drain.in_flight("other-llm") == 1

    first.release()
    first.release()
    assert drain.in_flight("llm") == 1
    second.release()
    other.release()
    assert drain.in_flight("llm") == 0


# ---------------------------------------------------------------------------
# clear_for_swap: wait, timeout; a lease never expires
# ---------------------------------------------------------------------------


def test_clear_for_swap_returns_at_once_when_idle() -> None:
    drain = SwapDrain()

    async def scenario() -> None:
        await drain.clear_for_swap("llm", _settings(timeout_s=5))

    # No lease is held and nothing releases one: returning at all is the oracle.
    # The 30 s wait_for only turns a hang into a failure (a 5 s drain timeout
    # would also end the wait, but with SwapDeferred).
    asyncio.run(asyncio.wait_for(scenario(), 30))


def test_clear_for_swap_waits_for_the_last_release() -> None:
    drain = SwapDrain()
    lease = drain.lease("llm")
    order: list[str] = []

    async def scenario() -> None:
        async def finish_request() -> None:
            await asyncio.sleep(0.05)
            order.append("request finished")
            lease.release()

        task = asyncio.create_task(finish_request())
        await drain.clear_for_swap("llm", _settings(timeout_s=5))
        order.append("swap cleared")
        await task

    asyncio.run(scenario())
    assert order == ["request finished", "swap cleared"]


def test_clear_for_swap_times_out_with_swap_deferred() -> None:
    loop = VirtualEventLoop()
    drain = SwapDrain(clock=loop.time)
    lease = drain.lease("llm")

    async def scenario() -> SwapDeferred:
        start = loop.time()
        with pytest.raises(SwapDeferred) as caught:
            await drain.clear_for_swap("llm", _settings(timeout_s=0.1))
        assert loop.time() - start == 0.1
        return caught.value

    with asyncio.Runner(loop_factory=lambda: loop) as runner:
        deferred = runner.run(scenario())
    assert deferred.backend == "llm"
    assert deferred.in_flight == 1
    assert deferred.retry_after_s > 0
    # The lease is still counted: a deferred swap changes nothing.
    assert drain.in_flight("llm") == 1
    lease.release()


def test_an_old_lease_still_defers_the_swap_and_reports_its_age(capsys) -> None:
    """A lease never ages out: an hours-old one is a long stream as far as the
    drain can tell. The swap is deferred and the deferral names its age."""
    offset = [0.0]
    drain = SwapDrain(clock=lambda: time.monotonic() + offset[0])
    lease = drain.lease("llm")
    offset[0] += 3 * 3600

    async def scenario() -> None:
        with pytest.raises(SwapDeferred):
            await drain.clear_for_swap("llm", _settings(timeout_s=0.05))

    asyncio.run(scenario())
    assert drain.in_flight("llm") == 1
    assert "oldest 10800s" in capsys.readouterr().out
    lease.release()


# ---------------------------------------------------------------------------
# Streams: the lease outlives the handler and is released by the stream
# ---------------------------------------------------------------------------


async def _chunks(gate: asyncio.Event, log: list[str]):
    try:
        yield b"first"
        await gate.wait()
        yield b"last"
    finally:
        log.append("upstream closed")


def test_stream_releases_after_the_final_byte() -> None:
    drain = SwapDrain()
    lease = drain.lease("llm")
    log: list[str] = []

    async def scenario() -> None:
        gate = asyncio.Event()
        stream = lease.hand_off(_chunks(gate, log))
        assert lease.handed_off
        assert await stream.__anext__() == b"first"
        assert drain.in_flight("llm") == 1
        gate.set()
        assert await stream.__anext__() == b"last"
        assert drain.in_flight("llm") == 1
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    asyncio.run(scenario())
    assert drain.in_flight("llm") == 0
    assert log == ["upstream closed"]


def test_stream_releases_on_cancellation() -> None:
    drain = SwapDrain()
    lease = drain.lease("llm")
    log: list[str] = []

    async def scenario() -> None:
        gate = asyncio.Event()
        stream = lease.hand_off(_chunks(gate, log))

        async def consume() -> None:
            async for _ in stream:
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.01)
        assert drain.in_flight("llm") == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert drain.in_flight("llm") == 0
    assert log == ["upstream closed"]


def test_stream_releases_when_closed_early() -> None:
    drain = SwapDrain()
    lease = drain.lease("llm")
    log: list[str] = []

    async def scenario() -> None:
        stream = lease.hand_off(_chunks(asyncio.Event(), log))
        assert await stream.__anext__() == b"first"
        await stream.aclose()

    asyncio.run(scenario())
    assert drain.in_flight("llm") == 0
    assert log == ["upstream closed"], "closing the wrapper must close the upstream stream too"


def test_stream_releases_on_upstream_error() -> None:
    drain = SwapDrain()
    lease = drain.lease("llm")

    async def broken():
        yield b"first"
        raise RuntimeError("upstream reset")

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            async for _ in lease.hand_off(broken()):
                pass

    asyncio.run(scenario())
    assert drain.in_flight("llm") == 0


def test_stream_that_is_never_started_releases_when_dropped() -> None:
    """A StreamingResponse whose client left before the first byte never runs
    its generator body, so a ``finally`` alone would leak the lease."""
    drain = SwapDrain()
    lease = drain.lease("llm")
    stream = lease.hand_off(_chunks(asyncio.Event(), []))
    assert drain.in_flight("llm") == 1
    del stream
    gc.collect()
    assert drain.in_flight("llm") == 0


# ---------------------------------------------------------------------------
# Diagnostics: count and oldest age per backend, read-only
# ---------------------------------------------------------------------------


def test_snapshot_reports_in_flight_count_and_oldest_age_per_backend() -> None:
    offset = [0.0]
    drain = SwapDrain(clock=lambda: 1000.0 + offset[0])
    old = drain.lease("llm")
    offset[0] += 30
    drain.lease("llm")
    drain.lease("other-llm")
    offset[0] += 10
    assert drain.snapshot() == {
        "llm": {"in_flight": 2, "oldest_lease_age_s": 40.0},
        "other-llm": {"in_flight": 1, "oldest_lease_age_s": 10.0},
    }
    old.release()
    assert drain.snapshot()["llm"] == {"in_flight": 1, "oldest_lease_age_s": 10.0}
    assert drain.in_flight("llm") == 1, "a snapshot changes nothing"


def test_status_swap_state_shows_the_drain() -> None:
    import types

    import status_endpoint

    drain = SwapDrain()
    lease = drain.lease("kobold")
    proxy_module = types.SimpleNamespace(
        _swap_drain=drain,
        _swap_drain_settings=lambda: SwapDrainSettings(enabled=True, timeout_s=120.0),
    )
    state = status_endpoint._swap_state(proxy_module)
    assert state["drain"]["enabled"] is True
    assert state["drain"]["timeout_s"] == 120.0
    assert state["drain"]["backends"]["kobold"]["in_flight"] == 1
    assert state["drain"]["backends"]["kobold"]["oldest_lease_age_s"] >= 0
    lease.release()
    assert status_endpoint._swap_state(proxy_module)["drain"]["backends"] == {}
