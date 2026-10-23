"""Round-3 fixes for request history slice 5a (docs/design/REQUEST_RETENTION.md, slice 5a review fixes).

Each test names the round-2 finding it pins (R2-n). The round-2 probes are in ``tests/test_review_retention_s5a_r2_probes.py``;
these tests cover what the probes do not.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from urllib.parse import urlencode

from gateway import request_history, request_history_capture
from gateway.request_history_capture import IDENTITY_STATE_KEY, RequestHistoryMiddleware
from tests.test_review_retention_s5a_probes import SENTENCE, _drive, _identity, _kept, member_db  # noqa: F401 - fixture

FORGET = {"com.azizam.prompts": {"mode": "forget"}}
FORGET_FIELD = "extensions[com.azizam.prompts][mode]"


# ------------------------------------------------------------------ R2-1: the control in a body of another media type


def test_r2_1_a_form_body_with_a_forget_field_keeps_no_row(member_db):
    db, media, person = member_db
    raw = urlencode({"model": "m", "input": SENTENCE, FORGET_FIELD: "forget"}).encode()
    _drive(db, media, person, chunks=[raw], content_type="application/x-www-form-urlencoded")
    assert _kept(media) == []


def test_r2_1_a_form_body_whose_extensions_field_holds_a_json_forget_keeps_no_row(member_db):
    db, media, person = member_db
    raw = urlencode({"model": "m", "input": SENTENCE, "extensions": json.dumps(FORGET)}).encode()
    _drive(db, media, person, chunks=[raw], content_type="application/x-www-form-urlencoded")
    assert _kept(media) == []


def test_r2_1_a_form_field_with_an_invalid_mode_is_a_forget_and_keeps_no_row(member_db):
    db, media, person = member_db
    raw = urlencode({"model": "m", "input": SENTENCE, FORGET_FIELD: "keep"}).encode()
    _drive(db, media, person, chunks=[raw], content_type="application/x-www-form-urlencoded")
    assert _kept(media) == []


def test_r2_1_a_body_with_no_content_type_that_carries_a_json_forget_keeps_no_row(member_db):
    db, media, person = member_db
    raw = json.dumps({"model": "m", "input": SENTENCE, "extensions": FORGET}).encode()
    _drive(db, media, person, chunks=[raw], content_type="")
    assert _kept(media) == []


def test_r2_1_a_body_over_the_record_limit_in_another_container_leaves_no_row(member_db):
    """Its control cannot be read in full, so nothing is kept on it (REQUEST_RETENTION.md, unreadable control)."""
    db, media, person = member_db
    raw = b"x" * (1024 * 1024 + 1)
    _drive(db, media, person, chunks=[raw], content_type="application/octet-stream")
    assert _kept(media) == []


def test_r2_1_control_a_form_body_without_a_control_is_kept_with_its_body_unsupported(member_db):
    db, media, person = member_db
    raw = urlencode({"model": "m", "input": SENTENCE}).encode()
    _drive(db, media, person, chunks=[raw], content_type="application/x-www-form-urlencoded")
    rows = _kept(media)
    assert len(rows) == 1 and SENTENCE not in rows[0] and "unsupported_container" in rows[0]


def test_r2_1_control_a_text_plain_json_body_without_a_forget_is_kept_with_its_body_unsupported(member_db):
    """A body the route parses is still not kept as content (rule 7). Only its control is read."""
    db, media, person = member_db
    raw = json.dumps({"model": "m", "input": SENTENCE}).encode()
    _drive(db, media, person, chunks=[raw], content_type="text/plain")
    rows = _kept(media)
    assert len(rows) == 1 and SENTENCE not in rows[0] and "unsupported_container" in rows[0]


# ------------------------------------------------------------------ R2-4: a slot is held only by a request that keeps


def _free_slots() -> int:
    """How many capture admission slots are free now. Takes each one and gives it back."""
    taken = 0
    while request_history_capture._PENDING.acquire(blocking=False):
        taken += 1
    for _ in range(taken):
        request_history_capture._PENDING.release()
    return taken


def test_r2_4_a_kept_request_gives_its_slot_back_once_its_write_is_done(member_db):
    db, media, person = member_db
    _drive(db, media, person, chunks=[b'{"model":"m","input":"x"}'], content_type="application/json")
    assert len(_kept(media)) == 1
    assert _free_slots() == request_history_capture.MAX_PENDING_CAPTURES


def test_r2_4_b_a_decision_that_cannot_be_read_takes_no_slot(member_db, monkeypatch):
    db, media, person = member_db

    def unreadable(self, identity, *, forget, now):
        raise RuntimeError("the decision could not be read")

    monkeypatch.setattr(request_history_capture.RequestHistoryMiddleware, "decide", unreadable)
    _drive(db, media, person, chunks=[b'{"model":"m","input":"x"}'], content_type="application/json")
    assert _kept(media) == []
    assert _free_slots() == request_history_capture.MAX_PENDING_CAPTURES


def test_r2_4_c_two_tasks_that_ask_for_the_decision_make_one_and_take_one_slot(member_db, monkeypatch):
    db, media, person = member_db
    made: list[int] = []
    real_decide = request_history_capture.RequestHistoryMiddleware.decide

    def counting(self, identity, *, forget, now):
        made.append(1)
        return real_decide(self, identity, forget=forget, now=now)

    monkeypatch.setattr(request_history_capture.RequestHistoryMiddleware, "decide", counting)
    owner = request_history_capture.RequestHistoryMiddleware(None, admin_path=lambda: db, media_path=lambda: media)
    capture = request_history_capture._Capture(owner, state={IDENTITY_STATE_KEY: _identity(person)}, headers={},
                                               header_forget=False, limit=1 << 20, route="/v1/responses")

    async def ask_twice():
        await asyncio.gather(capture.decide_once(), capture.decide_once())

    asyncio.run(ask_twice())
    try:
        assert len(made) == 1 and capture.admitted
        assert _free_slots() == request_history_capture.MAX_PENDING_CAPTURES - 1
    finally:
        request_history_capture._PENDING.release()


# ------------------------------------------------------------------ R2-6: the parts are the member's choice at write time


def _answer_during(db, media, person, during, reply: dict) -> None:
    """A request whose route reads its body, runs ``during`` on another thread, then answers with ``reply``."""
    from starlette.responses import JSONResponse

    async def route(scope, receive, send):
        scope["state"][IDENTITY_STATE_KEY] = _identity(person)
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        await asyncio.to_thread(during)
        await JSONResponse(reply)(scope, receive, send)

    delivered = {"body": False}

    async def receive():
        if delivered["body"]:
            return {"type": "http.disconnect"}
        delivered["body"] = True
        return {"type": "http.request", "body": b'{"model":"m","input":"x"}', "more_body": False}

    async def send(message):
        return None

    middleware = RequestHistoryMiddleware(route, admin_path=lambda: db, media_path=lambda: media)
    scope = {"type": "http", "method": "POST", "path": "/v1/responses",
             "headers": [(b"content-type", b"application/json")], "query_string": b"", "state": {}}
    asyncio.run(middleware(scope, receive, send))


def _set_parts(db, media, person, parts: list[str]) -> None:
    request_history.set_member_choice(person, keep=True, retention_id="7_days", parts=parts, admin_path=db,
                                      path=media, now=int(time.time()))


def _only_row(media) -> dict:
    conn = sqlite3.connect(media)
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(row) for row in conn.execute("SELECT parts_json, parts_kept, omissions_json FROM kept_requests")]
    finally:
        conn.close()
    assert len(rows) == 1, rows
    return rows[0]


def test_r2_6_a_a_reply_the_member_stops_keeping_mid_request_is_not_kept(member_db):
    db, media, person = member_db
    _set_parts(db, media, person, ["accepted", "received"])
    _answer_during(db, media, person, lambda: _set_parts(db, media, person, ["accepted"]), {"answer": SENTENCE})
    row = _only_row(media)
    assert json.loads(row["parts_kept"]) == ["accepted"]
    assert "received" not in row["parts_json"] and SENTENCE not in row["parts_json"]


def test_r2_6_b_the_omissions_of_a_dropped_reply_are_not_recorded(member_db):
    db, media, person = member_db
    _set_parts(db, media, person, ["accepted", "received"])
    _answer_during(db, media, person, lambda: _set_parts(db, media, person, ["accepted"]),
                   {"image": "data:image/png;base64,QUJD"})
    assert "media" not in json.loads(_only_row(media)["omissions_json"])


def test_r2_6_control_a_reply_the_member_keeps_is_kept_with_its_omissions(member_db):
    db, media, person = member_db
    _set_parts(db, media, person, ["accepted", "received"])
    _answer_during(db, media, person, lambda: None, {"image": "data:image/png;base64,QUJD"})
    row = _only_row(media)
    assert json.loads(row["parts_kept"]) == ["accepted", "received"]
    assert "media" in json.loads(row["omissions_json"])
