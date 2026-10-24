"""The activity feed reads a bounded number of receipts per request (OV3 slice 1).

Candidates are read newest first in batches, authorized as they arrive, and a request stops at its page size or at
``USER_ACTIVITY_SCAN_LIMIT`` rows read. A cursor resumes after the last row read, with no gap and no repeat.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from gateway import admin_store, receipt_store, user_runtime
from gateway.admin_oidc import AdminSession

N_ROWS = 2400
READABLE_EVERY = 400  # with the newest row first, rows 0, 400, ... are the person's own; the rest are a member's


@pytest.fixture
def feed(tmp_path: Path, monkeypatch):
    admin_path, receipts_path = tmp_path / "admin.db", tmp_path / "receipts.db"
    admin_store.init_db(admin_path)
    receipt_store.init_db(receipts_path)
    alice = admin_store.upsert_oidc_user("https://people.test", "alice", path=admin_path, email="alice@example.test")
    bob = admin_store.upsert_oidc_user("https://people.test", "bob", path=admin_path, email="bob@example.test")
    alice_app = admin_store.create_app("Desktop", kind="personal", owner_user_id=alice["id"],
                                       access_plan="person", path=admin_path)["id"]
    session = AdminSession(subject=alice["id"], email=alice["email"], role="none", issued_at=1, expires_at=4_000_000_000)
    rows = []
    readable: list[str] = []
    for index in range(N_ROWS):
        request_id = f"req_{index:05d}"
        maker = alice["id"] if index % READABLE_EVERY == 0 else bob["id"]
        if maker == alice["id"]:
            readable.append(request_id)
        receipt = {"request_id": request_id, "status": "succeeded", "intent": "chat.completions",
                   "app_id": alice_app, "maker_person_id": maker}
        rows.append((request_id, 1_000_000 + N_ROWS - index, "openai.chat", "chat.completions", "/v1/chat/completions",
                     None, alice_app, None, "succeeded", None, None, json.dumps(receipt, sort_keys=True)))
    with closing(sqlite3.connect(receipts_path)) as conn:
        conn.executemany(
            "INSERT INTO receipts (request_id, created_at, protocol, intent, path, app_profile, app_id, api_key_name, "
            "status, job_id, parent_request_id, receipt_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        conn.commit()

    scanned: list[int] = []
    read = receipt_store.recent

    def counting_recent(*args: Any, **kwargs: Any):
        found = read(*args, **kwargs)
        scanned.append(len(found))
        return found

    monkeypatch.setattr(receipt_store, "recent", counting_recent)

    def page(limit: int = 5, before: tuple[int, str] | None = None) -> dict[str, Any]:
        scanned.clear()
        snapshot = user_runtime.build_user_activity_snapshot(
            session, limit=limit, path=admin_path, receipts_path=receipts_path, before=before)
        snapshot["_scanned"] = sum(scanned)
        return snapshot

    return page, readable


def _cursor_key(snapshot: dict[str, Any]) -> tuple[int, str] | None:
    raw = snapshot["next_cursor"]
    return user_runtime.decode_activity_cursor(raw) if raw else None


def test_a_mostly_unreadable_table_stays_within_the_scan_limit_and_the_page_holds_only_readable_rows(feed):
    page, readable = feed
    first = page(limit=5)

    assert first["_scanned"] <= user_runtime.USER_ACTIVITY_SCAN_LIMIT
    assert [row["request_id"] for row in first["receipts"]] == readable[:3]
    assert first["has_more"] is True and first["next_cursor"]


def test_following_cursors_returns_every_readable_row_once_in_order(feed):
    page, readable = feed
    seen: list[str] = []
    cursor = None
    for _ in range(N_ROWS):
        snapshot = page(limit=5, before=cursor)
        assert snapshot["_scanned"] <= user_runtime.USER_ACTIVITY_SCAN_LIMIT
        seen.extend(row["request_id"] for row in snapshot["receipts"])
        if not snapshot["has_more"]:
            break
        cursor = _cursor_key(snapshot)
    else:
        pytest.fail("the feed never finished")

    assert seen == readable


def test_a_page_of_readable_rows_stops_at_its_size_and_resumes_after_its_last_row(feed):
    page, readable = feed
    first = page(limit=2)
    assert [row["request_id"] for row in first["receipts"]] == readable[:2]
    assert first["has_more"] is True

    second = page(limit=2, before=_cursor_key(first))
    assert [row["request_id"] for row in second["receipts"]] == readable[2:4]


def test_a_malformed_cursor_reads_as_the_first_page():
    assert user_runtime.decode_activity_cursor("not-a-cursor") is None
    assert user_runtime.decode_activity_cursor(None) is None
    assert user_runtime.decode_activity_cursor(user_runtime.encode_activity_cursor((7, "req_x"))) == (7, "req_x")
