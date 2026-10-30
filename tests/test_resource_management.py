"""Device admission and lifecycle journeys without host GPU/process access."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from gateway import resource_management as management
from gateway.engine_observations import Observation, UNKNOWN
from gateway.engine_residency import ResidencyController
from gateway.engine_switch import SwitchController, SwitchUnavailable
from gateway.swap_drain import SwapDrain


def service(sid="alpha", *, devices=None):
    return {"id": sid, "resources": {"management": {"mode": "capacity", "devices":
        devices if devices is not None else {"card-a": {"additional_peak_mb": 600}}}},
        "offerings": [{"id": "new"}], "residency": {"load": {"op": "load"}}}


def gpu(uid="card-a", used=100, index=0):
    return {"uuid": uid, "index": index, "memory_total_mb": 1000, "memory_used_mb": used}


SELECTION = {"mode": "switch", "switch": {"call": "select", "args": {"model": "new"},
    "observe_selected": "selected", "observe_ready": "ready", "on_failure": "restore"}}


@pytest.fixture
def coordinator(monkeypatch):
    instance = management.ResourceCoordinator(sample=AsyncMock(return_value=[gpu()]))
    monkeypatch.setattr(management, "RESOURCE_COORDINATOR", instance)
    return instance


def run(coro):
    return asyncio.run(coro)


def test_contending_services_resample_after_verified_load_and_account_external_memory(coordinator):
    async def scenario():
        entered, finish, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()
        used = [100]  # external device consumer, not a registered service
        coordinator._sample.side_effect = lambda: [gpu(used=used[0])]
        async def first_load():
            entered.set()
            await finish.wait()
            used[0] += 600
            return True
        first = asyncio.create_task(management.managed_operation(service(), first_load, observe=AsyncMock()))
        await entered.wait()
        assert coordinator.snapshot() == [{"service_id": "alpha", "devices": {"card-a": 600}}]
        async def second_load():
            second_started.set()
            return True
        queued = asyncio.create_task(management.managed_operation(service("beta"), second_load, observe=AsyncMock()))
        await asyncio.sleep(0)
        assert not second_started.is_set()
        assert coordinator._sample.call_count == 1
        finish.set()
        assert await first is True
        with pytest.raises(SwitchUnavailable, match="not enough"):
            await queued
        assert not second_started.is_set()
        assert coordinator._sample.call_count == 2
        assert coordinator.snapshot() == []
    run(scenario())


def test_independent_cards_load_concurrently_and_co_residents_fit(coordinator):
    async def scenario():
        coordinator._sample.return_value = [gpu(), gpu("card-b", index=1)]
        entered = [asyncio.Event(), asyncio.Event()]
        finish = asyncio.Event()
        async def operation(index):
            entered[index].set()
            await finish.wait()
            return True
        tasks = [asyncio.create_task(management.managed_operation(
            service(str(i), devices={uid: {"additional_peak_mb": 200}}),
            lambda i=i: operation(i), observe=AsyncMock())) for i, uid in enumerate(("card-a", "card-b"))]
        await asyncio.gather(*(e.wait() for e in entered))
        assert len(coordinator.snapshot()) == 2
        finish.set()
        assert await asyncio.gather(*tasks) == [True, True]
        assert coordinator.snapshot() == []
    run(scenario())


@pytest.mark.parametrize("rows", [None, [], [gpu(used=1001)], [gpu(), gpu()],
                                   [gpu(used=None)], [gpu(uid="replacement")]])
def test_missing_invalid_or_replaced_devices_fail_closed(coordinator, rows):
    coordinator._sample.return_value = rows
    operation = AsyncMock(return_value=True)
    with pytest.raises(SwitchUnavailable):
        run(management.managed_operation(service(), operation, observe=AsyncMock()))
    operation.assert_not_awaited()
    assert coordinator.snapshot() == []


@pytest.mark.parametrize("demand", [None, True, -1, 1.5, "600"])
def test_unknown_demand_is_never_zero(coordinator, demand):
    svc = service(devices={"card-a": {"additional_peak_mb": demand}})
    with pytest.raises(SwitchUnavailable, match="demand is unknown"):
        run(management.managed_operation(svc, AsyncMock(), observe=AsyncMock()))
    coordinator._sample.assert_not_awaited()


def test_observed_demand_and_explicit_zero_with_headroom(coordinator):
    async def scenario():
        svc = service(devices={"card-a": {"observe_mb": "prospective"}})
        svc["resources"]["management"]["headroom_mb"] = 100
        observe = AsyncMock(return_value=Observation(True, 800))
        assert await management.managed_operation(svc, AsyncMock(return_value=True), observe=observe)
        observe.assert_awaited_once_with("prospective")
        with pytest.raises(SwitchUnavailable, match="demand is unknown"):
            await management.managed_operation(svc, AsyncMock(), observe=AsyncMock(return_value=UNKNOWN))
        coordinator._sample.return_value = [gpu(used=1000)]
        assert await management.managed_operation(service(devices={"card-a": {"additional_peak_mb": 0}}),
                                                   AsyncMock(return_value=True), observe=observe)
    run(scenario())


def test_offering_demand_cannot_change_placement(coordinator):
    async def scenario():
        offering = {"resources": {"devices": {"card-a": {"additional_peak_mb": 901}}}}
        with pytest.raises(SwitchUnavailable, match="not enough"):
            await management.managed_operation(service(), AsyncMock(), observe=AsyncMock(), offering=offering)
        offering["resources"]["devices"] = {"card-b": {"additional_peak_mb": 0}}
        with pytest.raises(SwitchUnavailable, match="explicit device"):
            await management.managed_operation(service(), AsyncMock(), observe=AsyncMock(), offering=offering)
    run(scenario())


def test_cancellation_retains_reservations_until_load_finishes(coordinator):
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        async def operation():
            entered.set()
            await finish.wait()
            return True
        task = asyncio.create_task(management.managed_operation(service(), operation, observe=AsyncMock()))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert coordinator.snapshot()
        finish.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert coordinator.snapshot() == []
        assert not coordinator._locks["card-a"].locked()
    run(scenario())


def test_failed_load_quarantines_device_until_explicit_quiescence(coordinator):
    async def scenario():
        svc = service()
        svc["resources"]["management"]["observe_quiescent"] = "allocation_idle"
        with pytest.raises(SwitchUnavailable, match="could not be verified"):
            await management.managed_operation(svc, AsyncMock(return_value=False), observe=AsyncMock())
        assert coordinator.snapshot() == [{"state": "uncertain", "device": "card-a", "services": ["alpha"]}]
        with pytest.raises(SwitchUnavailable, match="unresolved"):
            await management.managed_operation(service("beta"), AsyncMock(), observe=AsyncMock())
        assert not await coordinator.reconcile(svc, observe=AsyncMock(return_value=UNKNOWN))
        assert not await coordinator.reconcile(svc, observe=AsyncMock(return_value=Observation(True, 1)))
        assert await coordinator.reconcile(svc, observe=AsyncMock(return_value=Observation(True, True)))
        assert await management.managed_operation(service("beta"), AsyncMock(return_value=True), observe=AsyncMock())
    run(scenario())


def test_cancelled_multi_device_wait_releases_partial_locks(coordinator):
    async def scenario():
        lock = coordinator._locks.setdefault("card-b", asyncio.Lock())
        await lock.acquire()
        svc = service(devices={uid: {"additional_peak_mb": 10} for uid in ("card-a", "card-b")})
        operation = AsyncMock()
        task = asyncio.create_task(coordinator.transaction(svc, observe=AsyncMock()).__aenter__())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert coordinator._locks["card-a"].locked()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not coordinator._locks["card-a"].locked()
        lock.release()
        operation.assert_not_awaited()
    run(scenario())


def test_switch_journey_drains_leases_loads_verifies_and_reuses_without_claim(coordinator):
    async def scenario():
        selected = ["old"]
        drain = SwapDrain()
        active = drain.lease("alpha")
        called = asyncio.Event()
        async def invoke(op, args):
            selected[0] = args["model"]
            called.set()
        async def observe(name):
            return selected[0] if name == "selected" else True
        controller = SwitchController(drain=drain, invoke=invoke, observe=observe)
        task = asyncio.create_task(controller.select(service(), "new", SELECTION))
        await asyncio.sleep(0)
        assert not called.is_set()
        assert coordinator.snapshot() == []
        active.release()
        lease = await task
        assert called.is_set() and selected == ["new"]
        assert drain.in_flight("alpha") == 1
        coordinator._sample.side_effect = AssertionError("already loaded requests must not reserve")
        next_lease = await controller.select(service(), "new", SELECTION)
        next_lease.release()
        lease.release()
        assert drain.in_flight("alpha") == 0
    run(scenario())


def test_denied_selection_never_invokes_restore_or_stops_any_service(coordinator):
    coordinator._sample.return_value = [gpu(used=800)]
    invoke = AsyncMock()
    controller = SwitchController(drain=SwapDrain(), invoke=invoke, observe=AsyncMock(return_value="old"))
    with pytest.raises(SwitchUnavailable, match="not enough"):
        run(controller.select(service(), "new", SELECTION))
    invoke.assert_not_awaited()
    assert coordinator.snapshot() == []


def test_declarative_prepare_and_switch_share_one_reservation(coordinator):
    async def scenario():
        state = {"resident": False, "selected": "old"}
        async def observe(name):
            return state.get(name, True)
        async def invoke(op, args):
            if op == "load":
                state["resident"] = True
            else:
                state["selected"] = args["model"]
        residency = ResidencyController(invoke=invoke, observe=observe)
        controller = SwitchController(drain=SwapDrain(), invoke=invoke, observe=observe)
        svc = service()
        lease = await controller.select(svc, "new", SELECTION, prepare=lambda: residency.load(svc))
        assert state == {"resident": True, "selected": "new"}
        assert coordinator._sample.call_count == 1
        lease.release()
    run(scenario())


def test_unmanaged_and_undeclared_services_do_not_sample_or_mutate(coordinator):
    async def scenario():
        svc = {"id": "external"}
        operation = AsyncMock(return_value=True)
        assert await management.managed_operation(svc, operation, observe=AsyncMock())
        coordinator._sample.assert_not_awaited()
        controller = ResidencyController(invoke=AsyncMock(), observe=AsyncMock())
        assert await controller.load(svc) is False
        controller._invoke.assert_not_awaited()
    run(scenario())


def test_cancelled_queued_operation_never_starts(coordinator):
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        async def operation():
            entered.set()
            await finish.wait()
            return True
        first = asyncio.create_task(management.managed_operation(service(), operation, observe=AsyncMock()))
        await entered.wait()
        mutation = AsyncMock(return_value=True)
        queued = asyncio.create_task(management.managed_operation(service("beta"), mutation, observe=AsyncMock()))
        await asyncio.sleep(0)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        mutation.assert_not_awaited()
        assert len(coordinator.snapshot()) == 1
        finish.set()
        assert await first is True
        assert coordinator.snapshot() == []
    run(scenario())


def test_proxy_managed_swap_keeps_serving_lease_with_legacy_drain_disabled(coordinator, monkeypatch):
    import proxy
    from gateway import lifecycle_drivers
    from gateway.swap_drain import SwapDrainSettings
    from unittest.mock import MagicMock

    async def scenario():
        svc = service()
        svc["lifecycle"] = {"driver": "systemd"}
        monkeypatch.setitem(proxy.config, "backends", {"alpha": svc})
        monkeypatch.setattr(proxy, "_swap_drain", SwapDrain())
        monkeypatch.setattr(proxy, "_service_swap_locks", {})
        monkeypatch.setattr(proxy, "_swap_drain_settings", lambda: SwapDrainSettings(enabled=False))
        monkeypatch.setattr(proxy, "_currently_loaded_model_for", AsyncMock(return_value="old"))
        monkeypatch.setattr(proxy, "_currently_loaded_aux_for", lambda _: None)
        driver = MagicMock()
        driver._SWAPPABLE = True
        driver.build_activation.return_value = {"model": "new"}
        driver.swap_required.return_value = True
        driver.swap = AsyncMock(return_value=True)
        monkeypatch.setattr(lifecycle_drivers, "driver_for_service", lambda *_: driver)
        activation = proxy.LocalModelActivation(backend="alpha", model="new")
        ready, lease = await proxy._ensure_target_leased("alpha", activation)
        assert ready and lease is not None
        assert proxy._swap_drain.in_flight("alpha") == 1
        queued = asyncio.create_task(proxy._ensure_target_leased("alpha", activation))
        await asyncio.sleep(0)
        assert driver.swap.await_count == 1
        lease.release()
        ready, second = await queued
        assert ready and second is not None
        second.release()
        assert driver.swap.await_count == 2
        assert proxy._swap_drain.in_flight("alpha") == 0
    run(scenario())


def test_proxy_managed_start_and_unmanaged_health_preserve_ownership(coordinator, monkeypatch):
    import proxy
    from gateway import lifecycle_drivers
    from unittest.mock import MagicMock

    async def scenario():
        svc = service()
        svc["process"] = {"driver": "command"}
        monkeypatch.setitem(proxy.config, "backends", {"alpha": svc})
        monkeypatch.setattr(proxy, "_swap_drain", SwapDrain())
        monkeypatch.setattr(proxy, "_service_swap_locks", {})
        driver = MagicMock()
        driver._SWAPPABLE = False
        driver.health = AsyncMock(return_value="unhealthy")
        driver.ensure_ready = AsyncMock(return_value=True)
        monkeypatch.setattr(lifecycle_drivers, "driver_for_service", lambda *_: driver)
        assert await proxy.ensure_target("alpha", None)
        assert coordinator._sample.await_count == 1
        driver.ensure_ready.assert_awaited_once()
        coordinator._sample.return_value = [gpu(used=950)]
        with pytest.raises(SwitchUnavailable, match="not enough"):
            await proxy.ensure_target("alpha", None)
        assert driver.ensure_ready.await_count == 1
        # A declared external process is only probed, without resource authority.
        svc["process"] = {"driver": "none"}
        assert await proxy.ensure_target("alpha", None)
        assert driver.ensure_ready.await_count == 2
        assert coordinator._sample.await_count == 2
    run(scenario())


def test_failed_switch_restores_under_reservation_and_retains_uncertainty(coordinator):
    async def scenario():
        selected = ["old"]
        calls = []
        async def invoke(op, args):
            assert coordinator.snapshot()[0]["devices"] == {"card-a": 600}
            calls.append(args["model"])
            selected[0] = args["model"]
        async def observe(name):
            if name == "selected":
                return "wrong" if selected[0] == "new" else selected[0]
            return True
        drain = SwapDrain()
        controller = SwitchController(drain=drain, invoke=invoke, observe=observe)
        with pytest.raises(SwitchUnavailable):
            await controller.select(service(), "new", SELECTION)
        assert calls == ["new", "old"]
        assert selected == ["old"]
        assert drain.in_flight("alpha") == 0
        assert coordinator.snapshot()[0]["state"] == "uncertain"
        assert not coordinator._locks["card-a"].locked()
    run(scenario())


def test_stale_or_failed_capacity_probe_never_mutates(coordinator):
    async def scenario():
        clock = [0.0]
        coordinator._clock = lambda: clock[0]
        async def old_sample():
            clock[0] += 6
            return [gpu()]
        coordinator._sample.side_effect = old_sample
        mutation = AsyncMock()
        with pytest.raises(SwitchUnavailable, match="not advertised"):
            await management.managed_operation(service(), mutation, observe=AsyncMock())
        coordinator._sample.side_effect = OSError("probe failed")
        with pytest.raises(SwitchUnavailable, match="unavailable"):
            await management.managed_operation(service(), mutation, observe=AsyncMock())
        mutation.assert_not_awaited()
        assert coordinator.snapshot() == []
    run(scenario())


def test_runtime_unload_waits_for_active_response_without_resource_claim(coordinator):
    from gateway.engine_control import EngineControlRuntime

    async def scenario():
        svc = service()
        svc["residency"]["unload"] = {"op": "unload"}
        drain = SwapDrain()
        active = drain.lease("alpha")
        locks = {}
        runtime = EngineControlRuntime(svc, drain=drain, lock_for=lambda key: locks.setdefault(key, asyncio.Lock()))
        runtime.invoke = AsyncMock()
        runtime.observe = AsyncMock(return_value=Observation(True, False))
        task = asyncio.create_task(runtime.unload())
        await asyncio.sleep(0)
        runtime.invoke.assert_not_awaited()
        active.release()
        assert await task is True
        runtime.invoke.assert_awaited_once_with("unload", {})
        coordinator._sample.assert_not_awaited()
    run(scenario())


def test_absent_load_declaration_does_not_reserve_or_quarantine(coordinator):
    svc = service()
    svc["residency"] = {}
    controller = ResidencyController(invoke=AsyncMock(), observe=AsyncMock())
    assert run(controller.load(svc)) is False
    coordinator._sample.assert_not_awaited()
    controller._invoke.assert_not_awaited()
    assert coordinator.snapshot() == []


def test_runtime_unload_protects_custom_selection_scope(coordinator):
    from gateway.engine_control import EngineControlRuntime

    async def scenario():
        svc = service()
        svc["residency"]["unload"] = {"op": "unload"}
        svc["offerings"][0]["selection"] = {"mode": "switch", "switch": {"exclusive_scope": "shared-slot"}}
        drain = SwapDrain()
        active = drain.lease("shared-slot")
        locks = {}
        runtime = EngineControlRuntime(svc, drain=drain, lock_for=lambda key: locks.setdefault(key, asyncio.Lock()))
        runtime.invoke = AsyncMock()
        runtime.observe = AsyncMock(return_value=Observation(True, False))
        task = asyncio.create_task(runtime.unload())
        await asyncio.sleep(0)
        runtime.invoke.assert_not_awaited()
        active.release()
        assert await task
        runtime.invoke.assert_awaited_once()
        assert all(not lock.locked() for lock in locks.values())
    run(scenario())


def test_concurrent_reconciliation_is_idempotent(coordinator):
    async def scenario():
        svc = service()
        svc["resources"]["management"]["observe_quiescent"] = "allocation_idle"
        coordinator._uncertain["card-a"] = {"alpha"}
        entered, release = asyncio.Event(), asyncio.Event()
        async def proof(name):
            entered.set()
            await release.wait()
            return Observation(True, True)
        first = asyncio.create_task(coordinator.reconcile(svc, observe=proof))
        await entered.wait()
        second = asyncio.create_task(coordinator.reconcile(svc, observe=proof))
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(first, second) == [True, True]
        assert coordinator.snapshot() == []
    run(scenario())
