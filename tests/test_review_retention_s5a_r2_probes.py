"""Round-2 review probes for retention slice 5a (feat/retention-s5a at 05fef5d).

Each test named for a finding was a ``xfail(strict=True)`` on the reviewed head, and failed there with the mark removed.
The fixes removed the marks, so each now passes as a plain assertion. Unmarked tests pin a claim the review proved holds.
Two controls pinned the gap the fixes close, and now assert the fixed behavior (see their docstrings).
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest
from starlette.responses import JSONResponse

from gateway import admin_store, request_history, request_history_capture, request_history_slot
from gateway.request_history_capture import IDENTITY_STATE_KEY, RequestHistoryMiddleware
from tests.test_prompt_memory_responses import responses_gateway  # noqa: F401 - fixture
from tests.test_review_retention_s5a_probes import CANARY, SENTENCE, _drive, _identity, _kept, member_db  # noqa: F401

FORGET = {"com.azizam.prompts": {"mode": "forget"}}


# ------------------------------------------------------------------ forget in a body the capture does not parse


@pytest.mark.parametrize("content_type", ["text/plain", "application/x-www-form-urlencoded"])
def test_a_forget_in_a_json_body_sent_as_another_media_type_keeps_no_row(responses_gateway, content_type):
    """``curl -d '{...}'`` sends application/x-www-form-urlencoded. The Responses route parses the body anyway and
    honours the forget, so the conversation is not stored. The capture sees an unsupported container, never reads the
    control, and keeps a request history row for a request the caller sent incognito."""
    gw = responses_gateway
    body = json.dumps({"model": "default", "input": SENTENCE, "extensions": FORGET}).encode()
    response = gw["client"].post("/v1/responses", content=body,
                                 headers={**gw["headers"], "content-type": content_type})
    assert response.status_code == 200, response.text
    import proxy
    assert proxy.responses_store.get(response.json()["id"], path=gw["responses_db"]) is None  # the route forgot
    assert _kept(gw["media_db"]) == []


def test_a_forget_sent_as_text_plain_keeps_no_reply_when_the_member_keeps_replies(responses_gateway):
    gw = responses_gateway
    request_history.set_member_choice(gw["person_id"], keep=True, retention_id="7_days",
                                      parts=["accepted", "received"], admin_path=gw["admin_db"],
                                      path=gw["media_db"], now=int(time.time()) - 5)
    body = json.dumps({"model": "default", "input": SENTENCE, "extensions": FORGET}).encode()
    response = gw["client"].post("/v1/responses", content=body, headers={**gw["headers"], "content-type": "text/plain"})
    assert response.status_code == 200, response.text
    rows = _kept(gw["media_db"])
    assert not any('"received"' in row for row in rows), rows


def test_positive_control_the_same_body_as_json_keeps_no_row_and_without_forget_keeps_one(responses_gateway):
    gw = responses_gateway
    forget = gw["client"].post("/v1/responses", headers=gw["headers"],
                               json={"model": "default", "input": SENTENCE, "extensions": FORGET})
    assert forget.status_code == 200, forget.text
    assert _kept(gw["media_db"]) == []
    kept = gw["client"].post("/v1/responses", headers=gw["headers"], json={"model": "default", "input": SENTENCE})
    assert kept.status_code == 200, kept.text
    assert len(_kept(gw["media_db"])) == 1


def test_the_unit_capture_keeps_no_text_plain_body_that_carries_a_forget(member_db):
    """The same gap at the middleware boundary, with no route in the way. Was a control that pinned the gap (R2-1)."""
    db, media, person = member_db
    body = json.dumps({"model": "m", "input": SENTENCE, "extensions": FORGET}).encode()
    _drive(db, media, person, chunks=[body], content_type="text/plain")
    assert _kept(media) == []


# ------------------------------------------------------------------ the secret floor: names rule 8 means to cover


@pytest.mark.parametrize("name", ["secrets", "client_secrets", "passwords", "password1", "apiKey2", "api_keys",
                                  "apiKeys", "access_tokens"])
def test_plural_and_numbered_credential_names_are_removed(name):
    kept, _ = request_history.redact_json({"config": {name: CANARY}})
    assert CANARY not in json.dumps(kept)


def test_a_secrets_map_is_removed_whole_under_its_credential_name():
    """The concrete shape: a provider map under ``secrets``. The values sit under neutral names, so only the parent's
    name can remove them, and the whole subtree goes. Was a control that pinned the gap (R2-2)."""
    kept, _ = request_history.redact_json({"secrets": {"openai": CANARY, "anthropic": CANARY}})
    assert CANARY not in json.dumps(kept)
    assert kept == {"secrets": "[removed]"}


# ------------------------------------------------------------------ the secret floor: URLs inside text (rule 4)


@pytest.mark.parametrize("text", [
    f"connect with postgres://admin:{CANARY}@db.local:5432/app",
    f"postgresql://admin:{CANARY}@db.local/app",
    f"open wss://stream.example.test/socket?token={CANARY}",
    f"see HTTPS://bucket.s3.amazonaws.com/a.png?X-Amz-Signature={CANARY}",
    f"redis://:{CANARY}@cache.local:6379/0",
])
def test_user_info_and_query_values_are_removed_from_any_url_scheme(text):
    kept, _ = request_history.redact_json({"messages": [{"role": "user", "content": text}]})
    assert CANARY not in json.dumps(kept)


def test_positive_control_a_lower_case_https_url_in_text_is_scrubbed():
    kept, found = request_history.redact_json({"content": f"see https://h.test/a.png?sig={CANARY}, thanks"})
    assert CANARY not in json.dumps(kept) and "url_parts_removed" in found
    assert kept == {"content": "see https://h.test/a.png?[removed] thanks"}


# ------------------------------------------------------------------ the header forget reaches Library prompts


def test_the_forget_header_reaches_the_library_prompt_decision(tmp_path):
    """Positive control for finding 4 on the media path (the round-1 probe covers Responses only)."""
    import proxy
    from unittest.mock import patch
    from gateway import media_delivery
    from gateway.receipts import RouteReceipt

    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    person = admin_store.upsert_oidc_user("issuer", "header-forget", path=admin_db)
    receipt = RouteReceipt(protocol="openai.images", intent="image.generate")
    context = media_delivery.begin(plan_bound="none", receipt=receipt)
    context.person_id = person["id"]
    token = request_history_slot.open_slot("/v1/images/generations", forget=True)
    try:
        with patch.object(proxy.ADMIN_STORE, "path", admin_db):
            assert proxy._note_media_metadata_body({"prompt": "private header prompt"}) is None
    finally:
        request_history_slot.close_slot(token)
    assert receipt.to_dict("client")["retention"] == [
        {"kind": "library_prompt", "decision": "not_kept", "reason": "request_forget"}]


# ------------------------------------------------------------------ admission: who holds the 32 slots


async def _on_member_rows_while_blocked(db, media, person, off, blocked_count: int) -> list[str]:
    release = asyncio.Event()

    async def one(person_id: str, *, wait: bool) -> None:
        async def route(scope, receive, send):
            scope["state"][IDENTITY_STATE_KEY] = _identity(person_id)
            await receive()
            if wait:
                await release.wait()
            await JSONResponse({"ok": True})(scope, receive, send)

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
        await middleware(scope, receive, send)

    blocked = [asyncio.create_task(one(off, wait=True)) for _ in range(blocked_count)]
    await asyncio.sleep(0.5)
    await one(person, wait=False)
    kept = _kept(media)
    release.set()
    await asyncio.gather(*blocked)
    return kept


def _off_member(db, media) -> str:
    off = admin_store.upsert_oidc_user("https://fixture.test", "off-person", path=db)["id"]
    request_history.set_member_choice(off, keep=False, retention_id="7_days", parts=["accepted"],
                                      admin_path=db, path=media, now=int(time.time()) - 5)
    return off


def test_requests_that_keep_nothing_do_not_use_up_the_capture_limit(member_db):
    """32 long requests (a stream, a long generation) from a member whose history is off hold every admission slot.
    A request from a member whose history is on, in that window, is not kept (receipt: storage_unavailable)."""
    db, media, person = member_db
    off = _off_member(db, media)
    rows = asyncio.run(_on_member_rows_while_blocked(db, media, person, off,
                                                     request_history_capture.MAX_PENDING_CAPTURES))
    assert len(rows) == 1


def test_positive_control_with_fewer_long_requests_the_on_member_is_kept(member_db):
    db, media, person = member_db
    off = _off_member(db, media)
    rows = asyncio.run(_on_member_rows_while_blocked(db, media, person, off,
                                                     request_history_capture.MAX_PENDING_CAPTURES - 1))
    assert len(rows) == 1


# ------------------------------------------------------------------ lock order: admin.db first, then media_jobs.db


def test_a_history_write_survives_a_concurrent_library_prompt_write(member_db, monkeypatch):
    """The history writer holds the household lock (admin.db), then its media transaction. A Library prompt write asks
    for the household lock meanwhile and waits for the history write, which is short, so both writes succeed. Before the
    fix the history writer took media_jobs.db first, so the two writers waited on each other: the history write gave up
    after 0.25 s and was lost, and the prompt write was blocked for that time (R2-5).

    The hook used to hold the media transaction until the prompt write reached the household lock from inside
    put(). Under one lock order the prompt write cannot reach that point while the history writer holds the lock. So
    the hook holds both locks for a moment after the prompt write has started, which forces the overlap."""
    import threading
    from contextlib import contextmanager
    from gateway import prompt_memory

    db, media, person = member_db
    prompt_memory.init_db(path=media)
    prompt_started, held = threading.Event(), threading.Event()
    real_transaction = request_history.write_transaction

    @contextmanager
    def holding_transaction(path=None):
        with real_transaction(path) as conn:
            held.set()
            prompt_started.wait(5)
            time.sleep(0.05)  # both locks stay held while the prompt write waits on the household lock
            yield conn

    monkeypatch.setattr(request_history, "write_transaction", holding_transaction)
    owner = RequestHistoryMiddleware(None, admin_path=lambda: db, media_path=lambda: media)
    identity = _identity(person)
    capture = request_history_capture._Capture(owner, state={IDENTITY_STATE_KEY: identity}, headers={},
                                               header_forget=False, limit=1 << 20, route="/v1/responses")
    now = int(time.time())
    decision = owner.decide(identity.identity, forget=False, now=now)
    record = {"record_id": request_history.new_record_id(), "request_id": None, "person_id": person,
              "app_id": "app_probe", "origin_kind": "app_key", "origin_id": "cred_probe", "created_at": now,
              "parts": {"accepted": {"headers": {}, "body": {"model": "m"}}}, "omissions": [], "policy_version": 1,
              "summary": {"route": "/v1/responses", "model": "m", "status": 200, "latency_ms": 1}}
    writer = threading.Thread(target=capture._write, args=(decision, record, None))
    writer.start()
    assert held.wait(5)
    started = time.monotonic()
    prompt_started.set()
    prompt_memory.put("asset_probe", person, "a prompt", kept_until=None, path=media, admin_path=db)
    blocked_for = time.monotonic() - started
    writer.join(10)
    assert len(_kept(media)) == 1, f"history write lost; the prompt write was blocked {blocked_for:.2f}s"


# ------------------------------------------------------------------ late writes through the writer thread (12)


def _drive_with(db, media, person, during) -> None:
    """A request whose route reads its body (so the early decision is made), then runs ``during`` before it answers."""
    async def route(scope, receive, send):
        scope["state"][IDENTITY_STATE_KEY] = _identity(person)
        await receive()
        await asyncio.to_thread(during)
        await JSONResponse({"ok": True})(scope, receive, send)

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


def test_a_delete_all_during_the_request_fences_the_late_write(member_db):
    db, media, person = member_db
    _drive_with(db, media, person, lambda: request_history.delete_all_for_person(
        person, admin_path=db, path=media, now=int(time.time())))
    assert _kept(media) == []


def test_turning_history_off_during_the_request_fences_the_late_write(member_db):
    db, media, person = member_db
    _drive_with(db, media, person, lambda: request_history.set_member_choice(
        person, keep=False, retention_id="7_days", parts=["accepted"], admin_path=db, path=media,
        now=int(time.time())))
    assert _kept(media) == []


def test_positive_control_with_nothing_during_the_request_the_row_is_kept(member_db):
    db, media, person = member_db
    _drive_with(db, media, person, lambda: None)
    assert len(_kept(media)) == 1


# ------------------------------------------------------------------ the session and the key (10)


from tests.test_request_history_s5a_review import CSRF, _browser, browser  # noqa: E402,F401 - fixture
from tests.test_request_history_routes import _record, _rows, _store, gateway  # noqa: E402,F401 - fixture


@pytest.mark.parametrize("header", ["authorization", "x-api-key", "api-key"])
def test_an_invalid_key_with_a_valid_session_cookie_is_refused_not_read_as_the_session(gateway, browser, header):
    maker = gateway["maker"]
    _store(gateway, _record(maker))
    browser(maker.person)
    value = "Bearer opk_not_a_real_key" if header == "authorization" else "opk_not_a_real_key"
    response = _browser("GET", "/gateway/v1/requests", headers={header: value})
    assert response.status_code == 401, response.text


def test_a_session_delete_all_needs_csrf_and_removes_only_the_sessions_own_rows(gateway, browser):
    maker, other = gateway["maker"], gateway["other"]
    _store(gateway, _record(maker), _record(other))
    browser(maker.person)
    refused = _browser("DELETE", "/gateway/v1/requests")
    assert refused.status_code == 403, refused.text
    assert _rows(gateway["media"], "SELECT COUNT(*) FROM kept_requests")[0][0] == 2
    done = _browser("DELETE", "/gateway/v1/requests", headers=CSRF)
    assert done.status_code == 200, done.text
    assert [row[0] for row in _rows(gateway["media"], "SELECT person_id FROM kept_requests")] == [other.person]
