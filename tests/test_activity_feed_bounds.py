"""The activity feed reads a bounded number of receipts per request (OV3 slice 1).

Candidates are read newest first in batches, authorized as they arrive, and a request stops at its page size or at
``USER_ACTIVITY_SCAN_LIMIT`` rows read. A cursor resumes after the last row read, with no gap and no repeat. The cursor
is sealed to the person it was issued to, so every refused cursor gets the same 400.
"""
from __future__ import annotations

import base64
import json
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gateway import admin_store, receipt_store, user_runtime
from gateway.admin_oidc import AdminSession

N_ROWS = 2400
READABLE_EVERY = 400  # with the newest row first, rows 0, 400, ... are the person's own; the rest are a member's
REQUEST_IDS = [f"req_{index:05d}" for index in range(N_ROWS)]


@pytest.fixture
def feed(tmp_path: Path, monkeypatch):
    admin_path, receipts_path = tmp_path / "admin.db", tmp_path / "receipts.db"
    admin_store.init_db(admin_path)
    receipt_store.init_db(receipts_path)
    alice = admin_store.upsert_oidc_user("https://people.test", "alice", path=admin_path, email="alice@example.test")
    bob = admin_store.upsert_oidc_user("https://people.test", "bob", path=admin_path, email="bob@example.test")
    alice_app = admin_store.create_app("Desktop", kind="personal", owner_user_id=alice["id"],
                                       access_plan="person", path=admin_path)["id"]
    sessions = {
        person: AdminSession(subject=user["id"], email=user["email"], role="none", issued_at=1, expires_at=4_000_000_000)
        for person, user in (("alice", alice), ("bob", bob))
    }
    rows = []
    readable: list[str] = []
    for index in range(N_ROWS):
        request_id = REQUEST_IDS[index]
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

    def page(limit: int = 5, cursor: str | None = None, viewer: str = "alice") -> dict[str, Any]:
        scanned.clear()
        snapshot = user_runtime.build_user_activity_snapshot(
            sessions[viewer], limit=limit, path=admin_path, receipts_path=receipts_path, cursor=cursor)
        snapshot["_scanned"] = sum(scanned)
        return snapshot

    return page, readable


def _clear_text(cursor: str) -> bytes:
    return base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))


def _refusal(call) -> user_runtime.UserPortalError:
    with pytest.raises(user_runtime.UserPortalError) as refused:
        call()
    return refused.value


def _tampered(cursor: str) -> str:
    return cursor[:10] + ("A" if cursor[10] != "A" else "B") + cursor[11:]


def test_a_mostly_unreadable_table_stays_within_the_scan_limit_and_the_page_holds_only_readable_rows(feed):
    page, readable = feed
    first = page(limit=5)

    assert first["_scanned"] <= user_runtime.USER_ACTIVITY_SCAN_LIMIT
    assert [row["request_id"] for row in first["receipts"]] == readable[:3]
    assert first["has_more"] is True and first["next_cursor"]


def test_the_next_cursor_names_no_receipt_in_clear_text(feed):
    # This page stops at the scan limit, so its cursor is the position of a row the person may not read.
    page, _ = feed
    raw = _clear_text(page(limit=5)["next_cursor"])

    assert not any(request_id.encode() in raw for request_id in REQUEST_IDS)
    assert not any(str(1_000_000 + N_ROWS - index).encode() in raw for index in range(N_ROWS))
    with pytest.raises(ValueError):
        json.loads(raw)


@pytest.mark.parametrize("limit", [2, 5, 10])
def test_following_cursors_returns_every_readable_row_once_in_order(feed, limit):
    page, readable = feed
    seen: list[str] = []
    cursor = None
    for _ in range(N_ROWS):
        snapshot = page(limit=limit, cursor=cursor)
        assert snapshot["_scanned"] <= user_runtime.USER_ACTIVITY_SCAN_LIMIT
        seen.extend(row["request_id"] for row in snapshot["receipts"])
        if not snapshot["has_more"]:
            break
        cursor = snapshot["next_cursor"]
    else:
        pytest.fail("the feed never finished")

    assert seen == readable


def test_a_page_of_readable_rows_stops_at_its_size_and_resumes_after_its_last_row(feed):
    page, readable = feed
    first = page(limit=2)
    assert [row["request_id"] for row in first["receipts"]] == readable[:2]
    assert first["has_more"] is True

    second = page(limit=2, cursor=first["next_cursor"])
    assert [row["request_id"] for row in second["receipts"]] == readable[2:4]


def test_a_tampered_cursor_is_refused_with_a_400(feed):
    page, _ = feed
    cursor = page(limit=5)["next_cursor"]

    refusal = _refusal(lambda: page(cursor=_tampered(cursor)))

    assert (refusal.status_code, refusal.code) == (400, "invalid_cursor")


def test_another_persons_cursor_is_refused_with_a_400(feed):
    page, _ = feed
    cursor = page(limit=5)["next_cursor"]

    refusal = _refusal(lambda: page(cursor=cursor, viewer="bob"))

    assert (refusal.status_code, refusal.code) == (400, "invalid_cursor")


def test_an_expired_cursor_is_refused_with_a_400(feed, monkeypatch):
    page, _ = feed
    cursor = page(limit=5)["next_cursor"]
    # Capture the real clock first: the patched clock must not call itself.
    later = user_runtime._activity_now() + user_runtime.ACTIVITY_CURSOR_TTL_SECONDS + 2
    monkeypatch.setattr(user_runtime, "_activity_now", lambda: later)

    refusal = _refusal(lambda: page(cursor=cursor))

    assert (refusal.status_code, refusal.code) == (400, "invalid_cursor")


@pytest.mark.parametrize("cursor", ["not-a-cursor", "!!!!", "A", "x" * 2000])
def test_a_malformed_cursor_is_refused_with_a_400(feed, cursor):
    page, _ = feed

    refusal = _refusal(lambda: page(cursor=cursor))

    assert (refusal.status_code, refusal.code) == (400, "invalid_cursor")


def test_every_way_a_cursor_is_refused_gives_the_same_400(feed, monkeypatch):
    page, _ = feed
    cursor = page(limit=5)["next_cursor"]
    refusals = [
        _refusal(lambda: page(cursor=_tampered(cursor))),
        _refusal(lambda: page(cursor=cursor, viewer="bob")),
        _refusal(lambda: page(cursor="not-a-cursor")),
    ]
    later = user_runtime._activity_now() + user_runtime.ACTIVITY_CURSOR_TTL_SECONDS + 2
    monkeypatch.setattr(user_runtime, "_activity_now", lambda: later)
    refusals.append(_refusal(lambda: page(cursor=cursor)))

    assert {(refusal.status_code, refusal.code, refusal.message) for refusal in refusals} == {
        (400, "invalid_cursor", refusals[0].message)}


@pytest.fixture
def viewer(tmp_path: Path):
    admin_path, receipts_path = tmp_path / "admin.db", tmp_path / "receipts.db"
    admin_store.init_db(admin_path)
    receipt_store.init_db(receipts_path)
    alice = admin_store.upsert_oidc_user("https://people.test", "alice", path=admin_path, email="alice@example.test")
    session = AdminSession(subject=alice["id"], email=alice["email"], role="none", issued_at=1, expires_at=4_000_000_000)
    return session, alice["id"], admin_path, receipts_path


def _sealed(admin_path: Path, viewer_id: str, body: str) -> str:
    """A cursor sealed under the server's own key, so only the field checks can refuse it."""
    nonce = secrets.token_bytes(12)
    sealed = AESGCM(user_runtime._activity_cursor_key(admin_path)).encrypt(
        nonce, body.encode("utf-8"), user_runtime._activity_cursor_aad(viewer_id))
    return base64.urlsafe_b64encode(nonce + sealed).decode("ascii").rstrip("=")


def _body(*, created_at: str = "1000", request_id: str = '"req_x"', exp: str | None = None) -> str:
    """A cursor's plaintext as JSON text, each field written as the raw JSON it should carry."""
    expires = exp or str(user_runtime._activity_now() + 600)
    return f'{{"created_at":{created_at},"exp":{expires},"request_id":{request_id}}}'


def _snapshot_for(viewer, cursor: str) -> dict[str, Any]:
    session, _, admin_path, receipts_path = viewer
    return user_runtime.build_user_activity_snapshot(session, path=admin_path, receipts_path=receipts_path, cursor=cursor)


@pytest.mark.parametrize("fields", [
    {"created_at": "1e999"},  # JSON reads this as infinity
    {"created_at": "NaN"},
    {"created_at": str(2**63)},  # one past the largest SQLite integer
    {"created_at": str(-(2**63) - 1)},  # one past the smallest
    {"created_at": '"1000"'},  # a string where an integer belongs
    {"created_at": "true"},
    {"exp": "1e999"},
    {"exp": str(2**63)},
    {"request_id": '"' + "r" * 129 + '"'},  # one past the id bound
    {"request_id": '""'},
    {"request_id": "42"},
])
def test_a_sealed_cursor_with_an_out_of_range_field_is_refused_with_a_400(viewer, fields):
    _, viewer_id, admin_path, _ = viewer

    refusal = _refusal(lambda: _snapshot_for(viewer, _sealed(admin_path, viewer_id, _body(**fields))))

    assert (refusal.status_code, refusal.code) == (400, "invalid_cursor")


def test_a_sealed_cursor_with_an_extra_field_is_refused_with_a_400(viewer):
    _, viewer_id, admin_path, _ = viewer
    body = _body()[:-1] + ',"extra":1}'

    refusal = _refusal(lambda: _snapshot_for(viewer, _sealed(admin_path, viewer_id, body)))

    assert (refusal.status_code, refusal.code) == (400, "invalid_cursor")


@pytest.mark.parametrize("fields", [
    {"created_at": str(2**63 - 1)},
    {"created_at": str(-(2**63))},
    {"request_id": '"' + "r" * 128 + '"'},
])
def test_a_sealed_cursor_at_the_edge_of_its_bounds_is_read_as_a_position(viewer, fields):
    _, viewer_id, admin_path, _ = viewer

    snapshot = _snapshot_for(viewer, _sealed(admin_path, viewer_id, _body(**fields)))

    assert snapshot["authenticated"] is True and snapshot["receipts"] == []
