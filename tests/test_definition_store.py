"""The content-addressed definition store: references, release and garbage collection (S2.1)."""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from gateway import definition_store, media_jobs
from gateway.definition_store import (
    DefinitionStore,
    DefinitionUnavailable,
    EnvelopeConflict,
    HolderIdError,
    clear_generated_in,
    collect_garbage_in,
    envelope_in,
    hold_in,
    put_blob_in,
    read_blob_in,
    record_envelope_in,
    release_holder_in,
    release_in,
)
from gateway.workflow_envelope import Envelope, parse_envelope

GENERATORS = {"gen-kept": "value"}


class Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> DefinitionStore:
    return DefinitionStore(tmp_path / "media_jobs.db", clock=clock)


def sha_of(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


def make_envelope(contract: str, binding: str, template: str | None = None, **generated) -> Envelope:
    definitions = {"contract": contract, "binding": binding, "protocol": {"id": "proto-x", "revision": 1}}
    if template is not None:
        definitions["template"] = template
    return parse_envelope(
        {
            "envelope_version": 1,
            "source": {"kind": "workflow", "id": "wf-1", "revision": 1},
            "definitions": definitions,
            "executor": {"adapter_id": "adapter-x", "interface_version": 1},
            "upstream": {"service_id": "svc-1", "origin_digest": "sha256:" + "d" * 64},
            "generated": generated,
            "results": {"bundle": None},
        },
        GENERATORS,
    )


def record(store: DefinitionStore, job_id: str, envelope: Envelope, **kwargs) -> None:
    store.record_envelope(job_id, envelope, generators=GENERATORS, **kwargs)


def rows(path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()  # noqa: S608 - test table names


def refs(path: Path) -> list[tuple]:
    return rows(path, "definition_ref")


def three_blobs(store: DefinitionStore) -> tuple[str, str, str]:
    return store.put_blob(b"contract"), store.put_blob(b"binding"), store.put_blob(b"template")


def test_schema_and_wal(store: DefinitionStore, tmp_path: Path):
    with sqlite3.connect(tmp_path / "media_jobs.db") as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"definition_blob", "definition_ref", "job_envelope"} <= tables
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_every_connection_overwrites_deleted_content(store: DefinitionStore, clock: Clock, tmp_path: Path):
    with media_jobs.connection(tmp_path / "media_jobs.db") as conn:
        assert conn.execute("PRAGMA secure_delete").fetchone()[0] == 1
    # And a delete really leaves the bytes out of the file.
    marker = b"unmistakable-definition-bytes" * 20
    sha = store.put_blob(marker)
    store.put_blob(b"neighbour" * 200)
    assert marker in (tmp_path / "media_jobs.db").read_bytes() or marker in (tmp_path / "media_jobs.db-wal").read_bytes()
    clock.now = 10**9
    assert store.collect_garbage(grace_s=0) == 2
    with media_jobs.connection(tmp_path / "media_jobs.db") as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert marker not in (tmp_path / "media_jobs.db").read_bytes()
    with pytest.raises(DefinitionUnavailable):
        store.read_blob(sha)


def test_a_failed_write_surfaces_its_own_error(store: DefinitionStore):
    class Boom(Exception):
        pass

    with pytest.raises(Boom):
        with store._write() as conn:  # noqa: SLF001
            conn.execute("COMMIT")  # the transaction is already over: a ROLLBACK would raise OperationalError
            raise Boom


def test_the_in_functions_share_the_callers_transaction(tmp_path: Path):
    path = tmp_path / "media_jobs.db"
    media_jobs.init_db(path)
    contract, binding = sha_of(b"contract"), sha_of(b"binding")
    envelope = make_envelope(contract, binding)
    with media_jobs.connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        record_envelope_in(conn, "job-1", envelope, generators=GENERATORS, now=1.0, blobs=[b"contract", b"binding"])
        assert envelope_in(conn, "job-1", GENERATORS) == envelope
        conn.rollback()  # the caller's transaction fails: nothing of the envelope remains
    assert rows(path, "job_envelope") == [] and refs(path) == [] and rows(path, "definition_blob") == []
    with media_jobs.connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert put_blob_in(conn, b"contract", now=1.0) == contract
        put_blob_in(conn, b"binding", now=1.0)
        record_envelope_in(conn, "job-1", envelope, generators=GENERATORS, now=1.0)
        hold_in(conn, "registry:1", [contract])
        assert read_blob_in(conn, contract) == b"contract"
        clear_generated_in(conn, "job-1")
        release_in(conn, "job-1")
        release_holder_in(conn, "registry:1")
        assert collect_garbage_in(conn, cutoff=100.0) == 2
        conn.commit()
    assert rows(path, "definition_blob") == [] and refs(path) == []


def test_reopening_keeps_the_data(tmp_path: Path, clock: Clock):
    path = tmp_path / "media_jobs.db"
    sha = DefinitionStore(path, clock=clock).put_blob(b"kept")
    assert DefinitionStore(path, clock=clock).read_blob(sha) == b"kept"


def test_put_blob_returns_the_sha_and_round_trips(store: DefinitionStore):
    sha = store.put_blob(b"hello")
    assert sha == sha_of(b"hello")
    assert store.read_blob(sha) == b"hello"
    assert store.put_blob(b"") == sha_of(b"")
    assert store.read_blob(sha_of(b"")) == b""


def test_put_blob_is_idempotent_and_refreshes_created(store: DefinitionStore, clock: Clock, tmp_path: Path):
    first = store.put_blob(b"same")
    clock.now = 5000.0
    assert store.put_blob(b"same") == first
    blobs = rows(tmp_path / "media_jobs.db", "definition_blob")
    assert len(blobs) == 1
    assert blobs[0][2] == 5000.0
    clock.now = 3000.0  # a clock that went back never makes a blob older
    store.put_blob(b"same")
    assert rows(tmp_path / "media_jobs.db", "definition_blob")[0][2] == 5000.0


def test_blob_put_again_after_the_grace_period_survives_a_sweep(store: DefinitionStore, clock: Clock):
    contract, binding, _ = three_blobs(store)
    envelope = make_envelope(contract, binding)
    record(store, "job-1", envelope)
    store.release("job-1")
    clock.now += 4000  # past the grace period: both blobs are now collectable
    contract, binding = store.put_blob(b"contract"), store.put_blob(b"binding")  # put again: young again
    assert store.collect_garbage(grace_s=3600) == 1  # only the template, which nobody put again
    record(store, "job-2", envelope)  # succeeds
    assert store.envelope("job-2", GENERATORS) == envelope


def test_blob_put_again_without_the_refresh_would_have_been_swept(store: DefinitionStore, clock: Clock):
    # The control for the test above: with no second put, the same sweep removes the blob and the record fails.
    contract, binding, _ = three_blobs(store)
    envelope = make_envelope(contract, binding)
    clock.now += 4000
    assert store.collect_garbage(grace_s=3600) == 3
    with pytest.raises(DefinitionUnavailable):
        record(store, "job-2", envelope)


def test_record_envelope_can_write_its_blobs_in_the_same_transaction(store: DefinitionStore, clock: Clock, tmp_path: Path):
    envelope = make_envelope(sha_of(b"contract"), sha_of(b"binding"))
    record(store, "job-1", envelope, blobs=[b"contract", b"binding"])
    assert store.read_blob(sha_of(b"contract")) == b"contract"
    assert len(refs(tmp_path / "media_jobs.db")) == 2
    # Blobs that do not match the envelope's digests do not help, and nothing is left behind.
    with pytest.raises(DefinitionUnavailable):
        record(store, "job-2", make_envelope(sha_of(b"x"), sha_of(b"y")), blobs=[b"other"])
    assert store.envelope("job-2", GENERATORS) is None
    assert len(refs(tmp_path / "media_jobs.db")) == 2
    # A record that carries a blob refreshes it, even when the blob is old.
    clock.now += 4000
    record(store, "job-3", envelope, blobs=[b"contract"])
    created = {sha: made for sha, _, made in rows(tmp_path / "media_jobs.db", "definition_blob")}
    assert created[sha_of(b"contract")] == 5000.0


def test_read_missing_blob_raises(store: DefinitionStore):
    with pytest.raises(DefinitionUnavailable):
        store.read_blob(sha_of(b"absent"))


def test_record_envelope_writes_envelope_and_one_ref_per_sha(store: DefinitionStore, tmp_path: Path):
    contract, binding, template = three_blobs(store)
    envelope = make_envelope(contract, binding, template, n={"generator": "gen-kept", "value": 7})
    record(store, "job-1", envelope)
    assert store.envelope("job-1", GENERATORS) == envelope
    assert refs(tmp_path / "media_jobs.db") == sorted(
        [(contract, "job-1", "job"), (binding, "job-1", "job"), (template, "job-1", "job")]
    )


def test_envelope_of_unknown_job_is_none(store: DefinitionStore):
    assert store.envelope("nope") is None


def test_envelope_with_generated_values_needs_the_reviewed_map(store: DefinitionStore):
    contract, binding, _ = three_blobs(store)
    record(store, "job-1", make_envelope(contract, binding, n={"generator": "gen-kept", "value": 7}))
    with pytest.raises(DefinitionUnavailable, match="not a reviewed generator"):
        store.envelope("job-1")
    store.envelope("job-1", GENERATORS)  # the reviewed map reads it
    with pytest.raises(DefinitionUnavailable):
        store.envelope("job-1", {"gen-other": "value"})  # the generator map changed since the write


def test_damaged_stored_envelope_is_unavailable_not_a_value_error(store: DefinitionStore, tmp_path: Path):
    with sqlite3.connect(tmp_path / "media_jobs.db") as conn:
        conn.execute("INSERT INTO job_envelope (job_id, envelope_json, created) VALUES ('job-1', 'not json', 1)")
        conn.execute("INSERT INTO job_envelope (job_id, envelope_json, created) VALUES ('job-2', '{}', 1)")
    for job_id in ("job-1", "job-2"):
        with pytest.raises(DefinitionUnavailable):
            store.envelope(job_id)


def test_shared_blob_gets_one_ref_per_job(store: DefinitionStore, tmp_path: Path):
    contract, binding, _ = three_blobs(store)
    envelope = make_envelope(contract, binding)
    record(store, "job-1", envelope)
    record(store, "job-2", envelope)
    assert len(refs(tmp_path / "media_jobs.db")) == 4


def test_envelope_with_a_missing_blob_writes_neither_row(store: DefinitionStore, tmp_path: Path):
    contract = store.put_blob(b"contract")
    missing = sha_of(b"never stored")
    with pytest.raises(DefinitionUnavailable):
        record(store, "job-1", make_envelope(contract, missing))
    path = tmp_path / "media_jobs.db"
    assert rows(path, "job_envelope") == []
    assert refs(path) == []
    assert store.envelope("job-1") is None
    # The blob that did exist is untouched and nothing was left half-written for a retry.
    other = store.put_blob(b"binding")
    record(store, "job-1", make_envelope(contract, other))
    assert len(refs(path)) == 2


def test_missing_template_blob_also_writes_nothing(store: DefinitionStore, tmp_path: Path):
    contract, binding, _ = three_blobs(store)
    with pytest.raises(DefinitionUnavailable):
        record(store, "job-1", make_envelope(contract, binding, sha_of(b"no template")))
    assert rows(tmp_path / "media_jobs.db", "job_envelope") == []
    assert refs(tmp_path / "media_jobs.db") == []


def test_a_different_envelope_for_a_job_conflicts_and_changes_nothing(store: DefinitionStore, tmp_path: Path):
    contract, binding, template = three_blobs(store)
    first = make_envelope(contract, binding)
    record(store, "job-1", first)
    with pytest.raises(EnvelopeConflict, match="different envelope"):
        record(store, "job-1", make_envelope(contract, binding, template))
    assert not issubclass(EnvelopeConflict, ValueError)
    assert store.envelope("job-1") == first
    assert len(refs(tmp_path / "media_jobs.db")) == 2


def test_recording_the_identical_envelope_again_is_a_no_op(store: DefinitionStore, tmp_path: Path):
    contract, binding, _ = three_blobs(store)
    envelope = make_envelope(contract, binding, n={"generator": "gen-kept", "value": 7})
    record(store, "job-1", envelope)
    path = tmp_path / "media_jobs.db"
    before = (rows(path, "job_envelope"), refs(path), rows(path, "definition_blob"))
    record(store, "job-1", envelope)
    record(store, "job-1", parse_envelope(envelope.to_json(), GENERATORS))  # equal content, new object
    assert (rows(path, "job_envelope"), refs(path), rows(path, "definition_blob")) == before


def test_an_envelope_built_by_hand_with_an_unreviewed_generator_writes_nothing(store: DefinitionStore, tmp_path: Path):
    contract, binding, _ = three_blobs(store)
    good = make_envelope(contract, binding)
    forged = Envelope(
        **{**good.__dict__, "generated": {"n": {"generator": "unreviewed", "value": "caller secret"}}}
    )
    with pytest.raises(ValueError, match="not a reviewed generator"):
        record(store, "job-1", forged)
    # A value that breaks the persistence class is refused too.
    bad_value = Envelope(**{**good.__dict__, "generated": {"n": {"generator": "gen-kept", "value": [1]}}})
    with pytest.raises(ValueError, match="expected a string"):
        record(store, "job-1", bad_value)
    path = tmp_path / "media_jobs.db"
    assert rows(path, "job_envelope") == []
    assert refs(path) == []


def test_generators_is_a_required_keyword(store: DefinitionStore):
    contract, binding, _ = three_blobs(store)
    with pytest.raises(TypeError):
        store.record_envelope("job-1", make_envelope(contract, binding))  # type: ignore[call-arg]


def test_generated_values_cannot_be_changed_after_parsing():
    envelope = make_envelope(sha_of(b"a"), sha_of(b"b"), n={"generator": "gen-kept", "value": 1})
    with pytest.raises(TypeError):
        envelope.generated["n2"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        envelope.generated["n"]["value"] = 2  # type: ignore[index]
    # A directly built Envelope copies what it is given.
    source = {"n": {"generator": "gen-kept", "value": 1}}
    built = Envelope(**{**envelope.__dict__, "generated": source})
    source["n"]["value"] = 99
    source["x"] = {"generator": "gen-kept", "value": 1}
    assert built.generated == {"n": {"generator": "gen-kept", "value": 1}}
    with pytest.raises(TypeError):
        built.generated["n"]["value"] = 3  # type: ignore[index]


def test_hold_and_release_holder(store: DefinitionStore, tmp_path: Path):
    contract, binding, _ = three_blobs(store)
    store.hold("registry:7", [contract, binding, contract])
    assert refs(tmp_path / "media_jobs.db") == sorted([(contract, "registry:7", "holder"), (binding, "registry:7", "holder")])
    store.hold("registry:7", [contract])  # idempotent
    assert len(refs(tmp_path / "media_jobs.db")) == 2
    store.release_holder("registry:7")
    assert refs(tmp_path / "media_jobs.db") == []
    store.release_holder("registry:7")  # idempotent


def test_release_holder_cannot_strip_a_jobs_references(store: DefinitionStore, tmp_path: Path):
    contract, binding, _ = three_blobs(store)
    record(store, "job-1", make_envelope(contract, binding))
    path = tmp_path / "media_jobs.db"
    before = refs(path)
    with pytest.raises(HolderIdError):
        store.release_holder("job-1")
    with pytest.raises(HolderIdError):
        store.hold("job-1", [contract])
    assert refs(path) == before
    assert store.envelope("job-1", GENERATORS) is not None


def test_a_holder_id_cannot_become_a_job(store: DefinitionStore, tmp_path: Path):
    contract, binding, _ = three_blobs(store)
    store.hold("registry:1", [contract])
    with pytest.raises(HolderIdError):
        record(store, "registry:1", make_envelope(contract, binding))
    assert rows(tmp_path / "media_jobs.db", "job_envelope") == []
    assert refs(tmp_path / "media_jobs.db") == [(contract, "registry:1", "holder")]


def test_release_of_a_holder_id_touches_nothing(store: DefinitionStore, tmp_path: Path):
    contract, binding, _ = three_blobs(store)
    store.hold("registry:1", [contract])
    store.release("registry:1")  # not a job: touches nothing
    assert refs(tmp_path / "media_jobs.db") == [(contract, "registry:1", "holder")]


def test_hold_with_a_missing_blob_writes_nothing(store: DefinitionStore, tmp_path: Path):
    contract = store.put_blob(b"contract")
    with pytest.raises(DefinitionUnavailable):
        store.hold("registry:7", [contract, sha_of(b"missing")])
    assert refs(tmp_path / "media_jobs.db") == []


def test_release_deletes_envelope_and_refs_and_is_idempotent(store: DefinitionStore, tmp_path: Path):
    contract, binding, _ = three_blobs(store)
    envelope = make_envelope(contract, binding)
    record(store, "job-1", envelope)
    record(store, "job-2", envelope)
    store.release("job-1")
    path = tmp_path / "media_jobs.db"
    assert store.envelope("job-1") is None
    assert store.envelope("job-2") == envelope
    assert refs(path) == sorted([(contract, "job-2", "job"), (binding, "job-2", "job")])
    store.release("job-1")
    store.release("never-existed")
    assert len(refs(path)) == 2


def test_release_leaves_holder_refs(store: DefinitionStore, clock: Clock):
    contract, binding, _ = three_blobs(store)
    store.hold("registry:1", [contract, binding])
    record(store, "job-1", make_envelope(contract, binding))
    store.release("job-1")
    clock.now += 10_000
    assert store.collect_garbage(grace_s=60) == 1  # only the unreferenced third blob
    assert store.read_blob(contract) == b"contract"


def test_released_blob_is_collected_only_after_the_grace_period(store: DefinitionStore, clock: Clock):
    contract, binding, _ = three_blobs(store)
    record(store, "job-1", make_envelope(contract, binding))
    store.release("job-1")
    assert store.collect_garbage(grace_s=3600) == 0  # all three are young
    clock.now += 3601
    assert store.collect_garbage(grace_s=3600) == 3
    with pytest.raises(DefinitionUnavailable):
        store.read_blob(contract)


def test_gc_grace_boundary_and_default(store: DefinitionStore, clock: Clock):
    sha = store.put_blob(b"x")
    clock.now = 1000.0 + 3600
    assert store.collect_garbage() == 0  # exactly at the grace age: kept
    clock.now += 0.5
    assert store.collect_garbage() == 1
    with pytest.raises(DefinitionUnavailable):
        store.read_blob(sha)


def test_gc_never_deletes_a_referenced_blob(store: DefinitionStore, clock: Clock):
    contract, binding, template = three_blobs(store)
    record(store, "job-1", make_envelope(contract, binding))
    clock.now += 10 * 3600
    assert store.collect_garbage(grace_s=0) == 1  # the unreferenced template only
    assert store.read_blob(contract) == b"contract"
    assert store.read_blob(binding) == b"binding"
    with pytest.raises(DefinitionUnavailable):
        store.read_blob(template)
    assert store.collect_garbage(grace_s=0) == 0


def test_blob_written_just_before_its_envelope_commit_survives_gc(store: DefinitionStore, clock: Clock):
    contract = store.put_blob(b"contract")
    binding = store.put_blob(b"binding")
    clock.now += 600  # a sweep runs between the writes and the commit
    assert store.collect_garbage(grace_s=3600) == 0
    record(store, "job-1", make_envelope(contract, binding))
    assert store.envelope("job-1") is not None


def test_clear_generated_rewrites_the_envelope(store: DefinitionStore):
    contract, binding, template = three_blobs(store)
    record(store, "job-1", make_envelope(contract, binding, template, n={"generator": "gen-kept", "value": 99}))
    store.clear_generated("job-1")
    cleared = store.envelope("job-1", GENERATORS)
    assert cleared is not None
    assert cleared.generated == {"n": {"generator": "gen-kept", "erased": True}}, "the value goes; the entry says so"
    assert (cleared.contract, cleared.binding, cleared.template) == (contract, binding, template)
    store.clear_generated("job-1")  # idempotent
    store.clear_generated("no-such-job")


def test_clear_generated_keeps_refs(store: DefinitionStore, tmp_path: Path):
    contract, binding, _ = three_blobs(store)
    record(store, "job-1", make_envelope(contract, binding, n={"generator": "gen-kept", "value": 1}))
    store.clear_generated("job-1")
    assert len(refs(tmp_path / "media_jobs.db")) == 2


def test_concurrent_put_and_record_against_gc(tmp_path: Path):
    # The writer's blobs are young (created at 1000) and the sweep's grace period is 60 s, so the sweep must never take
    # them. The orphans were written long ago and nobody references them, so the sweep must take exactly those: a sweep
    # that does nothing fails this test.
    clock = Clock(0.0)
    store = DefinitionStore(tmp_path / "media_jobs.db", clock=clock)
    orphans = [store.put_blob(f"orphan-{i}".encode()) for i in range(25)]
    clock.now = 1000.0
    rounds = 40
    errors: list[BaseException] = []
    swept: list[int] = []
    stop = threading.Event()

    def writer() -> None:
        try:
            for i in range(rounds):
                contract = store.put_blob(f"contract-{i}".encode())
                binding = store.put_blob(f"binding-{i}".encode())
                record(store, f"job-{i}", make_envelope(contract, binding))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
        finally:
            stop.set()

    def collector() -> None:
        try:
            while not stop.is_set():
                swept.append(store.collect_garbage(grace_s=60))
            swept.append(store.collect_garbage(grace_s=60))
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=collector)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert sum(swept) == len(orphans)
    for sha in orphans:
        with pytest.raises(DefinitionUnavailable):
            store.read_blob(sha)
    for i in range(rounds):
        envelope = store.envelope(f"job-{i}", GENERATORS)
        assert envelope is not None
        assert store.read_blob(envelope.contract) == f"contract-{i}".encode()
        assert store.read_blob(envelope.binding) == f"binding-{i}".encode()


def test_concurrent_gc_cannot_dangle_a_reference(tmp_path: Path):
    # A zero grace period lets the sweep win races. Whatever the order, a recorded envelope never has a missing blob,
    # and a lost race surfaces as DefinitionUnavailable, never as a dangling reference.
    clock = Clock(1000.0)
    store = DefinitionStore(tmp_path / "media_jobs.db", clock=clock)
    rounds = 40
    recorded: list[int] = []
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer() -> None:
        try:
            for i in range(rounds):
                clock.now += 1
                contract = store.put_blob(f"contract-{i}".encode())
                binding = store.put_blob(f"binding-{i}".encode())
                try:
                    record(store, f"job-{i}", make_envelope(contract, binding))
                except DefinitionUnavailable:
                    continue
                recorded.append(i)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)
        finally:
            stop.set()

    def collector() -> None:
        try:
            while not stop.is_set():
                clock.now += 1
                store.collect_garbage(grace_s=0)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer), threading.Thread(target=collector)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    for i in recorded:
        envelope = store.envelope(f"job-{i}")
        assert envelope is not None
        assert store.read_blob(envelope.contract) == f"contract-{i}".encode()
        assert store.read_blob(envelope.binding) == f"binding-{i}".encode()


@pytest.mark.parametrize("damage", ["not json", "[]"])
def test_clearing_a_damaged_envelope_releases_it(store: DefinitionStore, tmp_path: Path, damage: str):
    path = tmp_path / "media_jobs.db"
    contract, binding, _ = three_blobs(store)
    record(store, "job-1", make_envelope(contract, binding, n={"generator": "gen-kept", "value": 7}))
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE job_envelope SET envelope_json = ? WHERE job_id = 'job-1'", (damage,))
    store.clear_generated("job-1")  # erasure must not fail on a damaged row
    assert rows(path, "job_envelope") == [] and refs(path) == []


WRITES = {
    "put_blob_in": lambda conn: put_blob_in(conn, b"new", now=5.0),
    "record_envelope_in": lambda conn: record_envelope_in(
        conn, "job-2", make_envelope(sha_of(b"contract"), sha_of(b"binding")), generators=GENERATORS, now=5.0
    ),
    "clear_generated_in": lambda conn: clear_generated_in(conn, "job-1"),
    "release_in": lambda conn: release_in(conn, "job-1"),
    "hold_in": lambda conn: hold_in(conn, "registry:2", [sha_of(b"template")]),
    "release_holder_in": lambda conn: release_holder_in(conn, "registry:1"),
    "collect_garbage_in": lambda conn: collect_garbage_in(conn, cutoff=10**9),
}


def _write_fixture(store: DefinitionStore, tmp_path: Path) -> tuple[Path, list]:
    path = tmp_path / "media_jobs.db"
    contract, binding, template = three_blobs(store)
    store.put_blob(b"orphan")
    record(store, "job-1", make_envelope(contract, binding, n={"generator": "gen-kept", "value": 7}))
    store.hold("registry:1", [template])
    return path, [rows(path, t) for t in ("definition_blob", "definition_ref", "job_envelope")]


@pytest.mark.parametrize("name", sorted(WRITES))
def test_every_write_rolls_back_with_the_callers_transaction(store: DefinitionStore, tmp_path: Path, name: str):
    path, before = _write_fixture(store, tmp_path)
    with media_jobs.connection(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        WRITES[name](conn)
        assert conn.in_transaction, f"{name} ended the caller's transaction"
        conn.rollback()
    assert [rows(path, t) for t in ("definition_blob", "definition_ref", "job_envelope")] == before


@pytest.mark.parametrize("name", sorted(set(WRITES) - {"put_blob_in"}))
def test_a_write_outside_a_transaction_is_refused(store: DefinitionStore, tmp_path: Path, name: str):
    path, before = _write_fixture(store, tmp_path)
    with media_jobs.connection(path) as conn:
        assert not conn.in_transaction
        with pytest.raises(RuntimeError, match="BEGIN IMMEDIATE"):
            WRITES[name](conn)
    assert [rows(path, t) for t in ("definition_blob", "definition_ref", "job_envelope")] == before


def test_clear_generated_in_has_one_definition():
    # A second definition of the same name silently replaces the first, so a changed contract would be dead code.
    tree = ast.parse(Path(definition_store.__file__).read_text(encoding="utf-8"))
    assert [node.name for node in tree.body if isinstance(node, ast.FunctionDef)].count("clear_generated_in") == 1


def test_clear_generated_in_erases_every_value_keeps_the_generator_and_returns_nothing(store: DefinitionStore, tmp_path: Path):
    contract, binding, _ = three_blobs(store)
    record(store, "job-1", make_envelope(contract, binding, n={"generator": "gen-kept", "value": 5}, m={"generator": "gen-kept", "value": 6}))
    with media_jobs.connection(tmp_path / "media_jobs.db") as conn:
        conn.execute("BEGIN IMMEDIATE")
        assert clear_generated_in(conn, "job-1") is None
        assert clear_generated_in(conn, "job-1") is None  # idempotent: an erased entry stays erased
        assert clear_generated_in(conn, "no-such-job") is None
        conn.commit()
    stored = json.loads(rows(tmp_path / "media_jobs.db", "job_envelope")[0][1])["generated"]
    assert stored == {"n": {"generator": "gen-kept", "erased": True}, "m": {"generator": "gen-kept", "erased": True}}
    assert store.envelope("job-1", GENERATORS).generated == {
        "n": {"generator": "gen-kept", "erased": True}, "m": {"generator": "gen-kept", "erased": True}}


@pytest.mark.parametrize("entry", [
    {"generator": "gen-kept", "erased": False},
    {"generator": "gen-kept", "erased": True, "value": 3},
    {"generator": "gen-kept", "erased": "yes"},
], ids=["false", "with-value", "not-a-bool"])
def test_an_erased_entry_must_say_true_and_hold_no_value(store: DefinitionStore, tmp_path: Path, entry):
    contract, binding, _ = three_blobs(store)
    record(store, "job-1", make_envelope(contract, binding, n={"generator": "gen-kept", "value": 5}))
    body = json.loads(rows(tmp_path / "media_jobs.db", "job_envelope")[0][1])
    body["generated"]["n"] = entry
    with sqlite3.connect(tmp_path / "media_jobs.db") as conn:
        conn.execute("UPDATE job_envelope SET envelope_json = ?", (json.dumps(body),))
    with pytest.raises(DefinitionUnavailable):
        store.envelope("job-1", GENERATORS)
