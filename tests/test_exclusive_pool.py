"""Exclusive pool: closed capacity schema, RFC 6901 observer facts, the VRAM equation, and process-local leases."""

from __future__ import annotations

import asyncio
import copy

import pytest

from gateway.exclusive_pool import (
    MIB,
    ExclusivePoolLeaseManager,
    ExclusivePoolSpec,
    PoolCapacityError,
    PoolObserverError,
    observation_from_document,
    parse_resource_capacity,
    resolve_pointer,
    snapshot_and_claim,
    PoolObservation,
)

ENABLED = {
    "enabled": True,
    "mode": "exclusive-route-pool",
    "pool_id": "gpu0",
    "max_claim_bytes": 8 * 1024 * MIB,
    "reserve_bytes": 512 * MIB,
    "retry_after_seconds": 30,
    "timeout_seconds": 5,
    "observer": {
        "service": "comfyui",
        "path": "/system_stats",
        "free_pointer": "/devices/0/vram_free",
        "total_pointer": "/devices/0/vram_total",
    },
}


def spec_from(block: dict) -> ExclusivePoolSpec:
    spec = parse_resource_capacity("trellis", block)
    assert spec is not None
    return spec


def test_disabled_capacity_is_none_and_tombstone_holds_only_the_flag() -> None:
    assert parse_resource_capacity("trellis", {"enabled": False}) is None
    with pytest.raises(ValueError, match="holds only"):
        parse_resource_capacity("trellis", {"enabled": False, "pool_id": "gpu0"})


def test_enabled_capacity_is_the_complete_closed_schema() -> None:
    spec = spec_from(ENABLED)
    assert spec.pool_id == "gpu0"
    assert spec.observer_service == "comfyui"

    cases = [
        ({"mode": "shared-pool"}, "mode"),
        ({"pool_id": ""}, "pool_id"),
        ({"max_claim_bytes": 0}, "max_claim_bytes"),
        ({"reserve_bytes": True}, "reserve_bytes"),
        ({"retry_after_seconds": -1}, "retry_after_seconds"),
        ({"timeout_seconds": 1.5}, "timeout_seconds"),
        ({"surprise": 1}, "unknown keys"),
        ({"observer": {"service": "comfyui", "path": "/s"}}, "observer must hold"),
        ({"observer": {**ENABLED["observer"], "path": "http://evil/x"}}, "canonical relative path"),
        ({"observer": {**ENABLED["observer"], "free_pointer": "devices/0"}}, "RFC 6901"),
        ({"observer": {**ENABLED["observer"], "total_pointer": "/a~2b"}}, "RFC 6901"),
        ({"observer": {**ENABLED["observer"], "service": " "}}, "observer service"),
    ]
    for change, message in cases:
        block = copy.deepcopy(ENABLED)
        block.update({k: v for k, v in change.items() if k != "observer"})
        if "observer" in change:
            block["observer"] = change["observer"]
        with pytest.raises(ValueError, match=message):
            parse_resource_capacity("trellis", block)


def test_absent_enabled_flag_is_refused() -> None:
    block = copy.deepcopy(ENABLED)
    del block["enabled"]
    with pytest.raises(ValueError):
        parse_resource_capacity("trellis", block)


def test_rfc6901_pointers_resolve_with_escapes_and_arrays() -> None:
    document = {"a/b": {"c~d": [10, {"x": 3}]}, "list": [0, 1]}
    assert resolve_pointer(document, "/a~1b/c~0d/0") == 10
    assert resolve_pointer(document, "/a~1b/c~0d/1/x") == 3
    assert resolve_pointer(document, "") is document
    for bad in ("/list/01", "/list/-", "/missing", "/list/9"):
        with pytest.raises((KeyError, IndexError)):
            resolve_pointer(document, bad)


@pytest.mark.parametrize("free, total", [
    (True, 10), (5, True), (5.0, 10), (-1, 10), (11, 10), (0, 0),
])
def test_observer_facts_must_be_exact_integers_in_range(free, total) -> None:
    spec = spec_from(ENABLED)
    doc = {"devices": [{"vram_free": free, "vram_total": total}]}
    with pytest.raises(PoolObserverError):
        observation_from_document(doc, spec)


def test_observer_facts_missing_are_refused() -> None:
    spec = spec_from(ENABLED)
    with pytest.raises(PoolObserverError):
        observation_from_document({"devices": []}, spec)


def test_vram_equation_matches_the_design_at_numeric_boundaries() -> None:
    spec = spec_from(ENABLED)
    total = 24 * 1024 * MIB
    free = total - 3 * MIB - 1  # 3 MiB and one byte in use: ceil gives 4
    snapshot, claim = snapshot_and_claim(spec, PoolObservation(free, total), observed_at=None)

    assert snapshot.total_vram_mb == 24 * 1024
    assert snapshot.used_vram_mb == 4 + 512  # ceil((total-free)/MiB) + reserve_mb
    assert claim.vram_mb == 8 * 1024
    assert claim.exclusive_locks == ("gpu0",)


def test_admission_accepts_exact_fit_and_rejects_one_mib_more() -> None:
    spec = spec_from(ENABLED)
    total = 24 * 1024 * MIB

    async def attempt(free_mb: int) -> str:
        async def observe() -> dict:
            return {"devices": [{"vram_free": free_mb * MIB, "vram_total": total}]}

        manager = ExclusivePoolLeaseManager()
        try:
            async with manager.lease(spec, request_id="r1", observe=observe):
                return "accepted"
        except PoolCapacityError as error:
            return error.status.value

    # Free 15872 MiB: used = 8704 + 512 reserve = 9216, available = 24576 - 9216 = 15360 >= claim 8192.
    assert asyncio.run(attempt(15872)) == "accepted"
    # Free 8191 MiB: used = 16385 + 512 = 16897, available = 7679 < claim 8192.
    assert asyncio.run(attempt(8191)) == "delayed"


def test_same_pool_serializes_and_different_pools_do_not() -> None:
    other = dict(ENABLED, pool_id="gpu1")
    spec_a, spec_b = spec_from(ENABLED), spec_from(other)
    total = 24 * 1024 * MIB
    doc = {"devices": [{"vram_free": total, "vram_total": total}]}
    events: list[str] = []

    async def observe() -> dict:
        return doc

    async def job(manager: ExclusivePoolLeaseManager, spec: ExclusivePoolSpec, name: str, hold: float) -> None:
        async with manager.lease(spec, request_id=name, observe=observe):
            events.append(f"start {name}")
            await asyncio.sleep(hold)
            events.append(f"end {name}")

    async def scenario() -> None:
        manager = ExclusivePoolLeaseManager()
        await asyncio.gather(job(manager, spec_a, "a1", 0.05), job(manager, spec_a, "a2", 0.0),
                             job(manager, spec_b, "b1", 0.0))

    asyncio.run(scenario())
    # Same pool: a2 starts only after a1 has ended. Different pool: b1 is not held behind a1.
    assert events.index("start a2") > events.index("end a1")
    assert events.index("start b1") < events.index("end a1")


def test_cancellation_while_waiting_for_the_lock_never_observes_or_claims() -> None:
    spec = spec_from(ENABLED)
    total = 24 * 1024 * MIB
    observed: list[str] = []

    async def observe() -> dict:
        observed.append("x")
        return {"devices": [{"vram_free": total, "vram_total": total}]}

    async def scenario() -> ExclusivePoolLeaseManager:
        manager = ExclusivePoolLeaseManager()
        gate = asyncio.Event()

        async def holder() -> None:
            async with manager.lease(spec, request_id="holder", observe=observe):
                await gate.wait()

        held = asyncio.ensure_future(holder())
        await asyncio.sleep(0.01)
        waiter = asyncio.ensure_future(_enter(manager, spec, observe))
        await asyncio.sleep(0.01)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        gate.set()
        await held
        assert manager.active_claims() == 0
        return manager

    asyncio.run(scenario())
    assert observed == ["x"]  # only the holder observed


async def _enter(manager: ExclusivePoolLeaseManager, spec: ExclusivePoolSpec, observe) -> None:
    async with manager.lease(spec, request_id="waiter", observe=observe):
        pass


def test_claim_is_released_exactly_once_on_error() -> None:
    spec = spec_from(ENABLED)
    total = 24 * 1024 * MIB

    async def observe() -> dict:
        return {"devices": [{"vram_free": total, "vram_total": total}]}

    async def scenario() -> tuple[int, int]:
        manager = ExclusivePoolLeaseManager()
        with pytest.raises(RuntimeError):
            async with manager.lease(spec, request_id="r", observe=observe):
                assert manager.active_claims() == 1
                raise RuntimeError("downstream failed")
        after = manager.active_claims()
        async with manager.lease(spec, request_id="r2", observe=observe):
            pass  # the lock was released too
        return after, manager.active_claims()

    assert asyncio.run(scenario()) == (0, 0)


def test_observer_failure_releases_the_lock() -> None:
    spec = spec_from(ENABLED)

    async def bad() -> dict:
        raise PoolObserverError("observer did not answer")

    total = 24 * 1024 * MIB

    async def good() -> dict:
        return {"devices": [{"vram_free": total, "vram_total": total}]}

    async def scenario() -> None:
        manager = ExclusivePoolLeaseManager()
        with pytest.raises(PoolObserverError):
            async with manager.lease(spec, request_id="r", observe=bad):
                pass
        async with asyncio.timeout(1):
            async with manager.lease(spec, request_id="r2", observe=good):
                pass

    asyncio.run(scenario())
