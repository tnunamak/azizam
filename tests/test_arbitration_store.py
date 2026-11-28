"""Durable shadow ledger: idempotent acquire, settle by evidence, fencing, charge, units, reads and cross-process faults.

Invariant labels refer to docs/design/RESOURCE_ARBITRATION_FOUNDATION.md §4.7 (A1-A12) and §14 (B1-B3). Each
docstring names the label the test checks. Where the nearest label is a stretch, the docstring says so.
Every test uses a file under pytest's tmp_path; none touches data/arbitration.db.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

import gateway.arbitration.store as store_module
from gateway.arbitration.holdings import SettlementKind
from gateway.arbitration.report import build_report
from gateway.arbitration.store import (
    INVALID_EVIDENCE,
    ACQUIRED,
    ENDED_RECORDED,
    NOT_RELEASED,
    PAYLOAD_MISMATCH,
    RELEASED,
    REPLAYED,
    SCHEMA_VERSION,
    STALE_GENERATION,
    UNCHANGED,
    UNISSUED_GENERATION,
    UNIT_MISMATCH,
    UNKNOWN_OPERATION,
    ArbitrationStore,
    StoreSchemaError,
)
from gateway.arbitration.types import ResourceRef

ROOT = Path(__file__).resolve().parent.parent
POOL = ResourceRef("pool-a", "1")
OTHER_INCARNATION = ResourceRef("pool-a", "2")


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "arbitration.db"


@pytest.fixture
def store(db: Path):
    opened = ArbitrationStore(db)
    yield opened
    opened.close()


def _acquire(store: ArbitrationStore, key: str = "op-1", *, quantity: int = 8, generation: int | None = None,
             ref: ResourceRef = POOL, unit: str = "mib"):
    """Acquire at the current generation. The first write to a resource takes over, as a new executor would."""
    if generation is None:
        generation = store.generation(ref.id) or store.take_over(ref.id)
    return store.acquire(op_key=key, ref=ref, quantity=quantity, unit=unit, generation=generation)


def _state(store: ArbitrationStore, key: str) -> str:
    operation = store.operation(key)
    assert operation is not None, f"operation {key!r} should exist"
    return operation.state


def _release(store: ArbitrationStore, key: str = "op-1", *, ref: ResourceRef = POOL,
             kind: SettlementKind = SettlementKind.STOCK_RELEASED) -> str:
    return store.settle(op_key=key, ref=ref, kind=kind, source="test")


# --- schema, WAL and idempotent acquire ---------------------------------------------------------

def test_store_opens_in_wal_mode_with_its_schema_version(db: Path, store: ArbitrationStore) -> None:
    """A1 (each write is one transaction, which needs WAL and a checked file) and §16 criterion 1: WAL mode and the schema version on open."""
    raw = sqlite3.connect(db)
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        raw.close()
    assert store.schema_version() == SCHEMA_VERSION


def test_a_store_with_another_schema_version_is_refused_on_open(db: Path) -> None:
    """A1, nearest label: a file this code cannot read is refused on open, so no write runs against an unknown layout (§16 criterion 1)."""
    raw = sqlite3.connect(db)
    raw.execute(f"PRAGMA user_version={SCHEMA_VERSION + 41}")
    raw.commit()
    raw.close()
    with pytest.raises(StoreSchemaError):
        ArbitrationStore(db)


def test_acquire_records_the_reservation_in_the_same_write(store: ArbitrationStore) -> None:
    """A1: the intent and the reservation are one transaction, so a committed row is never missing."""
    acquired = _acquire(store)
    assert acquired.status == ACQUIRED
    assert acquired.operation is not None and acquired.operation.state == "committed"
    assert store.charge(POOL) == 8


def test_same_key_and_same_payload_returns_the_first_result(store: ArbitrationStore) -> None:
    """A11 (the idempotency key names the operation): a replay of the same key and payload returns the stored operation and writes nothing new."""
    first = _acquire(store)
    replay = _acquire(store)
    assert replay.status == REPLAYED
    assert replay.operation == first.operation
    assert store.charge(POOL) == 8


def test_same_key_with_a_different_payload_is_refused(store: ArbitrationStore) -> None:
    """A11: one key names one ask. A different quantity under the same key is refused, and nothing is written."""
    _acquire(store, quantity=8)
    refused = _acquire(store, quantity=16)
    assert refused.status == PAYLOAD_MISMATCH
    assert store.charge(POOL) == 8


# --- units --------------------------------------------------------------------------------------

def test_an_acquire_in_another_unit_is_refused_while_an_unsettled_operation_holds_the_incarnation(store: ArbitrationStore) -> None:
    """§16 criterion 5 with A10 (one stock, one unit): two units on one incarnation at once are refused, and the charge stays in one unit."""
    _acquire(store, "in-mib", quantity=8, unit="mib")
    refused = _acquire(store, "in-token", quantity=1, unit="token")
    assert refused.status == UNIT_MISMATCH
    assert refused.operation is None
    assert store.operation("in-token") is None
    assert store.charge(POOL) == 8


def test_two_different_units_at_once_are_refused_in_either_order(store: ArbitrationStore) -> None:
    """§16 criterion 5 with A10: the first unit to commit decides the incarnation's unit, whichever it is."""
    _acquire(store, "token-first", quantity=1, unit="token")
    assert _acquire(store, "mib-second", quantity=8, unit="mib").status == UNIT_MISMATCH
    assert store.open_operations() and [op.op_key for op in store.open_operations()] == ["token-first"]


def test_a_settled_operation_no_longer_fixes_the_unit_and_another_incarnation_is_free(store: ArbitrationStore) -> None:
    """§16 criterion 5 with A10: only unsettled operations count. After a settlement the unit is free, and a new incarnation has its own unit."""
    _acquire(store, "first", quantity=1, unit="token")
    assert _release(store, "first") == RELEASED
    assert _acquire(store, "second", quantity=8, unit="mib").status == ACQUIRED
    assert _acquire(store, "other-incarnation", quantity=1, unit="token", ref=OTHER_INCARNATION).status == ACQUIRED


# --- fencing ------------------------------------------------------------------------------------

def test_acquire_with_a_stale_generation_is_refused_and_writes_nothing(store: ArbitrationStore) -> None:
    """A4: after a takeover, the old generation can no longer write. A refused acquire leaves no row."""
    assert store.take_over("pool-a") == 1
    assert store.take_over("pool-a") == 2
    refused = _acquire(store, "late", generation=1)
    assert refused.status == STALE_GENERATION
    assert refused.operation is None
    assert store.operation("late") is None


def test_acquire_with_a_generation_never_issued_is_refused(store: ArbitrationStore) -> None:
    """A4: only a takeover issues a generation. A write that claims a newer one is refused."""
    store.take_over("pool-a")
    assert _acquire(store, "ahead", generation=2).status == UNISSUED_GENERATION
    assert store.operation("ahead") is None


# --- settlement by evidence ---------------------------------------------------------------------

def test_execution_ended_is_recorded_but_never_releases_stock(store: ArbitrationStore) -> None:
    """A10 and B1: an execution that ended is recorded on the operation. It does not release stock, so the charge stays."""
    _acquire(store)
    assert _release(store, kind=SettlementKind.EXECUTION_ENDED) == ENDED_RECORDED
    operation = store.operation("op-1")
    assert operation is not None and operation.state == "committed" and operation.execution_ended is True
    assert store.charge(POOL) == 8


def test_a_repeated_execution_ended_changes_nothing(store: ArbitrationStore) -> None:
    """A11 (evidence is counted once for its operation): a second EXECUTION_ENDED for the same key is a no-op."""
    _acquire(store)
    assert _release(store, kind=SettlementKind.EXECUTION_ENDED) == ENDED_RECORDED
    assert _release(store, kind=SettlementKind.EXECUTION_ENDED) == UNCHANGED
    assert store.charge(POOL) == 8


def test_stock_release_evidence_for_another_incarnation_never_releases(store: ArbitrationStore) -> None:
    """A11: evidence counts only for its own operation and scope. Another incarnation's evidence releases nothing."""
    _acquire(store)
    assert _release(store, ref=OTHER_INCARNATION) == NOT_RELEASED
    assert store.charge(POOL) == 8


def test_evidence_for_an_unknown_operation_changes_nothing(store: ArbitrationStore) -> None:
    """A11: evidence must name an operation the ledger has committed. Evidence for an unknown key is ignored."""
    assert _release(store, "never-committed") == UNKNOWN_OPERATION
    _acquire(store, "op-1")
    assert store.charge(POOL) == 8


def test_stock_release_settles_once_and_a_repeat_changes_nothing(store: ArbitrationStore) -> None:
    """A10 and B1: stock comes back on settlement. A second settle changes nothing."""
    _acquire(store)
    assert _release(store) == RELEASED
    assert store.charge(POOL) == 0
    assert _release(store) == UNCHANGED
    assert store.charge(POOL) == 0
    assert _state(store, "op-1") == "settled"


def test_no_evidence_means_no_release_after_a_takeover(store: ArbitrationStore) -> None:
    """A5 (time is never safety evidence) and §14 B3: a takeover and a long wait never release a holding by themselves."""
    _acquire(store)
    store.take_over("pool-a")
    store.take_over("pool-a")
    assert _state(store, "op-1") == "reconciling"
    assert store.charge(POOL) == 8


def test_out_of_order_settle_before_commit_then_after_commit(store: ArbitrationStore) -> None:
    """A11: evidence counts only after the commit of its operation's intent. A settle that arrives first is ignored."""
    assert _release(store) == UNKNOWN_OPERATION
    _acquire(store)
    assert _release(store) == RELEASED
    assert store.charge(POOL) == 0


# --- takeover and charge ------------------------------------------------------------------------

def test_takeover_raises_the_generation_and_keeps_old_operations_charged(store: ArbitrationStore) -> None:
    """A4: a takeover raises the fence in one write. Older open operations become reconciling and stay charged."""
    _acquire(store, "op-old", quantity=8, generation=store.take_over("pool-a"))
    assert store.take_over("pool-a") == 2
    assert store.generation("pool-a") == 2
    assert _state(store, "op-old") == "reconciling"
    assert store.charge(POOL) == 8


def test_charge_counts_every_operation_that_is_not_settled(store: ArbitrationStore) -> None:
    """A10: capacity comes back only on settlement. Charge is every unsettled operation, whatever its state."""
    generation = store.take_over("pool-a")
    _acquire(store, "committed-now", quantity=4, generation=generation)
    _acquire(store, "to-settle", quantity=2, generation=generation)
    store.take_over("pool-a")  # both move to reconciling
    _acquire(store, "fresh", quantity=1, generation=2)
    assert store.charge(POOL) == 4 + 2 + 1
    _release(store, "to-settle")
    assert store.charge(POOL) == 4 + 1


def test_open_holdings_counts_unsettled_operations_on_one_incarnation(store: ArbitrationStore) -> None:
    """A10: the count is the number of holdings, and it ignores settled ones and other incarnations."""
    _acquire(store, "a", quantity=1, unit="token")
    _acquire(store, "b", quantity=1, unit="token", ref=OTHER_INCARNATION)
    _acquire(store, "c", quantity=1, unit="token")
    _release(store, "c")
    assert store.open_holdings(POOL) == 1


# --- commit failure, locking and setup failure ---------------------------------------------------

class _CommitFails:
    """Wraps the connection so the first COMMIT fails the way SQLITE_BUSY does: the transaction stays open."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.armed = False

    def execute(self, sql: str, *args: Any) -> Any:
        if self.armed and sql == "COMMIT":
            self.armed = False
            raise sqlite3.OperationalError("database is locked")
        return self._connection.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def test_a_failed_commit_rolls_back_and_leaves_the_store_usable(store: ArbitrationStore) -> None:
    """A1: a write is all or nothing. A failed COMMIT rolls back, so the next call is not refused as a nested transaction."""
    wrapper = _CommitFails(store._conn)
    store._conn = wrapper  # type: ignore[assignment]
    wrapper.armed = True
    with pytest.raises(sqlite3.OperationalError):
        _acquire(store, "lost")
    assert not wrapper._connection.in_transaction
    assert store.operation("lost") is None
    assert _acquire(store, "kept").status == ACQUIRED
    assert store.charge(POOL) == 8


def test_concurrent_calls_on_one_store_object_are_serialized(store: ArbitrationStore) -> None:
    """A1 (serializable outcomes): eight threads share one store object. Every acquire commits once, and the charge is their sum."""
    generation = store.take_over("pool-a")
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for step in range(20):
                key = f"t{index}-{step}"
                assert store.acquire(op_key=key, ref=POOL, quantity=1, unit="mib", generation=generation).status == ACQUIRED
                store.charge(POOL)
        except BaseException as error:  # noqa: BLE001 - reported on the main thread
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert store.charge(POOL) == 8 * 20


def test_a_store_whose_setup_fails_closes_its_connection(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A1: a store that cannot finish opening holds no connection open. The failure is raised and the connection is closed."""
    raw = sqlite3.connect(db)
    raw.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    raw.commit()
    raw.close()
    opened: list[_Spy] = []
    real_connect = sqlite3.connect

    class _Spy:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection
            self.closed = False

        def close(self) -> None:
            self.closed = True
            self._connection.close()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._connection, name)

    def spy_connect(*args: Any, **kwargs: Any) -> _Spy:
        spy = _Spy(real_connect(*args, **kwargs))
        opened.append(spy)
        return spy

    monkeypatch.setattr(store_module.sqlite3, "connect", spy_connect)
    with pytest.raises(StoreSchemaError):
        ArbitrationStore(db)
    assert len(opened) == 1 and opened[0].closed


# --- cross-process fault injection --------------------------------------------------------------

def _child(db: Path, body: str) -> subprocess.CompletedProcess:
    """Run `body` in a fresh interpreter against one store file. `store` is open and `POOL` is defined."""
    preamble = (
        "import sys\n"
        "from gateway.arbitration.store import ArbitrationStore, ACQUIRED\n"
        "from gateway.arbitration.holdings import SettlementKind\n"
        "from gateway.arbitration.types import ResourceRef\n"
        "POOL = ResourceRef('pool-a', '1')\n"
        "store = ArbitrationStore(sys.argv[1], busy_timeout_s=5.0)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    return subprocess.run([sys.executable, "-c", preamble + body, str(db)], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=60, check=False)


_OVERLAP_CHILD = r"""
import json, sys
from gateway.arbitration.store import ArbitrationStore
from gateway.arbitration.holdings import SettlementKind
from gateway.arbitration.types import ResourceRef
tag, path = sys.argv[1], sys.argv[2]
ref = ResourceRef('shared', '1')
store = ArbitrationStore(path, busy_timeout_s=5.0)
results = []
generation = previous = 0
for step in range(200):
    if step % 25 == 0:
        previous, generation = generation, store.take_over('shared')
    if step % 25 == 1 and previous:
        # This process took over after `previous`, so this acquire is stale by construction.
        key = f'{tag}-stale-{step}'
        probe = store.acquire(op_key=key, ref=ref, quantity=1, unit='token', generation=previous)
        results.append([key, previous, probe.status, None])
    key = f'{tag}-{step}'
    acquired = store.acquire(op_key=key, ref=ref, quantity=1, unit='token', generation=generation)
    settled = None
    if acquired.status == 'committed' and step % 5 != 0:
        settled = store.settle(op_key=key, ref=ref, kind=SettlementKind.STOCK_RELEASED, source='child')
    results.append([key, generation, acquired.status, settled])
store.close()
print(json.dumps(results))
"""


def test_two_processes_writing_at_once_lose_no_operation_and_fence_every_stale_acquire(db: Path) -> None:
    """A1 (no lost or half-written operation), A4 (a stale generation is refused) and A10 (charge is the unsettled rows)
    across two processes that run at the same time against one file.

    Both children are started before either is waited on. Each takes over every 25 steps, so the other's
    generation goes stale while it still writes.
    """
    # The file exists before the writers start, as it does in use. Two processes creating a new file at once race on the
    # switch to WAL, which SQLite can refuse at once ("database is locked") instead of waiting: that is not what this tests.
    ArbitrationStore(db).close()
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    children = [subprocess.Popen([sys.executable, "-c", _OVERLAP_CHILD, tag, str(db)], cwd=ROOT, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                for tag in ("a", "b")]
    outputs = []
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err
        outputs.append(json.loads(out))

    results = [row for output in outputs for row in output]
    assert {row[2] for row in results} <= {ACQUIRED, STALE_GENERATION}
    assert sum(1 for row in results if row[2] == STALE_GENERATION) >= 2 * 7  # each child's probe, plus any raced acquire

    connection = sqlite3.connect(db)
    try:
        rows = {key: (state, settle_source) for key, state, settle_source in connection.execute(
            "SELECT op_key, state, settle_source FROM operations")}
    finally:
        connection.close()

    for key, _generation, status, settled in results:
        if status == STALE_GENERATION:
            assert key not in rows, f"a refused acquire {key} must leave no row"
            continue
        assert status == ACQUIRED
        assert key in rows, f"committed operation {key} was lost"
        if settled is None:
            assert rows[key][0] != "settled", f"{key} was never settled, so it must stay charged"
        else:
            assert settled == RELEASED, f"{key} settled as {settled}: a double or lost settle"
            assert rows[key] == ("settled", "child")
    unsettled = sum(1 for state, _ in rows.values() if state != "settled")
    assert unsettled == sum(1 for row in results if row[2] == ACQUIRED and row[3] is None)

    survivor = ArbitrationStore(db)
    try:
        assert survivor.charge(ResourceRef("shared", "1")) == unsettled
        assert survivor.open_holdings(ResourceRef("shared", "1")) == unsettled
    finally:
        survivor.close()


def test_crash_after_commit_and_before_settle_leaves_the_operation_charged(db: Path) -> None:
    """A1 and A5: a process that dies after its commit and before its settle leaves the reservation charged. Nothing else may release it."""
    crashed = _child(db, "generation = store.take_over('pool-a')\n"
                         "assert store.acquire(op_key='op-1', ref=POOL, quantity=8, unit='mib', "
                         "generation=generation).status == ACQUIRED\n"
                         "import os; os._exit(0)\n")
    assert crashed.returncode == 0, crashed.stderr

    survivor = ArbitrationStore(db)
    try:
        assert _state(survivor, "op-1") == "committed"
        assert survivor.take_over("pool-a") == 2
        assert _state(survivor, "op-1") == "reconciling"
        assert survivor.charge(POOL) == 8
    finally:
        survivor.close()


def test_a_takeover_in_one_process_fences_a_stale_writer_in_another(db: Path) -> None:
    """A4 across processes: executor 1 commits, executor 2 takes over, and executor 1's late write is refused."""
    first = _child(db, "generation = store.take_over('pool-a')\n"
                       "assert store.acquire(op_key='op-1', ref=POOL, quantity=8, unit='mib', "
                       "generation=generation).status == ACQUIRED\n")
    assert first.returncode == 0, first.stderr
    second = _child(db, "assert store.take_over('pool-a') == 2\n"
                        "assert store.operation('op-1').state == 'reconciling'\n")
    assert second.returncode == 0, second.stderr
    late = _child(db, "print(store.acquire(op_key='op-late', ref=POOL, quantity=1, unit='mib', "
                      "generation=1).status)\n")
    assert late.returncode == 0, late.stderr
    assert late.stdout.strip() == STALE_GENERATION

    parent = ArbitrationStore(db)
    try:
        assert parent.charge(POOL) == 8
        assert parent.operation("op-late") is None
    finally:
        parent.close()


def test_duplicate_and_out_of_order_settles_across_processes(db: Path) -> None:
    """A11 and A10: a settle from another process releases once. A duplicate from a third changes nothing, and an unknown key is ignored."""
    assert _child(db, "store.take_over('pool-a')\n"
                      "assert store.acquire(op_key='op-1', ref=POOL, quantity=8, unit='mib', generation=1)"
                      ".status == ACQUIRED\n").returncode == 0
    settled = _child(db, "print(store.settle(op_key='op-1', ref=POOL, kind=SettlementKind.STOCK_RELEASED, "
                         "source='test'))\n")
    assert settled.stdout.strip() == RELEASED
    duplicate = _child(db, "print(store.settle(op_key='op-1', ref=POOL, kind=SettlementKind.STOCK_RELEASED, "
                           "source='test'))\n")
    assert duplicate.stdout.strip() == UNCHANGED
    early = _child(db, "print(store.settle(op_key='op-9', ref=POOL, kind=SettlementKind.STOCK_RELEASED, "
                       "source='test'))\n")
    assert early.stdout.strip() == UNKNOWN_OPERATION


# --- read interface -----------------------------------------------------------------------------

def test_report_prints_counts_and_open_operations_as_json(db: Path, store: ArbitrationStore) -> None:
    """A10 (the open charge is what the report must show) and §16 criterion 9: agreement counts and open operations, from one file, as JSON."""
    _acquire(store, "open-op", quantity=8)
    _acquire(store, "settled-op", quantity=3)
    _release(store, "settled-op")
    store.record_comparison(attempt_key="open-op", status="agree", old_admits=True, new_outcome="fits", detail="x")
    store.record_comparison(attempt_key="x", status="disagree", old_admits=False, new_outcome="fits", detail="y")
    store.close()

    completed = subprocess.run([sys.executable, "-m", "gateway.arbitration.report", "--db", str(db)], cwd=ROOT,
                               env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True,
                               timeout=60, check=False)
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["comparisons"] == {"agree": 1, "disagree": 1}
    assert [op["op_key"] for op in report["open_operations"]] == ["open-op"]
    assert report["schema_version"] == SCHEMA_VERSION


def test_report_refuses_to_create_a_missing_ledger(tmp_path: Path) -> None:
    """A1 (a read opens no write transaction) and §16 criterion 9: a missing file is reported, and nothing is created."""
    missing = tmp_path / "absent.db"
    completed = subprocess.run([sys.executable, "-m", "gateway.arbitration.report", "--db", str(missing)],
                               cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True,
                               text=True, timeout=60, check=False)
    assert completed.returncode == 1
    assert not missing.exists()


def test_build_report_on_a_missing_path_raises_and_creates_nothing(tmp_path: Path) -> None:
    """A1 (a read is not a write, so it cannot create a ledger) and §16 criterion 9: the parent folder is not created either."""
    missing = tmp_path / "nested" / "absent.db"
    with pytest.raises(FileNotFoundError):
        build_report(missing)
    assert not missing.exists() and not missing.parent.exists()


def test_build_report_on_a_file_that_is_not_a_ledger_fails_clearly_and_writes_nothing(tmp_path: Path) -> None:
    """A1 (a read leaves the bytes alone) and §16 criterion 9: a non-ledger SQLite file is refused with a clear message, unchanged."""
    other = tmp_path / "other.db"
    raw = sqlite3.connect(other)
    raw.execute("CREATE TABLE unrelated (x INTEGER)")
    raw.commit()
    raw.close()
    before = other.read_bytes()
    with pytest.raises(StoreSchemaError, match="not a shadow ledger"):
        build_report(other)
    assert other.read_bytes() == before
    assert not (tmp_path / "other.db-wal").exists()


def test_build_report_works_while_a_writer_holds_an_open_transaction(db: Path, store: ArbitrationStore) -> None:
    """A1 (a report reads committed state only): with a writer holding an open transaction, the report answers and omits the uncommitted row."""
    _acquire(store, "committed-op", quantity=8)
    writer = sqlite3.connect(db, isolation_level=None)
    try:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO operations (op_key, resource_id, incarnation, generation, quantity, unit, payload, "
                       "state, execution_ended, settle_source) VALUES ('uncommitted', 'pool-a', '1', 1, 1, 'mib', "
                       "'x', 'committed', 0, NULL)")
        report = build_report(db)
    finally:
        writer.execute("ROLLBACK")
        writer.close()
    assert [op["op_key"] for op in report["open_operations"]] == ["committed-op"]


@pytest.mark.parametrize("kind", [None, "execution_ended", "stock_released", 7])
def test_settle_fails_closed_on_evidence_that_is_not_a_settlement_kind(store: ArbitrationStore, kind: Any) -> None:
    """B1 and A10: only STOCK_RELEASED evidence releases. Any other value is refused and the charge stays."""
    _acquire(store)
    assert store.settle(op_key="op-1", ref=POOL, kind=kind, source="test") == INVALID_EVIDENCE
    assert store.charge(POOL) == 8
    assert _state(store, "op-1") != "settled"


def test_v2_migration_preserves_unknown_operations_and_pins_durable_ticket(tmp_path):
    """Start from the deployed v2 SQL shape, rather than today's schema creator."""
    from gateway.arbitration.store import read_snapshot
    path = tmp_path / "v2.db"
    conn = sqlite3.connect(path)
    conn.executescript('''
        PRAGMA user_version=2;
        CREATE TABLE resource_generations (resource_id TEXT PRIMARY KEY, generation INTEGER NOT NULL CHECK (generation >= 1));
        CREATE TABLE operations (
            op_key TEXT PRIMARY KEY, resource_id TEXT NOT NULL, incarnation TEXT NOT NULL,
            generation INTEGER NOT NULL CHECK (generation >= 1), quantity INTEGER NOT NULL CHECK (quantity >= 0),
            unit TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('committed','reconciling','settled')),
            execution_ended INTEGER NOT NULL DEFAULT 0 CHECK (execution_ended IN (0,1)), settle_source TEXT);
        CREATE TABLE comparisons (id INTEGER PRIMARY KEY AUTOINCREMENT, attempt_key TEXT NOT NULL,
            status TEXT NOT NULL, old_admits INTEGER, new_outcome TEXT, open_holdings INTEGER, detail TEXT NOT NULL);
        INSERT INTO resource_generations VALUES ('card/a', 1);
        INSERT INTO operations VALUES ('old', 'card/a', 'boot', 1, 1, 'token', 'old-payload', 'reconciling', 1, NULL);
    ''')
    conn.close()
    # The read-only dashboard must remain usable before the first writer migrates.
    snapshot = read_snapshot(path)
    assert snapshot.schema_version == 2 and snapshot.open_operations[0].op_key == "old"
    store = ArbitrationStore(path)
    try:
        assert store.charge(ResourceRef("card/a", "boot")) == 1
        args = dict(request_id="job", caller="caller", policy_revision="rev1", service_id="service",
                    resources=("card/a",), claim_envelope={"vram_bytes": 100}, deadline=1200.0)
        first = store.enqueue_ticket(**args)
        again = store.enqueue_ticket(**{**args, "deadline": 1800.0})
        assert first == again
        assert first.phase == "waiting" and first.deadline == 1200.0
        assert store.grant_ticket("job", worker_capacity=1, now=1000)
        # Withdrawal and send intent race on the same persisted state.
        assert store.transition_ticket("job", "withdrawn", now=1001)
        assert not store.transition_ticket("job", "sending", now=1001)
    finally:
        store.close()
    reopened = ArbitrationStore(path)
    try:
        ticket = reopened.ticket("job")
        assert ticket.phase == "withdrawn" and ticket.sequence == first.sequence
        assert ticket.resources == ("card/a",) and ticket.claim_envelope == {"vram_bytes": 100}
        assert reopened.charge(ResourceRef("card/a", "boot")) == 1
    finally:
        reopened.close()


def test_ticket_deadline_and_send_intent_are_atomic(tmp_path):
    store = ArbitrationStore(tmp_path / "ledger.db")
    try:
        for name in ("expired", "sent"):
            store.enqueue_ticket(request_id=name, caller="caller", policy_revision="rev", service_id="svc",
                                 resources=("card/a",), claim_envelope={}, deadline=100.0)
        assert not store.grant_ticket("expired", worker_capacity=1, now=100)
        assert store.ticket("expired").phase == "expired"
        assert store.grant_ticket("sent", worker_capacity=1, now=99)
        assert store.transition_ticket("sent", "sending", now=99)
        assert not store.transition_ticket("sent", "withdrawn", now=99)
        store.finish_ticket("sent", now=101, evidence=None)
        assert store.ticket("sent").phase == "unknown"
        store.finish_ticket("sent", now=102, evidence="upstream absence confirmed")
        assert store.ticket("sent").phase == "completed"
    finally:
        store.close()


def test_ticket_bundle_grants_slot_and_resources_together_and_retains_unknown(tmp_path):
    path = tmp_path / "ledger.db"
    store = ArbitrationStore(path)
    def enqueue(name, resources):
        return store.enqueue_ticket(request_id=name, caller="caller", policy_revision="rev", service_id="svc",
                                    resources=resources, claim_envelope={}, deadline=100)
    try:
        enqueue("a", ("card/a",))
        enqueue("b", ("card/a",))
        enqueue("c", ("card/b",))
        assert not store.grant_ticket("b", worker_capacity=1, now=1)  # original FIFO
        assert store.grant_ticket("a", worker_capacity=1, now=1)
        assert not store.grant_ticket("c", worker_capacity=1, now=1)
        assert store.charge(ResourceRef("physical/card/b", "1")) == 0  # no idle resource holding
        assert store.transition_ticket("a", "sending", now=2)
        store.finish_ticket("a", now=3, evidence=None)  # HTTP timeout does not prove stop
        assert store.ticket("a").phase == "unknown"
        assert store.grant_ticket("c", worker_capacity=1, now=3)  # execution slot returned
        assert not store.grant_ticket("b", worker_capacity=2, now=3)
    finally:
        store.close()
    store = ArbitrationStore(path)
    try:
        assert not store.grant_ticket("b", worker_capacity=2, now=4)
        store.finish_ticket("a", now=5, evidence="upstream absence observed")
        assert store.grant_ticket("b", worker_capacity=2, now=5)
        assert store.charge(ResourceRef("physical/card/a", "1")) == 1
        store.finish_ticket("a", now=6, evidence="same evidence replay")
        assert store.charge(ResourceRef("physical/card/a", "1")) == 1  # settle only original operation
    finally:
        store.close()


def test_startup_sweep_refunds_unsent_grants_and_only_worker_for_sent_work(tmp_path):
    store = ArbitrationStore(tmp_path / "ledger.db")
    try:
        for name, resource in (("unsent", "card/a"), ("sent", "card/b")):
            store.enqueue_ticket(request_id=name, caller="caller", policy_revision="rev", service_id="svc",
                                 resources=(resource,), claim_envelope={}, deadline=100)
            assert store.grant_ticket(name, worker_capacity=2, now=1)
        assert store.transition_ticket("sent", "sending", now=2)
        store.recover_startup(now=3)
        assert store.ticket("unsent").phase == "withdrawn"
        assert store.ticket("sent").phase == "unknown"
        assert store.charge(ResourceRef("physical/card/a", "1")) == 0
        assert store.charge(ResourceRef("physical/card/b", "1")) == 1
        assert store.charge(ResourceRef("@gateway/worker", "1")) == 0
        store.recover_startup(now=4)  # repeat is harmless
        assert store.charge(ResourceRef("physical/card/b", "1")) == 1
    finally:
        store.close()


def test_dormant_ticket_tables_preserve_the_old_v2_writer_and_reader_contract(tmp_path):
    path = tmp_path / "ledger.db"
    store = ArbitrationStore(path)
    store.close()
    # The rollback binary checks the exact version, and reads only its own tables.
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        old_tables = {"resource_generations", "operations", "comparisons"}
        actual = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert old_tables <= actual
        assert conn.execute("SELECT op_key FROM operations WHERE state!='settled'").fetchall() == []
        conn.execute("INSERT INTO resource_generations VALUES ('legacy-pool',1)")
        conn.commit()
    finally:
        conn.close()
    reopened = ArbitrationStore(path)
    try:
        assert reopened.generation("legacy-pool") == 1
    finally:
        reopened.close()


def test_operator_settle_tool_requires_evidence_and_only_settles_unknown(tmp_path, capsys):
    import runpy
    tool = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts" / "admission-ledger.py"))["main"]
    path = tmp_path / "ledger.db"
    store = ArbitrationStore(path)
    for name in ("unknown", "unsent"):
        store.enqueue_ticket(request_id=name, caller="caller", policy_revision="rev", service_id="svc",
                             resources=(name,), claim_envelope={}, deadline=100)
    assert store.grant_ticket("unknown", worker_capacity=1, now=1)
    assert store.transition_ticket("unknown", "sending", now=2)
    store.finish_ticket("unknown", now=3, evidence=None)
    store.close()
    assert tool(["--ledger", str(path), "unknown"]) == 0
    assert '"unknown"' in capsys.readouterr().out
    assert tool(["--ledger", str(path), "settle", "unsent", "--evidence", "observed absent"]) == 2
    with pytest.raises(SystemExit):
        tool(["--ledger", str(path), "settle", "unknown"])
    assert tool(["--ledger", str(path), "settle", "unknown", "--evidence", "queue and history verified absent"]) == 0
    store = ArbitrationStore(path)
    try:
        assert store.ticket("unknown").phase == "completed"
        assert store.ticket("unsent").phase == "waiting"
    finally:
        store.close()


def test_ticket_physical_stocks_do_not_collide_with_legacy_shadow_pool_ids(tmp_path):
    store = ArbitrationStore(tmp_path / "ledger.db")
    try:
        ref = ResourceRef("card/a", "1")
        generation = store.take_over(ref.id)
        assert store.acquire(op_key="legacy", ref=ref, quantity=1, unit="token", generation=generation).status == ACQUIRED
        store.enqueue_ticket(request_id="new", caller="caller", policy_revision="rev", service_id="svc",
                             resources=("card/a",), claim_envelope={}, deadline=100)
        assert store.grant_ticket("new", worker_capacity=1, now=1)
        assert store.charge(ref) == 1
        assert store.charge(ResourceRef("physical/card/a", "1")) == 1
    finally:
        store.close()
