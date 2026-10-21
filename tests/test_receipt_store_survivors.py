"""Cases that kill the receipt_store survivors left after mutation round 2 (lane MUT r3).

Each test names the mutant it kills in its docstring (ID prefix: gateway/receipt_store.py).
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

import pytest

from gateway import receipt_store as rs
from gateway.receipts import RouteReceipt


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "receipts.db"
    rs.init_db(path)
    return path


def sql(db, statement, *params):
    with sqlite3.connect(db) as conn:
        return conn.execute(statement, params).fetchall()


def insert_raw(db, request_id, body, *, status="succeeded", created_at=1):
    sql(db, "INSERT INTO receipts(request_id, created_at, status, receipt_json) VALUES (?,?,?,?)",
        request_id, created_at, status, body if isinstance(body, str) else json.dumps(body))


def stored_json(db, request_id):
    return sql(db, "SELECT receipt_json FROM receipts WHERE request_id=?", request_id)[0][0]


def test_a_terminal_write_without_accepted_time_still_checks_the_updated_time():
    """:243 continue->break."""
    with pytest.raises(ValueError, match="receipt updated_at_ms must be a non-negative integer"):
        rs._assert_lifecycle_data(
            {"request_id": "r", "status": "succeeded", "accepted_at_ms": None, "updated_at_ms": None}, terminal=True)


def test_begin_without_a_clock_argument_stamps_the_current_time_in_milliseconds(db, monkeypatch):
    """:268 1000->1001."""
    monkeypatch.setattr(rs.time, "time", lambda: 12.0)
    rs.begin({"request_id": "r-clock", "protocol": "p", "intent": "i", "path": "/x"}, path=db)
    assert json.loads(stored_json(db, "r-clock"))["accepted_at_ms"] == 12_000


def test_phase_checkpoint_rewrites_the_row_sorted_and_without_ascii_escapes(db):
    """:303 True->False (sort_keys) and False->True (ensure_ascii)."""
    live = RouteReceipt(request_id="r-phase", protocol="p", path="/x", intent="i", requested_model="é")
    rs.begin(live, now_ms=1_000, path=db)
    shuffled = dict(reversed(list(json.loads(stored_json(db, "r-phase")).items())))
    sql(db, "UPDATE receipts SET receipt_json=? WHERE request_id='r-phase'", json.dumps(shuffled, ensure_ascii=False))
    live.mark_phase("started_at_ms", now_ms=1_500)
    rs.update_phases(live, path=db)
    text = stored_json(db, "r-phase")
    assert text == json.dumps(json.loads(text), ensure_ascii=False, sort_keys=True)
    assert "é" in text and json.loads(text)["started_at_ms"] == 1_500


def test_finalizing_a_row_stored_without_any_attempt_number_clears_the_callers_attempt_number(db):
    """:340 'attempt_number' key of the legacy clear."""
    insert_raw(db, "old", {"request_id": "old", "status": "in_progress", "accepted_at_ms": 1000}, status="in_progress")
    rs.finalize({"request_id": "old", "status": "failed", "updated_at_ms": 9000, "attempt_number": 4}, path=db)
    assert json.loads(stored_json(db, "old"))["attempt_number"] is None


def test_a_receipt_with_its_own_source_label_and_no_app_keeps_it():
    """:379 'source_label' key."""
    out = rs._with_source({"source_label": "Kitchen tablet"})
    assert out["source_label"] == "Kitchen tablet" and out["source"]["label"] == "Kitchen tablet"


def test_a_cost_with_a_forbidden_key_is_refused_with_the_documented_label(db):
    """:463 'receipt.cost' label."""
    insert_raw(db, "r-cost", {"request_id": "r-cost", "status": "succeeded"})
    with pytest.raises(ValueError, match=r"^receipt\.cost contains forbidden keys"):
        rs.update_cost("r-cost", {"prompt": "x"}, path=db)


def test_update_cost_refuses_a_stored_receipt_whose_metadata_is_unsafe_and_leaves_it_alone(db):
    """:482 _assert_safe(updated) removed."""
    body = json.dumps({"request_id": "r-unsafe", "status": "succeeded", "metadata": {"prompt": "x"}})
    insert_raw(db, "r-unsafe", body)
    with pytest.raises(ValueError, match=r"^receipt\.metadata contains forbidden keys"):
        rs.update_cost("r-unsafe", {"usd": 1.0}, path=db)
    assert stored_json(db, "r-unsafe") == body


def test_update_cost_rewrites_the_row_sorted_and_without_ascii_escapes(db):
    """:485 True->False (sort_keys) and False->True (ensure_ascii)."""
    insert_raw(db, "r-fmt", '{"status": "succeeded", "request_id": "r-fmt", "client_hint": "é"}')
    assert rs.update_cost("r-fmt", {"usd": 2.0}, path=db) is True
    text = stored_json(db, "r-fmt")
    assert text == json.dumps(json.loads(text), ensure_ascii=False, sort_keys=True)
    assert "é" in text


def test_get_many_asks_for_at_most_500_ids_per_query_and_returns_all_of_them(db, monkeypatch):
    """:520 and :521 500->501 (chunk step and chunk width)."""
    ids = [f"m{n:04d}" for n in range(501)]
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO receipts(request_id, created_at, status, receipt_json) VALUES (?,?,?,?)",
            [(i, 1, "succeeded", json.dumps({"request_id": i, "status": "succeeded"})) for i in ids])
    seen: list[str] = []
    real = rs._conn

    @contextmanager
    def traced(path=rs.DB_PATH):
        with real(path) as conn:
            conn.set_trace_callback(seen.append)
            yield conn

    monkeypatch.setattr(rs, "_conn", traced)
    assert sorted(rs.get_many(ids, path=db)) == ids
    widths = [statement.count("'") // 2 for statement in seen if "request_id IN" in statement]
    assert sorted(widths) == [1, 500]


def test_get_many_skips_an_unparseable_row_and_still_returns_the_next_ones(db):
    """:530 continue->break."""
    insert_raw(db, "a-bad", "[]")
    insert_raw(db, "b-ok", {"request_id": "b-ok", "status": "succeeded"})
    assert sorted(rs.get_many(["a-bad", "b-ok"], path=db)) == ["b-ok"]


def test_recent_skips_an_unparseable_row_and_returns_the_older_ones(db):
    """:642 continue->break."""
    insert_raw(db, "old-ok", {"request_id": "old-ok", "status": "succeeded"}, created_at=10)
    insert_raw(db, "new-bad", "[]", created_at=20)
    assert [r["request_id"] for r in rs.recent(10, path=db)] == ["old-ok"]


# --- mut4: entries that mut3 called equivalent or unproductive and the independent review rebutted ---

@pytest.mark.parametrize("limit", [0, -1])
def test_asking_for_no_receipts_returns_nothing_before_any_filter_is_read(db, limit):
    """:590 ``limit <= 0`` -> ``limit < 0``: the mutant parses ``since_s`` first and raises on this input."""
    rs.record({"request_id": "r-any", "metadata": {}}, created_at=1, path=db)
    assert rs.recent(limit, since_s="not a number", path=db) == []


def test_a_retention_preview_is_accepted_exactly_at_both_ends_of_its_window(db, monkeypatch):
    """:717 both ``<=`` -> ``<``: the preview window is closed, [now - days*86400 - 900, now - days*86400].

    The clock is frozen so the cutoff can sit exactly on each end.
    """
    monkeypatch.setattr(rs.time, "time", lambda: 2_000_000_000)
    latest = 2_000_000_000 - 30 * 86400
    saved = []
    for cutoff in (latest - 900, latest):
        assert rs.save_retention(30, {"cutoff": cutoff, "delete_count": 0}, lambda: saved.append(cutoff), path=db) == 0
    assert saved == [latest - 900, latest]
    for cutoff in (latest - 901, latest + 1):
        with pytest.raises(rs.RetentionPreviewChanged, match="Preview expired"):
            rs.save_retention(30, {"cutoff": cutoff, "delete_count": 0}, lambda: saved.append(cutoff), path=db)
    assert len(saved) == 2


def test_compaction_reports_the_checkpoint_busy_flag_not_the_frame_count(db, monkeypatch):
    """:766 ``checkpoint[0]`` -> ``checkpoint[1]``.

    PRAGMA wal_checkpoint returns (busy, log frames, checkpointed frames). With a real database the
    two columns agree after a TRUNCATE (log is 0 exactly when the checkpoint was not blocked), so a
    scripted answer is the only way to tell which column is read.
    """
    original = rs._conn
    answers = iter([(1, 0, 0), (0, 5, 5)])

    class Scripted:
        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, statement, *args):
            if "wal_checkpoint" in statement:
                self._conn.execute(statement, *args).fetchall()
                return type("Rows", (), {"fetchone": lambda _self, a=next(answers): a})()
            return self._conn.execute(statement, *args)

    @contextmanager
    def scripted(path):
        with original(path) as conn:
            yield Scripted(conn)

    monkeypatch.setattr(rs, "_conn", scripted)
    assert rs.compact(db)["checkpoint_pending"] is True
    assert rs.compact(db)["checkpoint_pending"] is False
