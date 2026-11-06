"""The transition executor wired into the gateway (proxy.py): a request for a parked domain wakes it, an admission
sees "parked", and unfinished operations are settled or held before the first admission."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

import proxy
from gateway import engine_egress, transition_gate
from gateway.engine_protocol import description, executor as protocol_executor, transport
from gateway.transition_gate import GateStore, TransitionUnavailable
from gateway.transition_ops import EffectResult, StepIntent
from gateway.transition_runtime import TransitionRuntime
from tests.support.fake_upstream import HandlerTransport
from tests.test_transition_admin import FAST, FakeEngine, PROFILE, services


@pytest.fixture
def wired(monkeypatch, tmp_path):
    backends = {**(proxy.config.get("backends") or {}), **{k: v for k, v in services().items() if v}}
    monkeypatch.setitem(proxy.config, "backends", backends)
    store = GateStore(tmp_path / "transition.db")
    store.load()
    monkeypatch.setattr(proxy, "TRANSITION_GATES", store)
    monkeypatch.setattr(proxy, "_swap_drain", proxy._new_swap_drain())
    monkeypatch.setattr(proxy, "_service_swap_locks", {})
    monkeypatch.setattr(proxy, "_strict_service_availability_overrides", lambda: {})
    engine = FakeEngine()
    runtime = TransitionRuntime(gates=lambda: store, services=lambda: proxy.config.get("backends") or {},
                                lock_for=proxy.swap_lock_for, drain=lambda: proxy._swap_drain, protocol_invoke=engine)
    monkeypatch.setattr(proxy, "TRANSITION_RUNTIME", runtime)
    return store, engine, runtime


async def park(runtime: TransitionRuntime, key: str = "park-1") -> str:
    executor = runtime.executor()
    began = await executor.submit(domain="dom", kind="park", idempotency_key=key)
    done = await executor.wait(began.operation.op_id)
    assert done.state == "succeeded"
    return done.op_id


def test_a_request_for_a_parked_domain_wakes_it_before_it_is_admitted(wired) -> None:
    store, engine, runtime = wired

    async def scenario() -> None:
        park_id = await park(runtime)
        assert engine.resident is None and store.parked("dom") == park_id
        async with proxy._admission_lock("alpha"):
            lease = proxy._swap_drain.lease("alpha")
            assert engine.resident == "alpha", "the wake loaded the offering the park released"
            assert store.parked("dom") is None and store.state("dom") == "open"
            lease.release()
        loads = [call for call in engine.calls if call[0] == "transition.load"]
        async with proxy._admission_lock("beta"):
            pass
        assert [call for call in engine.calls if call[0] == "transition.load"] == loads, "an awake domain is not woken again"

    asyncio.run(scenario())


def test_requests_that_find_the_domain_parked_share_one_wake(wired) -> None:
    store, engine, runtime = wired

    async def scenario() -> None:
        await park(runtime)

        async def request(service: str) -> None:
            async with proxy._admission_lock(service):
                proxy._swap_drain.lease(service).release()

        await asyncio.gather(*(request(name) for name in ("alpha", "beta", "alpha", "beta")))
        assert len([call for call in engine.calls if call[0] == "transition.load"]) == 1

    asyncio.run(scenario())


def test_a_wake_that_does_not_succeed_refuses_the_request_with_retry_after(wired) -> None:
    store, engine, runtime = wired

    async def scenario() -> None:
        await park(runtime)
        engine.resident = "other-offering"   # something else is resident after the load: the load is not verified

        original = engine.__call__

        async def refuse_load(service_id: str, op: str, body: Any) -> Any:
            if op == "transition.load":
                engine.calls.append((op, dict(body)))
                from tests.test_transition_admin import Reply
                return Reply(200, {"outcome": "failed"})
            return await original(service_id, op, body)

        runtime._protocol_invoke = refuse_load
        with pytest.raises(TransitionUnavailable) as caught:
            async with proxy._admission_lock("alpha"):
                raise AssertionError("not admitted")
        assert caught.value.retry_after_s == 7 and caught.value.reason.startswith("wake_")
        assert store.state("dom") == "failed_closed"

    asyncio.run(scenario())


def test_a_lease_is_refused_while_the_domain_is_parked(wired) -> None:
    store, engine, runtime = wired

    async def scenario() -> None:
        await park(runtime)
        with pytest.raises(TransitionUnavailable) as caught:
            proxy._admit_serving_lease("alpha")
        assert caught.value.reason == "parked"

    asyncio.run(scenario())


def test_startup_reconcile_holds_an_unfinished_operation_before_any_admission(wired) -> None:
    store, engine, runtime = wired

    async def scenario() -> None:
        executor = runtime.executor()
        began = executor.store.begin(domain="dom", kind="park", idempotency_key="crashed", target="alpha",
                                     incarnation="inc-1", config_revision=0)
        op = began.operation
        executor.store.write_intent(op.op_id, StepIntent(step="release", effect_key=f"{op.op_id}:release",
                                                         expected_incarnation="inc-1"))
        # The process "restarted": the gate table says closed; the engine cannot say what became of the release.
        engine.query_outcome = "unknown"
        fresh = GateStore(store.path)
        fresh.load()
        assert fresh.state("dom") == "closed"
        await executor.reconcile_startup()
        held = executor.store.get(op.op_id)
        assert held is not None and held.state == "reconciling" and store.state("dom") == "reconciling"
        with pytest.raises(TransitionUnavailable) as caught:
            proxy._admit_serving_lease("alpha")
        assert caught.value.reason == "gate_reconciling"

    asyncio.run(scenario())


def test_the_profile_domain_only_leases_and_forwards(wired) -> None:
    policy = proxy._transition_policy("alpha")
    assert policy.profile == PROFILE and policy.lease_only and not policy.pass_through
    assert set(FAST) >= {"retry_after_s"}


class RecordingStep:
    """Stands in for a checkpoint step another lane supplies: records what the executor hands a merged step."""

    name = "checkpoint"

    def __init__(self, engine: FakeEngine) -> None:
        self.engine = engine
        self.seen: list[Any] = []

    def applies(self, operation: Any, adapter: Any) -> bool:
        return operation.kind == "park"

    async def run(self, ctx: Any) -> str:
        self.seen.append((dict(ctx.policy), len(self.engine.calls)))
        return "continue"


def test_merged_steps_run_in_order_and_read_attested_fields_from_the_reviewed_profile_only(wired) -> None:
    store, engine, _ = wired
    step = RecordingStep(engine)
    runtime = TransitionRuntime(gates=lambda: store, services=lambda: proxy.config.get("backends") or {},
                                lock_for=proxy.swap_lock_for, drain=lambda: proxy._swap_drain, protocol_invoke=engine,
                                extra_steps={"checkpoint": step}, snapshots="store-handle")

    async def scenario() -> None:
        await park(runtime)
        (policy, calls_before), = step.seen
        assert policy["profile"]["id"] == PROFILE and policy["profile"]["attested"] is True
        assert policy["checkpoint_policy"] == "best_effort"
        assert policy["deadlines"]["release"] == 2.0 and policy["retry_after_s"] == 7
        assert not any(call[0] == "transition.release" for call in engine.calls[:calls_before]), "checkpoint runs before release"
        assert any(call[0] == "transition.release" for call in engine.calls[calls_before:])
        assert runtime.executor()._snapshots == "store-handle"

    asyncio.run(scenario())


def test_startup_hooks_run_before_the_executor_recovers_and_a_failing_hook_does_not_stop_it(wired) -> None:
    store, engine, _ = wired
    order: list[str] = []

    def failing() -> None:
        order.append("hook-failed")
        raise RuntimeError("snapshot store unreadable")

    runtime = TransitionRuntime(gates=lambda: store, services=lambda: proxy.config.get("backends") or {},
                                lock_for=proxy.swap_lock_for, drain=lambda: proxy._swap_drain, protocol_invoke=engine,
                                startup=[lambda: order.append("snapshots"), failing])

    async def scenario() -> None:
        executor = runtime.executor()
        original = executor.reconcile_startup

        async def recorded() -> None:
            order.append("executor")
            await original()

        executor.reconcile_startup = recorded  # type: ignore[method-assign]
        await runtime.reconcile_startup()

    asyncio.run(scenario())
    assert order == ["snapshots", "hook-failed", "executor"]


def test_an_owner_cannot_set_the_profile_blocks_the_snapshot_steps_trust(wired) -> None:
    from gateway.transition_gate import forbidden_profile_keys
    for key in ("servable_check", "placement_keys", "failure_semantics"):
        record = {"residency": {"domain": "dom", "transition": {"enabled": True, "profile": PROFILE, key: "x"}}}
        assert forbidden_profile_keys(record) == (key,)


def test_the_gateways_runtime_carries_the_snapshot_steps_and_opens_the_ledger_only_at_startup(wired, monkeypatch, tmp_path) -> None:
    """Building the runtime reads no path. The startup hook opens the ledger beside ``transition.db``. A domain whose
    adapter has no checkpoint (the generic adapter) never reaches the checkpoint or restore step, so a park and a wake
    send exactly the operations they sent before the snapshot steps were wired."""
    _, engine, _ = wired
    monkeypatch.setattr(proxy, "ADMIN_DB_PATH", tmp_path / "admin.db")
    runtime = proxy._new_transition_runtime()
    monkeypatch.setattr(runtime, "_protocol_invoke", engine)
    assert not (tmp_path / "transition-snapshots").exists(), "no directory is made when the runtime is built"
    assert runtime._startup and runtime._snapshots.__class__.__name__ == "LazySnapshotStore"

    async def scenario() -> None:
        await runtime.reconcile_startup()
        assert (tmp_path / "transition-snapshots").is_dir()
        steps = runtime.executor()._steps
        assert {"checkpoint", "restore"} <= set(steps)
        assert runtime._snapshots.opened().path == tmp_path / "transition.db"
        for kind, key in (("park", "park-1"), ("wake", "wake-1")):
            executor = runtime.executor()
            began = await executor.submit(domain="dom", kind=kind, idempotency_key=key, target="alpha" if kind == "wake" else None)
            assert (await executor.wait(began.operation.op_id)).state == "succeeded"
        ops = [call[0] for call in engine.calls]
        assert "transition.checkpoint" not in ops and "transition.restore" not in ops
        assert runtime._snapshots.rows("dom") == (), "the generic adapter wrote no snapshot"

# -- review round: startup with an unreadable table --------------------------------------------------------


def test_startup_completes_when_the_transition_table_is_unreadable_and_every_domain_reads_reconciling(wired, tmp_path, monkeypatch) -> None:
    _, engine, _ = wired
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"this is not a sqlite database" * 64)
    store = GateStore(broken)
    store.load()
    assert not store.readable()
    runtime = TransitionRuntime(gates=lambda: store, services=lambda: proxy.config.get("backends") or {},
                                lock_for=proxy.swap_lock_for, drain=lambda: proxy._swap_drain, protocol_invoke=engine)

    async def scenario() -> None:
        await runtime.reconcile_startup()  # must not raise

    asyncio.run(scenario())
    assert store.state("dom") == "reconciling", "the gate fails closed while the table cannot be read"
    monkeypatch.setattr(proxy, "TRANSITION_GATES", store)
    with pytest.raises(TransitionUnavailable) as caught:
        proxy._admit_serving_lease("alpha")
    assert caught.value.reason == "gate_reconciling"
    assert engine.calls == [], "nothing was asked of the engine"


def test_startup_completes_when_the_table_reads_then_fails_during_recovery(wired) -> None:
    store, engine, runtime = wired

    def broken() -> Any:
        raise sqlite3.OperationalError("database is locked")

    runtime.executor().store.unfinished = broken  # type: ignore[method-assign]
    asyncio.run(runtime.reconcile_startup())


# -- review round: a table that was unreadable at start-up is reconciled once it reads --------------------------


def _leftover_operation(store, runtime, engine, *, with_intent: bool):
    executor = runtime.executor()
    began = executor.store.begin(domain="dom", kind="park", idempotency_key="crashed", target="alpha",
                                 incarnation="inc-1", config_revision=0)
    op = began.operation
    if with_intent:
        executor.store.write_intent(op.op_id, StepIntent(step="release", effect_key=f"{op.op_id}:release",
                                                         expected_incarnation="inc-1"))
    engine.query_outcome = "unknown"
    return executor, op


def _unreadable_at_start(store, monkeypatch):
    """The table could not be read at start-up and cannot be until ``readable_again`` runs."""
    real_load = store.load
    monkeypatch.setattr(store, "load", lambda: None)
    store._readable = False

    def readable_again() -> None:
        monkeypatch.setattr(store, "load", real_load)
        store._last_load = None

    return readable_again


def test_an_operation_left_running_is_reconciled_when_the_table_first_reads_after_an_unreadable_start(wired, monkeypatch) -> None:
    store, engine, runtime = wired
    executor, op = _leftover_operation(store, runtime, engine, with_intent=True)
    readable_again = _unreadable_at_start(store, monkeypatch)

    async def scenario() -> None:
        await runtime.reconcile_startup()
        assert not executor.reconciled() and executor.store.get(op.op_id).state == "running", "nothing could be settled yet"
        readable_again()
        with pytest.raises(TransitionUnavailable) as caught:
            async with proxy._admission_lock("alpha"):
                raise AssertionError("not admitted")
        assert caught.value.reason == "gate_reconciling", "the first request settled the leftover operation"
        assert executor.reconciled() and executor.store.get(op.op_id).state == "reconciling"
        assert not [call for call in engine.calls if call[0] == "transition.release"], "no effect was repeated"

    asyncio.run(scenario())


def test_a_resumable_operation_left_running_finishes_and_the_gate_opens_after_an_unreadable_start(wired, monkeypatch) -> None:
    store, engine, runtime = wired
    executor, op = _leftover_operation(store, runtime, engine, with_intent=False)
    readable_again = _unreadable_at_start(store, monkeypatch)

    async def scenario() -> None:
        await runtime.reconcile_startup()
        readable_again()
        with pytest.raises(TransitionUnavailable):
            async with proxy._admission_lock("alpha"):
                raise AssertionError("the gate is still closed for the first request")
        done = await executor.wait(op.op_id)
        assert (done.state, store.state("dom")) == ("succeeded", "open"), "the gate was not left closed for good"
        assert store.parked("dom") == op.op_id
        await executor.ensure_reconciled()
        assert len([call for call in engine.calls if call[0] == "transition.release"]) == 1, "reconciled once"

    asyncio.run(scenario())


def test_the_reconcile_runs_once_even_when_requests_arrive_together(wired, monkeypatch) -> None:
    store, engine, runtime = wired
    executor, _ = _leftover_operation(store, runtime, engine, with_intent=True)
    readable_again = _unreadable_at_start(store, monkeypatch)
    runs: list[int] = []
    real = executor.reconcile_startup

    async def counted() -> None:
        runs.append(1)
        await real()

    executor.reconcile_startup = counted  # type: ignore[method-assign]

    async def scenario() -> None:
        await runtime.reconcile_startup()
        readable_again()
        await asyncio.gather(*(executor.ensure_reconciled() for _ in range(5)), runtime.reconcile_if_due(5.0))
        await executor.ensure_reconciled()

    asyncio.run(scenario())
    assert runs == [1]


def test_nothing_begins_while_the_leftover_operations_are_not_yet_settled(wired, monkeypatch) -> None:
    store, engine, runtime = wired
    executor, op = _leftover_operation(store, runtime, engine, with_intent=True)
    _unreadable_at_start(store, monkeypatch)

    async def scenario() -> None:
        await runtime.reconcile_startup()
        assert not executor.reconciled()
        with pytest.raises(TransitionUnavailable) as caught:
            await executor.submit(domain="dom", kind="wake", idempotency_key="early", target="alpha")
        assert caught.value.reason == "reconciling"
        assert executor.active() == () and executor.store.get(op.op_id).state == "running", "nothing began or moved"

    asyncio.run(scenario())


def test_a_reconcile_that_fails_mid_read_refuses_the_submit_with_a_503(wired) -> None:
    store, engine, runtime = wired
    executor = runtime.executor()

    def broken() -> Any:
        raise sqlite3.OperationalError("database is locked")

    executor.store.unfinished = broken  # type: ignore[method-assign]

    async def scenario() -> None:
        with pytest.raises(TransitionUnavailable) as caught:
            await executor.submit(domain="dom", kind="park", idempotency_key="p", target=None)
        assert caught.value.reason == "reconciling" and executor.active() == ()

    asyncio.run(scenario())


def test_the_reconcile_leaves_alone_an_operation_this_process_drives(wired, monkeypatch) -> None:
    store, engine, runtime = wired
    executor = runtime.executor()

    async def scenario() -> None:
        await runtime.reconcile_startup()
        assert executor.reconciled()
        began = await executor.submit(domain="dom", kind="park", idempotency_key="live", target=None)
        live = began.operation.op_id
        assert live in executor.active()
        recovered: list[str] = []
        real = executor._recover

        async def spy(operation) -> None:
            recovered.append(operation.op_id)
            await real(operation)

        monkeypatch.setattr(executor, "_recover", spy)
        await executor.reconcile_startup()  # a second pass, as a late reconcile would run
        assert live not in recovered, "the reconcile touched an operation a live task drives"
        done = await executor.wait(live)
        assert done.state == "succeeded", "the live operation finished on its own driver"

    asyncio.run(scenario())


# -- review round: the waking request's lease is minted under the lock -------------------------------------


def _quick_drain_deadline() -> None:
    for name in ("alpha", "beta"):
        proxy.config["backends"][name]["residency"]["transition"]["drain_deadline_s"] = 0.3


def test_an_owner_park_that_wins_the_lock_after_the_wake_cannot_park_under_the_waking_request(wired, monkeypatch) -> None:
    """The wake minted the request's lease under its own hold of the lock. An owner park that takes the lock before the
    request does must wait for that lease, miss its drain deadline and abort; the request is then served."""
    store, engine, runtime = wired
    _quick_drain_deadline()
    real_wake = proxy._wake_if_parked
    parks: list[Any] = []

    async def wake_then_owner_park(service_id: str, policy: Any) -> Any:
        trigger = await real_wake(service_id, policy)
        assert proxy._swap_drain.in_flight("alpha") == 1, "the wake minted the lease before it released the lock"
        parks.append(await runtime.executor().submit(domain="dom", kind="park", idempotency_key="owner-park"))
        await asyncio.sleep(0)
        return trigger

    monkeypatch.setattr(proxy, "_wake_if_parked", wake_then_owner_park)

    async def scenario() -> None:
        await park(runtime)
        served: list[str | None] = []
        async with proxy._admission_lock("alpha"):
            lease = proxy._swap_drain.lease("alpha")
            served.append(engine.resident)
            assert proxy._swap_drain.in_flight("alpha") == 2, "the bridge lease and the request's own lease"
            lease.release()
        assert served == ["alpha"], "the request was served by the offering the wake loaded"
        assert proxy._swap_drain.in_flight("alpha") == 0, "the bridge lease ends with the hold"
        [began] = parks
        aborted = await runtime.executor().wait(began.operation.op_id)
        assert (aborted.state, aborted.step, aborted.step_outcome) == ("aborted", "drain", "no_change")
        assert engine.resident == "alpha" and store.parked("dom") is None and store.state("dom") == "open"

    asyncio.run(scenario())


def test_a_request_that_leaves_while_it_waits_for_the_lock_gives_the_wakes_lease_back(wired, monkeypatch) -> None:
    store, engine, runtime = wired
    real_wake = proxy._wake_if_parked
    parks: list[Any] = []

    async def wake_then_owner_park(service_id: str, policy: Any) -> Any:
        trigger = await real_wake(service_id, policy)
        parks.append(await runtime.executor().submit(domain="dom", kind="park", idempotency_key="owner-park"))
        await asyncio.sleep(0)
        return trigger

    monkeypatch.setattr(proxy, "_wake_if_parked", wake_then_owner_park)

    async def scenario() -> None:
        await park(runtime)

        async def request() -> None:
            async with proxy._admission_lock("alpha"):
                raise AssertionError("not admitted")

        task = asyncio.get_running_loop().create_task(request())
        for _ in range(500):  # the wake and the owner park run on threads: wait for the park to hold the lock
            if parks and proxy.swap_lock_for("alpha").locked():
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert proxy._swap_drain.in_flight("alpha") == 1, "the lease waits for the request's turn"
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert proxy._swap_drain.in_flight("alpha") == 0, "a request that is cancelled leaks no lease"
        [began] = parks
        done = await runtime.executor().wait(began.operation.op_id)
        assert done.state == "succeeded" and store.parked("dom") == done.op_id, "with the lease gone the park drains and parks"

    asyncio.run(scenario())


def test_a_request_that_only_joins_another_wake_holds_no_bridge_lease(wired, monkeypatch) -> None:
    store, engine, runtime = wired
    bridges: list[Any] = []
    real_lease = proxy._swap_drain.lease

    def counting(backend: str, **kwargs: Any) -> Any:
        lease = real_lease(backend, **kwargs)
        if kwargs.get("admit") is False:
            bridges.append(lease)  # only the transition executor mints without the admit hook
        return lease

    monkeypatch.setattr(proxy._swap_drain, "lease", counting)

    async def scenario() -> None:
        await park(runtime)

        async def request(service: str) -> None:
            async with proxy._admission_lock(service):
                proxy._swap_drain.lease(service).release()

        await asyncio.gather(*(request(name) for name in ("alpha", "beta", "alpha")))
        assert len(bridges) == 1, "only the request whose submit began the wake gets a bridge lease"
        assert bridges[0].released and proxy._swap_drain.in_flight("alpha") == 0

    asyncio.run(scenario())


# -- review round: the executor's own calls through the real invoke path and the real egress hook -----------

TGATE_FIXTURE = Path(__file__).parent / "fixtures" / "engine_protocol" / "tgate"
REAL_FAST = {"admission_wait_s": 2, "drain_deadline_s": 1, "idle_poll_s": 0.01, "probe_deadline_s": 1, "release_deadline_s": 2,
             "load_deadline_s": 2, "query_deadline_s": 0.5, "lock_wait_s": 1, "retry_after_s": 7}


class EngineWire:
    """The engine's HTTP side: answers the fixture protocol's five operations from its own state and records each request."""

    def __init__(self) -> None:
        self.resident: str | None = "alpha"
        self.requests: list[tuple[str, str, bytes]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.url.host + ":" + str(request.url.port), request.url.path, request.content))
        body = json.loads(request.content or b"{}")
        path = request.url.path
        if path == "/t/resident":
            answer = {"resident": self.resident is not None, "offering": self.resident, "ready": True, "incarnation": "inc-1"}
        elif path == "/t/idle":
            answer = {"idle": True, "incarnation": "inc-1"}
        elif path == "/t/release":
            self.resident = None
            answer = {"outcome": "success", "incarnation": "inc-1"}
        elif path == "/t/load":
            self.resident = body["target"]
            answer = {"outcome": "success", "evidence_ref": "loaded"}
        else:
            answer = {"outcome": "unknown"}
        return httpx.Response(200, json=answer)


def _engine_record(domain: str, port: int) -> dict[str, Any]:
    return {"enabled": True,
            "config": {"connection": {"transport": "http", "dispatch": "protocol", "protocol": "tgate",
                                      "address": f"http://127.0.0.1:{port}", "credentials": {},
                                      "timeouts": {"connect_s": 2, "first_byte_s": 5, "total_s": 10}}},
            "residency": {"domain": domain, "transition": {"enabled": True, "profile": PROFILE, **REAL_FAST}}}


@pytest.fixture
def real(monkeypatch, tmp_path):
    """Two enabled domains on two addresses, the real http-v1 invoke path, the real egress hook and a recording wire."""
    records = {"eng-a": _engine_record("dom-a", 9201), "eng-b": _engine_record("dom-b", 9202)}
    monkeypatch.setitem(proxy.config, "backends", {**(proxy.config.get("backends") or {}), **records})
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: {"schema_version": 2, "services": records})
    monkeypatch.setattr(description, "REGISTRY_DIR", TGATE_FIXTURE)
    original = description.load_registry
    monkeypatch.setattr(description, "load_registry", lambda directory=TGATE_FIXTURE: original(directory))
    wire = EngineWire()
    base = transport.HttpxTransport.for_tests(HandlerTransport(wire))
    monkeypatch.setattr(protocol_executor, "HttpxTransport", lambda: base)
    monkeypatch.setattr(engine_egress, "_live", engine_egress._live)  # restored after the test
    engine_egress.publish(proxy.config["backends"])
    store = GateStore(tmp_path / "transition.db")
    store.load()
    monkeypatch.setattr(proxy, "TRANSITION_GATES", store)
    monkeypatch.setattr(proxy, "_swap_drain", proxy._new_swap_drain())
    monkeypatch.setattr(proxy, "_service_swap_locks", {})
    monkeypatch.setattr(proxy, "_strict_service_availability_overrides", lambda: {})
    token = transition_gate._EGRESS_LEASES.set(())
    runtime = TransitionRuntime(gates=lambda: store, services=lambda: proxy.config.get("backends") or {},
                                lock_for=proxy.swap_lock_for, drain=lambda: proxy._swap_drain,
                                protocol_invoke=proxy._transition_protocol_invoke)
    monkeypatch.setattr(proxy, "TRANSITION_RUNTIME", runtime)
    yield SimpleNamespace(wire=wire, store=store, runtime=runtime)
    transition_gate._EGRESS_LEASES.reset(token)


def test_the_executors_control_call_is_sent_for_its_own_domain_and_refused_for_any_other(real) -> None:
    async def scenario() -> None:
        # A lease-less send to an enabled domain's address is refused by the hook: the map is real.
        client = engine_egress.async_client(transport=HandlerTransport(real.wire))
        with pytest.raises(TransitionUnavailable) as refused:
            await client.post("http://127.0.0.1:9201/t/resident", content=b"{}")
        assert refused.value.reason == "path_not_leased" and real.wire.requests == []

        reply = await proxy._transition_protocol_invoke("eng-a", "transition.resident", {})
        assert reply.status == 200 and reply.selected["offering"] == "alpha"
        assert [(host, path) for host, path, _ in real.wire.requests] == [("127.0.0.1:9201", "/t/resident")]

        # The permit covers the domain of the call only, and only for the call.
        with engine_egress.permit_domain("dom-a"):
            with pytest.raises(TransitionUnavailable) as other:
                await client.post("http://127.0.0.1:9202/t/resident", content=b"{}")
            assert other.value.domain == "dom-b"
        with pytest.raises(TransitionUnavailable):
            await client.post("http://127.0.0.1:9201/t/resident", content=b"{}")
        assert len(real.wire.requests) == 1, "nothing else was sent"
        await client.aclose()

    asyncio.run(scenario())


def test_a_park_and_a_wake_run_over_the_real_invoke_path_with_no_serving_lease(real) -> None:
    async def scenario() -> None:
        executor = real.runtime.executor()
        began = await executor.submit(domain="dom-a", kind="park", idempotency_key="park-1")
        parked = await executor.wait(began.operation.op_id)
        assert (parked.state, real.wire.resident) == ("succeeded", None)
        assert real.store.parked("dom-a") == parked.op_id
        woke = await executor.ensure_awake("dom-a", wait_s=5.0)
        assert woke is not None and woke.state == "succeeded" and real.wire.resident == "alpha"
        paths = [path for _, path, _ in real.wire.requests]
        assert "/t/release" in paths and "/t/load" in paths and paths.count("/t/resident") >= 2
        assert {host for host, _, _ in real.wire.requests} == {"127.0.0.1:9201"}, "no call reached the other domain"
        assert proxy._swap_drain.in_flight("dom-a") == 0 and real.store.state("dom-a") == "open"

    asyncio.run(scenario())
