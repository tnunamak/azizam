"""Recording the envelope with the job row, and releasing it with the job row (S2.1b)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gateway import media_jobs
from gateway.definition_store import DefinitionStore, collect_garbage_in
from gateway.job_envelope import JobEnvelope, build_job_envelope, canonical_bytes

ORIGIN = "http://engine.test:1"


def make_envelope(tag: str = "a", *, template: bytes | None = b"graph-bytes") -> JobEnvelope:
    return build_job_envelope(
        source_kind="workflow",
        source_id=f"wf-{tag}",
        contract={"inputs": {"x": {"type": "string"}}, "tag": tag},
        binding={"map": {"x": [{"node": "1", "input": "x"}]}, "tag": tag},
        template=template,
        protocol_id="proto-x",
        adapter_id="adapter-x",
        interface_version=1,
        service_id="svc-1",
        origin=ORIGIN,
    )


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "media_jobs.db"
    media_jobs.init_db(path)
    return path


def rows(path: Path, table: str) -> list[tuple]:
    with sqlite3.connect(path) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()  # noqa: S608 - test table names


def create(db: Path, request_id: str, envelope: JobEnvelope | None, **kwargs) -> media_jobs.MediaJob:
    return media_jobs.create(
        media_jobs.MediaJob(intent="image.generate", request_id=request_id, **kwargs), envelope=envelope, path=db,
    )


def test_a_created_job_has_its_envelope_and_references(db: Path):
    envelope = make_envelope()
    job = create(db, "req-1", envelope)
    assert [r[0] for r in rows(db, "job_envelope")] == [job.job_id]
    assert len(rows(db, "definition_blob")) == 3
    assert {(r[1], r[2]) for r in rows(db, "definition_ref")} == {(job.job_id, "job")}
    stored = DefinitionStore(db).envelope(job.job_id)
    assert stored == envelope.envelope
    for sha in (stored.contract, stored.binding, stored.template):
        assert DefinitionStore(db).read_blob(sha)


def test_a_job_without_an_envelope_is_created_as_before(db: Path):
    job = create(db, "req-1", None)
    assert media_jobs.get(job.job_id, path=db) is not None
    assert rows(db, "job_envelope") == [] and rows(db, "definition_blob") == [] and rows(db, "definition_ref") == []


def test_jobs_that_pin_the_same_definitions_share_blobs(db: Path):
    first = create(db, "req-1", make_envelope())
    second = create(db, "req-2", make_envelope())
    assert len(rows(db, "definition_blob")) == 3
    assert len(rows(db, "definition_ref")) == 6
    assert {r[0] for r in rows(db, "job_envelope")} == {first.job_id, second.job_id}


def test_the_envelope_and_the_job_share_one_transaction(db: Path, monkeypatch: pytest.MonkeyPatch):
    real = media_jobs._record_envelope

    def record_then_fail(conn, job_id, envelope, **kwargs):
        real(conn, job_id, envelope, **kwargs)
        assert rows_in(conn, "job_envelope") == 1, "the envelope row is written before the commit"
        raise RuntimeError("the process stops before the commit")

    monkeypatch.setattr(media_jobs, "_record_envelope", record_then_fail)
    with pytest.raises(RuntimeError):
        create(db, "req-1", make_envelope())
    assert rows(db, "media_jobs") == []
    assert rows(db, "job_envelope") == [] and rows(db, "definition_blob") == [] and rows(db, "definition_ref") == []


def rows_in(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - test table names


def test_an_envelope_the_store_refuses_leaves_the_job_and_no_partial_rows(db: Path, caplog: pytest.LogCaptureFixture):
    # A reference to a blob that is not stored: the store refuses, and the savepoint undoes any blob it wrote.
    good = make_envelope()
    missing_blob = JobEnvelope(envelope=good.envelope, blobs=good.blobs[:1])  # the binding and template are absent
    job = create(db, "req-1", missing_blob)
    assert media_jobs.get(job.job_id, path=db) is not None, "the job is created as it always was"
    assert rows(db, "job_envelope") == [] and rows(db, "definition_ref") == []
    assert rows(db, "definition_blob") == [], "a refused envelope leaves none of its blobs"
    assert "the envelope was not recorded" in caplog.text
    # And the transaction is still usable: the next job records normally.
    later = create(db, "req-2", good)
    assert [r[0] for r in rows(db, "job_envelope")] == [later.job_id]


def test_a_required_envelope_that_cannot_be_written_stores_no_job(db: Path):
    good = make_envelope()
    missing_blob = JobEnvelope(envelope=good.envelope, blobs=good.blobs[:1])
    with pytest.raises(media_jobs.EnvelopeNotRecorded):
        media_jobs.create(media_jobs.MediaJob(intent="image.generate", request_id="req-1"),
                          envelope=missing_blob, require_envelope=True, path=db)
    with pytest.raises(media_jobs.EnvelopeNotRecorded):
        media_jobs.create(media_jobs.MediaJob(intent="image.generate", request_id="req-2"),
                          envelope=None, require_envelope=True, path=db)
    for table in ("media_jobs", "job_envelope", "definition_blob", "definition_ref"):
        assert rows(db, table) == [], table
    # The transaction is released: a later job records normally.
    later = media_jobs.create(media_jobs.MediaJob(intent="image.generate", request_id="req-3"),
                              envelope=good, require_envelope=True, path=db)
    assert [r[0] for r in rows(db, "job_envelope")] == [later.job_id]


def test_a_replay_records_nothing_new(db: Path):
    first = create(db, "req-1", make_envelope("a"))
    replay = media_jobs.create(
        media_jobs.MediaJob(intent="image.generate", request_id="req-1"), envelope=make_envelope("b"), path=db,
    )
    assert replay.job_id == first.job_id
    assert [r[0] for r in rows(db, "job_envelope")] == [first.job_id]
    assert DefinitionStore(db).envelope(first.job_id) == make_envelope("a").envelope
    assert len(rows(db, "definition_blob")) == 3


def test_deleting_a_job_releases_its_envelope_and_keeps_the_others(db: Path):
    done = create(db, "req-1", make_envelope("a"), status=media_jobs.STATUS_SUCCEEDED, progress=100)
    other = create(db, "req-2", make_envelope("b"), status=media_jobs.STATUS_SUCCEEDED, progress=100)
    queued = create(db, "req-3", make_envelope("c"))
    assert media_jobs.delete_succeeded([done.job_id, queued.job_id], path=db) == 1  # the queued job stays
    assert {r[0] for r in rows(db, "job_envelope")} == {other.job_id, queued.job_id}
    assert {r[1] for r in rows(db, "definition_ref")} == {other.job_id, queued.job_id}


def test_released_blobs_are_collected_after_the_grace_period_and_shared_ones_stay(db: Path):
    done = create(db, "req-1", make_envelope("a"), status=media_jobs.STATUS_SUCCEEDED, progress=100)
    keep = create(db, "req-2", make_envelope("b"))
    media_jobs.delete_succeeded([done.job_id], path=db)
    store = DefinitionStore(db)
    assert store.collect_garbage(grace_s=3600) == 0, "young blobs wait out the grace period"
    assert store.collect_garbage(grace_s=-1) == 2, "the released job's own contract and binding go; the shared graph stays"
    survivors = store.envelope(keep.job_id)
    assert survivors is not None
    assert all(store.read_blob(sha) for sha in (survivors.contract, survivors.binding, survivors.template))


def test_the_sweep_is_bounded_per_run(db: Path):
    store = DefinitionStore(db)
    for index in range(7):
        store.put_blob(f"orphan-{index}".encode())
    assert store.collect_garbage(grace_s=-1, limit=3) == 3
    assert store.collect_garbage(grace_s=-1, limit=3) == 3
    assert store.collect_garbage(grace_s=-1, limit=3) == 1
    assert store.collect_garbage(grace_s=-1, limit=3) == 0
    assert rows(db, "definition_blob") == []


def test_the_sweep_takes_the_oldest_first(db: Path):
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for created, body in ((30.0, b"c"), (10.0, b"a"), (20.0, b"b")):
            conn.execute("INSERT INTO definition_blob (sha, body, created) VALUES (?, ?, ?)", (body.hex(), body, created))
        assert collect_garbage_in(conn, cutoff=100.0, limit=2) == 2
        conn.commit()
    assert [r[2] for r in rows(db, "definition_blob")] == [30.0]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_the_sweep_refuses_a_bad_limit(db: Path, limit):
    with media_jobs.connection(db) as conn:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError):
            collect_garbage_in(conn, cutoff=100.0, limit=limit)
        conn.rollback()


def test_delete_on_a_database_without_the_definition_tables_still_works(tmp_path: Path):
    """A database that predates the tables has no envelopes to release; the delete must not fail on them."""
    path = tmp_path / "old.db"
    media_jobs.init_db(path)
    job = create(path, "req-1", None, status=media_jobs.STATUS_SUCCEEDED, progress=100)
    with sqlite3.connect(path) as conn:
        for table in ("definition_ref", "definition_blob", "job_envelope"):
            conn.execute(f"DROP TABLE {table}")  # noqa: S608 - test table names
    assert media_jobs.delete_succeeded([job.job_id], path=path) == 1


def test_canonical_bytes_refuses_what_json_cannot_hold():
    with pytest.raises(ValueError):
        canonical_bytes({"x": float("nan")})
    with pytest.raises(ValueError):
        canonical_bytes({"x": object()})


def test_the_maintenance_pass_runs_the_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The sweep rides the loop that already maintains the jobs database; no new scheduler."""
    import proxy

    db = tmp_path / "media_jobs.db"
    media_jobs.init_db(db)
    monkeypatch.setattr(media_jobs, "DB_PATH", db)
    monkeypatch.setattr(proxy, "_definition_stores", {})
    old = DefinitionStore(db, clock=lambda: 0.0)  # written long ago, so past the grace period
    for index in range(proxy._DEFINITION_GC_BATCH + 5):
        old.put_blob(f"orphan-{index}".encode())
    kept = create(db, "req-1", make_envelope())  # young and referenced

    proxy._collect_definition_garbage()  # one pass takes at most one batch
    assert len(rows(db, "definition_blob")) == 3 + 5
    proxy._collect_definition_garbage()
    assert len(rows(db, "definition_blob")) == 3
    assert DefinitionStore(db).envelope(kept.job_id) is not None

    ran: list[str] = []
    for name in ("_retry_prompt_deadline_repair", "_maintain_household_prompt_memory", "_maintain_request_history",
                 "_drain_prompt_purges", "_prune_orphaned_media_job_payloads"):
        monkeypatch.setattr(proxy, name, lambda: None)
    monkeypatch.setattr(proxy.admin_store, "list_users", lambda **_: [])
    monkeypatch.setattr(proxy.prompt_memory, "prune", lambda: None)
    monkeypatch.setattr(proxy, "_collect_definition_garbage", lambda: ran.append("sweep"))
    proxy._maintain_prompt_memory()
    assert ran == ["sweep"]


def test_a_busy_database_skips_the_sweep_and_other_errors_surface(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import proxy

    class Store:
        def __init__(self, error: Exception) -> None:
            self.error = error

        def collect_garbage(self, **_kwargs) -> int:
            raise self.error

    db = tmp_path / "media_jobs.db"
    monkeypatch.setattr(media_jobs, "DB_PATH", db)
    monkeypatch.setattr(proxy, "_definition_stores", {db: Store(sqlite3.OperationalError("database is locked"))})
    proxy._collect_definition_garbage()  # skipped quietly
    monkeypatch.setattr(proxy, "_definition_stores", {db: Store(sqlite3.OperationalError("disk I/O error"))})
    with pytest.raises(sqlite3.OperationalError):
        proxy._collect_definition_garbage()


def test_a_failure_that_ends_the_whole_transaction_raises_its_real_cause(db: Path, monkeypatch: pytest.MonkeyPatch):
    # Disk full or an I/O error: SQLite rolls back the whole transaction, savepoint included. The job row is gone,
    # so create must fail, with the real cause and not "no such savepoint".
    from gateway import definition_store

    def disk_fails(conn, *_args, **_kwargs):
        conn.rollback()
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(definition_store, "record_envelope_in", disk_fails)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        create(db, "req-1", make_envelope())
    assert rows(db, "media_jobs") == [] and rows(db, "job_envelope") == []
