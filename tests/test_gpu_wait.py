"""A request for a GPU pool another request holds waits its turn (``LeaseWait``) without its run slot (``RunSlots``).

The run-core cells (a job waits, runs in order, runs out of budget, is cancelled while it waits) are in
``test_adapter_differential_s7_mesh_rows.py``; this file proves the two primitives they stand on, with no engine.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager, nullcontext
from typing import Any, AsyncIterator, Callable

import pytest

from gateway.binding_descriptors import ErrorCollector, _check_retry
from gateway.exclusive_pool import (
    MIB,
    ExclusivePoolLeaseManager,
    LeaseWait,
    LeaseWaitStopped,
    PoolCapacityError,
    parse_resource_capacity,
)
from gateway.run_slots import RunSlots
from gateway.workflow_run import GPU_WAIT_S, retry_policy

TOTAL = 24 * 1024 * MIB


def spec(pool_id: str):
    return parse_resource_capacity("svc", {
        "enabled": True, "mode": "exclusive-route-pool", "pool_id": pool_id, "max_claim_bytes": 8 * 1024 * MIB,
        "reserve_bytes": 512 * MIB, "retry_after_seconds": 30, "timeout_seconds": 5,
        "observer": {"service": "obs", "path": "/stats", "free_pointer": "/free", "total_pointer": "/total"}})


async def roomy() -> dict[str, int]:
    return {"free": TOTAL, "total": TOTAL}


async def never() -> None:
    await asyncio.Event().wait()


def turn(budget_s: float = 5.0, *, queued: Callable[[], Any] | None = None, stop: Callable[[], Any] = never,
         log: list[str] | None = None, name: str = "") -> LeaseWait:
    @asynccontextmanager
    async def bracket() -> AsyncIterator[None]:
        if log is not None:
            log.append(f"wait {name}")
        async with (queued() if queued is not None else nullcontext()):
            yield
        if log is not None:
            log.append(f"ready {name}")
    return LeaseWait(budget_s=budget_s, queued=bracket, stop=stop)


async def settle_loop() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_waiters_take_a_held_pool_in_arrival_order_and_each_waits_once() -> None:
    pool = spec("gpu0")
    log: list[str] = []

    async def scenario() -> list[str]:
        leases = ExclusivePoolLeaseManager()
        release_first = asyncio.Event()

        async def job(name: str, wait: Any) -> None:
            async with leases.lease(pool, request_id=name, observe=roomy, wait=wait):
                log.append(f"run {name}")
                if name == "a":
                    await release_first.wait()

        first = asyncio.create_task(job("a", True))
        await settle_loop()
        waiters = []
        for name in ("b", "c", "d"):
            waiters.append(asyncio.create_task(job(name, turn(log=log, name=name))))
            await settle_loop()
        assert log == ["run a", "wait b", "wait c", "wait d"]
        release_first.set()
        await asyncio.gather(first, *waiters)
        return log

    assert asyncio.run(scenario()) == ["run a", "wait b", "wait c", "wait d", "ready b", "run b", "ready c", "run c",
                                       "ready d", "run d"]


def test_a_free_pool_is_leased_without_a_wait() -> None:
    log: list[str] = []

    async def scenario() -> None:
        async with ExclusivePoolLeaseManager().lease(spec("gpu0"), request_id="a", observe=roomy, wait=turn(log=log)):
            pass

    asyncio.run(scenario())
    assert log == []


def test_the_end_of_the_budget_refuses_as_held_and_leaves_no_lock_behind() -> None:
    pool = spec("gpu0")

    async def scenario() -> tuple[PoolCapacityError, str]:
        leases = ExclusivePoolLeaseManager()
        held = asyncio.Event()
        done = asyncio.Event()

        async def holder() -> None:
            async with leases.lease(pool, request_id="a", observe=roomy):
                held.set()
                await done.wait()

        task = asyncio.create_task(holder())
        await held.wait()
        with pytest.raises(PoolCapacityError) as refused:
            async with leases.lease(pool, request_id="b", observe=roomy, wait=turn(0.05)):
                pass
        done.set()
        await task
        async with leases.lease(pool, request_id="c", observe=roomy, wait=False):  # the lock is free: not leaked
            return refused.value, "c leased"

    error, after = asyncio.run(scenario())
    assert error.lease_held and error.status.value == "delayed"
    assert after == "c leased"


def test_stop_ends_the_wait_at_once_and_the_next_waiter_gets_the_pool() -> None:
    pool = spec("gpu0")

    async def scenario() -> list[str]:
        leases = ExclusivePoolLeaseManager()
        stop_b = asyncio.Event()
        done = asyncio.Event()
        order: list[str] = []

        async def job(name: str, wait: Any) -> None:
            try:
                async with leases.lease(pool, request_id=name, observe=roomy, wait=wait):
                    order.append(name)
                    if name == "a":
                        await done.wait()
            except LeaseWaitStopped:
                order.append(f"{name} stopped")

        a = asyncio.create_task(job("a", True))
        await settle_loop()
        b = asyncio.create_task(job("b", turn(stop=stop_b.wait)))
        await settle_loop()
        c = asyncio.create_task(job("c", turn()))
        await settle_loop()
        stop_b.set()
        await b  # ends while a still holds the pool
        assert order == ["a", "b stopped"]
        done.set()
        await asyncio.gather(a, c)
        return order

    assert asyncio.run(scenario()) == ["a", "b stopped", "c"]


def test_a_wait_gives_its_run_slot_back_so_a_job_for_another_pool_runs() -> None:
    """Two run slots. Job a holds pool gpu0; job b waits for gpu0. Job c (pool gpu1) runs while a still works, because b
    gave its slot back for the wait. Without that, c would wait for a to end."""
    slots_held = RunSlots()

    async def scenario() -> tuple[list[str], int]:
        slots = asyncio.Semaphore(2)
        leases = ExclusivePoolLeaseManager()
        finish_a = asyncio.Event()
        order: list[str] = []

        async def job(name: str, pool_id: str) -> None:
            async with slots_held.hold(name, slots):
                wait = turn(queued=lambda: slots_held.released(name))
                async with leases.lease(spec(pool_id), request_id=name, observe=roomy, wait=wait):
                    order.append(f"run {name}")
                    if name == "a":
                        await finish_a.wait()

        a = asyncio.create_task(job("a", "gpu0"))
        await settle_loop()
        b = asyncio.create_task(job("b", "gpu0"))
        await settle_loop()
        c = asyncio.create_task(job("c", "gpu1"))
        await asyncio.wait_for(c, timeout=1)  # c ends while a holds gpu0 and b waits for it
        assert order == ["run a", "run c"]
        finish_a.set()
        await asyncio.gather(a, b)
        return order, slots._value

    order, free = asyncio.run(scenario())
    assert order == ["run a", "run c", "run b"]
    assert free == 2  # every slot was given back once


def test_a_wait_that_ends_without_the_pool_takes_no_slot_again() -> None:
    slots_held = RunSlots()

    async def scenario() -> int:
        slots = asyncio.Semaphore(1)
        async with slots_held.hold("a", slots):
            with pytest.raises(LeaseWaitStopped):
                async with slots_held.released("a"):
                    assert slots._value == 1
                    raise LeaseWaitStopped()
            assert slots._value == 1  # not taken again: the job ends without work
        return slots._value

    assert asyncio.run(scenario()) == 1


def test_the_wait_budget_is_binding_data_under_retry() -> None:
    assert GPU_WAIT_S == 1800
    assert retry_policy({}).gpu_wait_s == 1800
    assert retry_policy({"retry": {"gpu_wait_s": 60}}).gpu_wait_s == 60
    assert retry_policy({"retry": {"gpu_wait_s": 0}}).gpu_wait_s == 0
    assert retry_policy({"retry": {"gpu_wait_s": -1}}).gpu_wait_s == 1800
    for value, ok in ((0, True), (1800, True), (86400, True), (86401, False), (True, False)):
        errors = ErrorCollector()
        _check_retry({"gpu_wait_s": value}, (), errors)
        assert (not errors.errors) is ok, value


def test_one_slot_a_lease_in_the_turn_after_a_release_queues_without_its_slot_and_all_finish() -> None:
    """One run slot. ``h`` holds the pool (it holds no slot here). ``w`` waits for the pool and gave its slot back. In the
    step ``h`` leaves, ``n`` takes the free slot and asks for the pool. ``n`` must wait inside its bracket (slot given
    back), so ``w`` can take a slot after it wins the pool. Judging by ``lock.locked()`` put ``n`` on the plain path with
    its slot: ``w`` then held the lock and waited for ``n``'s slot, and ``n`` waited for ``w``'s lock."""
    pool = spec("gpu0")
    slots_held = RunSlots()

    async def scenario() -> tuple[list[str], int, int]:
        slots = asyncio.Semaphore(1)
        leases = ExclusivePoolLeaseManager()
        order: list[str] = []

        async def job(name: str) -> None:
            async with slots_held.hold(name, slots):
                wait = turn(queued=lambda: slots_held.released(name))
                async with leases.lease(pool, request_id=name, observe=roomy, wait=wait):
                    order.append(f"run {name}")
                    await asyncio.sleep(0)

        holder = leases.lease(pool, request_id="h", observe=roomy)
        await holder.__aenter__()
        w = asyncio.create_task(job("w"))
        await settle_loop()
        assert slots._value == 1  # w waits for the pool without its slot
        leave = asyncio.ensure_future(holder.__aexit__(None, None, None))
        n = asyncio.ensure_future(job("n"))  # runs in the same step as the release, before w wakes
        await leave
        await asyncio.wait_for(asyncio.gather(w, n), timeout=2)
        return order, slots._value, leases.active_claims()

    order, free, claims = asyncio.run(scenario())
    assert order == ["run w", "run n"]
    assert (free, claims) == (1, 0)


def test_no_wait_refuses_in_the_turn_after_a_release_while_a_waiter_owns_the_next_turn() -> None:
    """``wait=False`` (``gpu_wait_s: 0``) is refused at once while another request waits for the pool, even in the turn
    where the lock reads free; it never queues on the lock."""
    pool = spec("gpu0")

    async def scenario() -> tuple[str, list[str]]:
        leases = ExclusivePoolLeaseManager()
        order: list[str] = []

        async def waiter() -> None:
            async with leases.lease(pool, request_id="w", observe=roomy, wait=turn()):
                order.append("run w")

        async def newcomer() -> str:
            try:
                async with leases.lease(pool, request_id="n", observe=roomy, wait=False):
                    return "leased"
            except PoolCapacityError as error:
                return "refused" if error.lease_held else "other"

        holder = leases.lease(pool, request_id="h", observe=roomy)
        await holder.__aenter__()
        w = asyncio.create_task(waiter())
        await settle_loop()
        leave = asyncio.ensure_future(holder.__aexit__(None, None, None))
        n = asyncio.ensure_future(newcomer())
        await leave
        answer = await asyncio.wait_for(n, timeout=2)
        await asyncio.wait_for(w, timeout=2)
        return answer, order

    assert asyncio.run(scenario()) == ("refused", ["run w"])


@pytest.mark.parametrize("stagger", [(0, 0, 0, 0), (0, 1, 2, 3), (3, 0, 1, 0), (0, 2, 0, 1), (1, 1, 0, 0)])
def test_one_slot_three_jobs_on_one_pool_and_one_on_another_all_finish(stagger: tuple[int, ...]) -> None:
    """One run slot, three jobs for gpu0 and one for gpu1, started a few loop steps apart in different orders. Every job
    runs once, no job runs while it holds no slot, and every slot is given back."""
    slots_held = RunSlots()

    async def scenario() -> tuple[list[str], int, int]:
        slots = asyncio.Semaphore(1)
        leases = ExclusivePoolLeaseManager()
        order: list[str] = []

        async def job(name: str, pool_id: str, delay: int) -> None:
            for _ in range(delay):
                await asyncio.sleep(0)
            async with slots_held.hold(name, slots):
                wait = turn(queued=lambda: slots_held.released(name))
                async with leases.lease(spec(pool_id), request_id=name, observe=roomy, wait=wait):
                    assert slots._value == 0  # the working job holds the one slot
                    order.append(name)
                    for _ in range(3):
                        await asyncio.sleep(0)

        jobs = [job(name, pool_id, delay) for (name, pool_id), delay in
                zip((("a", "gpu0"), ("b", "gpu0"), ("c", "gpu0"), ("x", "gpu1")), stagger)]
        await asyncio.wait_for(asyncio.gather(*jobs), timeout=2)
        return order, slots._value, leases.active_claims()

    order, free, claims = asyncio.run(scenario())
    assert sorted(order) == ["a", "b", "c", "x"]
    assert (free, claims) == (1, 0)


def test_a_stop_that_raises_ends_the_wait_as_stopped_not_as_busy() -> None:
    pool = spec("gpu0")

    async def broken_stop() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("the cancel read failed")

    async def scenario() -> BaseException:
        leases = ExclusivePoolLeaseManager()
        holder = leases.lease(pool, request_id="h", observe=roomy)
        await holder.__aenter__()
        try:
            with pytest.raises(LeaseWaitStopped) as stopped:
                async with leases.lease(pool, request_id="b", observe=roomy, wait=turn(stop=broken_stop)):
                    pass
        finally:
            await holder.__aexit__(None, None, None)
        assert leases._occupants == {}  # nothing left counted
        return stopped.value

    error = asyncio.run(scenario())
    assert isinstance(error.__cause__, RuntimeError)


# ---------------------------------------------------------------------------------------- a cancel during the wait
# A shutdown or a client cancel raises ``asyncio.CancelledError`` in the waiting job. At each await of the wait it must
# leave the pool free, the occupant count empty, every run slot back and the next lease able to run at once.


async def assert_nothing_left(leases: ExclusivePoolLeaseManager, pool: Any, slots: asyncio.Semaphore, limit: int) -> None:
    await settle_loop()
    assert leases._occupants == {}
    assert not leases._locks[pool.pool_id].locked()
    assert slots._value == limit
    async with asyncio.timeout(1):  # a new lease runs at once: it does not wait, and the pool does not refuse it
        async with leases.lease(pool, request_id="fresh", observe=roomy, wait=False):
            pass


def test_a_cancel_while_queued_for_the_pool_leaves_the_pool_the_slot_and_the_count_clean() -> None:
    """Guards ``acquire.cancel()`` in ``_acquire_in_turn``: without it the cancelled waiter's pending acquire would take
    the lock when the holder leaves, and nothing would release it."""
    pool = spec("gpu0")
    slots_held = RunSlots()

    async def scenario() -> None:
        slots = asyncio.Semaphore(1)
        leases = ExclusivePoolLeaseManager()

        async def job(name: str) -> None:
            async with slots_held.hold(name, slots):
                wait = turn(queued=lambda: slots_held.released(name))
                async with leases.lease(pool, request_id=name, observe=roomy, wait=wait):
                    pass

        holder = leases.lease(pool, request_id="h", observe=roomy)
        await holder.__aenter__()
        w = asyncio.ensure_future(job("w"))
        await settle_loop()
        assert (slots._value, leases._occupants) == (1, {"gpu0": 2})  # w waits without its slot
        w.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w
        assert leases._occupants == {"gpu0": 1}  # only the holder is counted
        await holder.__aexit__(None, None, None)
        await assert_nothing_left(leases, pool, slots, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("with_stop", [False, True], ids=["no-stop", "stop"])
def test_a_cancel_while_the_won_pool_waits_for_a_run_slot_releases_the_pool_in_fifo_order(with_stop: bool) -> None:
    """Guards ``if owned: lock.release()`` in ``lease``: ``w`` won the lock, then waits for a run slot (``x`` holds it) in
    the leave of its bracket, and is cancelled there. The pool goes to the next waiter ``c``, and a newcomer ``n`` runs
    after ``c``, not before it. With ``stop`` the wait for the slot is the one the run core makes (``RunSlots.released``
    with a stop check); a cancel of the task must leave it clean the same way."""
    pool = spec("gpu0")
    slots_held = RunSlots()

    async def scenario() -> list[str]:
        slots = asyncio.Semaphore(1)
        leases = ExclusivePoolLeaseManager()
        order: list[str] = []
        x_done = asyncio.Event()

        async def job(name: str) -> None:
            async with slots_held.hold(name, slots):
                wait = turn(queued=lambda: slots_held.released(name, stop=never if with_stop else None))
                async with leases.lease(pool, request_id=name, observe=roomy, wait=wait):
                    order.append(name)

        async def x() -> None:  # takes the slot the waiters gave back
            async with slots_held.hold("x", slots):
                await x_done.wait()

        holder = leases.lease(pool, request_id="h", observe=roomy)
        await holder.__aenter__()
        w = asyncio.ensure_future(job("w"))
        await settle_loop()
        holder_x = asyncio.ensure_future(x())
        await settle_loop()
        assert slots._value == 0  # x holds the only slot
        await holder.__aexit__(None, None, None)  # w wins the lock and waits for a slot
        await settle_loop()
        assert leases._locks["gpu0"].locked() and leases._occupants == {"gpu0": 1} and order == []
        w.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w
        assert not leases._locks["gpu0"].locked() and leases._occupants == {}
        x_done.set()
        await holder_x
        await assert_nothing_left(leases, pool, slots, 1)
        assert order == []  # w never ran
        c = asyncio.ensure_future(job("c"))  # then the rest still run, in arrival order
        await settle_loop()
        await asyncio.wait_for(c, timeout=1)
        return order

    assert asyncio.run(scenario()) == ["c"]


def test_a_cancel_in_the_same_turn_the_wait_wins_the_lock_does_not_keep_the_lock() -> None:
    """Guards ``lock.release()`` for an acquire that finished in the turn the wait was cancelled (``_acquire_in_turn``):
    the holder leaves and the waiter is cancelled in the same step, so the waiter's acquire has taken the lock by the time
    its ``asyncio.wait`` raises."""
    pool = spec("gpu0")
    slots_held = RunSlots()

    async def scenario() -> None:
        slots = asyncio.Semaphore(1)
        leases = ExclusivePoolLeaseManager()

        async def job(name: str) -> None:
            async with slots_held.hold(name, slots):
                wait = turn(queued=lambda: slots_held.released(name))
                async with leases.lease(pool, request_id=name, observe=roomy, wait=wait):
                    pass

        holder = leases.lease(pool, request_id="h", observe=roomy)
        await holder.__aenter__()
        w = asyncio.ensure_future(job("w"))
        await settle_loop()
        await holder.__aexit__(None, None, None)  # no suspension: w's acquire is woken, not yet run
        w.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w
        await assert_nothing_left(leases, pool, slots, 1)

    asyncio.run(scenario())


def test_a_wait_that_wins_the_lock_in_the_turn_its_budget_ends_keeps_the_lock_and_runs() -> None:
    """Guards the ``if owned: return`` of ``_acquire_in_turn``. The holder leaves, then the loop is blocked past the
    budget, so the next loop step has both the woken acquire and the expired timer. The acquire is first in the queue and
    wins: the waiter runs (it is not told the pool is busy), and leaves nothing behind."""
    pool = spec("gpu0")
    slots_held = RunSlots()

    async def scenario() -> list[str]:
        slots = asyncio.Semaphore(1)
        leases = ExclusivePoolLeaseManager()
        order: list[str] = []

        async def job(name: str) -> None:
            async with slots_held.hold(name, slots):
                wait = turn(0.05, queued=lambda: slots_held.released(name))
                async with leases.lease(pool, request_id=name, observe=roomy, wait=wait):
                    order.append(name)

        holder = leases.lease(pool, request_id="h", observe=roomy)
        await holder.__aenter__()
        w = asyncio.ensure_future(job("w"))
        await settle_loop()
        await holder.__aexit__(None, None, None)
        time.sleep(0.12)  # past the 0.05 s budget, with no suspension
        await asyncio.wait_for(w, timeout=2)
        await assert_nothing_left(leases, pool, slots, 1)
        return order

    assert asyncio.run(scenario()) == ["w"]


def test_a_stop_while_the_won_pool_waits_for_a_run_slot_ends_the_wait_and_the_next_waiter_gets_the_pool() -> None:
    """``w`` won the pool and waits for a run slot (``x`` holds it); its cancel (``stop``) ends that wait: ``w`` is told it
    was stopped, the pool goes to ``c`` (next in line), and a newcomer ``n`` runs after ``c``."""
    pool = spec("gpu0")
    slots_held = RunSlots()

    async def scenario() -> list[str]:
        slots = asyncio.Semaphore(1)
        leases = ExclusivePoolLeaseManager()
        order: list[str] = []
        stop_w, x_done = asyncio.Event(), asyncio.Event()

        async def job(name: str, stop: Callable[[], Any] = never) -> None:
            try:
                async with slots_held.hold(name, slots):
                    wait = turn(stop=stop, queued=lambda: slots_held.released(name, stop=stop))
                    async with leases.lease(pool, request_id=name, observe=roomy, wait=wait):
                        order.append(name)
            except LeaseWaitStopped:
                order.append(f"{name} stopped")

        async def x() -> None:
            async with slots_held.hold("x", slots):
                await x_done.wait()

        holder = leases.lease(pool, request_id="h", observe=roomy)
        await holder.__aenter__()
        w = asyncio.ensure_future(job("w", stop_w.wait))
        await settle_loop()
        c = asyncio.ensure_future(job("c"))
        await settle_loop()
        held_x = asyncio.ensure_future(x())
        await settle_loop()
        await holder.__aexit__(None, None, None)  # w wins the pool, then waits for a slot
        await settle_loop()
        assert leases._locks["gpu0"].locked() and slots._value == 0 and order == []
        stop_w.set()
        await asyncio.wait_for(w, timeout=1)  # ends while x still holds the only slot
        assert order == ["w stopped"] and leases._occupants == {"gpu0": 1}  # c is next
        n = asyncio.ensure_future(job("n"))
        await settle_loop()
        x_done.set()
        await asyncio.wait_for(asyncio.gather(held_x, c, n), timeout=2)
        await assert_nothing_left(leases, pool, slots, 1)
        return order

    assert asyncio.run(scenario()) == ["w stopped", "c", "n"]


def test_a_cancel_in_the_turn_a_run_slot_is_given_does_not_keep_the_slot() -> None:
    """Guards ``slots.release()`` in ``run_slots._take``: the slot is handed to ``w`` in the step ``x`` leaves and ``w`` is
    cancelled in that step, so its acquire holds the slot when its wait raises."""
    pool = spec("gpu0")
    slots_held = RunSlots()

    async def scenario() -> None:
        slots = asyncio.Semaphore(1)
        leases = ExclusivePoolLeaseManager()

        async def job(name: str) -> None:
            async with slots_held.hold(name, slots):
                wait = turn(queued=lambda: slots_held.released(name, stop=never))
                async with leases.lease(pool, request_id=name, observe=roomy, wait=wait):
                    pass

        holder = leases.lease(pool, request_id="h", observe=roomy)
        await holder.__aenter__()
        w = asyncio.ensure_future(job("w"))
        await settle_loop()
        x = slots_held.hold("x", slots)
        await x.__aenter__()
        await holder.__aexit__(None, None, None)
        await settle_loop()  # w holds the pool and waits for the slot x holds
        assert slots._value == 0 and leases._locks["gpu0"].locked()
        await x.__aexit__(None, None, None)  # no suspension: the slot is handed to w's acquire, which has not run
        w.cancel()
        with pytest.raises(asyncio.CancelledError):
            await w
        await assert_nothing_left(leases, pool, slots, 1)

    asyncio.run(scenario())


def test_a_stop_and_a_free_run_slot_in_the_same_turn_keep_the_slot_and_run() -> None:
    """The slot wins the tie: the job runs (the caller reads the cancel before it sends), holding its slot, and gives it back."""
    pool = spec("gpu0")
    slots_held = RunSlots()

    async def scenario() -> list[str]:
        slots = asyncio.Semaphore(1)
        leases = ExclusivePoolLeaseManager()
        order: list[str] = []
        stop_w = asyncio.Event()

        async def job(name: str) -> None:
            async with slots_held.hold(name, slots):
                wait = turn(stop=stop_w.wait, queued=lambda: slots_held.released(name, stop=stop_w.wait))
                async with leases.lease(pool, request_id=name, observe=roomy, wait=wait):
                    assert slots._value == 0  # it holds its slot again
                    order.append(name)

        holder = leases.lease(pool, request_id="h", observe=roomy)
        await holder.__aenter__()
        w = asyncio.ensure_future(job("w"))
        await settle_loop()
        x = slots_held.hold("x", slots)
        await x.__aenter__()
        await holder.__aexit__(None, None, None)
        await settle_loop()
        await x.__aexit__(None, None, None)
        stop_w.set()
        await asyncio.wait_for(w, timeout=1)
        await assert_nothing_left(leases, pool, slots, 1)
        return order

    assert asyncio.run(scenario()) == ["w"]


# ------------------------------------------------------------------------------------------- the end-of-wait event


def _runner_with_host(monkeypatch: pytest.MonkeyPatch, writes: list[tuple[str, str]], fail: int) -> Any:
    """A multipart runner with no engine: ``append_event`` fails ``fail`` times, then records its writes."""
    from types import SimpleNamespace

    from gateway import media_jobs
    from gateway.multipart_adapter import MultipartRunner

    calls = {"n": 0}

    def append_event(job_id: str, kind: str, payload: Any, *, path: Any) -> int:
        calls["n"] += 1
        if calls["n"] <= fail:
            raise OSError("the job store is locked")
        writes.append((job_id, kind))
        return calls["n"]

    monkeypatch.setattr(media_jobs, "append_event", append_event)
    monkeypatch.setattr(media_jobs, "get", lambda *args, **kwargs: {"progress": 5})
    runner = object.__new__(MultipartRunner)
    runner._host = SimpleNamespace(jobs_path=lambda: None)
    return runner


@pytest.mark.parametrize("fail,written", [(0, 1), (1, 1), (2, 0)])
def test_the_end_of_the_wait_is_written_again_once_when_the_first_write_fails(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, fail: int, written: int) -> None:
    """``gpu_ready`` is what tells a sync caller the job holds the GPU. One failed write is tried again; two are logged
    (with the risk named) and the job goes on."""
    writes: list[tuple[str, str]] = []
    runner = _runner_with_host(monkeypatch, writes, fail)
    with caplog.at_level("ERROR"):
        runner._tell_gpu_ready("job-1", 3.2)
    assert writes == [("job-1", "gpu_ready")] * written
    assert [r.getMessage() for r in caplog.records].count("the end of the GPU wait was not recorded (try 2 of 2); "
                                                         "a sync caller that times out may read this job as still waiting") == (fail == 2)
    assert len(caplog.records) == fail
