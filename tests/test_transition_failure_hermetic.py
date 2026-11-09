"""S8b wire/crash checks: real executor and SQLite stores, loopback engine only.

These complement the prewritten crash rows in test_transition_failure_recovery:
the reset intent here is produced by an actual restore/reset exchange.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from concurrent.futures import Future
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Mapping

import httpx

from gateway import transition_snapshot_steps
from gateway.transition_adapter import DeclaredOperationAdapter
from gateway.transition_ops import SameOperation
from gateway.transition_store import SqliteOperationStore
from tests.support.fake_upstream.app import RecordedRequest
from tests.support.fake_upstream.fixtures import fake_upstreams  # noqa: F401 - local fixture
from tests.support.fake_upstream.protocols import Protocol
from tests.support.fake_upstream.scenario import Exchange, Scenario
from tests.test_transition_admin import Reply
from tests.test_transition_failure_semantics import Engine, run, wire


MUTATIONS = ("checkpoint", "release", "load", "restore", "reset")
OBSERVATIONS = ("idle", "resident", "compatibility", "servable", "clean", "query")


class Barrier:
    """Named, cross-event-loop rendezvous; no sleeps or blocking thread waits."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.reached: Future[None] = Future()
        self.release: Future[None] = Future()

    async def stop_here(self) -> None:
        if not self.reached.done():
            self.reached.set_result(None)
        await asyncio.shield(asyncio.wrap_future(self.release))

    async def arrived(self) -> None:
        await asyncio.shield(asyncio.wrap_future(self.reached))

    def open(self) -> None:
        if not self.release.done():
            self.release.set_result(None)


@dataclass(frozen=True)
class Applied:
    operation: str
    selected: Mapping[str, Any]
    changed: bool


class WireEngine:
    """Reuse the existing engine's snapshot behavior behind the shared wire fake.

    The ledger is server-side and keyed by the real request's effect key. It does
    not forge executor/store outcomes. Replayed requests remain counted even
    though the engine answers them idempotently.
    """

    def __init__(self, fakes: Any, *, refuse: str = "", disconnect: str = "") -> None:
        self.engine = Engine()
        self.refuse = refuse
        self.disconnect = disconnect
        self.ledger: dict[str, Applied] = {}
        self.query_barrier: Barrier | None = None
        self.reset_barrier: Barrier | None = None
        self.readiness_barrier: Barrier | None = None
        self.violations: list[str] = []
        operations = MUTATIONS + OBSERVATIONS
        self.fake = fakes.loopback(Scenario(
            protocol=Protocol("transition-failure-test", ("/transition/{operation}",), "none"),
            name="transition-failure-hermetic",
            provenance={"source": "hand-written", "reference": "SERVICE_TRANSITION_GATE.md G7/G10/G14"},
            exchanges=tuple(Exchange(
                id=op, match={"method": "POST", "path": f"/transition/{op}"},
                response={}, repeat=True, required=False,
            ) for op in operations),
        ))
        self.fake.response_hook = self.respond

    async def respond(self, request: RecordedRequest, configured: Mapping[str, Any]) -> Mapping[str, Any]:
        op = request.path.rsplit("/", 1)[-1]
        envelope = request.json
        body, service_id = envelope["body"], envelope["service_id"]
        if op == "query":
            if self.query_barrier is not None:
                await self.query_barrier.stop_here()
            effect = self.ledger.get(body["effect_key"])
            return {"json": dict(effect.selected) if effect else {"outcome": "unknown"}}
        if op == "resident" and self.readiness_barrier is not None:
            await self.readiness_barrier.stop_here()
        if op not in MUTATIONS:
            reply = await self.engine(service_id, f"transition.{op}", body)
            return {"status": reply.status, "json": dict(reply.selected)}
        key = body.get("effect_key")
        if not isinstance(key, str) or not key:
            self.violations.append(f"{op} has no effect key")
            return {"status": 599, "json": {"outcome": "unknown"}}
        previous = self.ledger.get(key)
        if previous is not None:
            if previous.operation != op:
                self.violations.append(f"{key} reused for {op}")
            return {"json": dict(previous.selected)}
        if op == self.refuse:
            selected = {"outcome": "failed", "incarnation": "inc-1", "evidence_ref": "refused_without_write"}
            changed = False  # The existing engine is deliberately not called.
        else:
            reply = await self.engine(service_id, f"transition.{op}", body)
            selected = dict(reply.selected)
            changed = selected.get("outcome") in {"success", "partial"}
            if op == "restore" and selected.get("outcome") == "partial":
                self.engine.clean = False
            if op == "reset" and selected.get("outcome") == "success":
                self.engine.clean = True
                self.engine.ready = False
        self.ledger[key] = Applied(op, selected, changed)
        if op == "reset" and self.reset_barrier is not None:
            await self.reset_barrier.stop_here()
        if op == self.disconnect:
            return {"disconnect_before_headers": True}
        return {"json": selected}

    def mutation_counts(self) -> Counter[str]:
        return Counter({op: len(self.fake.calls(op)) for op in MUTATIONS})

    def changed(self) -> dict[str, Applied]:
        return {key: entry for key, entry in self.ledger.items() if entry.changed}

    def assert_clean_fixture(self) -> None:
        assert not self.violations
        assert not self.fake.problems()

    def release_barriers(self) -> None:
        for barrier in (self.query_barrier, self.reset_barrier, self.readiness_barrier):
            if barrier is not None:
                barrier.open()


class HttpInvoke:
    """Only the Invoke transport seam changes: the declared adapter remains real."""

    def __init__(self, client: httpx.AsyncClient, engine: WireEngine) -> None:
        self.client, self.engine = client, engine
        self.received: Counter[str] = Counter()

    async def __call__(self, service_id: str, operation: str, body: Mapping[str, Any]) -> Reply:
        assert operation.startswith("transition.")
        op = operation.removeprefix("transition.")
        response = await self.client.post(
            f"{self.engine.fake.base_url}/transition/{op}",
            json={"service_id": service_id, "body": dict(body)},
        )
        self.received[op] += 1
        return Reply(response.status_code, response.json())


def wired(tmp_path: Path, monkeypatch: Any, invoke: HttpInvoke, **semantics: str):
    gates, _, runtime, snapshots = wire(
        tmp_path, monkeypatch, policy="required", verify=True, invoke=invoke, **semantics,
    )
    executor = runtime.executor()
    assert isinstance(executor.store, SqliteOperationStore)
    assert isinstance(executor._adapter_for("dom"), DeclaredOperationAdapter)
    original_policy = executor._policy_of

    def bounded_policy(domain: str) -> dict[str, Any]:
        policy = dict(original_policy(domain))
        profile = dict(policy["profile"])
        # The fixture declares query; keep that choice explicit instead of
        # depending on a future change to the shipped profile's defaults.
        profile["checkpoint"] = {**profile["checkpoint"], "query": True}
        profile["restore"] = {
            **profile["restore"], "query": True,
            "reset": {**profile["restore"]["reset"], "query": True},
        }
        policy.update(
            profile=profile, probe_deadline_s=0.3, query_deadline_s=0.1,
            release_deadline_s=0.3, load_deadline_s=0.3, drain_deadline_s=0.3,
            lock_wait_s=0.3, idle_poll_s=0.001,
            deadlines={"checkpoint": 0.3, "restore": 0.3, "reset": 0.3, "query": 0.1},
        )
        return policy

    monkeypatch.setattr(executor, "_policy_of", bounded_policy)
    return gates, executor, snapshots


def bounded(coro: Any) -> None:
    started = time.monotonic()

    async def go() -> None:
        async with asyncio.timeout(1.5):
            await coro

    run(go())
    assert time.monotonic() - started < 2.0


def test_no_write_failure_is_durable_and_same_key_never_replays(tmp_path, monkeypatch, fake_upstreams) -> None:
    engine = WireEngine(fake_upstreams, refuse="checkpoint")
    before = (engine.engine.resident, engine.engine.clean)

    async def go() -> None:
        async with httpx.AsyncClient(trust_env=False, timeout=0.5) as client:
            invoke = HttpInvoke(client, engine)
            gates, executor, snapshots = wired(tmp_path, monkeypatch, invoke, checkpoint="no_change")
            begun = await executor.submit(domain="dom", kind="select", target="beta", idempotency_key="no-write")
            done = await executor.wait(begun.operation.op_id)
            durable = await asyncio.to_thread(executor.store.get, done.op_id)
            # "aborted" is the executor's terminal failed-but-unchanged state.
            assert (durable.state, durable.step, durable.step_outcome) == ("aborted", "checkpoint", "no_change")
            assert gates.state("dom") == "open"
            assert (engine.engine.resident, engine.engine.clean) == before
            assert not engine.changed()
            assert engine.mutation_counts() == Counter(checkpoint=1)
            assert len(engine.ledger) == 1
            assert durable.step_intent.effect_key in engine.ledger
            assert snapshots.pointer("dom") is None
            counts = Counter(request.path for request in engine.fake.requests)
            replay = await executor.submit(domain="dom", kind="select", target="beta", idempotency_key="no-write")
            assert isinstance(replay, SameOperation)
            assert replay.operation.op_id == done.op_id
            assert Counter(request.path for request in engine.fake.requests) == counts
            engine.assert_clean_fixture()

    bounded(go())


def test_applied_then_disconnected_effect_stays_unknown_after_late_query_reply(
        tmp_path, monkeypatch, fake_upstreams) -> None:
    engine = WireEngine(fake_upstreams, disconnect="checkpoint")
    engine.query_barrier = Barrier("checkpoint-query-reply")

    async def go() -> None:
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=0.5) as client:
                invoke = HttpInvoke(client, engine)
                gates, executor, _ = wired(tmp_path, monkeypatch, invoke, checkpoint="unknown")
                baseline_late = set(transition_snapshot_steps._LATE_EFFECTS)
                begun = await executor.submit(domain="dom", kind="select", target="beta", idempotency_key="drop")
                await engine.query_barrier.arrived()
                done = await executor.wait(begun.operation.op_id)
                durable = await asyncio.to_thread(executor.store.get, done.op_id)
                assert (durable.state, durable.step, durable.step_outcome) == ("reconciling", "checkpoint", "unknown")
                assert gates.state("dom") == "reconciling"
                assert engine.mutation_counts() == Counter(checkpoint=1)
                key = durable.step_intent.effect_key
                assert set(engine.ledger) == set(engine.changed()) == {key}
                assert engine.ledger[key].selected["outcome"] == "success"
                assert engine.fake.calls("query")[0].json["body"]["effect_key"] == key
                assert len(engine.fake.calls("query")) == 1
                assert invoke.received["checkpoint"] == invoke.received["query"] == 0
                late = set(transition_snapshot_steps._LATE_EFFECTS) - baseline_late
                assert len(late) == 1, "the query is still waiting on a real HTTP reply"
                counts = engine.mutation_counts()
                engine.query_barrier.open()
                await asyncio.gather(*late)
                assert invoke.received["query"] == 1
                after = await asyncio.to_thread(executor.store.get, done.op_id)
                assert after == durable, "a late success must not overwrite the durable unknown outcome"
                assert gates.state("dom") == "reconciling"
                assert engine.mutation_counts() == counts
                engine.assert_clean_fixture()
        finally:
            engine.release_barriers()

    bounded(go())


def test_reset_applied_before_outcome_survives_restart_without_replay_or_early_open(
        tmp_path, monkeypatch, fake_upstreams) -> None:
    engine = WireEngine(fake_upstreams)
    engine.reset_barrier = Barrier("reset-applied-before-response")
    engine.engine.restore_outcome = "partial"

    async def go() -> None:
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=0.5) as client:
                invoke = HttpInvoke(client, engine)
                gates, executor, snapshots = wired(tmp_path, monkeypatch, invoke)
                park = await executor.submit(domain="dom", kind="park", idempotency_key="park-for-reset")
                assert (await executor.wait(park.operation.op_id)).state == "succeeded"
                assert snapshots.pointer("dom") is not None
                baseline_late = set(transition_snapshot_steps._LATE_EFFECTS)
                wake = await executor.submit(domain="dom", kind="wake", target="alpha", idempotency_key="reset-crash")
                await engine.reset_barrier.arrived()
                outstanding = await asyncio.to_thread(executor.store.get, wake.operation.op_id)
                reset_key = f"{wake.operation.op_id}:reset"
                assert outstanding.step_intent.effect_key == reset_key
                assert outstanding.step_outcome is None
                assert engine.ledger[reset_key].selected["outcome"] == "success"
                assert engine.ledger[reset_key].changed
                assert engine.mutation_counts() == Counter(checkpoint=1, release=1, load=1, restore=1, reset=1)
                assert gates.state("dom") != "open"
                task = executor._tasks[wake.operation.op_id]
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                assert task.cancelled()
                # Simulate process loss, not just a cancelled requester: its
                # detached HTTP effect must not survive into the new executor.
                abandoned = set(transition_snapshot_steps._LATE_EFFECTS) - baseline_late
                assert len(abandoned) == 1
                for pending in abandoned:
                    pending.cancel()
                await asyncio.gather(*abandoned, return_exceptions=True)
                saved = await asyncio.to_thread(executor.store.get, wake.operation.op_id)
                assert saved.step_intent.effect_key == reset_key and saved.step_outcome is None
                engine.reset_barrier.open()
                engine.readiness_barrier = Barrier("restart-readiness-evidence")
                fresh_gates, recovered, fresh_snapshots = wired(tmp_path, monkeypatch, invoke)
                assert recovered is not executor and recovered.store is not executor.store
                assert fresh_gates is not gates and fresh_gates.path == gates.path
                assert fresh_snapshots is not snapshots
                assert fresh_gates.state("dom") != "open"
                counts = engine.mutation_counts()
                await recovered.reconcile_startup()
                await engine.readiness_barrier.arrived()
                assert fresh_gates.state("dom") != "open", "no readiness reply has been released"
                assert engine.mutation_counts() == counts
                queries = engine.fake.calls("query")
                assert len(queries) == 1 and queries[0].json["body"]["effect_key"] == reset_key
                engine.engine.ready = True
                engine.readiness_barrier.open()
                done = await recovered.wait(wake.operation.op_id)
                assert (done.state, fresh_gates.state("dom")) == ("succeeded", "open")
                assert engine.mutation_counts() == counts
                assert len(engine.fake.calls("reset")) == 1
                assert len([entry for entry in engine.changed().values() if entry.operation == "reset"]) == 1
                engine.assert_clean_fixture()
        finally:
            engine.release_barriers()

    bounded(go())
