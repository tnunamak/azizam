"""The exclusive pool's shadow in the arbitration store: a token per lease, settled by the lease's own outcome.

Design §16 slice 1, criteria 6 and 7 (revised after review), with §4.2 (charge), §4.3 (operation intent),
§4.4 (evidence), A10 (release is a settlement, not an action) and B1 (stock release is the only settlement
that frees stock). Each test passes its own store file or a fake store. None touches data/arbitration.db.

Timing in these tests comes from threading.Event barriers. A gate holds the worker at a chosen store call,
and drain_shadow() waits until the worker has run every queued job. No test relies on sleep.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import gateway.exclusive_pool as pool
from gateway.arbitration.holdings import SettlementKind
from gateway.arbitration.store import ENDED_RECORDED, RELEASED, ArbitrationStore
from gateway.arbitration.types import ResourceRef
from gateway.exclusive_pool import (
    MIB,
    ExclusivePoolLeaseManager,
    PoolCapacityError,
    parse_resource_capacity,
    pool_inventory,
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

TOKEN = ResourceRef("gpu0/lock", "1")
VRAM = ResourceRef("gpu0", "1")
TOTAL = 24 * 1024 * MIB
LOGGER = "gateway.exclusive_pool"
GATE_WAIT_S = 10.0


def spec():
    spec_ = parse_resource_capacity("trellis", ENABLED)
    assert spec_ is not None
    return spec_


def document(free_mb: int) -> dict:
    return {"devices": [{"vram_free": free_mb * MIB, "vram_total": TOTAL}]}


def observer(free_mb: int):
    async def observe() -> dict:
        return document(free_mb)

    return observe


def _rows(path: Path, sql: str) -> list[tuple]:
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql).fetchall()
    finally:
        connection.close()


class GatedStore(ArbitrationStore):
    """A real store whose comparison write waits on the worker until `gate` is set. `started` is set when it begins to wait."""

    def __init__(self, path: Path, gate: threading.Event) -> None:
        super().__init__(path, busy_timeout_s=5.0)
        self.gate = gate
        self.started = threading.Event()
        self.settled_keys: list[str] = []

    def record_comparison(self, **kwargs: Any) -> None:
        self.started.set()
        self.gate.wait(GATE_WAIT_S)
        super().record_comparison(**kwargs)

    def settle(self, **kwargs: Any) -> str:
        self.settled_keys.append(kwargs["op_key"])
        return super().settle(**kwargs)


class FailingComparisonStore(ArbitrationStore):
    """Commits every acquire, then fails the comparison write, as a disk error between the two writes would."""

    def record_comparison(self, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("disk I/O error")


def test_the_shadow_token_is_the_pool_lock_and_the_vram_reading_is_a_separate_resource() -> None:
    """§16 criterion 6: the exclusion token is the pool's lock, separate from the VRAM reading (A10, §3.1)."""
    inventory = pool_inventory(spec())
    assert inventory["gpu0"] == TOKEN
    assert inventory["vram_mb"] == VRAM
    assert inventory["gpu0"] != inventory["vram_mb"]


def test_a_normal_return_settles_the_token_with_stock_release_evidence(tmp_path: Path) -> None:
    """§16 criterion 6, B1: the body returned, so the engine's terminal result is the evidence. The token is released."""
    path = tmp_path / "shadow.db"
    store = ArbitrationStore(path)
    manager = ExclusivePoolLeaseManager(shadow_store=store)

    async def run() -> None:
        async with manager.lease(spec(), request_id="r1", observe=observer(20 * 1024)):
            pass

    asyncio.run(run())
    manager.drain_shadow()
    assert store.charge(TOKEN) == 0  # settled by the exit: the token is the only holding, and no VRAM is held
    assert store.charge(VRAM) == 0
    assert _rows(path, "SELECT state, execution_ended, settle_source FROM operations") == [("settled", 0, "lease_completed")]
    assert store.comparison_counts() == {"agree": 1}
    store.close()


def test_a_refused_lease_records_the_comparison_but_no_token(tmp_path: Path) -> None:
    """A10 and §4.3: a refused lease reserves no token. Its comparison is still stored, so disagreements show up."""
    store = ArbitrationStore(tmp_path / "shadow.db")
    manager = ExclusivePoolLeaseManager(shadow_store=store)

    async def run() -> None:
        async with manager.lease(spec(), request_id="r2", observe=observer(1)):
            pass

    with pytest.raises(PoolCapacityError):
        asyncio.run(run())
    manager.drain_shadow()
    assert store.charge(TOKEN) == 0
    assert store.open_operations() == []
    assert store.comparison_counts() == {"agree": 1}
    store.close()


def test_the_shadow_stamps_its_own_observation_time_not_the_legacy_one(tmp_path: Path) -> None:
    """§6 delays: the shadow's freshness comes from its own observe() call. A stale legacy stamp must not make it legacy-only."""
    store = ArbitrationStore(tmp_path / "shadow.db")
    manager = ExclusivePoolLeaseManager(shadow_store=store)

    async def run() -> None:
        async with manager.lease(spec(), request_id="r3", observe=observer(20 * 1024), observed_at=lambda: 0.0):
            pass

    asyncio.run(run())
    manager.drain_shadow()
    assert store.comparison_counts() == {"agree": 1}
    store.close()


def test_each_comparison_records_the_token_holdings_open_at_admission(tmp_path: Path) -> None:
    """§16 criterion 6: a body that raised leaves its token charged. The next admission records one open holding."""
    path = tmp_path / "shadow.db"
    store = ArbitrationStore(path)
    manager = ExclusivePoolLeaseManager(shadow_store=store)

    async def run() -> None:
        with pytest.raises(RuntimeError):
            async with manager.lease(spec(), request_id="first", observe=observer(20 * 1024)):
                raise RuntimeError("engine failed")
        async with manager.lease(spec(), request_id="second", observe=observer(20 * 1024)):
            pass

    asyncio.run(run())
    manager.drain_shadow()
    assert _rows(path, "SELECT attempt_key, open_holdings FROM comparisons ORDER BY id") == [
        ("gpu0/first", 0), ("gpu0/second", 1),
    ]
    store.close()


# --- barrier tests: the lease never waits for the worker --------------------------------------

def test_an_acquire_that_committed_is_settled_when_the_gated_comparison_is_released(tmp_path: Path) -> None:
    """§16 criteria 6 and 7: the token is committed while the comparison is held. The lease finishes without waiting, and the row settles once the gate opens."""
    path = tmp_path / "shadow.db"
    gate = threading.Event()
    store = GatedStore(path, gate)
    manager = ExclusivePoolLeaseManager(shadow_store=store)

    async def run() -> None:
        async with manager.lease(spec(), request_id="g1", observe=observer(20 * 1024)):
            pass

    asyncio.run(run())
    assert store.started.wait(GATE_WAIT_S)  # the worker has committed the acquire and is held at the comparison
    assert store.charge(TOKEN) == 1
    gate.set()
    manager.drain_shadow()
    assert store.charge(TOKEN) == 0
    assert _rows(path, "SELECT state, settle_source FROM operations") == [("settled", "lease_completed")]
    assert _rows(path, "SELECT attempt_key FROM comparisons") == [("gpu0/g1",)]
    store.close()


def test_a_body_that_raises_keeps_its_original_error_and_leaves_the_token_charged(tmp_path: Path) -> None:
    """§16 criterion 6 and 7: the caller sees the body's exception unchanged. The exit records EXECUTION_ENDED, and the token stays charged."""
    path = tmp_path / "shadow.db"
    gate = threading.Event()
    store = GatedStore(path, gate)
    manager = ExclusivePoolLeaseManager(shadow_store=store)
    sentinel = ValueError("engine refused")

    async def run() -> None:
        async with manager.lease(spec(), request_id="e1", observe=observer(20 * 1024)):
            raise sentinel

    with pytest.raises(ValueError) as caught:
        asyncio.run(run())
    assert caught.value is sentinel
    assert store.started.wait(GATE_WAIT_S)
    gate.set()
    manager.drain_shadow()
    assert _rows(path, "SELECT state, execution_ended FROM operations") == [("committed", 1)]
    assert store.charge(TOKEN) == 1
    store.close()


def test_a_cancelled_body_propagates_cancellation_and_leaves_the_token_charged(tmp_path: Path) -> None:
    """§16 criterion 7: CancelledError reaches the caller unchanged. The exit records EXECUTION_ENDED, and the token stays charged."""
    path = tmp_path / "shadow.db"
    gate = threading.Event()
    store = GatedStore(path, gate)
    manager = ExclusivePoolLeaseManager(shadow_store=store)

    async def run() -> None:
        entered = asyncio.Event()

        async def leased() -> None:
            async with manager.lease(spec(), request_id="c1", observe=observer(20 * 1024)):
                entered.set()
                await asyncio.Event().wait()  # never set: the body waits until it is cancelled

        task = asyncio.create_task(leased())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert store.started.wait(GATE_WAIT_S)
    gate.set()
    manager.drain_shadow()
    assert _rows(path, "SELECT state, execution_ended FROM operations") == [("committed", 1)]
    assert store.charge(TOKEN) == 1
    store.close()


def test_a_fully_blocked_worker_adds_no_measurable_wait_to_a_lease(tmp_path: Path) -> None:
    """§16 criterion 7: with the worker held at every comparison, each lease still finishes in under 50 ms."""
    gate = threading.Event()
    store = GatedStore(tmp_path / "shadow.db", gate)
    manager = ExclusivePoolLeaseManager(shadow_store=store)
    durations: list[float] = []

    async def run() -> None:
        for index in range(3):
            started = time.perf_counter()
            async with manager.lease(spec(), request_id=f"b{index}", observe=observer(20 * 1024)):
                pass
            durations.append(time.perf_counter() - started)

    asyncio.run(run())
    assert store.started.wait(GATE_WAIT_S)
    assert max(durations) < 0.05, durations
    gate.set()
    manager.drain_shadow()
    store.close()


def test_a_full_queue_drops_the_admission_and_its_exit_together(tmp_path: Path) -> None:
    """§16 criterion 7: when the admission queue is full, the admission and its exit are dropped together. Queued work still runs."""
    path = tmp_path / "shadow.db"
    gate = threading.Event()
    store = GatedStore(path, gate)
    manager = ExclusivePoolLeaseManager(shadow_store=store, shadow_queue_limit=3)

    async def run() -> None:
        async with manager.lease(spec(), request_id="q1", observe=observer(20 * 1024)):
            pass
    asyncio.run(run())
    assert store.started.wait(GATE_WAIT_S)  # the worker holds q1; the queue is empty

    async def queued() -> None:
        for name in ("q2", "q3", "q4"):
            async with manager.lease(spec(), request_id=name, observe=observer(20 * 1024)):
                pass

    asyncio.run(queued())  # q1 still holds its slot; q2 and q3 take the other two; q4 is dropped
    gate.set()
    manager.drain_shadow()
    assert _rows(path, "SELECT attempt_key FROM comparisons ORDER BY id") == [("gpu0/q1",), ("gpu0/q2",), ("gpu0/q3",)]
    assert sorted(store.settled_keys) == ["gpu0/q1", "gpu0/q2", "gpu0/q3"]
    assert store.operation("gpu0/q4") is None
    assert store.charge(TOKEN) == 0
    store.close()


def test_a_comparison_that_fails_after_the_acquire_still_settles_the_token(tmp_path: Path) -> None:
    """§16 criterion 7: the acquire committed, then the comparison failed. The exit still runs, so the token is released."""
    path = tmp_path / "shadow.db"
    store = FailingComparisonStore(path)
    manager = ExclusivePoolLeaseManager(shadow_store=store)

    async def run() -> None:
        async with manager.lease(spec(), request_id="f1", observe=observer(20 * 1024)):
            pass

    asyncio.run(run())
    manager.drain_shadow()
    assert _rows(path, "SELECT state, settle_source FROM operations") == [("settled", "lease_completed")]
    assert store.comparison_counts() == {}
    store.close()


# --- failures never reach the lease ------------------------------------------------------------

class _BrokenStore:
    """Every call fails the way a locked or unreadable database does."""

    def __getattr__(self, name: str) -> Any:
        def fail(*_args: Any, **_kwargs: Any) -> None:
            raise sqlite3.OperationalError(f"{name}: disk I/O error")

        return fail


def test_a_failing_store_never_changes_a_lease_and_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    """§16 criterion 7: any store error is caught. The lease result is the same, and the error kind is logged once."""
    manager = ExclusivePoolLeaseManager(shadow_store=_BrokenStore())  # type: ignore[arg-type]
    results: list[int] = []

    async def run() -> None:
        for index in range(3):
            async with manager.lease(spec(), request_id=f"r{index}", observe=observer(20 * 1024)) as claim:
                results.append(claim.vram_mb)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        asyncio.run(run())
        manager.drain_shadow()
    assert results == [8 * 1024, 8 * 1024, 8 * 1024]
    warnings = [record for record in caplog.records if record.name == LOGGER]
    assert len(warnings) == 1
    assert "OperationalError" in warnings[0].getMessage()


def test_a_store_that_cannot_be_opened_is_logged_once_and_the_leases_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                       caplog: pytest.LogCaptureFixture) -> None:
    """§16 criterion 7: if the default store cannot open, leases run unchanged, and the failure is logged once."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    monkeypatch.setenv("AZIZAM_ARBITRATION_DB", str(blocker / "arbitration.db"))
    opens: list[Path] = []
    real_store = pool.ArbitrationStore

    def counting_open(path: Path) -> ArbitrationStore:
        opens.append(path)
        return real_store(path)

    monkeypatch.setattr(pool, "ArbitrationStore", counting_open)
    manager = ExclusivePoolLeaseManager()
    lease_results: list[int] = []

    async def run() -> None:
        for index in range(3):
            async with manager.lease(spec(), request_id=f"d{index}", observe=observer(20 * 1024)) as claim:
                lease_results.append(claim.vram_mb)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        asyncio.run(run())
        manager.drain_shadow()
    assert lease_results == [8 * 1024, 8 * 1024, 8 * 1024]
    assert len(opens) == 1  # the retry waits a minute, so three leases make one attempt
    assert len([record for record in caplog.records if record.name == LOGGER]) == 1


def test_a_failed_open_is_retried_after_the_retry_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§16 criterion 7: a failed open is retried once the window has passed, and then the shadow records again."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")
    path = tmp_path / "arbitration.db"
    monkeypatch.setenv("AZIZAM_ARBITRATION_DB", str(blocker / "arbitration.db"))
    manager = ExclusivePoolLeaseManager()

    async def lease(name: str) -> None:
        async with manager.lease(spec(), request_id=name, observe=observer(20 * 1024)):
            pass

    asyncio.run(lease("early"))
    manager.drain_shadow()
    monkeypatch.setenv("AZIZAM_ARBITRATION_DB", str(path))
    asyncio.run(lease("within-window"))
    manager.drain_shadow()
    assert not path.exists()  # still inside the retry window: nothing was opened
    monkeypatch.setattr(pool, "SHADOW_OPEN_RETRY_S", 0.0)
    asyncio.run(lease("after-window"))
    manager.drain_shadow()
    assert _rows(path, "SELECT attempt_key FROM comparisons") == [("gpu0/after-window",)]


def test_the_queue_limit_bounds_lifecycles_across_pools_until_their_exits_run(tmp_path: Path) -> None:
    """§16 criterion 7: an admitted lease keeps its slot until its exit has run, so leases on many pools cannot grow the queue past the limit."""
    path = tmp_path / "shadow.db"
    gate = threading.Event()
    store = GatedStore(path, gate)
    manager = ExclusivePoolLeaseManager(shadow_store=store, shadow_queue_limit=1)
    other = parse_resource_capacity("other", {**ENABLED, "pool_id": "gpu1"})
    assert other is not None

    async def run() -> None:
        async with manager.lease(spec(), request_id="a", observe=observer(20 * 1024)):
            assert store.started.wait(GATE_WAIT_S)  # the worker is inside a's admission
            async with manager.lease(other, request_id="b", observe=observer(20 * 1024)):
                pass  # a's lifecycle holds the only slot, so b is dropped with its exit

    asyncio.run(run())
    gate.set()
    manager.drain_shadow()
    assert _rows(path, "SELECT attempt_key FROM comparisons ORDER BY id") == [("gpu0/a",)]
    assert store.operation("gpu1/b") is None
    assert store.charge(TOKEN) == 0
    store.close()


def test_a_worker_that_fails_to_start_is_started_again_by_the_next_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """§16 criterion 7: a failed thread start queues nothing and leaves no orphan. The next lease starts the worker."""
    path = tmp_path / "shadow.db"
    store = ArbitrationStore(path)
    manager = ExclusivePoolLeaseManager(shadow_store=store)
    real_start = threading.Thread.start
    calls = {"n": 0}

    def flaky_start(self: threading.Thread) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("can't start new thread")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", flaky_start)

    async def run() -> None:
        for name in ("t1", "t2"):
            async with manager.lease(spec(), request_id=name, observe=observer(20 * 1024)):
                pass

    asyncio.run(run())
    manager.drain_shadow()
    assert _rows(path, "SELECT attempt_key FROM comparisons ORDER BY id") == [("gpu0/t2",)]
    assert _rows(path, "SELECT op_key, state FROM operations") == [("gpu0/t2", "settled")]
    store.close()
