"""Retention slice 5a review fixes that the reviewer's probes do not cover (REQUEST_RETENTION.md 3.4-3.6, 10, 12).

Each test here fails on the slice as reviewed and passes once its fix lands. The probes in
test_review_retention_s5a_probes.py cover the other findings; these cover the rest of the fix list.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import sqlite3
import time

import pytest

import proxy
from gateway import receipt_store, request_history, request_history_capture, retention
from gateway.admin_oidc import AdminSession
from gateway.request_history_capture import IDENTITY_STATE_KEY, RequestHistoryMiddleware
from starlette.responses import JSONResponse
from tests.support import gateway as gateway_support
from tests.support.gateway import gateway_client
from tests.test_receipt_detail_context import CHAT_RECEIPT
from tests.test_request_history_routes import NOW, _call, _record, _rows, _store  # noqa: F401 - helpers
from tests.test_request_history_routes import gateway  # noqa: F401 - fixture
from tests.test_review_retention_s5a_probes import (  # noqa: F401 - fixture
    BOUNDARY, CANARY, SENTENCE, _drive, _identity, _multipart, member_db,
)

RECEIPT_ID = "req_s5a_review_limit"
CSRF = {"x-azizam-user-action": "1", "x-azizam-csrf": "csrf-for-test"}


def _run(db, media, person, messages, *, content_type, headers=(), route_reads=None, record_receipt=False):
    """The capture around a route that answers 413. ``messages`` are the body's receive messages, in order; after
    them, and after the response is complete, ``receive`` answers ``http.disconnect`` (as uvicorn does)."""
    state = {"index": 0, "response_complete": False}

    async def route(scope, receive, send):
        scope["state"][IDENTITY_STATE_KEY] = _identity(person)
        if record_receipt:
            receipt_store.record({**CHAT_RECEIPT, "request_id": RECEIPT_ID, "maker_person_id": person,
                                  "path": scope["path"]})
        reads = 0
        while route_reads is None or reads < route_reads:
            message = await receive()
            reads += 1
            if message["type"] != "http.request" or not message.get("more_body", False):
                break
        await JSONResponse({"error": {"message": "refused early"}}, status_code=413)(scope, receive, send)

    async def receive():
        if state["response_complete"] or state["index"] >= len(messages):
            return {"type": "http.disconnect"}
        message = messages[state["index"]]
        state["index"] += 1
        return message

    async def send(message):
        if message["type"] == "http.response.body" and not message.get("more_body", False):
            state["response_complete"] = True

    middleware = RequestHistoryMiddleware(route, admin_path=lambda: db, media_path=lambda: media)
    scope = {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions",
             "headers": [(b"content-type", content_type.encode()), *headers], "query_string": b"", "state": {}}
    asyncio.run(middleware(scope, receive, send))


def _kept(media) -> list[str]:
    return [row[0] for row in _rows(media, "SELECT parts_json FROM kept_requests")] if _has_table(media) else []


def _has_table(media) -> bool:
    return bool(_rows(media, "SELECT 1 FROM sqlite_master WHERE name='kept_requests'"))


# ----------------------------------------------------------------- the list reads metadata only (3.6, 4)


def test_the_list_reads_no_body_so_a_body_it_cannot_parse_does_not_break_the_page(gateway):
    record = _record(gateway["maker"])
    _store(gateway, record)
    conn = sqlite3.connect(gateway["media"])
    conn.execute("UPDATE kept_requests SET parts_json='not json'")
    conn.commit()
    conn.close()
    response = _call("GET", "/gateway/v1/requests", gateway["maker"].key)
    assert response.status_code == 200, response.text
    assert [item["record_id"] for item in response.json()["requests"]] == [record["record_id"]]


# ----------------------------------------------------------------- the per-person byte cap (8)


def test_the_byte_cap_expires_the_oldest_rows_first(gateway, monkeypatch):
    maker = gateway["maker"]
    records = [_record(maker, created=NOW - index) for index in range(4)]
    _store(gateway, *records)
    size = _rows(gateway["media"], "SELECT byte_size FROM kept_requests")[0][0]
    monkeypatch.setattr(request_history, "byte_cap", lambda: 2 * size + 1)
    request_history.maintain(admin_path=gateway["db"], path=gateway["media"], now=NOW)
    kept = {row[0] for row in _rows(gateway["media"], "SELECT record_id FROM kept_requests")}
    assert kept == {records[0]["record_id"], records[1]["record_id"]}


def test_the_byte_cap_is_counted_per_person(gateway, monkeypatch):
    mine = [_record(gateway["maker"], created=NOW - index) for index in range(2)]
    theirs = [_record(gateway["other"], created=NOW - index) for index in range(2)]
    _store(gateway, *mine, *theirs)
    size = _rows(gateway["media"], "SELECT byte_size FROM kept_requests")[0][0]
    monkeypatch.setattr(request_history, "byte_cap", lambda: size + 1)
    request_history.maintain(admin_path=gateway["db"], path=gateway["media"], now=NOW)
    kept = {row[0] for row in _rows(gateway["media"], "SELECT record_id FROM kept_requests")}
    assert kept == {mine[0]["record_id"], theirs[0]["record_id"]}


def test_an_existing_kept_requests_table_gains_the_metadata_columns(tmp_path):
    media = tmp_path / "media_jobs.db"
    conn = sqlite3.connect(media)
    conn.execute(
        "CREATE TABLE kept_requests (record_id TEXT PRIMARY KEY, request_id TEXT, kind TEXT NOT NULL, "
        "person_id TEXT NOT NULL, app_id TEXT, origin_kind TEXT NOT NULL, origin_id TEXT, engine_id TEXT, "
        "debug_window_id TEXT, created_at INTEGER NOT NULL, kept_until INTEGER, parts_json TEXT NOT NULL, "
        "omissions_json TEXT NOT NULL, policy_version INTEGER NOT NULL, linked_asset_ids TEXT NOT NULL DEFAULT '[]', "
        "fact_revisions TEXT NOT NULL DEFAULT '[]', summary_json TEXT NOT NULL)")
    conn.execute("INSERT INTO kept_requests(record_id, kind, person_id, origin_kind, created_at, parts_json, "
                 "omissions_json, policy_version, summary_json) VALUES ('kr_old', 'request_history', 'p', "
                 "'app_key', 1, '{\"accepted\": {}}', '[]', 1, '{}')")
    conn.commit()
    conn.close()
    request_history.connect(media).close()
    columns = {row[1] for row in _rows(media, "PRAGMA table_info(kept_requests)")}
    assert {"parts_kept", "byte_size"} <= columns
    assert _rows(media, "SELECT parts_kept FROM kept_requests WHERE record_id='kr_old'") == [('["accepted"]',)]


# ----------------------------------------------------------------- the writer and the lock (7, 12)


def test_request_history_writes_go_through_one_dedicated_worker_thread():
    assert isinstance(request_history_capture.WRITER, concurrent.futures.ThreadPoolExecutor)
    assert request_history_capture.WRITER._max_workers == 1


# ----------------------------------------------------------------- the maker's browser session (10)


def _session_for(person: str) -> AdminSession:
    return AdminSession(subject=person, email=f"{person}@example.test", role="none",
                        issued_at=time.time(), expires_at=time.time() + 3600)


@pytest.fixture
def browser(monkeypatch):
    """Requests that carry no App key and the cookie-session of ``sign_in``'s person."""
    monkeypatch.setitem(gateway_support._APP_KEY, "key", "")
    monkeypatch.setattr(proxy, "_csrf_token_for_oidc_session", lambda session: "csrf-for-test")

    def sign_in(person: str) -> None:
        monkeypatch.setattr(proxy, "_oidc_session_from_request", lambda request: _session_for(person))

    return sign_in


def _browser(method: str, path: str, *, headers: dict | None = None):
    async def run():
        async with gateway_client() as client:
            return await client.request(method, path, headers=headers or {})
    return asyncio.run(run())


def test_a_browser_session_reads_and_deletes_its_own_history_and_nobody_elses(gateway, browser):
    maker, other = gateway["maker"], gateway["other"]
    mine, theirs = _record(maker), _record(other)
    _store(gateway, mine, theirs)
    browser(maker.person)
    listing = _browser("GET", "/gateway/v1/requests")
    assert listing.status_code == 200, listing.text
    assert [item["record_id"] for item in listing.json()["requests"]] == [mine["record_id"]]
    assert _browser("GET", f"/gateway/v1/requests/{theirs['record_id']}").status_code == 404
    assert _browser("GET", f"/gateway/v1/requests/{mine['record_id']}").status_code == 200
    assert _browser("DELETE", f"/gateway/v1/requests/{theirs['record_id']}", headers=CSRF).status_code == 404
    refused = _browser("DELETE", f"/gateway/v1/requests/{mine['record_id']}")
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "user_action_header_required"
    assert _browser("DELETE", f"/gateway/v1/requests/{mine['record_id']}", headers=CSRF).status_code == 204
    assert _browser("GET", f"/gateway/v1/requests/{mine['record_id']}").status_code == 404


def test_a_key_sent_with_a_session_cookie_is_judged_as_the_key(gateway, browser):
    """A key in any form the key layer reads keeps the call on the key's view, even when a session cookie is present."""
    maker, other = gateway["maker"], gateway["other"]
    mine, theirs = _record(maker), _record(other)
    _store(gateway, mine, theirs)
    browser(maker.person)

    async def run():
        async with gateway_client() as client:
            return await client.request("GET", "/gateway/v1/requests", headers={"x-api-key": other.key})

    listing = asyncio.run(run())
    assert listing.status_code == 200, listing.text
    assert [item["record_id"] for item in listing.json()["requests"]] == [theirs["record_id"]]


def test_delete_all_under_admin_lock_contention_is_503_with_retry_after_and_deletes_nothing(gateway, browser):
    maker = gateway["maker"]
    _store(gateway, _record(maker))
    browser(maker.person)
    holder = sqlite3.connect(gateway["db"], isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        response = _browser("DELETE", "/gateway/v1/requests", headers=CSRF)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "retry_required"
    assert response.headers.get("retry-after")
    assert _rows(gateway["media"], "SELECT COUNT(*) FROM kept_requests")[0][0] == 1


# ----------------------------------------------------------------- the body and its control (2, 3, 5)


def test_a_json_body_cut_short_by_a_disconnect_is_not_replayed_as_complete(member_db):
    db, media, person = member_db
    body = json.dumps({"model": "m", "input": "x" * 200, "extensions": {"com.azizam.prompts": {"mode": "forget"}}}).encode()
    _run(db, media, person, [{"type": "http.request", "body": body[:40], "more_body": True}],
         content_type="application/json", headers=[(b"content-length", str(len(body)).encode())])
    assert _kept(media) == []


def test_a_multipart_extensions_field_that_is_not_json_is_a_forget(member_db):
    db, media, person = member_db
    raw = _multipart([("prompt", SENTENCE, None), ("extensions", "{not json", None)])
    _drive(db, media, person, chunks=[raw], content_type=f"multipart/form-data; boundary={BOUNDARY}")
    assert _kept(media) == []


def test_a_multipart_extensions_field_sent_as_a_file_is_a_forget(member_db):
    db, media, person = member_db
    raw = _multipart([("prompt", SENTENCE, None),
                      ("extensions", b'{"com.azizam.prompts": {"mode": "keep"}}', "extensions.json")])
    _drive(db, media, person, chunks=[raw], content_type=f"multipart/form-data; boundary={BOUNDARY}")
    assert _kept(media) == []


def test_an_over_limit_body_leaves_no_row_and_the_receipt_says_incomplete_not_kept(member_db):
    db, media, person = member_db
    body = json.dumps({"model": "m", "input": "x" * (1 << 20)}).encode()
    _run(db, media, person, [{"type": "http.request", "body": body, "more_body": False}],
         content_type="application/json", record_receipt=True)
    assert _kept(media) == []
    receipt = receipt_store.get(RECEIPT_ID, audience="client", viewer_person_id=person)
    assert receipt["retention"] == [{"kind": "request_history", "decision": "not_kept", "reason": "incomplete"}]


# ----------------------------------------------------------------- the secret floor's name and text rules (1, 6, 9)


def test_a_bracketed_json_key_is_a_credential_name_like_a_bracketed_form_field():
    kept, found = request_history.redact_json({"headers[Authorization]": CANARY, "provider[api_key]": CANARY,
                                               "max_tokens": 5})
    assert CANARY not in json.dumps(kept)
    assert kept["max_tokens"] == 5
    assert "credential_field" in found


def test_a_signed_url_followed_by_punctuation_loses_its_query_and_the_punctuation_with_it():
    """A query runs to the next space, so a full stop written straight after it is part of the query and goes with it."""
    text = f"see https://bucket.example.test/a.png?X-Amz-Signature={CANARY}."
    kept, _ = request_history.redact_json({"content": text})
    assert CANARY not in json.dumps(kept)
    assert kept["content"] == "see https://bucket.example.test/a.png?[removed]"


def test_tool_call_arguments_that_are_not_json_are_cleaned_as_text():
    args = f"not json, fetch https://api.example.test/v1?token={CANARY}"
    body = {"messages": [{"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "fetch", "arguments": args}}]}]}
    kept, _ = request_history.redact_json(body)
    assert CANARY not in json.dumps(kept)


def test_a_decision_that_cannot_be_read_keeps_nothing_and_the_route_still_answers(member_db, monkeypatch):
    """Capture never changes what the caller receives: an unreadable member choice keeps nothing, and the reply
    the route sent still goes out."""
    db, media, person = member_db

    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(retention, "member_choice", unreadable)
    sent = _drive(db, media, person, chunks=[b'{"model":"m","input":"x"}'], content_type="application/json")
    assert _kept(media) == []
    assert sent[0]["status"] == 413
