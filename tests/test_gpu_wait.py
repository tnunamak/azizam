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
from tests.support.fake_upstream.handler_transport import HandlerTransport

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


def test_an_expired_admission_deadline_wins_even_when_the_pool_becomes_free() -> None:
    """Admission includes observation and slot reacquisition; winning the lock
    at expiry no longer authorizes a fresh preparation phase."""
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
        with pytest.raises(PoolCapacityError):
            await asyncio.wait_for(w, timeout=2)
        await assert_nothing_left(leases, pool, slots, 1)
        return order

    assert asyncio.run(scenario()) == []


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


@pytest.mark.xfail(strict=True, reason="Whole-card admission is not enabled: protocol egress has no GPU holding check")
@pytest.mark.parametrize("path,body", [
    ("/prompt", {"prompt": {}}),
    ("/v1/chat/completions", {"model": "fixture", "messages": [], "stream": True}),
    ("/v1/audio/transcriptions", {}),
])
def test_protocol_egress_cannot_bypass_a_conflicting_pool(path, body):
    """Coverage activation gate: a fake transport must not see conflicting work.

    This probes the common HTTP boundary, rather than claiming route integration.
    It is deliberately red until graph/chat/speech share physical admission.
    """
    import httpx
    from gateway import engine_egress

    async def scenario():
        leases = ExclusivePoolLeaseManager()
        sent = []

        async def transport(request):
            sent.append(request.url.path)
            return httpx.Response(200, json={})

        async with leases.lease(spec("physical-card"), request_id="multipart", observe=roomy):
            async with engine_egress.async_client(transport=HandlerTransport(transport)) as client:
                await client.post("http://fixture.invalid" + path, json=body)
            assert sent == [], "protocol egress bypasses the occupied GPU admission boundary"

    asyncio.run(scenario())


def test_multipart_admission_deadline_survives_shortage_retry_and_recovery(monkeypatch):
    from gateway import workflow_run, media_jobs
    clock = [1000.0]
    row = {"status": "running", "metadata": {}}
    writes = []
    monkeypatch.setattr(workflow_run.time, "time", lambda: clock[0])
    monkeypatch.setattr(media_jobs, "get", lambda *a, **k: row)

    def update(*args, metadata_patch, **kwargs):
        writes.append(metadata_patch)
        row["metadata"].update(metadata_patch)

    monkeypatch.setattr(media_jobs, "update_status", update)
    assert workflow_run.admission_remaining("job", 30, path=None) == 30
    clock[0] += 12  # lock wait, observation, then shortage
    assert workflow_run.admission_remaining("job", 30, path=None) == 18
    clock[0] += 10  # retry/recovery; policy cannot reset or extend this ticket
    assert workflow_run.admission_remaining("job", 90, path=None) == 8
    clock[0] += 9
    assert workflow_run.admission_remaining("job", 30, path=None) == 0
    assert writes == [{"gpu_admission_deadline": 1030.0}]


def test_memory_shortage_keeps_fifo_turn_and_wait_bracket_until_capacity_returns():
    from dataclasses import replace
    pool = replace(spec("gpu0"), retry_after_seconds=0.01)

    async def scenario():
        leases = ExclusivePoolLeaseManager()
        enough = False
        log = []
        observed_short = asyncio.Event()

        async def observe():
            observed_short.set()
            return {"free": TOTAL if enough else 0, "total": TOTAL}

        async def job(name):
            async with leases.lease(pool, request_id=name, observe=observe, wait=turn(log=log, name=name)):
                log.append("run " + name)

        a = asyncio.create_task(job("a"))
        try:
            await observed_short.wait()
            await settle_loop()
            assert not a.done(), "shortage must wait inside the original attempt"
            b = asyncio.create_task(job("b"))
            await settle_loop()
            assert log == ["wait a", "wait b"]
            enough = True
            await asyncio.wait_for(asyncio.gather(a, b), 1)
            assert log == ["wait a", "wait b", "ready a", "run a", "ready b", "run b"]
        finally:
            a.cancel()
            await asyncio.gather(a, return_exceptions=True)

    asyncio.run(scenario())


def test_observer_recovery_waits_in_the_same_turn_and_impossible_claim_fails_once():
    from dataclasses import replace
    from gateway.exclusive_pool import PoolObserverError
    pool = replace(spec("gpu0"), retry_after_seconds=0.001)

    async def scenario():
        calls = 0
        async def observe():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise PoolObserverError("temporarily missing")
            return await roomy()
        leases = ExclusivePoolLeaseManager()
        async with leases.lease(pool, request_id="recover", observe=observe, wait=turn()):
            assert calls == 2
        with pytest.raises(PoolCapacityError) as failure:
            async with leases.lease(replace(pool, max_claim_bytes=TOTAL), request_id="impossible",
                                    observe=roomy, wait=turn()):
                pytest.fail("impossible claim admitted")
        assert failure.value.status.value == "rejected"
    asyncio.run(scenario())


def test_durable_scheduler_two_cards_one_slot_and_ambiguous_upstream(tmp_path):
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler

    async def scenario():
        store = ArbitrationStore(tmp_path / "ledger.db")
        scheduler = AdmissionScheduler(store, worker_capacity=1, poll_s=0.001)
        order = []
        released = asyncio.Event()
        sent = asyncio.Event()
        def args(name, card):
            return dict(request_id=name, caller="caller", policy_revision="rev", service_id=name,
                        resources=(card,), claim_envelope={}, deadline=time.time() + 1, ready=lambda: roomy())
        async def multipart():
            async with scheduler.admit(**args("upload", "card/a")) as lease:
                assert lease.send_intent()
                sent.set()
                await released.wait()
                # Transport closed; intentionally no completion evidence.
        a = asyncio.create_task(multipart())
        await sent.wait()
        async def graph():
            async with scheduler.admit(**args("graph", "card/a")):
                order.append("graph")
        b = asyncio.create_task(graph())
        await settle_loop()
        async def chat():
            async with scheduler.admit(**args("chat", "card/b")) as lease:
                assert lease.send_intent()
                order.append("chat")
                lease.completed("stream reached declared terminal state")
        c = asyncio.create_task(chat())
        await settle_loop()
        assert order == []
        released.set()
        await a
        await c
        assert order == ["chat"]
        assert store.ticket("upload").phase == "unknown"
        assert not b.done()
        store.finish_ticket("upload", now=time.time(), evidence="upstream absence confirmed")
        await b
        assert order == ["chat", "graph"]
        store.close()
    asyncio.run(scenario())


def test_transport_timeout_does_not_refund_a_fake_upstream_that_keeps_computing(tmp_path):
    import httpx
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler

    async def scenario():
        store = ArbitrationStore(tmp_path / "ledger.db")
        scheduler = AdmissionScheduler(store, worker_capacity=1, poll_s=0.001)
        upstream_finished = asyncio.Event()
        still_computing = asyncio.Event()
        async def compute():
            still_computing.set()
            await upstream_finished.wait()
        upstream = asyncio.create_task(compute())
        await still_computing.wait()
        def args(name):
            return dict(request_id=name, caller="caller", policy_revision="rev", service_id="svc",
                        resources=("card/a",), claim_envelope={}, deadline=time.time() + 1, ready=roomy)
        async def timeout(request):
            raise httpx.ReadTimeout("response lost; engine still computing", request=request)
        try:
            with pytest.raises(httpx.ReadTimeout):
                async with scheduler.admit(**args("first")) as lease:
                    assert lease.send_intent()
                    async with httpx.AsyncClient(transport=HandlerTransport(timeout)) as client:
                        await client.post("http://fixture.invalid/execute")
            assert not upstream.done()
            ran = []
            async def second():
                async with scheduler.admit(**args("second")):
                    ran.append("second")
            pending = asyncio.create_task(second())
            await settle_loop()
            assert ran == []
            assert store.ticket("first").phase == "unknown"
            upstream_finished.set()
            await upstream
            store.finish_ticket("first", now=time.time(), evidence="fake upstream observed completed")
            await pending
            assert ran == ["second"]
        finally:
            upstream_finished.set()
            await upstream
            store.close()
    asyncio.run(scenario())


def test_duplicate_admission_cannot_withdraw_another_executor(tmp_path):
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler, AdmissionInProgress
    async def scenario():
        store = ArbitrationStore(tmp_path / "ledger.db")
        scheduler = AdmissionScheduler(store, worker_capacity=1)
        args = dict(request_id="job", caller="caller", policy_revision="rev", service_id="svc",
                    resources=("card/a",), claim_envelope={}, deadline=time.time() + 1, ready=roomy)
        try:
            async with scheduler.admit(**args) as first:
                with pytest.raises(AdmissionInProgress):
                    async with scheduler.admit(**args):
                        pytest.fail("duplicate executor granted")
                assert first.send_intent()
                first.completed("response complete")
            assert store.ticket("job").phase == "completed"
        finally:
            store.close()
    asyncio.run(scenario())


def test_restart_recovery_adopts_unknown_without_leaking_execution_slots(tmp_path):
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler
    path = tmp_path / "ledger.db"
    store = ArbitrationStore(path)
    args = dict(request_id="sent", caller="caller", policy_revision="rev", service_id="svc",
                resources=("card/a",), claim_envelope={}, deadline=time.time() + 60)
    store.enqueue_ticket(**args)
    assert store.grant_ticket("sent", worker_capacity=1, now=time.time())
    assert store.transition_ticket("sent", "sending", now=time.time())
    store.close()  # process vanished after send intent, before cleanup
    async def scenario():
        store = ArbitrationStore(path)
        store.recover_startup(now=time.time())  # explicit, quiescent process startup
        scheduler = AdmissionScheduler(store, worker_capacity=1)
        try:
            assert store.ticket("sent").phase == "unknown"
            async with scheduler.admit(**{**args, "request_id": "other", "resources": ("card/b",)}, ready=roomy):
                pass  # stale worker charge cannot block an idle independent card
            plan = scheduler.pin(**args, ready=roomy, recover=True)
            async with plan.grant() as lease:
                assert lease.recovering
                assert not lease.send_intent()  # adoption permits observation, never replay
                lease.completed("recovered upstream terminal result")
            assert store.ticket("sent").phase == "completed"
        finally:
            store.close()
    asyncio.run(scenario())


def test_zero_wait_tries_once_on_free_capacity_and_refuses_busy_without_sleep(tmp_path):
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler, AdmissionExpired
    async def scenario():
        store = ArbitrationStore(tmp_path / "ledger.db")
        scheduler = AdmissionScheduler(store, worker_capacity=1)
        def args(name, *, no_wait=False):
            return dict(request_id=name, caller="caller", policy_revision="rev", service_id="svc",
                        resources=("card/a",), claim_envelope={}, deadline=time.time() if no_wait else time.time() + 60,
                        ready=roomy, no_wait=no_wait)
        try:
            async with scheduler.admit(**args("free", no_wait=True)) as lease:
                assert lease.send_intent()
                lease.completed("synchronous response complete")
            async with scheduler.admit(**args("holder")) as lease:
                assert lease.send_intent()
                with pytest.raises(AdmissionExpired):
                    async with scheduler.admit(**args("busy", no_wait=True)):
                        pytest.fail("busy immediate request was granted")
                assert store.ticket("busy").phase == "expired"
                lease.completed("holder completed")
        finally:
            store.close()
    asyncio.run(scenario())


def test_one_waiter_cannot_clear_another_waiters_capacity_wakeup(tmp_path):
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler
    async def scenario():
        store = ArbitrationStore(tmp_path / "ledger.db")
        scheduler = AdmissionScheduler(store, worker_capacity=1, poll_s=60)
        probing_a, release_probe, probing_b = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = 0
        async def readiness_a():
            nonlocal calls
            calls += 1
            if calls == 1:
                probing_a.set()
                await release_probe.wait()
                return False  # old observation, taken before the notification
            return True
        async def readiness_b():
            probing_b.set()
            return False
        async def job(name, ready):
            async with scheduler.admit(request_id=name, caller="caller", policy_revision="rev", service_id="svc",
                                       resources=(name,), claim_envelope={}, deadline=time.time() + 60, ready=ready):
                pass
        a = asyncio.create_task(job("a", readiness_a))
        b = None
        try:
            await probing_a.wait()
            scheduler.withdraw("not-present")  # notify A while its observation is in flight
            b = asyncio.create_task(job("b", readiness_b))
            await probing_b.wait()
            await settle_loop()  # B enters its wait; it must not consume A's wake
            release_probe.set()
            await asyncio.wait_for(a, 1)
            assert calls == 2
        finally:
            a.cancel()
            if b is not None:
                b.cancel()
            await asyncio.gather(a, *([b] if b is not None else []), return_exceptions=True)
            store.close()
    asyncio.run(scenario())


def test_cleanup_store_failure_still_discards_executor(tmp_path, monkeypatch):
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler
    async def scenario():
        store = ArbitrationStore(tmp_path / "ledger.db")
        scheduler = AdmissionScheduler(store, worker_capacity=1)
        def broken(*args, **kwargs):
            raise RuntimeError("injected ledger write failure")
        monkeypatch.setattr(store, "finish_ticket", broken)
        try:
            with pytest.raises(RuntimeError, match="injected ledger"):
                async with scheduler.admit(request_id="job", caller="caller", policy_revision="rev", service_id="svc",
                                           resources=("card/a",), claim_envelope={}, deadline=time.time() + 1, ready=roomy):
                    pass
            assert "job" not in scheduler._executors
        finally:
            store.close()
    asyncio.run(scenario())


def test_pin_assigns_conflict_fifo_before_staging_or_first_send(tmp_path):
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler, AdmissionExpired
    async def scenario():
        store = ArbitrationStore(tmp_path / "ledger.db")
        scheduler = AdmissionScheduler(store, worker_capacity=1)
        def facts(name, no_wait=False):
            return dict(request_id=name, caller="caller", policy_revision="rev", service_id="svc",
                        resources=("card/a",), claim_envelope={}, deadline=time.time() if no_wait else time.time() + 30,
                        ready=roomy, no_wait=no_wait)
        try:
            first = scheduler.pin(**facts("slow-staging"))
            second = scheduler.pin(**facts("fast-staging", no_wait=True))
            assert first.ticket.sequence < second.ticket.sequence
            with pytest.raises(AdmissionExpired):
                async with second.grant():
                    pytest.fail("first-send order overtook eligibility order")
            async with first.grant() as lease:
                assert lease.send_intent()
                lease.completed("first completed")
        finally:
            store.close()
    asyncio.run(scenario())


def test_ledger_grant_does_not_block_the_event_loop(tmp_path, monkeypatch):
    import threading
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler
    async def scenario():
        store = ArbitrationStore(tmp_path / "ledger.db")
        scheduler = AdmissionScheduler(store, worker_capacity=1)
        started, proceed = threading.Event(), threading.Event()
        original = store.grant_ticket
        def delayed(*args, **kwargs):
            started.set()
            assert proceed.wait(2), "the event loop could not release the ledger writer"
            return original(*args, **kwargs)
        monkeypatch.setattr(store, "grant_ticket", delayed)
        async def ticker():
            while not started.is_set():
                await asyncio.sleep(0.001)
            proceed.set()
        tick = asyncio.create_task(ticker())
        try:
            async with scheduler.admit(request_id="job", caller="caller", policy_revision="rev", service_id="svc",
                                       resources=("card/a",), claim_envelope={}, deadline=time.time() + 10, ready=roomy):
                pass
            await tick
        finally:
            proceed.set()
            tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)
            store.close()
    asyncio.run(scenario())


def test_transient_cleanup_failure_returns_worker_on_next_admission(tmp_path, monkeypatch):
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler
    async def scenario():
        store = ArbitrationStore(tmp_path / "ledger.db")
        scheduler = AdmissionScheduler(store, worker_capacity=1)
        original = store.finish_ticket
        failed = False
        def once(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("temporary cleanup write failure")
            return original(*args, **kwargs)
        monkeypatch.setattr(store, "finish_ticket", once)
        def args(name, resource, no_wait=False):
            return dict(request_id=name, caller="caller", policy_revision="rev", service_id="svc",
                        resources=(resource,), claim_envelope={}, deadline=time.time() if no_wait else time.time() + 10,
                        no_wait=no_wait, ready=roomy)
        try:
            with pytest.raises(RuntimeError, match="temporary cleanup"):
                async with scheduler.admit(**args("first", "card/a")) as lease:
                    assert lease.send_intent()
                    lease.completed("upstream completed")
            async with scheduler.admit(**args("next", "card/b", no_wait=True)):
                pass
            assert store.ticket("first").phase == "completed"
            assert scheduler._executors == set()
        finally:
            store.close()
    asyncio.run(scenario())


def test_cancellation_during_a_threaded_grant_refunds_before_dispatch(tmp_path, monkeypatch):
    import threading
    from gateway.arbitration.store import ArbitrationStore
    from gateway.arbitration.types import ResourceRef
    from gateway.gpu_admission import AdmissionScheduler
    async def scenario():
        store = ArbitrationStore(tmp_path / "ledger.db")
        scheduler = AdmissionScheduler(store, worker_capacity=1)
        started, proceed = threading.Event(), threading.Event()
        original = store.grant_ticket
        sent = []
        def delayed(*args, **kwargs):
            started.set()
            assert proceed.wait(2)
            return original(*args, **kwargs)
        monkeypatch.setattr(store, "grant_ticket", delayed)
        async def job():
            async with scheduler.admit(request_id="job", caller="caller", policy_revision="rev", service_id="svc",
                                       resources=("card/a",), claim_envelope={}, deadline=time.time() + 10, ready=roomy):
                sent.append("dispatched")
        task = asyncio.create_task(job())
        try:
            while not started.is_set():
                await asyncio.sleep(0.001)
            task.cancel()
            proceed.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert sent == []
            assert store.ticket("job").phase == "withdrawn"
            assert store.charge(ResourceRef("physical/card/a", "1")) == 0
            assert store.charge(ResourceRef("@gateway/worker", "1")) == 0
        finally:
            proceed.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            store.close()
    asyncio.run(scenario())


def test_unrelated_grant_does_not_expire_pinned_no_wait(tmp_path):
    from gateway.arbitration.store import ArbitrationStore
    store = ArbitrationStore(tmp_path / "ledger.db")
    facts = dict(caller="caller", policy_revision="rev", service_id="svc", claim_envelope={})
    try:
        store.enqueue_ticket(request_id="immediate", resources=("card/a",), deadline=1, no_wait=True, **facts)
        store.enqueue_ticket(request_id="other", resources=("card/b",), deadline=100, **facts)
        assert store.grant_ticket("other", worker_capacity=2, now=2)
        assert store.ticket("immediate").phase == "waiting"
        assert store.grant_ticket("immediate", worker_capacity=2, now=2)
    finally:
        store.close()


def test_cleanup_failure_joins_other_tasks_before_dropping_tracking(tmp_path):
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler
    async def scenario():
        store = ArbitrationStore(tmp_path / "ledger.db")
        scheduler = AdmissionScheduler(store, worker_capacity=1)
        release, failed = asyncio.Event(), asyncio.Event()
        async def broken():
            failed.set()
            raise RuntimeError("cleanup failed")
        async def slow():
            await release.wait()
        slow_task = asyncio.create_task(slow())
        scheduler._cleanup_tasks.update((asyncio.create_task(broken()), slow_task))
        drain = asyncio.create_task(scheduler.drain_cleanup())
        try:
            await failed.wait()
            for _ in range(10):
                await asyncio.sleep(0)
            assert slow_task in scheduler._cleanup_tasks
            assert not drain.done()
            release.set()
            await drain
            assert slow_task.done() and not scheduler._cleanup_tasks
        finally:
            release.set()
            await asyncio.gather(drain, slow_task, return_exceptions=True)
            store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("sent", [False, True])
def test_scheduler_construction_cannot_recover_live_holding(tmp_path, sent):
    from gateway.arbitration.store import ArbitrationStore
    from gateway.gpu_admission import AdmissionScheduler
    async def scenario():
        store = ArbitrationStore(tmp_path / "ledger.db")
        first = AdmissionScheduler(store, worker_capacity=1)
        try:
            async with first.admit(request_id="live", caller="caller", policy_revision="rev", service_id="svc",
                                   resources=("card/a",), claim_envelope={}, deadline=time.time() + 60, ready=roomy) as lease:
                if sent:
                    assert lease.send_intent()
                phase = store.ticket("live").phase
                holdings = store.open_operations()
                second = AdmissionScheduler(store, worker_capacity=1)
                assert second.store is store
                assert store.ticket("live").phase == phase
                assert store.open_operations() == holdings
                lease.completed("live operation ended")
        finally:
            store.close()
    asyncio.run(scenario())
