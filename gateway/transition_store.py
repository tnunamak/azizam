"""The durable operation record and gate table for the transition gate (D2, step T1.2).

``SqliteOperationStore`` implements ``OperationStore`` (gateway/transition_ops.py) on
``data/transition.db``. The operation record is the authority; the ``transition_gate`` row of a
domain is written in the same transaction as the operation change that moves it, and the in-process
``GateStore`` cache is refreshed right after that commit, before the method returns.

- ``begin`` is a compare-and-set in one transaction: it inserts the operation only when the domain
  has no unfinished one (a unique partial index on ``domain``), and sets the gate to ``closed``.
  It answers ``Begun``, ``SameOperation`` (same idempotency key, same request) or ``Conflict``
  (another unfinished operation, or the same key with another request, decision G4).
- Write-ahead: ``write_intent`` and ``write_outcome`` are separate transactions. An outcome is
  written only when the operation is unfinished, the step matches the intent, the intent has no
  settled outcome yet and the incarnation matches; anything else is ignored and logged and never
  becomes a ``step_outcome`` (decision G10).
- ``finish`` sets the operation's final or holding state and the domain's gate in one transaction.

The module is engine-agnostic: domains, targets and handles are opaque strings. Every method is
blocking sqlite; an async caller runs it off the event loop.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Callable, Optional, Sequence

from gateway.transition_gate import GateStore
from gateway.transition_ops import (
    FINISHED_STATES, BeginResult, Begun, Conflict, EffectResult, GateState, Operation, OperationState, SameOperation,
    StepIntent, StepName, TransitionKind,
)

# The gate each operation state leaves behind. ``aborted`` and ``succeeded`` reopen; the others hold.
GATE_FOR_STATE: dict[str, str] = {
    "succeeded": "open", "aborted": "open", "failed_closed": "failed_closed", "reconciling": "reconciling",
}
_OUTCOMES = ("success", "no_change", "partial", "unknown")
_COLUMNS = ("op_id, domain, kind, idempotency_key, request_hash, target, incarnation, config_revision, state, step, "
            "step_intent, step_outcome, evidence_ref, snapshot_handle, compensates, stock, created, updated")


class StaleOutcome(Exception):
    """A write named an operation, step or incarnation that is no longer current."""


def request_hash(kind: str, target: Optional[str], compensates: Optional[str]) -> str:
    """The hash that tells a retry from a different request under one idempotency key (decision G4)."""
    # Only a select names its target. A park records the offering it released and a wake the one it loads, both read
    # from the domain, so neither is part of the request and a retry after the state moved is still the same request.
    return hashlib.sha256(json.dumps([kind, target if kind == "select" else None, compensates]).encode()).hexdigest()[:32]


def _intent_json(intent: Optional[StepIntent]) -> Optional[str]:
    if intent is None:
        return None
    return json.dumps({"step": intent.step, "effect_key": intent.effect_key,
                       "expected_incarnation": intent.expected_incarnation}, sort_keys=True)


def _intent_from(text: Optional[str]) -> Optional[StepIntent]:
    if not text:
        return None
    data = json.loads(text)
    return StepIntent(step=data["step"], effect_key=data["effect_key"],
                      expected_incarnation=data.get("expected_incarnation"))


def _operation(row: Sequence[Any]) -> Operation:
    (op_id, domain, kind, key, rhash, target, incarnation, revision, state, step, intent, outcome, evidence,
     handle, compensates, stock, created, updated) = row
    return Operation(op_id=op_id, domain=domain, kind=kind, idempotency_key=key, incarnation=incarnation,
                     config_revision=int(revision), state=state, step=step, target=target,
                     step_intent=_intent_from(intent), step_outcome=outcome, evidence_ref=evidence,
                     snapshot_handle=handle, compensates=compensates, created=float(created), updated=float(updated),
                     request_hash=rhash, stock=stock)


class SqliteOperationStore:
    def __init__(self, gates: GateStore, *, clock: Callable[[], float] = time.time,
                 new_id: Callable[[], str] = lambda: uuid.uuid4().hex) -> None:
        self.gates = gates
        self._clock = clock
        self._new_id = new_id

    # -- plumbing -----------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.gates.path, timeout=5, isolation_level=None)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _transaction(self, work: Callable[[sqlite3.Connection], Any]) -> Any:
        """Run ``work`` in one IMMEDIATE transaction. The tables exist: ``GateStore.load`` makes them."""
        self._ensure()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = work(connection)
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
            return result
        finally:
            connection.close()

    def _fetch(self, connection: sqlite3.Connection, where: str, args: tuple[Any, ...]) -> Optional[Operation]:
        row = connection.execute(f"SELECT {_COLUMNS} FROM transition_op WHERE {where}", args).fetchone()
        return _operation(row) if row is not None else None

    def _ensure(self) -> None:
        if not self.gates.readable():
            self.gates.load()

    def _write_gate(self, connection: sqlite3.Connection, domain: str, state: str, op_id: Optional[str]) -> None:
        connection.execute(
            "INSERT INTO transition_gate (domain, state, op_id, updated) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(domain) DO UPDATE SET state = excluded.state, op_id = excluded.op_id, updated = excluded.updated",
            (domain, state, op_id, self._clock()))

    # -- OperationStore -----------------------------------------------------

    def begin(self, *, domain: str, kind: TransitionKind, idempotency_key: str, target: Optional[str],
              incarnation: Optional[str], config_revision: int, compensates: Optional[str] = None) -> BeginResult:
        rhash = request_hash(kind, target, compensates)

        def work(connection: sqlite3.Connection) -> BeginResult:
            same_key = self._fetch(connection, "domain = ? AND idempotency_key = ?", (domain, idempotency_key))
            if same_key is not None:
                return SameOperation(same_key) if same_key.request_hash == rhash else Conflict(same_key)
            held = self._fetch(connection, "domain = ? AND state IN ('running', 'reconciling')", (domain,))
            if held is not None:
                return Conflict(held)
            now = self._clock()
            seq = connection.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM transition_op").fetchone()[0]
            op_id = self._new_id()
            connection.execute(
                "INSERT INTO transition_op (op_id, domain, kind, idempotency_key, request_hash, target, incarnation, "
                "config_revision, state, step, compensates, created, updated, seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', 'close', ?, ?, ?, ?)",
                (op_id, domain, kind, idempotency_key, rhash, target, incarnation, config_revision, compensates,
                 now, now, seq))
            self._write_gate(connection, domain, "closed", op_id)
            return Begun(self._fetch(connection, "op_id = ?", (op_id,)))  # type: ignore[arg-type]

        result = self._transaction(work)
        if isinstance(result, Begun):
            self.gates.note_write(domain, "closed")
        return result

    def write_intent(self, op_id: str, intent: StepIntent, held: Optional[EffectResult] = None) -> Operation:
        def work(connection: sqlite3.Connection) -> Operation:
            current = self._fetch(connection, "op_id = ?", (op_id,))
            if current is None or current.state != "running":
                raise StaleOutcome(f"operation {op_id} is not running; intent for {intent.step} refused")
            if held is not None and held.outcome != "unknown":
                raise ValueError("an intent is born holding only an unknown outcome")
            connection.execute(
                "UPDATE transition_op SET step = ?, step_intent = ?, step_outcome = ?, evidence_ref = ?, "
                "updated = ? WHERE op_id = ?",
                (intent.step, _intent_json(intent), held.outcome if held else None,
                 held.evidence_ref if held else None, self._clock(), op_id))
            return self._fetch(connection, "op_id = ?", (op_id,))  # type: ignore[return-value]

        return self._transaction(work)

    def write_outcome(self, op_id: str, step: StepName, result: EffectResult) -> Operation:
        """Record the outcome of ``step``, or ignore it (and log) when it is stale (decision G10).

        Stale means: the operation is finished; the step is not the one with the open intent; the step
        already has a settled outcome (anything but ``unknown`` on a ``reconciling`` operation); or the
        result came from another incarnation than the intent expected. The unchanged operation is returned.
        """
        if result.outcome not in _OUTCOMES:
            raise ValueError(f"unknown outcome {result.outcome!r}")

        def work(connection: sqlite3.Connection) -> Operation:
            current = self._fetch(connection, "op_id = ?", (op_id,))
            if current is None:
                raise KeyError(op_id)
            reason = self._stale_reason(current, step, result)
            if reason is not None:
                print(f"[transition-gate] stale outcome for {op_id} step {step} ignored: {reason}", flush=True)
                return current
            stock = current.stock
            if step == "release":
                stock = "released" if result.outcome == "success" and result.attested else (
                    "observed_not_resident" if result.outcome == "success" else None)
            connection.execute(
                "UPDATE transition_op SET step_outcome = ?, evidence_ref = ?, stock = ?, updated = ? WHERE op_id = ?",
                (result.outcome, result.evidence_ref, stock, self._clock(), op_id))
            return self._fetch(connection, "op_id = ?", (op_id,))  # type: ignore[return-value]

        return self._transaction(work)

    @staticmethod
    def _stale_reason(current: Operation, step: StepName, result: EffectResult) -> Optional[str]:
        if current.state in FINISHED_STATES:
            return f"operation is {current.state}"
        if current.step != step or current.step_intent is None:
            return f"current step is {current.step!r}, not {step!r}, or it has no intent"
        if current.step_outcome is not None and current.step_outcome != "unknown":
            return f"step already has the outcome {current.step_outcome!r}"
        expected = current.step_intent.expected_incarnation
        if expected is not None and result.incarnation is not None and result.incarnation != expected:
            return f"incarnation {result.incarnation!r} is not the expected {expected!r}"
        return None

    def set_snapshot_handle(self, op_id: str, handle: Optional[str]) -> Operation:
        def work(connection: sqlite3.Connection) -> Operation:
            connection.execute("UPDATE transition_op SET snapshot_handle = ?, updated = ? WHERE op_id = ?",
                               (handle, self._clock(), op_id))
            found = self._fetch(connection, "op_id = ?", (op_id,))
            if found is None:
                raise KeyError(op_id)
            return found

        return self._transaction(work)

    def finish(self, op_id: str, state: OperationState, gate: GateState) -> Operation:
        """Set the operation's final or holding state and the domain's gate in one transaction.

        ``succeeded`` and ``aborted`` open the gate, ``failed_closed`` and ``reconciling`` hold it, and
        a state with another gate is refused. A finished operation is never finished again. The open
        step's outcome is recorded here, so the lease the open step minted, the gate and the final state
        share one commit (decision G6). An operation whose step outcome is ``unknown`` is not finished until
        the outcome is resolved, and only such an operation is held ``reconciling``.
        """
        if state not in GATE_FOR_STATE or GATE_FOR_STATE[state] != gate:
            raise ValueError(f"operation state {state!r} cannot leave the gate {gate!r}")
        parked_by: Optional[str] = None
        clears_park = False

        def work(connection: sqlite3.Connection) -> Operation:
            nonlocal parked_by, clears_park
            current = self._fetch(connection, "op_id = ?", (op_id,))
            if current is None:
                raise KeyError(op_id)
            if current.state in FINISHED_STATES:
                raise StaleOutcome(f"operation {op_id} is already {current.state}")
            if state in FINISHED_STATES and current.step_outcome == "unknown":
                # An unknown outcome is settled by a query or the owner first; it never ends the operation.
                raise StaleOutcome(f"operation {op_id} has an unknown outcome at {current.step!r}; resolve it first")
            if state == "reconciling" and current.step_outcome != "unknown":
                raise StaleOutcome(f"operation {op_id} has no unknown step to reconcile")
            if state == "succeeded" and current.step == "open" and current.step_outcome is None:
                connection.execute("UPDATE transition_op SET step_outcome = 'success' WHERE op_id = ?", (op_id,))
            seq = connection.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM transition_op").fetchone()[0]
            connection.execute("UPDATE transition_op SET state = ?, updated = ?, seq = ? WHERE op_id = ?",
                               (state, self._clock(), seq, op_id))
            self._write_gate(connection, current.domain, gate, op_id)
            if state == "succeeded":
                if current.kind == "park":
                    parked_by = op_id
                else:
                    clears_park = True
            return self._fetch(connection, "op_id = ?", (op_id,))  # type: ignore[return-value]

        done = self._transaction(work)
        self.gates.note_write(done.domain, gate, parked_by, clears_park=clears_park)
        return done

    def resume(self, op_id: str) -> Operation:
        """Move a ``reconciling`` operation back to ``running`` with the gate ``closed``, after a held step
        was resolved to an outcome that lets the operation go on. Only a reconciling operation resumes."""
        def work(connection: sqlite3.Connection) -> Operation:
            current = self._fetch(connection, "op_id = ?", (op_id,))
            if current is None:
                raise KeyError(op_id)
            if current.state != "reconciling":
                raise StaleOutcome(f"operation {op_id} is {current.state}, not reconciling")
            connection.execute("UPDATE transition_op SET state = 'running', updated = ? WHERE op_id = ?",
                               (self._clock(), op_id))
            self._write_gate(connection, current.domain, "closed", op_id)
            return self._fetch(connection, "op_id = ?", (op_id,))  # type: ignore[return-value]

        done = self._transaction(work)
        self.gates.note_write(done.domain, "closed")
        return done

    def get(self, op_id: str) -> Optional[Operation]:
        return self._read("op_id = ?", (op_id,))

    def by_idempotency_key(self, domain: str, idempotency_key: str) -> Optional[Operation]:
        return self._read("domain = ? AND idempotency_key = ?", (domain, idempotency_key))

    def current(self, domain: str) -> Optional[Operation]:
        return self._read("domain = ? AND state IN ('running', 'reconciling')", (domain,))

    def _read(self, where: str, args: tuple[Any, ...]) -> Optional[Operation]:
        self._ensure()
        connection = self._connect()
        try:
            return self._fetch(connection, where, args)
        finally:
            connection.close()

    def unfinished(self) -> Sequence[Operation]:
        self._ensure()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM transition_op WHERE state IN ('running', 'reconciling') ORDER BY created, op_id"
            ).fetchall()
        finally:
            connection.close()
        return [_operation(row) for row in rows]

    def last_target(self, domain: str) -> Optional[str]:
        self._ensure()
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT target FROM transition_op WHERE domain = ? AND state = 'succeeded' "
                "AND target IS NOT NULL ORDER BY seq DESC LIMIT 1", (domain,)).fetchone()
        finally:
            connection.close()
        return row[0] if row else None

    def history(self, domain: str, limit: int = 50) -> Sequence[Operation]:
        """The domain's operations, newest first (diagnostics and tests)."""
        self._ensure()
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT {_COLUMNS} FROM transition_op WHERE domain = ? ORDER BY seq DESC LIMIT ?", (domain, limit)).fetchall()
        finally:
            connection.close()
        return [_operation(row) for row in rows]
