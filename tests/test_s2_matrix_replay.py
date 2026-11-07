"""S2 acceptance matrix, item 3: crash and replay of the job store (docs/design/CODE_TO_DATA_S2.md).

Each test names the matrix bullet it closes. A "crash" is a dedicated BaseException raised at one named point inside
``media_jobs.create`` (the connection is then closed, so SQLite rolls back whatever was not committed), never an
ordinary caught error. Row counts are read back through a second, fresh connection, as a restarted process would.
"""

from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

from gateway import media_jobs
from gateway.definition_store import DefinitionStore
from gateway.engine_protocol import jobs as protocol_jobs
from gateway.engine_protocol.adapter import ADAPTER_INTERFACE_VERSION
from gateway.engine_protocol.errors import ProtocolError
from gateway.engine_protocol.types import (CanonicalRequest, Connection, JobRef, JobState, JobStatus, Timeouts)
from gateway.job_envelope import JobEnvelope, build_job_envelope

ORIGIN = "http://engine.test:1"
WINDOW = media_jobs.REPLAY_WINDOW_S
NOW = 5_000_000


class Crash(BaseException):
    """The process stops here. Not an Exception, so no handler in the store can swallow it as a failure."""


def make_envelope(tag: str = "a") -> JobEnvelope:
    return build_job_envelope(
        source_kind="workflow",
        source_id="wf-replay",
        contract={"inputs": {"x": {"type": "string"}}, "tag": tag},
        binding={"map": {"x": [{"node": "1", "input": "x"}]}, "tag": tag},
        template=b"graph-bytes-" + tag.encode(),
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


def count(path: Path, table: str) -> int:
    # A new connection each time: what a process that restarted would see.
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 - test table names
    finally:
        conn.close()


def totals(path: Path) -> tuple[int, int, int, int]:
    jobs, envelopes, refs, blobs = (count(path, t) for t in ("media_jobs", "job_envelope", "definition_ref", "definition_blob"))
    return jobs, envelopes, refs, blobs


def snapshot(path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()  # noqa: S608 - test table names
                for t in ("media_jobs", "job_envelope", "definition_ref", "definition_blob")}
    finally:
        conn.close()


@pytest.fixture
def envelope_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every call of the envelope writer, by job id."""
    real = media_jobs._record_envelope
    calls: list[str] = []

    def counted(conn, job_id, envelope, *, require):
        calls.append(job_id)
        return real(conn, job_id, envelope, require=require)

    monkeypatch.setattr(media_jobs, "_record_envelope", counted)
    return calls


class _Connection:
    """A connection that forwards everything and lets a test act at ``commit`` and at ``BEGIN IMMEDIATE``."""

    def __init__(self, real: sqlite3.Connection, hooks: "_Hooks") -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_hooks", hooks)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._real, name, value)

    def execute(self, sql: str, *args: Any) -> Any:
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            self._hooks.on_begin()
        return self._real.execute(sql, *args)

    def commit(self) -> None:
        self._hooks.on_commit(self._real)


class _Hooks:
    def __init__(self) -> None:
        self.on_begin: Callable[[], None] = lambda: None
        self.on_commit: Callable[[sqlite3.Connection], None] = lambda real: real.commit()


@pytest.fixture
def hooks(monkeypatch: pytest.MonkeyPatch) -> _Hooks:
    owned = _Hooks()
    real_connect = media_jobs.connect
    monkeypatch.setattr(media_jobs, "connect", lambda *a, **kw: _Connection(real_connect(*a, **kw), owned))
    return owned


def job(request_id: str = "req-1", **kw: Any) -> media_jobs.MediaJob:
    kw.setdefault("intent", "image.generate")
    return media_jobs.MediaJob(request_id=request_id, **kw)


def make(db: Path, request_id: str = "req-1", envelope: JobEnvelope | None = None, **kw: Any) -> media_jobs.MediaJob:
    maker_from_request = kw.pop("maker_from_request", True)
    input_data = kw.pop("input_data", {"value": "A"})
    return media_jobs.create(job(request_id, **kw), input_data=input_data, envelope=envelope or make_envelope(),
                             path=db, maker_from_request=maker_from_request)


# --- 3.1: crash before and after the envelope commit ------------------------------------------------------------------

@pytest.mark.parametrize("cut", ["before_commit", "after_commit"])
def test_a_crash_around_the_envelope_commit_replays_to_one_job(db: Path, hooks: _Hooks, envelope_calls: list[str],
                                                               cut: str):
    """3.1: crash before and after the envelope commit; the job, its envelope and its pins are all there or all absent."""
    first, second = make_envelope("first"), make_envelope("second")

    def crash(real: sqlite3.Connection) -> None:
        if cut == "after_commit":
            real.commit()
        raise Crash()

    hooks.on_commit = crash
    with pytest.raises(Crash):
        make(db, envelope=first)
    hooks.on_commit = lambda real: real.commit()

    if cut == "before_commit":
        assert totals(db) == (0, 0, 0, 0), "a crash before the commit leaves no job, envelope, reference or blob"
    else:
        assert totals(db) == (1, 1, 3, 3), "a crash after the commit leaves the whole job"
    survivor = [row[0] for row in snapshot(db)["media_jobs"]]

    # The client never saw the reply, so it retries the same request with a different proposed envelope.
    retry = make(db, envelope=second)
    assert totals(db) == (1, 1, 3, 3)
    pinned = DefinitionStore(db).envelope(retry.job_id)
    if cut == "before_commit":
        assert len(envelope_calls) == 2, "nothing was accepted, so the retry records its own envelope"
        assert pinned == second.envelope
    else:
        assert retry.job_id == survivor[0], "the retry finds the job that was committed"
        assert len(envelope_calls) == 1, "a replay records no second envelope"
        assert pinned == first.envelope, "the first acceptance's pins stand"


def test_a_crash_after_the_job_row_but_before_the_envelope_leaves_nothing(db: Path, monkeypatch: pytest.MonkeyPatch):
    """3.1: the job row and its envelope share one transaction; a crash between the two writes keeps neither."""
    def stop(*_args: Any, **_kwargs: Any) -> None:
        raise Crash()

    monkeypatch.setattr(media_jobs, "_record_envelope", stop)
    with pytest.raises(Crash):
        make(db)
    assert totals(db) == (0, 0, 0, 0)


# --- 3.1: crash during the final commit -------------------------------------------------------------------------------

@pytest.mark.parametrize("where", ["in_before_commit_hook", "at_commit"])
def test_a_crash_in_the_final_job_commit_leaves_the_prior_state(db: Path, hooks: _Hooks, where: str):
    """3.1: crash during the final commit; the job stays running, and a restart finishes it once."""
    created = make(db, status=media_jobs.STATUS_RUNNING, request_id="req-final")
    before = snapshot(db)
    staged: list[str] = []

    def stage(conn: sqlite3.Connection, _job: dict[str, Any]) -> None:
        conn.execute("UPDATE media_jobs SET error = 'staged-with-the-result' WHERE job_id = ?", (created.job_id,))
        staged.append("staged")
        if where == "in_before_commit_hook" and len(staged) == 1:
            raise Crash()

    if where == "at_commit":
        def crash(real: sqlite3.Connection) -> None:
            raise Crash()
        hooks.on_commit = crash
    with pytest.raises(Crash):
        media_jobs.update_status(created.job_id, "succeeded", before_commit=stage, path=db)
    hooks.on_commit = lambda real: real.commit()

    assert staged == ["staged"], "the crash came after the result was written inside the transaction"
    assert snapshot(db) == before, "no part of the final transition survived the crash"
    assert media_jobs.get(created.job_id, path=db)["status"] == "running"

    done = media_jobs.update_status(created.job_id, "succeeded", before_commit=stage, path=db)
    assert done["status"] == "succeeded"
    assert media_jobs.get(created.job_id, path=db)["status"] == "succeeded"
    assert count(db, "media_jobs") == 1
    assert staged == ["staged", "staged"], "the restart staged the result once, for the one transition that committed"


# --- 3.2: concurrent same-key retries ---------------------------------------------------------------------------------

def test_concurrent_same_key_creates_record_one_job_and_one_envelope(db: Path, hooks: _Hooks,
                                                                     envelope_calls: list[str]):
    """3.2: two retries race on one key; the second waits for the first's write lock, then replays it."""
    held = threading.Event()
    release = threading.Event()
    second_asked_for_the_lock = threading.Event()
    results: dict[str, media_jobs.MediaJob] = {}
    errors: list[BaseException] = []
    first_thread_id: list[int] = []

    def on_commit(real: sqlite3.Connection) -> None:
        if threading.get_ident() == first_thread_id[0]:
            held.set()
            assert release.wait(10), "the test never released the first writer"
        real.commit()

    def on_begin() -> None:
        if first_thread_id and threading.get_ident() != first_thread_id[0]:
            second_asked_for_the_lock.set()

    hooks.on_commit = on_commit
    hooks.on_begin = on_begin

    def run(name: str) -> None:
        try:
            if name == "a":
                first_thread_id.append(threading.get_ident())
            results[name] = make(db, "req-race")
        except BaseException as exc:  # noqa: BLE001 - reported by the main thread
            errors.append(exc)

    a = threading.Thread(target=run, args=("a",))
    a.start()
    assert held.wait(10), "the first create never reached its commit"
    b = threading.Thread(target=run, args=("b",))
    b.start()
    assert second_asked_for_the_lock.wait(10), "the second create never asked for the write lock"
    b.join(0.3)
    assert b.is_alive(), "the second create must wait while the first holds the write lock"
    release.set()
    a.join(10)
    b.join(10)
    assert not errors, errors
    assert results["a"].job_id == results["b"].job_id
    assert totals(db) == (1, 1, 3, 3)
    assert len(envelope_calls) == 1, "only the create that won the lock recorded an envelope"
    before = snapshot(db)
    assert make(db, "req-race").job_id == results["a"].job_id
    assert snapshot(db) == before and len(envelope_calls) == 1, "a later matching replay changes nothing"


# --- 3.2: a changed input, owner or person ----------------------------------------------------------------------------

@pytest.mark.parametrize("change", ["input", "owner", "person"])
def test_a_replay_with_one_changed_submission_fact_conflicts(db: Path, envelope_calls: list[str], change: str):
    """3.2: a changed input, owner or person conflicts and writes nothing."""
    base: dict[str, Any] = dict(app_id="app-A", maker_person_id="person-P", maker_from_request=False, input_data={"value": "A"})
    make(db, "req-fact", **base)
    before = snapshot(db)
    changed: dict[str, Any] = dict(base)
    if change == "input":
        changed["input_data"] = {"value": "B"}
    elif change == "owner":
        changed["app_id"] = "app-B"
    else:
        changed["maker_person_id"] = "person-Q"
    with pytest.raises(media_jobs.IdempotencyConflict):
        make(db, "req-fact", **changed)
    assert snapshot(db) == before
    assert len(envelope_calls) == 1, "a refused replay records no envelope"
    assert totals(db) == (1, 1, 3, 3)
    # The control: the unchanged retry still replays.
    assert make(db, "req-fact", **base).job_id == snapshot(db)["media_jobs"][0][0]


# --- 3.2: erased fingerprints -----------------------------------------------------------------------------------------

OWNER = "app-A"


def _erase(db: Path) -> int:
    return media_jobs.erase_prompt_derivatives({f"app:{OWNER}"}, before=100, path=db)


def _stored(db: Path, job_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(db)
    try:
        return json.loads(conn.execute("SELECT job_json FROM media_jobs WHERE job_id = ?", (job_id,)).fetchone()[0])
    finally:
        conn.close()


@pytest.mark.parametrize("stale_rewrite", [False, True], ids=["plain", "stale-worker-rewrite"])
def test_an_erased_fingerprint_cannot_authorize_a_replay(db: Path, envelope_calls: list[str], stale_rewrite: bool):
    """3.2: after erasure the input digest is gone, so an identical retry conflicts instead of replaying."""
    created = make(db, "req-erased", app_id=OWNER, created_at=100, metadata={"request_hash": "fingerprint-canary-1"})
    assert _stored(db, created.job_id)["input_hash"]
    stale_copy = copy.deepcopy(created)
    assert _erase(db) == 1
    if stale_rewrite:
        media_jobs.upsert(stale_copy, path=db, maker_from_request=False)
    stored = _stored(db, created.job_id)
    assert stored["input_hash"] is None and "request_hash" not in stored["metadata"]
    before = snapshot(db)
    with pytest.raises(media_jobs.IdempotencyConflict):
        make(db, "req-erased", app_id=OWNER, created_at=100, metadata={"request_hash": "fingerprint-canary-1"})
    assert snapshot(db) == before and len(envelope_calls) == 1
    assert totals(db) == (1, 1, 3, 3)
    assert _stored(db, created.job_id)["input_hash"] is None, "a refused retry does not restore the digest"


# --- 3.2: both sides of the 14-day replay window ----------------------------------------------------------------------

def _age(db: Path, job_id: str, updated_at: int) -> None:
    conn = sqlite3.connect(db)
    try:
        body = json.loads(conn.execute("SELECT job_json FROM media_jobs WHERE job_id = ?", (job_id,)).fetchone()[0])
        body["updated_at"] = updated_at
        conn.execute("UPDATE media_jobs SET updated_at = ?, job_json = ? WHERE job_id = ?",
                     (updated_at, json.dumps(body, sort_keys=True), job_id))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["succeeded", "failed", "canceled"])
@pytest.mark.parametrize("age,replays", [(WINDOW - 1, True), (WINDOW, True), (WINDOW + 1, False)],
                         ids=["inside", "edge", "outside"])
def test_a_finished_job_replays_up_to_and_including_the_window_edge(db: Path, envelope_calls: list[str],
                                                                   monkeypatch: pytest.MonkeyPatch, status: str,
                                                                   age: int, replays: bool):
    """3.2: a finished job replays while its age is at most 14 days; one second later the key is free again."""
    monkeypatch.setattr(media_jobs, "_now", lambda: NOW)
    original = make(db, "req-window", status=status, created_at=NOW - WINDOW - 100)
    _age(db, original.job_id, NOW - age)
    looked_up = media_jobs.get_by_request_id("req-window", path=db)
    again = make(db, "req-window")
    if replays:
        assert looked_up is not None and looked_up["job_id"] == original.job_id
        assert again.job_id == original.job_id
        assert totals(db) == (1, 1, 3, 3) and len(envelope_calls) == 1
    else:
        assert looked_up is None
        assert again.job_id != original.job_id
        assert totals(db) == (2, 2, 6, 3), "two jobs, each with its envelope, sharing three definition blobs"
        assert _stored(db, original.job_id)["request_id"] == "", "the old job gives up the key and is kept"
        assert DefinitionStore(db).envelope(original.job_id) is not None
        assert len(envelope_calls) == 2


@pytest.mark.parametrize("status", ["queued", "running"])
def test_an_unfinished_job_replays_however_old(db: Path, envelope_calls: list[str], monkeypatch: pytest.MonkeyPatch,
                                              status: str):
    """3.2 (control for the window): only a finished job leaves the window."""
    monkeypatch.setattr(media_jobs, "_now", lambda: NOW)
    original = make(db, "req-live", status=status)
    _age(db, original.job_id, NOW - WINDOW - 1)
    assert make(db, "req-live").job_id == original.job_id
    assert totals(db) == (1, 1, 3, 3) and len(envelope_calls) == 1


# --- 3.1: a crash around the attempt marker, the send, the saved id and the fetch -------------------------------------
# One real ``jobs.run`` on a real job row; the engine is a counting adapter. "Restart" is a second ``run`` on the same
# job row with a new runtime, as the startup scan would start it. Every effect is counted at the engine.

class CountingEngine:
    interface_version = ADAPTER_INTERFACE_VERSION

    def __init__(self, crash_at: str | None) -> None:
        self.crash_at = crash_at
        self.sends = 0
        self.polls = 0
        self.fetches = 0
        self.fetch_attempts = 0

    def _maybe_crash(self, point: str) -> None:
        if self.crash_at == point:
            self.crash_at = None  # the process dies once
            raise Crash()

    def validate(self, _conn: Connection, _op: str, _request: CanonicalRequest) -> None:
        self._maybe_crash("before_marker")

    async def submit(self, _conn: Connection, op: str, _request: CanonicalRequest) -> JobRef:
        self._maybe_crash("after_marker_before_send")
        self.sends += 1
        return JobRef(op, "engine-job-1")

    async def poll(self, _conn: Connection, ref: JobRef) -> JobStatus:
        self._maybe_crash("during_poll")
        self.polls += 1
        return JobStatus(ref, JobState.succeeded, assets=("out",))

    async def cancel(self, _conn: Connection, _ref: JobRef) -> None:
        raise AssertionError("a restart must not cancel the engine's job")

    async def fetch_assets(self, _conn: Connection, _ref: JobRef, _status: JobStatus):
        self.fetch_attempts += 1
        self._maybe_crash("during_fetch")
        self.fetches += 1
        return ("the-result",)


class Runtime:
    def __init__(self, engine: CountingEngine) -> None:
        self.adapter = engine
        self.conn = Connection(address="http://engine.test:1", timeouts=Timeouts(connect_s=1, first_byte_s=1, total_s=5))

    def connection_for(self, _op: str) -> Connection:
        return self.conn

    def poll_timing(self, _op: str) -> tuple[float, float]:
        return 0.01, 5.0

    def request(self, op: str, body: dict[str, Any]) -> CanonicalRequest:
        return CanonicalRequest(op=op, body=dict(body), params={})


def _run(engine: CountingEngine, job_id: str) -> Any:
    return asyncio.run(protocol_jobs.run(Runtime(engine), "audio.generate", {"prompt": "x"}, job_id=job_id))


def _marker(db: Path, job_id: str) -> dict[str, Any]:
    return (_stored(db, job_id).get("metadata") or {})


@pytest.mark.parametrize("cut,resumes,sends,fetched", [
    ("before_marker", True, 1, 1),
    ("after_marker_before_send", False, 0, 0),
    ("after_send_before_id_saved", False, 1, 0),
    ("after_id_saved", True, 1, 1),
    ("during_fetch", True, 1, 1),
], ids=lambda value: str(value))
def test_a_crash_at_each_point_of_a_protocol_run_resends_only_when_nothing_could_have_been_sent(
        db: Path, monkeypatch: pytest.MonkeyPatch, cut: str, resumes: bool, sends: int, fetched: int):
    """3.1: before the marker a restart may send; once the marker is saved it never sends again; after the id, it polls."""
    created = make(db, "req-run", status=media_jobs.STATUS_RUNNING)
    monkeypatch.setattr(media_jobs, "DB_PATH", db)
    engine = CountingEngine({"after_id_saved": "during_poll"}.get(cut, cut))
    if cut == "after_send_before_id_saved":
        real = media_jobs.update_status
        crashed: list[bool] = []

        def crash_on_the_id(job_id: str, status: str, **kwargs: Any) -> Any:
            if (kwargs.get("metadata_patch") or {}).get("protocol_job") and not crashed:
                crashed.append(True)
                raise Crash()
            return real(job_id, status, **kwargs)

        monkeypatch.setattr(media_jobs, "update_status", crash_on_the_id)
    with pytest.raises(Crash):
        _run(engine, created.job_id)
    sent_before_restart = engine.sends
    marker = _marker(db, created.job_id)
    assert bool(marker.get("protocol_submitting")) == (cut != "before_marker"), "the marker is saved before the send"
    assert bool(marker.get("protocol_job")) == (cut in ("after_id_saved", "during_fetch")), "the id is saved after it"
    assert sent_before_restart == (1 if cut in ("after_send_before_id_saved", "after_id_saved", "during_fetch") else 0)

    if cut == "after_send_before_id_saved":
        monkeypatch.setattr(media_jobs, "update_status", real)
    if resumes:
        assert _run(engine, created.job_id) == ("the-result",)
        assert engine.fetches == fetched
    else:
        with pytest.raises(ProtocolError) as refused:
            _run(engine, created.job_id)
        assert refused.value.code == "submission_unknown"
        assert engine.fetches == 0
    assert engine.sends == sends, "the engine started the job exactly as often as the table allows"
    if cut in ("after_id_saved", "during_fetch"):
        assert engine.sends == sent_before_restart == 1, "a saved id means the restart polls and never sends"
        assert _marker(db, created.job_id)["protocol_job"] == "engine-job-1"
