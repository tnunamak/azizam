"""Write locks, rollbacks and connection closes of the media_jobs, receipt_store and admin_store writers.

Round 2 called these unobservable without two concurrent writers or a descriptor probe. They are
observable: SQLite reports every statement a connection runs, and a connection that was closed refuses
the next statement. Each test names the mutant (statement deleted) it kills; line numbers are those of
the sweep that found it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

import pytest

from gateway import admin_store as store
from gateway import media_jobs as mj
from gateway import receipt_store as rs


def trace(monkeypatch, module) -> list[str]:
    """Record every statement the module's connections run."""
    seen: list[str] = []
    real = module._conn

    @contextmanager
    def traced(*args, **kwargs):
        with real(*args, **kwargs) as conn:
            conn.set_trace_callback(seen.append)
            yield conn

    monkeypatch.setattr(module, "_conn", traced)
    return seen


def trace_direct(monkeypatch) -> list[str]:
    """Record the statements of connections that the schema initializers open with sqlite3.connect."""
    seen: list[str] = []
    real = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real(*args, **kwargs)
        conn.set_trace_callback(seen.append)
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    return seen


def first(seen: list[str], prefix: str) -> int:
    return next(i for i, statement in enumerate(seen) if statement.startswith(prefix))


def assert_locked_before(seen: list[str], first_read: str) -> None:
    assert "BEGIN IMMEDIATE" in seen, seen
    assert first(seen, "BEGIN IMMEDIATE") < first(seen, first_read), seen


# --- admin_store -----------------------------------------------------------------------------------

@pytest.fixture
def admin_db(tmp_path):
    path = tmp_path / "admin.db"
    store.init_db(path)
    return path


def claim(path):
    return store.claim_first_local_owner(
        issuer="i", subject="s", email="o@example.com", display_name="Owner",
        verifier="v", granted_by="test", path=path)


def test_claiming_the_first_owner_takes_the_write_lock_before_it_checks_for_an_owner(admin_db, monkeypatch):
    """admin_store.py:917 (929 after the trusted-networks merge) BEGIN IMMEDIATE."""
    seen = trace(monkeypatch, store)
    assert claim(admin_db) is not None
    assert_locked_before(seen, "SELECT 1 FROM admin_role_assignments")


def test_a_second_claim_rolls_back_its_transaction(admin_db, monkeypatch):
    """admin_store.py:924 (936) rollback."""
    assert claim(admin_db) is not None
    seen = trace(monkeypatch, store)
    assert claim(admin_db) is None
    assert "ROLLBACK" in seen


def test_granting_reconciling_and_revoking_admin_roles_take_the_write_lock_first(admin_db, monkeypatch):
    """admin_store.py assign_admin_role (2041/2111), reconcile_oidc_admin_roles (2089/2159), revoke_admin_role (2139/2209)."""
    user = store.upsert_oidc_user("i", "adm", path=admin_db)["id"]
    seen = trace(monkeypatch, store)
    grant = store.assign_admin_role(role="admin", subject_kind="user", subject_id=user, now=1, path=admin_db)
    assert_locked_before(seen, "SELECT * FROM admin_role_assignments")
    seen.clear()
    store.reconcile_oidc_admin_roles(user, ["owner"], now=2, path=admin_db)
    assert_locked_before(seen, "SELECT * FROM admin_role_assignments")
    seen.clear()
    assert store.revoke_admin_role(grant["id"], now=3, path=admin_db) is True
    assert_locked_before(seen, "SELECT * FROM admin_role_assignments")


# --- media_jobs ------------------------------------------------------------------------------------

@pytest.fixture
def jobs_db(tmp_path):
    path = tmp_path / "jobs.db"
    mj.init_db(path)
    return path


def seed(path, job_id, **kw):
    return mj.create(mj.MediaJob(intent="image.generate", job_id=job_id, request_id=f"req-{job_id}", **kw), path=path)


def test_media_jobs_initialization_migrates_under_a_write_lock(tmp_path, monkeypatch):
    """media_jobs.py:373 BEGIN IMMEDIATE (reopening an existing store)."""
    mj.init_db(tmp_path / "again.db")
    seen = trace_direct(monkeypatch)
    mj.init_db(tmp_path / "again.db")
    assert "BEGIN IMMEDIATE" in seen


def test_media_jobs_schema_mismatch_names_the_stored_version(tmp_path):
    """media_jobs.py:368 row[0]."""
    path = tmp_path / "old.db"
    mj.init_db(path)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE media_jobs_schema_version SET version = 99")
    with pytest.raises(sqlite3.DatabaseError) as raised:
        mj.init_db(path)
    assert str(raised.value) == f"media_jobs schema version mismatch: db=99 code={mj.SCHEMA_VERSION}"


def test_writing_a_job_takes_the_write_lock_before_it_reads_the_previous_row(jobs_db, monkeypatch):
    """media_jobs.py:640 BEGIN IMMEDIATE."""
    seen = trace(monkeypatch, mj)
    mj.upsert(mj.MediaJob(intent="image.generate", job_id="j1", request_id="r1"), path=jobs_db)
    assert_locked_before(seen, "SELECT job_json, request_id FROM media_jobs")


def test_a_replayed_create_and_a_full_limit_roll_their_transaction_back(jobs_db, monkeypatch):
    """media_jobs.py:601 rollback after a replay; :614 rollback when a limit is full."""
    seed(jobs_db, "j1")
    seen = trace(monkeypatch, mj)
    assert seed(jobs_db, "j1").job_id == "j1"
    assert "ROLLBACK" in seen
    seen.clear()
    limit = mj.OutstandingLimit(scope="video", maximum=0, mode="video")
    with pytest.raises(mj.OutstandingLimitReached):
        mj.create(mj.MediaJob(intent="image.generate", request_id="r-new", metadata={"mode": "video"}),
                  outstanding_limits=[limit], path=jobs_db)
    assert "ROLLBACK" in seen


def test_prompt_erasure_locks_each_batch_and_rolls_a_failed_batch_back(jobs_db, monkeypatch):
    """media_jobs.py:715 and :750 BEGIN IMMEDIATE; :762 rollback."""
    seed(jobs_db, "j1", app_id="app-1")
    seen = trace(monkeypatch, mj)
    owner = {"app:app-1"}
    assert mj.erase_prompt_derivatives(owner, before=2**40, path=jobs_db) == 1
    assert seen[first(seen, "BEGIN IMMEDIATE")] == "BEGIN IMMEDIATE"
    assert seen.count("BEGIN IMMEDIATE") == 2 and first(seen, "BEGIN IMMEDIATE") < first(seen, "CREATE TABLE")
    seed(jobs_db, "j2", app_id="app-2")
    seen.clear()

    def boom(*args, **kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(mj, "_write_job", boom)
    with pytest.raises(RuntimeError, match="write failed"):
        mj.erase_prompt_derivatives({"app:app-2"}, before=2**41, path=jobs_db)
    assert "ROLLBACK" in seen


def test_beginning_a_new_attempt_locks_before_it_reads_the_job(jobs_db, monkeypatch):
    """media_jobs.py:1048 BEGIN IMMEDIATE."""
    seed(jobs_db, "j1", status="running")
    seen = trace(monkeypatch, mj)
    assert mj.begin_attempt("j1", now=5, path=jobs_db)["attempt_number"] == 2
    assert_locked_before(seen, "SELECT job_json FROM media_jobs")


def test_reading_events_repairs_missing_sequences_under_the_write_lock(jobs_db, monkeypatch):
    """media_jobs.py:1173 BEGIN IMMEDIATE."""
    seed(jobs_db, "j1")
    with sqlite3.connect(jobs_db) as conn:
        conn.execute("DROP TRIGGER media_job_event_assign_sequence")
        conn.execute("INSERT INTO media_job_events(job_id, created_at, event_type, payload_json, source) "
                     "VALUES ('j1', 1, 'note', '{}', 'gateway')")
    seen = trace(monkeypatch, mj)
    assert [event["sequence"] for event in mj.events("j1", path=jobs_db)] == [1]
    assert_locked_before(seen, "UPDATE media_job_events")


class _Closable:
    """Wraps the real connection so the test can ask whether the code closed it."""

    def __init__(self, conn):
        self.conn = conn
        self.closed = False

    def __getattr__(self, name):
        return getattr(self.conn, name)

    def close(self):
        self.closed = True
        self.conn.close()


@pytest.mark.parametrize("module", [mj, rs], ids=["media_jobs", "receipt_store"])
def test_a_connection_whose_pragmas_fail_is_closed_before_the_error_escapes(module, tmp_path, monkeypatch):
    """media_jobs.py:434 and receipt_store.py:189 close()."""
    opened: list[_Closable] = []
    real_connect = module.connect

    def connect(*args, **kwargs):
        wrapped = _Closable(real_connect(*args, **kwargs))
        opened.append(wrapped)
        return wrapped

    def refuse(conn):
        raise RuntimeError("pragma failed")

    monkeypatch.setattr(module, "connect", connect)
    monkeypatch.setattr(module, "_apply_pragmas", refuse)
    with pytest.raises(RuntimeError, match="pragma failed"):
        with module._conn(tmp_path / "x.db"):
            pass
    assert [c.closed for c in opened] == [True]


# --- receipt_store ---------------------------------------------------------------------------------

@pytest.fixture
def receipts_db(tmp_path):
    path = tmp_path / "receipts.db"
    rs.init_db(path)
    return path


def put(path, request_id, body=None, *, status="in_progress", created_at=1):
    body = body if body is not None else {"request_id": request_id, "status": status}
    with sqlite3.connect(path) as conn:
        conn.execute("INSERT INTO receipts(request_id, created_at, status, receipt_json) VALUES (?,?,?,?)",
                     (request_id, created_at, status, body if isinstance(body, str) else json.dumps(body)))


def test_the_read_only_reader_closes_its_connection(receipts_db, monkeypatch):
    """receipt_store.py:179 close()."""
    opened: list[_Closable] = []
    real = rs.connect_readonly

    def connect_readonly(*args, **kwargs):
        wrapped = _Closable(real(*args, **kwargs))
        opened.append(wrapped)
        return wrapped

    monkeypatch.setattr(rs, "connect_readonly", connect_readonly)
    with rs.reader(receipts_db) as conn:
        conn.execute("SELECT 1").fetchall()
    assert [c.closed for c in opened] == [True]


def test_receipt_schema_initialization_rolls_back_a_newer_schema_refusal(receipts_db, monkeypatch):
    """receipt_store.py:132 rollback."""
    with sqlite3.connect(receipts_db) as conn:
        conn.execute("UPDATE schema_version SET version = 99")
    seen = trace_direct(monkeypatch)
    with pytest.raises(sqlite3.DatabaseError, match="downgrade not supported"):
        rs.init_db(receipts_db)
    assert "ROLLBACK" in seen


def test_receipt_writers_take_the_write_lock_before_they_read(receipts_db, monkeypatch):
    """receipt_store.py:291 update_phases, :720 save_retention, :734 maintain (BEGIN IMMEDIATE)."""
    from gateway.receipts import RouteReceipt
    live = RouteReceipt(request_id="rr", protocol="p", path="/x", intent="i")
    rs.begin(live, now_ms=1_000, path=receipts_db)
    seen = trace(monkeypatch, rs)
    rs.update_phases(live, path=receipts_db)
    assert_locked_before(seen, "SELECT receipt_json FROM receipts")
    seen.clear()
    saved = []
    rs.save_retention(None, None, lambda: saved.append(1), path=receipts_db)
    assert saved == [1] and "BEGIN IMMEDIATE" in seen
    seen.clear()
    monkeypatch.setattr(rs, "retention_days", lambda admin_path: None)
    rs.maintain(receipts_db.parent / "admin.db", path=receipts_db)
    assert "BEGIN IMMEDIATE" in seen


def test_update_cost_rolls_back_for_a_missing_and_for_a_malformed_receipt(receipts_db, monkeypatch):
    """receipt_store.py:475 and :479 rollback."""
    put(receipts_db, "bad", "[]")
    seen = trace(monkeypatch, rs)
    assert rs.update_cost("ghost", {"usd": 1}, path=receipts_db) is False
    assert "ROLLBACK" in seen
    seen.clear()
    with pytest.raises(ValueError, match="cannot update malformed stored receipt"):
        rs.update_cost("bad", {"usd": 1}, path=receipts_db)
    assert "ROLLBACK" in seen


def test_the_stale_projection_reads_the_clock_in_milliseconds(monkeypatch):
    """receipt_store.py:402 time.time() * 1000."""
    monkeypatch.setattr(rs.time, "time", lambda: 1_000.0)
    body = {"request_id": "r", "status": "in_progress", "updated_at_ms": 1_000_000 - rs.STALE_AFTER_MS - 1}
    assert rs._project_incomplete(body, now_ms=None, stale_after_ms=rs.STALE_AFTER_MS)["status"] != "in_progress"
    fresh = {"request_id": "r", "status": "in_progress", "updated_at_ms": 1_000_000}
    assert rs._project_incomplete(fresh, now_ms=None, stale_after_ms=rs.STALE_AFTER_MS)["status"] == "in_progress"


def test_compaction_reports_the_size_after_the_rebuild_and_the_log_sizes(receipts_db):
    """receipt_store.py:757 page_count * page_size; :774 missing log size."""
    for n in range(50):
        put(receipts_db, f"r{n}", {"request_id": f"r{n}", "status": "succeeded", "pad": "x" * 2000})
    with sqlite3.connect(receipts_db) as conn:
        conn.execute("DELETE FROM receipts")
    result = rs.compact(receipts_db)
    with sqlite3.connect(receipts_db) as conn:
        size = conn.execute("PRAGMA page_count").fetchone()[0] * conn.execute("PRAGMA page_size").fetchone()[0]
    assert result["after_bytes"] == size and result["after_bytes"] < result["before_bytes"]
    assert result["checkpoint_pending"] is False
    lone = receipts_db.parent / "lone.db"
    lone.write_bytes(b"x" * 10)
    assert rs._file_bytes(lone) == {"main_bytes": 10, "wal_bytes": 0, "physical_bytes": 10}


def test_adding_a_trusted_address_takes_the_write_lock_and_stores_the_entry_as_given(admin_db, monkeypatch):
    """admin_store.py:1652 id prefix 'tad'; :1654 BEGIN IMMEDIATE; :1658 session-app refusal text; :1668 absent note."""
    owner = store.upsert_oidc_user("i", "o", path=admin_db)["id"]
    app = store.create_app("a", kind="personal", owner_user_id=owner, access_plan="person", path=admin_db)["id"]
    seen = trace(monkeypatch, store)
    entry = store.add_trusted_address(app, "10.0.0.0/24", max_entries=3, path=admin_db)
    assert_locked_before(seen, "SELECT 1 FROM apps")
    assert entry["id"].startswith("tad") and entry["note"] is None
    assert store.add_trusted_address(app, "10.0.1.0/24", note="  Kitchen  ", max_entries=3, path=admin_db)["note"] == "Kitchen"
    with sqlite3.connect(admin_db) as conn:
        conn.execute("INSERT INTO session_apps(user_id, app_id) VALUES (?, ?)", (owner, app))
    with pytest.raises(ValueError) as raised:
        store.add_trusted_address(app, "10.0.2.0/24", max_entries=3, path=admin_db)
    assert str(raised.value) == "A person's own signed-in app cannot be reached by address."
