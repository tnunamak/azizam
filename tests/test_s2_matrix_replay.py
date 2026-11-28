"""S2 acceptance matrix, item 3: crash and replay of the job store (docs/design/CODE_TO_DATA_S2.md).

Each test names the matrix bullet it closes. A "crash" is a dedicated BaseException raised at one named point inside
``media_jobs.create`` (the connection is then closed, so SQLite rolls back whatever was not committed), never an
ordinary caught error. Row counts are read back through a second, fresh connection, as a restarted process would.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import shutil
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

import proxy
from tests.support.graph_service import GRAPH_SERVICE_ID
from gateway import directory_index, media_input_assets, media_input_leases, media_jobs, receipt_store
from gateway.definition_store import DefinitionStore
from gateway.engine_protocol import jobs as protocol_jobs
from gateway.engine_protocol.adapter import ADAPTER_INTERFACE_VERSION
from gateway.engine_protocol.errors import ProtocolError
from gateway.engine_protocol.types import (CanonicalRequest, Connection, JobRef, JobState, JobStatus, Timeouts)
from gateway.job_envelope import JobEnvelope, build_job_envelope
from gateway.receipts import RouteReceipt
from tests.support.access import access_context
from tests.test_image_extensions import CONTROLNET_EDIT_MANIFEST
from tests.test_media_inputs_image_route import OWNER_APP, image_bytes, stored

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

    def counted(conn, job_id, envelope, **kwargs):
        calls.append(job_id)
        return real(conn, job_id, envelope, **kwargs)

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


# --- 3.1: a crash around each upload of a media input, and around the media input ledger ------------------------------
# The real extension upload (`proxy._upload_image_workflow_extension_assets`) on the real media input resolver, ledger
# and asset store, in the order a route runs: the receipt is begun, the input is resolved and sent to the engine, the
# job is created, the receipt is finished. The engine is a counting fake of the upload call. A "crash" is the dedicated
# BaseException at one named point. Python runs `finally` blocks while a Crash unwinds, and a dead process runs none, so
# the durable state at the crash instant is copied first and put back after the unwind: what the restart sees is what
# a killed process left. The retry is the same request again with the same request id. Every effect is counted: the
# uploads the engine received, the ledger rows (markers, removal duties, leases), the files in the store, the jobs and
# the receipts.

REQUEST_ID = "req-media-input"


class UploadEngine:
    """The engine's upload call. `sent` is what the engine has received; a crash can come before, or after it holds the bytes."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.crash_at: str | None = None
        self.on_crash: Callable[[], None] = lambda: None

    def crash(self, point: str) -> None:
        if self.crash_at == point:
            self.crash_at = None
            self.on_crash()
            raise Crash()

    async def upload(self, _client: Any, _headers: Any, content: bytes, *, filename: str, media_type: str = "image/png"):
        self.crash("engine_before_send")
        self.sent.append(hashlib.sha256(content).hexdigest())
        self.crash("engine_during")  # the engine holds the file; the reply is lost
        return f"engine-{len(self.sent)}.png", None


class MediaRig:
    def __init__(self, db: Path, tmp_path: Path, engine: UploadEngine) -> None:
        self.db, self.engine = db, engine
        self.receipts = tmp_path / "receipts.db"
        self.assets = tmp_path / "assets"
        self.frozen = tmp_path / "frozen"
        self.crashes = 0

    # What the dead process left: the three durable stores, copied at the crash instant and put back after the unwind.
    def freeze(self) -> None:
        self.frozen.mkdir(exist_ok=True)
        for name, path in (("jobs", self.db), ("receipts", self.receipts)):
            self._copy_db(path, self.frozen / f"{name}.db")
        shutil.rmtree(self.frozen / "assets", ignore_errors=True)
        if self.assets.exists():
            shutil.copytree(self.assets, self.frozen / "assets")

    def thaw(self) -> None:
        for name, path in (("jobs", self.db), ("receipts", self.receipts)):
            self._copy_db(self.frozen / f"{name}.db", path)
        shutil.rmtree(self.assets, ignore_errors=True)
        if (self.frozen / "assets").exists():
            shutil.copytree(self.frozen / "assets", self.assets)
        directory_index.forget()

    @staticmethod
    def _copy_db(source: Path, target: Path) -> None:
        src, dst = sqlite3.connect(source), sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            src.close()
            dst.close()

    def restart(self, *, aged: bool = True) -> None:
        """A new process: no cached ledger; by default two hours have passed for every row the old process wrote."""
        proxy._media_input_ledgers.clear()
        if not aged:
            return
        conn = sqlite3.connect(self.db)
        try:
            conn.execute("UPDATE media_input_effect SET created = created - 7200")
            conn.execute("UPDATE media_input_created SET created = created - 7200")
            conn.execute("UPDATE media_input_lease SET created = created - 7200")
            conn.commit()
        finally:
            conn.close()

    def ledger(self) -> dict[str, int]:
        """Every ledger row, as a restarted process would count it, and the files in the store."""
        conn = sqlite3.connect(self.db)
        try:
            rows = {
                "markers": conn.execute("SELECT COUNT(*) FROM media_input_effect").fetchone()[0],
                "duties": conn.execute("SELECT COUNT(*) FROM media_input_created").fetchone()[0],
                "leases": conn.execute("SELECT COUNT(*) FROM media_input_lease").fetchone()[0],
                "live": conn.execute("SELECT COUNT(*) FROM media_input_lease WHERE released_at IS NULL").fetchone()[0],
            }
        finally:
            conn.close()
        rows["files"] = len([p for p in self.assets.glob("*") if p.suffix in (".png", ".jpg", ".webp", ".gif")]) if self.assets.exists() else 0
        return rows

    def effects(self) -> dict[str, int]:
        """The ledger, plus what the engine received, the jobs and the receipts."""
        return {**self.ledger(), "sent": len(self.engine.sent), "jobs": count(self.db, "media_jobs"),
                "receipts": receipt_store.count(self.receipts)}

    def sweep(self) -> None:
        """The maintenance round of a restarted process."""
        proxy._collect_media_input_leftovers()


@pytest.fixture
def media_rig(db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MediaRig:
    monkeypatch.setattr(proxy, "IMAGE_CACHE_DIR", tmp_path / "assets")
    monkeypatch.setattr(media_jobs, "DB_PATH", db)
    monkeypatch.setattr(proxy, "_media_input_ledgers", {})
    directory_index.forget()
    registry = {"schema_version": 2, "services": {
        GRAPH_SERVICE_ID: {"id": GRAPH_SERVICE_ID, "config": {"base_url": ORIGIN}}}}
    monkeypatch.setattr(proxy.ADMIN_STORE, "get_service_registry", lambda: registry)
    receipts = tmp_path / "receipts.db"
    receipt_store.init_db(receipts)
    engine = UploadEngine()
    monkeypatch.setattr(proxy, "_upload_graph_image_bytes", engine.upload)
    rig = MediaRig(db, tmp_path, engine)
    engine.on_crash = rig.freeze
    yield rig
    directory_index.forget()


def _crash_here(rig: MediaRig, monkeypatch: pytest.MonkeyPatch, cls: type, name: str, *, after: bool) -> None:
    """Make one call of ``cls.name`` the crash point: at its entry, or right after it has committed."""
    real = getattr(cls, name)
    fired: list[bool] = []

    def wrapped(self: Any, *args: Any, **kwargs: Any) -> Any:
        if not after and not fired:
            fired.append(True)
            rig.freeze()
            raise Crash()
        result = real(self, *args, **kwargs)
        if after and not fired:
            fired.append(True)
            rig.freeze()
            raise Crash()
        return result

    monkeypatch.setattr(cls, name, wrapped)


def _arm(rig: MediaRig, monkeypatch: pytest.MonkeyPatch, cut: str) -> None:
    ledger, port = media_input_leases.MediaInputLedger, media_input_assets.CacheAssetPort
    where = {
        "before_marker": (ledger, "begin_effect", False),
        "marker_committed": (ledger, "begin_effect", True),
        "file_stored_before_lease": (ledger, "lease", False),
        "before_lease": (ledger, "lease", False),
        "lease_committed": (ledger, "lease", True),
        "after_reply_before_release": (ledger, "release_holder", False),
        "release_committed": (ledger, "release_holder", True),
        "sweep_before_removal": (port, "discard", False),
        "sweep_after_removal": (port, "discard", True),
    }.get(cut)
    if where is not None:
        _crash_here(rig, monkeypatch, *where[:2], after=where[2])
    else:
        rig.engine.crash_at = cut


def _auth() -> Any:
    return access_context(app_id=OWNER_APP["app_id"], key_hash=OWNER_APP["key_hash"])


class _Part:
    """An uploaded multipart part."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.filename, self.content_type = "control.png", "image/png"

    async def read(self) -> bytes:
        return self._data


def one_request(rig: MediaRig, source: dict[str, Any]) -> media_jobs.MediaJob:
    """One attempt of the request, in the route's order. ``source`` is an upload or a stored asset."""
    receipt = RouteReceipt(protocol="openai", intent="image.edit", request_id=REQUEST_ID)
    receipt_store.begin(receipt, path=rig.receipts)
    value = {"asset_id": source["asset_id"]} if "asset_id" in source else {"multipart_field": "control"}
    form = SimpleNamespace(get=lambda name: _Part(source["bytes"]) if name == "control" else None)
    updated, error = asyncio.run(proxy._upload_image_workflow_extension_assets(
        form=form, request_body={"prompt": "x", "extensions": {proxy.WORKFLOW_EXTENSION_NAMESPACE: {"control_image": value}}},
        manifest=CONTROLNET_EDIT_MANIFEST, client=object(), headers={}, receipt=receipt, auth_context=_auth()))
    assert error is None
    created = make(rig.db, REQUEST_ID)
    receipt.finish("succeeded")
    receipt_store.finalize(receipt, path=rig.receipts)
    return created


def _crashed(rig: MediaRig, source: dict[str, Any]) -> None:
    with pytest.raises(Crash):
        one_request(rig, source)
    rig.thaw()
    rig.engine.crash_at = None


def _upload_source() -> dict[str, Any]:
    return {"bytes": image_bytes("PNG")}


NOTHING = {"markers": 0, "duties": 0, "leases": 0, "live": 0, "files": 0}

# cut -> (what the ledger and the store hold after the crash, uploads the engine received by then)
UPLOAD_CUTS = {
    "before_marker": ({**NOTHING}, 0),
    "marker_committed": ({**NOTHING, "markers": 1, "duties": 1}, 0),
    "file_stored_before_lease": ({**NOTHING, "markers": 1, "duties": 1, "files": 1}, 0),
    "lease_committed": ({**NOTHING, "duties": 1, "leases": 1, "live": 1, "files": 1}, 0),
    "engine_before_send": ({**NOTHING, "duties": 1, "leases": 1, "live": 1, "files": 1}, 0),
    "engine_during": ({**NOTHING, "duties": 1, "leases": 1, "live": 1, "files": 1}, 1),
    "after_reply_before_release": ({**NOTHING, "duties": 1, "leases": 1, "live": 1, "files": 1}, 1),
    "release_committed": ({**NOTHING, "duties": 1, "leases": 1, "files": 1}, 1),
    "sweep_before_removal": ({**NOTHING, "duties": 1, "files": 1}, 1),
    "sweep_after_removal": ({**NOTHING, "duties": 1}, 1),
}
# A marker with no lease holds the file it names until its hour is up, so a retry within the hour cannot remove the file.
STALE_MARKER = ("marker_committed", "file_stored_before_lease")


@pytest.mark.parametrize("cut", UPLOAD_CUTS)
def test_a_crash_around_an_uploaded_input_leaves_what_the_ledger_allows_and_the_retry_runs_once(
        media_rig: MediaRig, monkeypatch: pytest.MonkeyPatch, cut: str):
    """3.1: before, during and after the engine upload of an upload, and around each ledger write."""
    source = _upload_source()
    _arm(media_rig, monkeypatch, cut)
    _crashed(media_rig, source)
    left, sent = UPLOAD_CUTS[cut]
    assert media_rig.effects() == {**left, "sent": sent, "jobs": 0, "receipts": 1}, "what the dead process left"
    assert receipt_store.get(REQUEST_ID, path=media_rig.receipts)["status"] != "succeeded"

    media_rig.restart()
    # The retry. The engine has no memory of the first attempt, so an upload it received is sent once more.
    created = one_request(media_rig, source)
    done = media_rig.effects()
    assert (done["sent"], done["jobs"], done["receipts"], done["live"]) == (sent + 1, 1, 1, 0), done
    assert receipt_store.get(REQUEST_ID, path=media_rig.receipts)["status"] == "succeeded"
    assert set(media_rig.engine.sent) == {hashlib.sha256(source["bytes"]).hexdigest()}, "the same bytes every time"
    # Two hours on, the retry's own sweep removes everything the crash left: no marker, duty, lease or file stays.
    assert done == {**NOTHING, "sent": sent + 1, "jobs": 1, "receipts": 1}, done
    assert make(media_rig.db, REQUEST_ID).job_id == created.job_id and count(media_rig.db, "media_jobs") == 1


@pytest.mark.parametrize("cut", STALE_MARKER)
def test_a_retry_within_the_hour_of_a_crash_before_the_lease_leaves_the_file_until_the_hour_is_up(
        media_rig: MediaRig, monkeypatch: pytest.MonkeyPatch, cut: str):
    """3.1: a marker with no lease holds the file it names for its grace period; the next round after that removes it."""
    source = _upload_source()
    _arm(media_rig, monkeypatch, cut)
    _crashed(media_rig, source)
    media_rig.restart(aged=False)
    one_request(media_rig, source)
    done = media_rig.effects()
    assert (done["sent"], done["jobs"], done["receipts"], done["live"]) == (1, 1, 1, 0)
    assert (done["markers"], done["duties"], done["files"]) == (1, 1, 1), "the crashed attempt's marker holds its file"
    media_rig.restart()
    media_rig.sweep()
    assert media_rig.effects() == {**NOTHING, "sent": 1, "jobs": 1, "receipts": 1}


@pytest.mark.parametrize("cut", UPLOAD_CUTS)
def test_the_restart_sweep_before_the_retry_removes_what_the_crash_left_without_touching_the_job(
        media_rig: MediaRig, monkeypatch: pytest.MonkeyPatch, cut: str):
    """3.1: a restart that sweeps first leaves no marker, duty, lease or file; the retry then makes one job and one upload."""
    source = _upload_source()
    _arm(media_rig, monkeypatch, cut)
    _crashed(media_rig, source)
    sent = UPLOAD_CUTS[cut][1]
    media_rig.restart()
    media_rig.sweep()
    assert media_rig.effects() == {**NOTHING, "sent": sent, "jobs": 0, "receipts": 1}
    one_request(media_rig, source)
    assert media_rig.effects() == {**NOTHING, "sent": sent + 1, "jobs": 1, "receipts": 1}


# A stored asset is the caller's own file: the lease keeps it for the request and nothing ever owes its removal.
ASSET_CUTS = {
    "before_lease": ({**NOTHING, "files": 1}, 0),
    "lease_committed": ({**NOTHING, "leases": 1, "live": 1, "files": 1}, 0),
    "engine_before_send": ({**NOTHING, "leases": 1, "live": 1, "files": 1}, 0),
    "engine_during": ({**NOTHING, "leases": 1, "live": 1, "files": 1}, 1),
    "after_reply_before_release": ({**NOTHING, "leases": 1, "live": 1, "files": 1}, 1),
    "release_committed": ({**NOTHING, "leases": 1, "files": 1}, 1),
}


@pytest.mark.parametrize("cut", ASSET_CUTS)
def test_a_crash_around_the_upload_of_a_stored_asset_never_removes_the_callers_file(
        media_rig: MediaRig, monkeypatch: pytest.MonkeyPatch, cut: str):
    """3.1: before, during and after the engine upload of an asset id; the file stays through the crash, the sweep and the retry."""
    asset_id = stored(image_bytes("PNG"))
    source = {"asset_id": asset_id}
    _arm(media_rig, monkeypatch, cut)
    left, sent = ASSET_CUTS[cut]
    _crashed(media_rig, source)
    assert media_rig.effects() == {**left, "sent": sent, "jobs": 0, "receipts": 1}

    media_rig.restart()
    media_rig.sweep()  # a request lease past its hour is released; no duty names the file, so it stays
    assert media_rig.effects() == {**NOTHING, "files": 1, "sent": sent, "jobs": 0, "receipts": 1}

    one_request(media_rig, source)
    # The retry's own lease is released and kept as a row until a later round deletes it.
    assert media_rig.effects() == {**NOTHING, "leases": 1, "files": 1, "sent": sent + 1, "jobs": 1, "receipts": 1}
    media_rig.restart()
    media_rig.sweep()
    assert media_rig.effects() == {**NOTHING, "files": 1, "sent": sent + 1, "jobs": 1, "receipts": 1}
    assert proxy.asset_cache_path(asset_id).exists(), "the caller's file is still there"
