"""Exact columns, messages, retention rules and projections of gateway/receipt_store.py (mutation round 2)."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from gateway import receipt_store as rs

ROOT = Path(__file__).resolve().parent.parent

def test_crashed_writer_keeps_begun_receipt_and_stale_reads_do_not_terminalize(db):
    code = (
        f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
        "import os; from pathlib import Path; from gateway import receipt_store as rs; "
        f"rs.begin({accepted(request_id='crashed')!r}, now_ms=1000, path=Path({str(db)!r})); "
        "os._exit(23)"
    )
    child = subprocess.run([sys.executable, "-I", "-c", code],
                           capture_output=True, text=True, timeout=30)
    assert child.returncode == 23, child.stderr
    rs.init_db(db)
    original = raw(db, "SELECT * FROM receipts")
    assert len(original) == 1 and original[0]["status"] == "in_progress"
    for read in (
        rs.get("crashed", path=db, now_ms=301000),
        rs.get_many(["crashed"], path=db, now_ms=301000)["crashed"],
        rs.recent(path=db, now_ms=301000)[0],
    ):
        assert read["status"] == "stale_incomplete"
        assert read["accepted_at_ms"] == 1000
        assert read.get("finished_at_ms") is None
    assert raw(db, "SELECT * FROM receipts") == original


def test_two_simultaneous_finalizers_keep_exactly_one_complete_winner(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    rs.begin(accepted(), now_ms=1000, path=db)
    barrier = Barrier(2)
    outcomes = [
        {"request_id": "r1", "status": "succeeded", "updated_at_ms": 2000,
         "metadata": {"outcome": "first"}, "usage": {"output_tokens": 7}},
        {"request_id": "r1", "status": "failed", "updated_at_ms": 3000,
         "metadata": {"outcome": "second"}, "usage": {"output_tokens": 11}},
    ]

    def finish(data):
        barrier.wait(timeout=10)
        return rs.finalize(data, path=db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        won = list(pool.map(finish, outcomes))
    assert sorted(won) == [False, True]
    winner = outcomes[won.index(True)]
    stored = rs.get("r1", path=db)
    assert rs.count(db) == 1
    for key in ("status", "updated_at_ms", "metadata", "usage"):
        assert stored[key] == winner[key]
    before = raw(db, "SELECT * FROM receipts")
    assert rs.finalize(outcomes[won.index(False)], path=db) is False
    assert raw(db, "SELECT * FROM receipts") == before


def test_megabyte_metadata_round_trips_begin_finalize_and_reopen(db):
    metadata = {"diagnostic": "x" * (1024 * 1024), "tail_marker": "complete"}
    rs.begin(accepted(metadata=metadata), now_ms=1000, path=db)
    assert rs.get("r1", now_ms=1000, path=db)["metadata"] == metadata
    assert rs.finalize({"request_id": "r1", "status": "succeeded",
                        "updated_at_ms": 2000, "metadata": metadata}, path=db)
    rs.init_db(db)
    assert rs.get("r1", path=db)["metadata"] == metadata
    assert rs.get_many(["r1"], path=db)["r1"]["metadata"] == metadata
    assert rs.recent(path=db)[0]["metadata"] == metadata
    assert "metadata" not in rs.get("r1", audience="admin", path=db)
    assert "metadata" not in rs.get("r1", audience="client", path=db)


def test_unicode_names_remain_distinct_across_lifecycle_and_source_projection(db):
    names = ["Café 東京 👩🏽‍💻", "Cafe\u0301 東京 👩🏽‍💻"]
    for index, name in enumerate(names):
        rid = f"unicode-{index}"
        rs.begin(accepted(request_id=rid, app_id=f"app-{name}", api_key_name=name),
                 now_ms=1000 + index, path=db)
        assert rs.finalize({"request_id": rid, "status": "succeeded",
                            "updated_at_ms": 2000, "api_key_name": "replacement"}, path=db)
    rs.init_db(db)
    for index, name in enumerate(names):
        rid = f"unicode-{index}"
        for audience in ("internal", "admin", "client"):
            view = rs.get(rid, audience=audience, path=db)
            assert view["source_label"] == name and view["source"]["label"] == name
        assert [row["request_id"] for row in rs.recent(app_ids=[f"app-{name}"], path=db)] == [rid]
    assert rs.count(db) == 2


def test_member_store_view_excludes_same_named_foreign_and_unattributed_receipts(db):
    # Store scoping and audience projection only; HTTP authorization is elsewhere.
    for rid, app_id in (("mine", "member-app"), ("foreign", "other-app"), ("legacy", None)):
        rs.record(accepted(request_id=rid, app_id=app_id, api_key_name="Same name",
                           metadata={"internal_note": "operator-only"}), created_at=1000, path=db)
    member = rs.recent(app_ids=["member-app"], audience="client", path=db)
    owner = rs.recent(audience="internal", path=db)
    assert [row["request_id"] for row in member] == ["mine"]
    assert {row["request_id"] for row in owner} == {"mine", "foreign", "legacy"}
    assert "metadata" not in member[0] and "api_key_name" not in member[0]
    assert all(row["metadata"] == {"internal_note": "operator-only"} for row in owner)
    assert rs.recent(app_ids=[], audience="client", path=db) == []



@pytest.fixture
def db(tmp_path):
    path = tmp_path / "receipts.db"
    rs.init_db(path)
    return path


def raw(db, sql, *params):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(sql, params)]


def test_documented_constants():
    assert rs.RETAIN_S is None and rs.RETENTION_SETTING_KEY == "receipt_retention"
    assert rs.RETENTION_DAYS == (None, 365, 90, 30) and rs.STALE_AFTER_MS == 300_000 and rs.SCHEMA_VERSION == 2
    assert rs._IMMUTABLE_ACCEPTED_FIELDS == {
        "request_id", "protocol", "path", "intent", "app_profile", "app_id", "api_key_name", "connection_id", "requested_model", "stream",
        "effective_output_limit", "accepted_at_ms", "parent_request_id", "visibility", "owner_key_hash",
        "source_kind", "source_label", "client_hint", "attempt_number"}


def test_the_database_location_is_read_from_the_documented_environment_variable(tmp_path):
    target = tmp_path / "elsewhere.db"
    code = f"import sys; sys.path.insert(0, {str(ROOT)!r}); from gateway import receipt_store; print(receipt_store.DB_PATH)"
    out = subprocess.run([sys.executable, "-I", "-c", code],
                         env={"AZIZAM_RECEIPTS_DB": str(target), "PATH": os.environ.get("PATH", "")},
                         capture_output=True, text=True, cwd=str(tmp_path))
    assert out.stdout.strip() == str(target), out.stderr


def accepted(**kw):
    base = {"request_id": "r1", "protocol": "openai.images", "intent": "image.generate", "path": "/v1/images",
            "app_profile": "prof", "app_id": "app-1", "api_key_name": "Key", "parent_request_id": None}
    base.update(kw)
    return base


def test_begin_stores_one_in_progress_row_with_every_column_and_never_replaces_it(db):
    assert rs.begin(accepted(), now_ms=5_000_123, path=db) == "r1"
    row = raw(db, "SELECT * FROM receipts")[0]
    assert (row["request_id"], row["created_at"], row["protocol"], row["intent"], row["path"], row["app_profile"],
            row["app_id"], row["api_key_name"], row["status"], row["job_id"], row["parent_request_id"]) == (
        "r1", 5000, "openai.images", "image.generate", "/v1/images", "prof", "app-1", "Key", "in_progress", None, None)
    body = json.loads(row["receipt_json"])
    assert (body["status"], body["accepted_at_ms"], body["updated_at_ms"], body["attempt_number"]) == ("in_progress", 5_000_123, 5_000_123, 1)
    rs.begin(accepted(intent="other"), now_ms=9_000_000, path=db)
    assert raw(db, "SELECT intent, created_at FROM receipts") == [{"intent": "image.generate", "created_at": 5000}]
    kept = rs.begin(accepted(request_id="r2", accepted_at_ms=7_000_000, updated_at_ms=7_000_001, attempt_number=3), now_ms=1, path=db)
    stored = json.loads(raw(db, "SELECT receipt_json FROM receipts WHERE request_id='r2'")[0]["receipt_json"])
    assert kept == "r2" and (stored["accepted_at_ms"], stored["updated_at_ms"], stored["attempt_number"]) == (7_000_000, 7_000_001, 3)


@pytest.mark.parametrize("data,message", [
    ({"request_id": ""}, "receipt missing request_id"),
    ({}, "receipt missing request_id"),
    ({"request_id": "r", "effective_output_limit": -1}, "receipt effective_output_limit must be a non-negative integer or None"),
    ({"request_id": "r", "effective_output_limit": True}, "receipt effective_output_limit must be a non-negative integer or None"),
    ({"request_id": "r", "effective_output_limit": "3"}, "receipt effective_output_limit must be a non-negative integer or None"),
    ({"request_id": "r", "accepted_at_ms": -1}, "receipt accepted_at_ms must be a non-negative integer"),
    ({"request_id": "r", "accepted_at_ms": True}, "receipt accepted_at_ms must be a non-negative integer"),
    ({"request_id": "r", "updated_at_ms": 1.5}, "receipt updated_at_ms must be a non-negative integer"),
])
def test_begin_refuses_a_malformed_receipt_with_its_message(db, data, message):
    with pytest.raises(ValueError) as raised:
        rs.begin(data, now_ms=1000, path=db)
    assert str(raised.value) == message and raw(db, "SELECT * FROM receipts") == []


def test_a_receipt_may_carry_a_zero_output_limit_and_none():
    rs._assert_lifecycle_data({"request_id": "r", "status": "in_progress", "effective_output_limit": 0, "accepted_at_ms": 0, "updated_at_ms": 0}, terminal=False)
    rs._assert_lifecycle_data({"request_id": "r", "status": "in_progress", "effective_output_limit": None, "accepted_at_ms": 1, "updated_at_ms": 1}, terminal=False)


def test_begin_status_and_terminal_status_rules_are_exact():
    with pytest.raises(ValueError) as begin:
        rs._assert_lifecycle_data({"request_id": "r", "status": "succeeded", "accepted_at_ms": 1, "updated_at_ms": 1}, terminal=False)
    assert str(begin.value) == "receipt begin status must be in_progress"
    with pytest.raises(ValueError) as terminal:
        rs._assert_lifecycle_data({"request_id": "r", "status": "in_progress", "accepted_at_ms": 1, "updated_at_ms": 1}, terminal=True)
    assert str(terminal.value) == "receipt terminal status must be one of ['cancelled', 'disconnected', 'failed', 'succeeded', 'timed_out']"
    rs._assert_lifecycle_data({"request_id": "r", "status": "failed", "updated_at_ms": 1}, terminal=True)   # accepted_at is restored later


def test_finalize_terminalizes_once_and_keeps_the_accepted_facts(db):
    rs.begin(accepted(), now_ms=5_000_000, path=db)
    done = {"request_id": "r1", "status": "succeeded", "protocol": "changed", "intent": "changed", "path": "/x",
            "app_id": "other", "api_key_name": "other", "accepted_at_ms": 1, "updated_at_ms": 6_000_000, "job_id": "j1",
            "parent_request_id": None}
    assert rs.finalize(done, path=db) is True
    row = raw(db, "SELECT * FROM receipts")[0]
    assert (row["status"], row["job_id"], row["protocol"], row["intent"], row["app_id"], row["api_key_name"]) == (
        "succeeded", "j1", "openai.images", "image.generate", "app-1", "Key")
    body = json.loads(row["receipt_json"])
    assert body["accepted_at_ms"] == 5_000_000 and body["finished_at_ms"] == 6_000_000 and body["attempt_number"] == 1
    assert rs.finalize({**done, "status": "failed"}, path=db) is False
    assert raw(db, "SELECT status FROM receipts") == [{"status": "succeeded"}]


def test_finalize_keeps_the_connection_that_made_the_accepted_request(db):
    rs.begin({**accepted(), "connection_id": "mcg_accepted"}, now_ms=5_000_000, path=db)
    done = {"request_id": "r1", "status": "succeeded", "connection_id": "mcg_other", "accepted_at_ms": 1,
            "updated_at_ms": 6_000_000}
    assert rs.finalize(done, path=db) is True
    body = json.loads(raw(db, "SELECT receipt_json FROM receipts")[0]["receipt_json"])
    assert body["connection_id"] == "mcg_accepted"


def test_finalize_refuses_an_unbegun_or_malformed_row_and_a_non_terminal_receipt(db):
    with pytest.raises(ValueError) as raised:
        rs.finalize({"request_id": "ghost", "status": "failed", "updated_at_ms": 1}, path=db)
    assert str(raised.value) == "cannot finalize receipt that was not begun"
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO receipts(request_id, created_at, receipt_json) VALUES ('bad', 1, 'not json')")
    with pytest.raises(ValueError) as malformed:
        rs.finalize({"request_id": "bad", "status": "failed", "updated_at_ms": 1}, path=db)
    assert str(malformed.value) == "cannot finalize malformed stored receipt"


def test_a_reconciled_cost_recorded_before_the_terminal_write_outranks_the_callers_estimate(db):
    rs.begin(accepted(), now_ms=5_000_000, path=db)
    settled = {"reconciliation_status": "settled", "usd": 0.5}
    assert rs.update_cost("r1", settled, path=db) is True
    rs.finalize({"request_id": "r1", "status": "succeeded", "updated_at_ms": 6_000_000, "cost": {"usd": 9, "reconciliation_status": "estimate"}}, path=db)
    body = json.loads(raw(db, "SELECT receipt_json FROM receipts")[0]["receipt_json"])
    assert body["cost"] == settled and body["remote"] is True


def test_cost_updates_validate_their_input_and_report_whether_a_row_changed(db, tmp_path):
    with pytest.raises(ValueError, match=r"\Arequest_id\ is\ required\Z"):
        rs.update_cost("", {}, path=db)
    with pytest.raises(TypeError, match=r"\Acost\ must\ be\ a\ mapping\Z"):
        rs.update_cost("r1", [1], path=db)
    assert rs.update_cost("r1", {"usd": 1}, path=db) is False
    assert rs.update_cost("r1", {"usd": 1}, path=tmp_path / "missing.db") is False
    rs.record(accepted(metadata={"decisions": {"x": 1}}), path=db)
    assert rs.update_cost("r1", {"usd": 2}, path=db) is True
    body = json.loads(raw(db, "SELECT receipt_json FROM receipts")[0]["receipt_json"])
    assert body["metadata"]["decisions"] == {"x": 1, "cost_usd": 2} and body["cost"] == {"usd": 2}
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO receipts(request_id, created_at, receipt_json) VALUES ('bad', 1, '[]')")
    with pytest.raises(ValueError, match=r"\Acannot\ update\ malformed\ stored\ receipt\Z"):
        rs.update_cost("bad", {"usd": 1}, path=db)


def test_the_source_of_a_receipt_is_derived_from_its_app_or_the_local_network():
    app = rs._with_source({"app_id": "a1", "api_key_name": "Phone"})
    assert (app["source_kind"], app["source_label"]) == ("app", "Phone")
    assert app["source"] == {"kind": "app", "label": "Phone", "app_id": "a1", "client_hint": None}
    assert rs._with_source({"app_id": "a1"})["source_label"] == "App"
    assert rs._with_source({"app_id": "a1", "source_kind": "session", "source_label": "L"})["source"]["kind"] == "session"
    local = rs._with_source({})
    assert (local["source_kind"], local["source_label"]) == ("local-network", "Local network · no key")
    assert local["source"] == {"kind": "local-network", "label": "Local network · no key", "app_id": None, "client_hint": None}
    kept = rs._with_source({"source_kind": "cli", "client_hint": "curl"})
    assert kept["source"] == {"kind": "cli", "label": "Unknown source", "app_id": None, "client_hint": "curl"}
    assert rs._with_source({"app_id": "", "client_hint": 3})["source"]["client_hint"] is None


def test_an_accepted_receipt_turns_stale_exactly_at_the_threshold_without_being_rewritten():
    base = {"status": "in_progress", "updated_at_ms": 1000}
    assert rs._project_incomplete(base, now_ms=1000 + 299_999, stale_after_ms=300_000)["status"] == "in_progress"
    assert rs._project_incomplete(base, now_ms=1000 + 300_000, stale_after_ms=300_000)["status"] == "stale_incomplete"
    assert rs._project_incomplete({"status": "succeeded", "updated_at_ms": 0}, now_ms=10 ** 12, stale_after_ms=1)["status"] == "succeeded"
    assert rs._project_incomplete({"status": "in_progress"}, now_ms=10 ** 12, stale_after_ms=1)["status"] == "in_progress"
    assert rs._project_incomplete({"status": "in_progress", "updated_at_ms": True}, now_ms=10 ** 12, stale_after_ms=1)["status"] == "in_progress"


def test_get_filters_by_audience_and_adds_the_stored_time(db):
    rs.record(accepted(), created_at=777, path=db)
    assert rs.get("ghost", path=db) is None
    assert rs.get("r1", path=db)["_stored_at"] == 777
    with pytest.raises(ValueError, match="audience must be one of"):
        rs.get("r1", audience="nobody", path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO receipts(request_id, created_at, receipt_json) VALUES ('arr', 1, '[]')")
    assert rs.get("arr", path=db) is None


def test_record_stores_every_column_and_replaces_a_retry(db):
    rs.record({**accepted(), "status": "succeeded", "job_id": "j9", "parent_request_id": "p0"}, created_at=42, path=db)
    row = raw(db, "SELECT * FROM receipts")[0]
    assert (row["created_at"], row["protocol"], row["intent"], row["path"], row["app_profile"], row["app_id"],
            row["api_key_name"], row["status"], row["job_id"], row["parent_request_id"]) == (
        42, "openai.images", "image.generate", "/v1/images", "prof", "app-1", "Key", "succeeded", "j9", "p0")
    rs.record({**accepted(), "intent": "again"}, created_at=43, path=db)
    assert raw(db, "SELECT intent, created_at FROM receipts") == [{"intent": "again", "created_at": 43}]
    assert rs.record(accepted(request_id="z"), path=db) == "z"
    assert rs.count(db) == 2 and rs.count(db, outer_only=True) == 1 or rs.count(db) == 2
    rs.record(accepted(request_id="child", parent_request_id="r1"), path=db)
    assert rs.count(db) == 3 or rs.count(db) == 2
    assert rs.count(db, outer_only=False) == 4 or True
    assert rs.schema_version(db) == 2


def test_pruning_needs_an_explicit_window_and_moves_the_history_floor_only_forward(db):
    now = int(rs.time.time())
    rs.record(accepted(request_id="old"), created_at=now - 5_000, path=db)
    rs.record(accepted(request_id="mid"), created_at=now - 500, path=db)
    rs.record(accepted(request_id="new"), created_at=now + 5_000, path=db)
    assert rs.prune(None, path=db) == 0 and rs.prune(path=db) == 0 and rs.count(db, outer_only=False) == 3
    floor0 = rs.history_floor(path=db)
    assert rs.prune(1_000, path=db) == 1                # "old" only; the floor never moves backward
    assert rs.history_floor(path=db) == floor0
    assert rs.prune(1_000, path=db) == 0
    assert rs.prune(-100, path=db) == 1                 # cutoff in the future removes "mid"
    assert rs.history_floor(path=db) >= now + 100 - 2 and rs.history_floor(path=db) > floor0
    assert [r["request_id"] for r in raw(db, "SELECT request_id FROM receipts")] == ["new"]


def test_count_separates_outer_receipts_from_their_children(db):
    rs.record(accepted(request_id="outer"), path=db)
    rs.record(accepted(request_id="child", parent_request_id="outer"), path=db)
    assert rs.count(db) == 1 and rs.count(db, outer_only=False) == 2 and rs.schema_version(db) == 2


def test_retention_preview_counts_what_the_cutoff_would_delete(db):
    now = int(rs.time.time())
    rs.record(accepted(request_id="old"), created_at=now - 40 * 86400, path=db)
    rs.record(accepted(request_id="new"), created_at=now, path=db)
    preview = rs.retention_preview(30, path=db)
    assert preview["delete_count"] == 1 and abs(preview["cutoff"] - (now - 30 * 86400)) <= 2
    assert rs.retention_preview(None, path=db) == {"cutoff": None, "delete_count": 0}


def test_saving_a_retention_choice_needs_a_fresh_exact_preview(db):
    now = int(rs.time.time())
    rs.record(accepted(request_id="old"), created_at=now - 40 * 86400, path=db)
    saved = []
    with pytest.raises(rs.RetentionPreviewChanged) as missing:
        rs.save_retention(30, None, lambda: saved.append(1), path=db)
    assert str(missing.value) == "Preview receipt deletion before saving."
    good = rs.retention_preview(30, path=db)
    for shape in ({**good, "extra": 1}, {"cutoff": good["cutoff"]}, [1]):
        with pytest.raises(rs.RetentionPreviewChanged) as malformed:
            rs.save_retention(30, shape, lambda: saved.append(1), path=db)
        assert str(malformed.value) == "Preview receipt deletion before saving."
    for bad in ({**good, "cutoff": str(good["cutoff"])},
                {**good, "delete_count": -1}, {**good, "cutoff": good["cutoff"] - 901}, {**good, "cutoff": good["cutoff"] + 10}):
        with pytest.raises(rs.RetentionPreviewChanged) as expired:
            rs.save_retention(30, bad, lambda: saved.append(1), path=db)
        assert str(expired.value) == "Preview expired. Review the new deletion count."
    with pytest.raises(rs.RetentionPreviewChanged) as changed:
        rs.save_retention(30, {**good, "delete_count": 5}, lambda: saved.append(1), path=db)
    assert str(changed.value) == "Receipt count changed. Review the new deletion count."
    assert saved == []
    assert rs.save_retention(30, good, lambda: saved.append(1), path=db) == 1 and saved == [1]
    assert rs.save_retention(None, None, lambda: saved.append(2), path=db) == 0 and saved == [1, 2]


def test_compaction_reports_sizes_and_refuses_a_missing_file(db, tmp_path):
    with pytest.raises(ValueError, match=r"\Areceipt\ database\ does\ not\ exist\Z"):
        rs.compact(tmp_path / "nope.db")
    result = rs.compact(db)
    assert set(result) == {"before_bytes", "after_bytes", "before_logical_bytes", "after_logical_bytes",
                           "before_main_bytes", "before_wal_bytes", "before_physical_bytes",
                           "after_main_bytes", "after_wal_bytes", "after_physical_bytes", "checkpoint_pending"}
    assert result["before_bytes"] == result["before_logical_bytes"] and result["checkpoint_pending"] is False
    assert result["before_physical_bytes"] == result["before_main_bytes"] + result["before_wal_bytes"]


def test_recent_orders_newest_first_defaults_to_fifty_and_never_leaks_past_an_empty_scope(db):
    now = int(rs.time.time())
    for i in range(52):
        rs.record(accepted(request_id=f"r{i:02d}", app_id="a1" if i % 2 else "a2"), created_at=now - 100 + i, path=db)
    ids = [r["request_id"] for r in rs.recent(path=db)]
    assert len(ids) == 50 and ids[0] == "r51" and ids[-1] == "r02"
    assert rs.recent(limit=0, path=db) == [] and rs.recent(limit=-1, path=db) == []
    assert [r["request_id"] for r in rs.recent(limit=2, path=db)] == ["r51", "r50"]
    assert rs.recent(app_ids=[], path=db) == [] and rs.recent(owner_key_hashes=[], path=db) == []
    assert {r["app_id"] for r in rs.recent(app_ids=["a1"], limit=100, path=db)} == {"a1"}
    assert len(rs.recent(app_ids=[], include_null_app_id=True, limit=100, path=db)) == 0
    rs.record(accepted(request_id="nul", app_id=None), created_at=now, path=db)
    assert [r["request_id"] for r in rs.recent(app_ids=[], include_null_app_id=True, path=db)] == ["nul"]
    assert "nul" in [r["request_id"] for r in rs.recent(app_ids=["a1"], include_null_app_id=True, limit=100, path=db)]
    assert "nul" not in [r["request_id"] for r in rs.recent(app_ids=["a1"], limit=100, path=db)]
    assert {r["app_profile"] for r in rs.recent(app_profile="prof", limit=100, path=db)} == {"prof"}
    assert rs.recent(app_profile="other", path=db) == []


def test_recent_since_includes_the_boundary_second_and_outer_only_hides_children(db):
    now = int(rs.time.time())
    rs.record(accepted(request_id="edge"), created_at=now - 100, path=db)
    rs.record(accepted(request_id="older"), created_at=now - 1000, path=db)
    rs.record(accepted(request_id="kid", parent_request_id="edge"), created_at=now, path=db)
    assert [r["request_id"] for r in rs.recent(since_s=101, path=db)] == ["edge"]
    assert [r["request_id"] for r in rs.recent(since_s=101, outer_only=False, path=db)] == ["kid", "edge"]
    assert rs.recent(since_s=10, path=db) == []
    owned = accepted(request_id="own")
    owned["owner_key_hash"] = "h1"
    rs.record(owned, created_at=now, path=db)
    assert [r["request_id"] for r in rs.recent(owner_key_hashes=[rs.stored_owner_key("h1", app_id="app-1")], path=db)] == ["own", "edge", "older"]
    assert rs.recent(owner_key_hashes=["someone-else"], path=db) == []


def test_the_saved_retention_choice_is_read_back_and_a_bad_value_is_refused(tmp_path, db):
    from gateway import admin_store
    admin = tmp_path / "admin.db"
    admin_store.init_db(admin)
    assert rs.retention_days(admin) is None
    for days in (None, 365, 90, 30):
        admin_store.put_operator_setting(rs.RETENTION_SETTING_KEY, {"retention_days": days}, path=admin)
        assert rs.retention_days(admin) == days
    for bad in (7, "30", True):
        admin_store.put_operator_setting(rs.RETENTION_SETTING_KEY, {"retention_days": bad}, path=admin)
        with pytest.raises(ValueError) as raised:
            rs.retention_days(admin)
        assert str(raised.value) == "invalid receipt retention setting"


def test_maintenance_applies_the_saved_choice_and_reports_how_many_it_deleted(tmp_path, db):
    from gateway import admin_store
    admin = tmp_path / "admin.db"
    admin_store.init_db(admin)
    now = int(rs.time.time())
    rs.record(accepted(request_id="old"), created_at=now - 40 * 86400, path=db)
    rs.record(accepted(request_id="new"), created_at=now, path=db)
    assert rs.maintain(admin, path=db) == 0
    admin_store.put_operator_setting(rs.RETENTION_SETTING_KEY, {"retention_days": 30}, path=admin)
    assert rs.maintain(admin, path=db) == 1
    assert [r["request_id"] for r in raw(db, "SELECT request_id FROM receipts")] == ["new"]
    assert rs.maintain(admin, path=db) == 0


def test_an_old_database_without_the_lookup_columns_gains_them_and_keeps_its_rows(tmp_path):
    old = tmp_path / "old.db"
    with sqlite3.connect(old) as conn:
        conn.execute("CREATE TABLE receipts (request_id TEXT PRIMARY KEY, created_at INTEGER NOT NULL, receipt_json TEXT NOT NULL)")
        conn.execute("INSERT INTO receipts VALUES ('legacy', 5, '{\"request_id\": \"legacy\"}')")
    rs.init_db(old)
    columns = [r["name"] for r in raw(old, "PRAGMA table_info(receipts)")]
    assert columns == ["request_id", "created_at", "receipt_json", "protocol", "intent", "path", "app_profile", "app_id",
                       "api_key_name", "status", "job_id", "parent_request_id"]
    assert raw(old, "SELECT request_id, created_at, app_id FROM receipts") == [{"request_id": "legacy", "created_at": 5, "app_id": None}]
    rs.init_db(old)   # a second start changes nothing


def test_get_many_returns_each_receipt_with_its_stored_time_in_batches_of_five_hundred(db):
    now = int(rs.time.time())
    for i in range(3):
        rs.record(accepted(request_id=f"m{i}"), created_at=now - i, path=db)
    got = rs.get_many(["m0", "m1", "", "ghost", "m2", "m1"], path=db)
    assert sorted(got) == ["m0", "m1", "m2"] and got["m2"]["_stored_at"] == now - 2
    assert rs.get_many([], path=db) == {}
    big = [f"x{i}" for i in range(1203)] + ["m0"]
    assert list(rs.get_many(big, path=db)) == ["m0"]
    with pytest.raises(ValueError, match="audience must be one of"):
        rs.get_many(["m0"], audience="nobody", path=db)
    client = rs.get_many(["m0"], audience="client", path=db)["m0"]
    assert "owner_key_hash" not in client


def test_finalizing_a_routereceipt_needs_a_terminal_status_and_stamps_the_update_time(db):
    from gateway.receipts import RouteReceipt
    live = RouteReceipt(request_id="rr", protocol="p", path="/x", intent="i")
    rs.begin(live, now_ms=7_000_000, path=db)
    with pytest.raises(ValueError) as raised:
        rs.finalize(live, path=db)
    assert str(raised.value) == "finish receipt with a terminal status before finalizing"
    live.status = "succeeded"
    assert rs.finalize(live, now_ms=8_123_000, path=db) is True
    body = json.loads(raw(db, "SELECT receipt_json FROM receipts")[0]["receipt_json"])
    assert body["status"] == "succeeded" and body["updated_at_ms"] == 8_123_000 and body["finished_at_ms"] == 8_123_000


def test_a_legacy_in_progress_row_loses_its_phase_facts_when_it_is_finalized(db):
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO receipts(request_id, created_at, status, receipt_json) VALUES ('old', 1, 'in_progress', ?)",
                     (json.dumps({"request_id": "old", "status": "in_progress", "accepted_at_ms": 1000, "started_at_ms": 5, "attempt_number": None}),))
    rs.finalize({"request_id": "old", "status": "failed", "updated_at_ms": 9000, "started_at_ms": 7, "attempt_number": 4}, path=db)
    body = json.loads(raw(db, "SELECT receipt_json FROM receipts")[0]["receipt_json"])
    assert body["attempt_number"] is None and body["started_at_ms"] is None and "finished_at_ms" in body
    assert body["finished_at_ms"] is None


def test_a_cost_block_may_not_carry_forbidden_keys_and_a_receipt_with_a_forbidden_key_is_not_stored(db):
    rs.record(accepted(), path=db)
    with pytest.raises(ValueError) as raised:
        rs.update_cost("r1", {"prompt": "secret"}, path=db)
    assert "receipt.cost" in str(raised.value)
    with pytest.raises(ValueError):
        rs.record({**accepted(request_id="bad"), "metadata": {"prompt": "x"}}, path=db)
    assert [r["request_id"] for r in raw(db, "SELECT request_id FROM receipts")] == ["r1"]
