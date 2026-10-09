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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.swap_drain import (  # noqa: E402
    SwapDeferred,
    SwapDrain,
    SwapDrainSettings,
)


def _settings(**overrides) -> SwapDrainSettings:
    values = {"enabled": True, "timeout_s": 0.2, "lease_max_age_s": 60.0}
    values.update(overrides)
    return SwapDrainSettings(**values)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_settings_default_off_and_bounded() -> None:
    settings = SwapDrainSettings.from_llm_config(None)
    assert settings.enabled is False
    assert settings.timeout_s == 120.0
    assert settings.lease_max_age_s == 1800.0


def test_settings_read_from_llm_block() -> None:
    settings = SwapDrainSettings.from_llm_config(
        {"swap_drain": {"enabled": True, "timeout_s": 30, "lease_max_age_s": 600}}
    )
    assert settings == SwapDrainSettings(enabled=True, timeout_s=30.0, lease_max_age_s=600.0)


@pytest.mark.parametrize(
    "block",
    [
        {"enabled": True, "timeout_s": "soon"},
        {"enabled": True, "timeout_s": -1},
        {"enabled": True, "lease_max_age_s": 0},
    ],
)
def test_settings_reject_unbounded_values_by_falling_back_to_defaults(block) -> None:
    settings = SwapDrainSettings.from_llm_config({"swap_drain": block})
    assert settings.enabled is True
    assert settings.timeout_s > 0
    assert settings.lease_max_age_s > 0


# ---------------------------------------------------------------------------
# Lease counting
# ---------------------------------------------------------------------------


def test_lease_counts_until_released_and_release_is_idempotent() -> None:
    drain = SwapDrain()
    first = drain.lease("llm", "a")
    second = drain.lease("llm", "a")
    other = drain.lease("other-llm", "z")
    assert drain.in_flight("llm", max_age_s=60) == 2
    assert drain.in_flight("other-llm", max_age_s=60) == 1

    first.release()
    first.release()
    assert drain.in_flight("llm", max_age_s=60) == 1
    second.release()
    other.release()
    assert drain.in_flight("llm", max_age_s=60) == 0


def test_lease_is_refused_when_backend_was_made_ready_for_another_target() -> None:
    drain = SwapDrain()
    # Nothing recorded yet: any target may lease.
    assert drain.lease("llm", "a") is not None
    drain.mark_ready("llm", "b")
    assert drain.lease("llm", "a") is None
    assert drain.lease("llm", "b") is not None
    # A lease for "whatever is loaded" never checks the target.
    assert drain.lease("llm", None) is not None


# ---------------------------------------------------------------------------
# clear_for_swap: wait, timeout, max age
# ---------------------------------------------------------------------------


def test_clear_for_swap_returns_at_once_when_idle() -> None:
    drain = SwapDrain()

    async def scenario() -> float:
        start = time.monotonic()
        await drain.clear_for_swap("llm", _settings(timeout_s=5))
        return time.monotonic() - start

    assert asyncio.run(scenario()) < 0.5


def test_clear_for_swap_waits_for_the_last_release() -> None:
    drain = SwapDrain()
    lease = drain.lease("llm", "a")
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
    drain = SwapDrain()
    lease = drain.lease("llm", "a")

    async def scenario() -> SwapDeferred:
        start = time.monotonic()
        with pytest.raises(SwapDeferred) as caught:
            await drain.clear_for_swap("llm", _settings(timeout_s=0.1))
        assert 0.09 <= time.monotonic() - start < 2.0
        return caught.value

    deferred = asyncio.run(scenario())
    assert deferred.backend == "llm"
    assert deferred.in_flight == 1
    assert deferred.retry_after_s > 0
    # The lease is still counted: a deferred swap changes nothing.
    assert drain.in_flight("llm", max_age_s=60) == 1
    lease.release()


def test_a_leaked_lease_stops_blocking_swaps_after_its_max_age() -> None:
    clock = _FakeClock()
    drain = SwapDrain(clock=clock)
    leaked = drain.lease("llm", "a")  # never released
    assert drain.in_flight("llm", max_age_s=60) == 1

    clock.now += 61
    assert drain.in_flight("llm", max_age_s=60) == 0

    async def scenario() -> float:
        start = time.monotonic()
        await drain.clear_for_swap("llm", _settings(timeout_s=5, lease_max_age_s=60))
        return time.monotonic() - start

    assert asyncio.run(scenario()) < 0.5
    # Releasing an expired lease later is harmless.
    leaked.release()
    assert drain.in_flight("llm", max_age_s=60) == 0


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
    lease = drain.lease("llm", "a")
    log: list[str] = []

    async def scenario() -> None:
        gate = asyncio.Event()
        stream = lease.hand_off(_chunks(gate, log))
        assert lease.handed_off
        assert await stream.__anext__() == b"first"
        assert drain.in_flight("llm", max_age_s=60) == 1
        gate.set()
        assert await stream.__anext__() == b"last"
        assert drain.in_flight("llm", max_age_s=60) == 1
        with pytest.raises(StopAsyncIteration):
            await stream.__anext__()

    asyncio.run(scenario())
    assert drain.in_flight("llm", max_age_s=60) == 0
    assert log == ["upstream closed"]


def test_stream_releases_on_cancellation() -> None:
    drain = SwapDrain()
    lease = drain.lease("llm", "a")
    log: list[str] = []

    async def scenario() -> None:
        gate = asyncio.Event()
        stream = lease.hand_off(_chunks(gate, log))

        async def consume() -> None:
            async for _ in stream:
                pass

        task = asyncio.create_task(consume())
        await asyncio.sleep(0.01)
        assert drain.in_flight("llm", max_age_s=60) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert drain.in_flight("llm", max_age_s=60) == 0
    assert log == ["upstream closed"]


def test_stream_releases_when_closed_early() -> None:
    drain = SwapDrain()
    lease = drain.lease("llm", "a")
    log: list[str] = []

    async def scenario() -> None:
        stream = lease.hand_off(_chunks(asyncio.Event(), log))
        assert await stream.__anext__() == b"first"
        await stream.aclose()

    asyncio.run(scenario())
    assert drain.in_flight("llm", max_age_s=60) == 0
    assert log == ["upstream closed"], "closing the wrapper must close the upstream stream too"


def test_stream_releases_on_upstream_error() -> None:
    drain = SwapDrain()
    lease = drain.lease("llm", "a")

    async def broken():
        yield b"first"
        raise RuntimeError("upstream reset")

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            async for _ in lease.hand_off(broken()):
                pass

    asyncio.run(scenario())
    assert drain.in_flight("llm", max_age_s=60) == 0


def test_stream_that_is_never_started_releases_when_dropped() -> None:
    """A StreamingResponse whose client left before the first byte never runs
    its generator body, so a ``finally`` alone would leak the lease."""
    drain = SwapDrain()
    lease = drain.lease("llm", "a")
    stream = lease.hand_off(_chunks(asyncio.Event(), []))
    assert drain.in_flight("llm", max_age_s=60) == 1
    del stream
    gc.collect()
    assert drain.in_flight("llm", max_age_s=60) == 0
