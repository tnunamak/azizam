"""The operation record (D2, T1.2): the compare-and-set, write-ahead, stale outcomes, the gate cache, and a seeded
state-machine test of the record's invariants (gateway/transition_store.py)."""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path
from typing import Optional

import pytest

from gateway.transition_gate import GateStore
from gateway.transition_ops import FINISHED_STATES, Begun, Conflict, EffectResult, SameOperation, StepIntent
from gateway.transition_store import GATE_FOR_STATE, SqliteOperationStore, StaleOutcome, request_hash


def make(tmp_path: Path, name: str = "transition.db") -> tuple[GateStore, SqliteOperationStore]:
    gates = GateStore(tmp_path / name)
    gates.load()
    return gates, SqliteOperationStore(gates)


def begin(store: SqliteOperationStore, domain: str = "d", kind: str = "select", key: str = "k", target: Optional[str] = "t",
          incarnation: Optional[str] = "i1"):
    return store.begin(domain=domain, kind=kind, idempotency_key=key, target=target,  # type: ignore[arg-type]
                       incarnation=incarnation, config_revision=3)


def intent(op_id: str, step: str, incarnation: Optional[str] = "i1") -> StepIntent:
    return StepIntent(step=step, effect_key=f"{op_id}:{step}", expected_incarnation=incarnation)  # type: ignore[arg-type]


# -- begin: the compare-and-set ---------------------------------------------------------------------------


def test_begin_inserts_the_operation_and_closes_the_gate_durably_and_in_the_cache(tmp_path: Path) -> None:
    gates, store = make(tmp_path)
    assert gates.state("d") == "open"
    result = begin(store)
    assert isinstance(result, Begun)
    op = result.operation
    assert (op.state, op.step, op.target, op.incarnation, op.config_revision) == ("running", "close", "t", "i1", 3)
    assert gates.state("d") == "closed", "the cache is refreshed before begin returns"
    other, _ = make(tmp_path)
    assert other.state("d") == "closed", "the table holds it: a new process reads it"


def test_a_second_contender_gets_the_existing_operation_and_the_same_key_gets_the_same_one(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    first = begin(store, key="a")
    rival = begin(store, key="b")
    assert isinstance(rival, Conflict) and rival.operation.op_id == first.operation.op_id
    retry = begin(store, key="a")
    assert isinstance(retry, SameOperation) and retry.operation.op_id == first.operation.op_id
    different = begin(store, key="a", target="elsewhere")
    assert isinstance(different, Conflict) and different.operation.op_id == first.operation.op_id, "G4: another request under one key"
    assert isinstance(begin(store, domain="e", key="a"), Begun), "another domain is independent"


def test_a_retry_after_the_operation_finished_is_the_same_operation(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    first = begin(store, key="a")
    store.finish(first.operation.op_id, "aborted", "open")
    assert isinstance(begin(store, key="a"), SameOperation)
    assert isinstance(begin(store, key="a", target="x"), Conflict)
    assert isinstance(begin(store, key="b"), Begun)


def test_the_database_itself_allows_one_unfinished_operation_per_domain(tmp_path: Path) -> None:
    gates, store = make(tmp_path)
    begin(store)
    with sqlite3.connect(gates.path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO transition_op (op_id, domain, kind, idempotency_key, request_hash, config_revision, state, "
            "created, updated, seq) VALUES ('x', 'd', 'park', 'other', 'h', 1, 'reconciling', 0, 0, 99)")


def test_the_request_hash_ignores_what_a_park_or_wake_reads_from_the_engine() -> None:
    assert request_hash("park", "a", None) == request_hash("park", "b", None)
    assert request_hash("wake", "a", None) == request_hash("wake", None, None)
    assert request_hash("select", "a", None) != request_hash("select", "b", None)
    assert request_hash("select", "a", None) != request_hash("select", "a", "op-1")


# -- write-ahead ---------------------------------------------------------------------------------------


def test_the_intent_is_committed_before_the_outcome_exists(tmp_path: Path) -> None:
    gates, store = make(tmp_path)
    op = begin(store).operation
    store.write_intent(op.op_id, intent(op.op_id, "release"))
    # Another connection sees the intent with no outcome: the intent is its own transaction.
    other = SqliteOperationStore(GateStore(gates.path))
    seen = other.get(op.op_id)
    assert seen.step == "release" and seen.step_intent.effect_key == f"{op.op_id}:release" and seen.step_outcome is None
    done = store.write_outcome(op.op_id, "release", EffectResult(outcome="success", evidence_ref="e", attested=True))
    assert (done.step_outcome, done.evidence_ref, done.stock) == ("success", "e", "released")


def test_the_release_disposition_follows_the_attestation(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    for domain, attested, stock in (("a", True, "released"), ("b", False, "observed_not_resident")):
        op = begin(store, domain=domain).operation
        store.write_intent(op.op_id, intent(op.op_id, "release"))
        assert store.write_outcome(op.op_id, "release", EffectResult(outcome="success", attested=attested)).stock == stock
    op = begin(store, domain="c").operation
    store.write_intent(op.op_id, intent(op.op_id, "release"))
    assert store.write_outcome(op.op_id, "release", EffectResult(outcome="partial", attested=True)).stock is None


def test_an_intent_for_a_finished_operation_is_refused(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    op = begin(store).operation
    store.finish(op.op_id, "aborted", "open")
    with pytest.raises(StaleOutcome):
        store.write_intent(op.op_id, intent(op.op_id, "release"))


# -- stale outcomes (decision G10) ---------------------------------------------------------------------


def test_a_stale_outcome_is_ignored_and_never_becomes_a_step_outcome(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _, store = make(tmp_path)
    op = begin(store).operation
    assert store.write_outcome(op.op_id, "release", EffectResult(outcome="success")).step_outcome is None, "no intent yet"
    store.write_intent(op.op_id, intent(op.op_id, "release"))
    assert store.write_outcome(op.op_id, "load", EffectResult(outcome="success")).step_outcome is None, "another step"
    assert store.write_outcome(op.op_id, "release", EffectResult(outcome="success", incarnation="i2")).step_outcome is None, "another incarnation"
    assert "ignored" in capsys.readouterr().out
    assert store.write_outcome(op.op_id, "release", EffectResult(outcome="success", incarnation="i1")).step_outcome == "success"
    late = store.write_outcome(op.op_id, "release", EffectResult(outcome="partial"))
    assert late.step_outcome == "success", "a settled step does not change"
    store.finish(op.op_id, "aborted", "open")
    assert store.write_outcome(op.op_id, "release", EffectResult(outcome="unknown")).step_outcome == "success", "finished"


def test_an_unknown_outcome_can_be_resolved_once_by_a_later_one(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    op = begin(store).operation
    store.write_intent(op.op_id, intent(op.op_id, "load", None))
    store.write_outcome(op.op_id, "load", EffectResult(outcome="unknown"))
    store.finish(op.op_id, "reconciling", "reconciling")
    assert store.write_outcome(op.op_id, "load", EffectResult(outcome="success")).step_outcome == "success"
    assert store.write_outcome(op.op_id, "load", EffectResult(outcome="partial")).step_outcome == "success"


def test_an_unknown_outcome_never_finishes_the_operation_and_reconciling_needs_one(tmp_path: Path) -> None:
    _, store = make(tmp_path)
    op = begin(store).operation
    with pytest.raises(StaleOutcome):
        store.finish(op.op_id, "reconciling", "reconciling")  # nothing unknown to reconcile
    store.write_intent(op.op_id, intent(op.op_id, "release"))
    store.write_outcome(op.op_id, "release", EffectResult(outcome="unknown"))
    for state in ("succeeded", "aborted", "failed_closed"):
        with pytest.raises(StaleOutcome):
            store.finish(op.op_id, state, GATE_FOR_STATE[state])  # type: ignore[arg-type]
    held = store.finish(op.op_id, "reconciling", "reconciling")
    assert held.state == "reconciling"


def test_finish_refuses_a_gate_the_state_does_not_leave_and_a_second_finish(tmp_path: Path) -> None:
    gates, store = make(tmp_path)
    op = begin(store).operation
    for state, gate in (("succeeded", "closed"), ("aborted", "failed_closed"), ("failed_closed", "open")):
        with pytest.raises(ValueError):
            store.finish(op.op_id, state, gate)  # type: ignore[arg-type]
    assert gates.state("d") == "closed"
    done = store.finish(op.op_id, "failed_closed", "failed_closed")
    assert done.state in FINISHED_STATES and gates.state("d") == "failed_closed"
    with pytest.raises(StaleOutcome):
        store.finish(op.op_id, "succeeded", "open")


def test_a_failed_closed_gate_accepts_a_new_operation_and_a_reconciling_one_does_not(tmp_path: Path) -> None:
    gates, store = make(tmp_path)
    first = begin(store, key="a").operation
    store.finish(first.op_id, "failed_closed", "failed_closed")
    second = begin(store, key="b", kind="park", target=None)
    assert isinstance(second, Begun) and gates.state("d") == "closed"
    store.write_intent(second.operation.op_id, intent(second.operation.op_id, "release"))
    store.write_outcome(second.operation.op_id, "release", EffectResult(outcome="unknown"))
    store.finish(second.operation.op_id, "reconciling", "reconciling")
    assert isinstance(begin(store, key="c"), Conflict)
    resumed = store.resume(second.operation.op_id)
    assert resumed.state == "running" and gates.state("d") == "closed"
    with pytest.raises(StaleOutcome):
        store.resume(second.operation.op_id)


def test_the_gate_cache_and_the_parked_set_follow_the_finished_operations(tmp_path: Path) -> None:
    gates, store = make(tmp_path)
    park = begin(store, kind="park", key="p", target=None).operation
    store.finish(park.op_id, "succeeded", "open")
    assert gates.parked("d") == park.op_id and gates.state("d") == "open"
    reloaded, _ = make(tmp_path)
    assert reloaded.parked("d") == park.op_id, "parked survives a restart"
    wake = begin(store, kind="wake", key="w").operation
    store.finish(wake.op_id, "aborted", "open")
    assert gates.parked("d") == park.op_id, "an aborted wake does not unpark"
    wake2 = begin(store, kind="wake", key="w2").operation
    store.finish(wake2.op_id, "succeeded", "open")
    assert gates.parked("d") is None
    reloaded, _ = make(tmp_path)
    assert reloaded.parked("d") is None
    assert store.last_target("d") == "t"


def test_an_unreadable_table_is_reconciling_never_open(tmp_path: Path) -> None:
    gates = GateStore(tmp_path / "missing" / "x" / "transition.db")
    (tmp_path / "missing").write_text("a file, not a directory")
    gates.load()
    assert gates.state("d") == "reconciling"


# -- the seeded state machine ---------------------------------------------------------------------------------


DOMAINS = ("d1", "d2")
STEPS = ("drain", "release", "load", "open")
OUTCOMES = ("success", "no_change", "partial", "unknown")


def assert_invariants(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        ops = connection.execute(
            "SELECT op_id, domain, state, step, step_intent, step_outcome, seq FROM transition_op ORDER BY seq").fetchall()
        gate = {domain: (state, op_id) for domain, state, op_id in connection.execute("SELECT domain, state, op_id FROM transition_gate")}
    finally:
        connection.close()
    for domain in DOMAINS:
        mine = [row for row in ops if row[1] == domain]
        unfinished = [row for row in mine if row[2] not in FINISHED_STATES]
        assert len(unfinished) <= 1, "at most one unfinished operation per domain"
        state = gate.get(domain, ("open", None))[0]
        if unfinished:
            row = unfinished[0]
            assert state == ("closed" if row[2] == "running" else "reconciling"), "the gate is held while an operation is unfinished"
            if row[2] == "reconciling":
                assert row[5] == "unknown", "reconciling means an unknown step"
        else:
            assert state in ("open", "failed_closed"), "no unfinished operation: open, or failed_closed after a failure"
            if mine:
                assert state == {"succeeded": "open", "aborted": "open", "failed_closed": "failed_closed"}[mine[-1][2]]
    for op_id, _domain, state, step, step_intent, step_outcome, _seq in ops:
        if step_outcome is not None:
            assert step_intent is not None and json.loads(step_intent)["step"] == step, "an outcome always has an earlier intent"
        if state in FINISHED_STATES:
            assert step_outcome != "unknown", "an unknown outcome is never finished unresolved"


@pytest.mark.parametrize("seed", range(12))
def test_the_operation_record_keeps_its_invariants_under_random_sequences(tmp_path: Path, seed: int) -> None:
    rng = random.Random(seed)
    gates, store = make(tmp_path)
    ids: list[str] = []
    keys: dict[tuple[str, str], tuple[str, str]] = {}   # (domain, key) -> (op_id, request hash)
    for _ in range(400):
        action = rng.choice(["begin"] * 5 + ["intent"] * 4 + ["outcome"] * 5 + ["finish"] * 4 + ["resume", "restart"])
        domain = rng.choice(DOMAINS)
        if action == "begin":
            key = f"k{rng.randrange(5)}"
            kind = rng.choice(["select", "park", "wake"])
            target = rng.choice(["t1", "t2"]) if kind == "select" else None
            before = store.current(domain)
            result = begin(store, domain=domain, kind=kind, key=key, target=target)
            rhash = request_hash(kind, target, None)
            if (domain, key) in keys:
                known_id, known_hash = keys[(domain, key)]
                assert result.operation.op_id == known_id, "an old key always names its operation"
                assert isinstance(result, SameOperation if known_hash == rhash else Conflict)
            elif before is not None:
                assert isinstance(result, Conflict) and result.operation.op_id == before.op_id
            else:
                assert isinstance(result, Begun) and result.operation.state == "running"
                keys[(domain, key)] = (result.operation.op_id, rhash)
                ids.append(result.operation.op_id)
        elif action == "restart":
            gates, store = make(tmp_path)
        elif ids:
            op_id = rng.choice(ids)
            try:
                if action == "intent":
                    store.write_intent(op_id, intent(op_id, rng.choice(STEPS), rng.choice([None, "i1", "i2"])))
                elif action == "outcome":
                    store.write_outcome(op_id, rng.choice(STEPS), EffectResult(  # type: ignore[arg-type]
                        outcome=rng.choice(OUTCOMES), incarnation=rng.choice([None, "i1", "i2"]), attested=rng.random() < 0.5))
                elif action == "finish":
                    state = rng.choice(["succeeded", "aborted", "failed_closed", "reconciling"])
                    store.finish(op_id, state, rng.choice([GATE_FOR_STATE[state]] * 4 + ["open"]))  # type: ignore[arg-type]
                elif action == "resume":
                    store.resume(op_id)
            except (StaleOutcome, ValueError):
                pass
        assert_invariants(tmp_path / "transition.db")
        for d in DOMAINS:
            current = store.current(d)
            expected = "open" if current is None else ("closed" if current.state == "running" else "reconciling")
            if current is not None:
                assert gates.state(d) == expected, "the cache follows every committed gate write"
