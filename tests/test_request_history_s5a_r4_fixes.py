"""Round-4 fixes for request history slice 5a (docs/design/REQUEST_RETENTION.md, slice 5a review fixes).

Each test names the round-4 finding it pins (R4-n). The round-2 probes are in
``tests/test_review_retention_s5a_r2_probes.py``; the round-3 fixes are in
``tests/test_request_history_s5a_r3_fixes.py``.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from starlette.responses import JSONResponse

from gateway import request_history, request_history_capture
from gateway.request_history_capture import IDENTITY_STATE_KEY, RequestHistoryMiddleware
from tests.test_review_retention_s5a_probes import _drive, _identity, _kept, member_db  # noqa: F401 - fixture

SECRET = "SYNTHETIC_MARKER"
MAX_SLOTS = request_history_capture.MAX_PENDING_CAPTURES
#: The RFC 3986 sub-delims, and the quote a pasted string may carry in a user name or password.
USER_INFO_DELIMITERS = "!$&'()*+,;=\""
#: The same, minus ``&``, which separates query items and so is not part of a value.
QUERY_DELIMITERS = "!$'()*+,;=\""
KEEP_BODY = json.dumps({"model": "m", "input": "a sentence to keep"}).encode()


@pytest.fixture(autouse=True)
def own_admission_limit(monkeypatch):
    """Each test has its own admission limit, so a slot that a failing test leaves taken cannot reach the next test."""
    monkeypatch.setattr(request_history_capture, "_PENDING", threading.BoundedSemaphore(MAX_SLOTS))


# ------------------------------------------------------------------ R4-1: a URL's user info, sub-delims included


# Round 7 (R7-1) replaced the output of the next four tests: a URL with an @ is replaced whole, and so is any URL in them.
@pytest.mark.parametrize("delimiter", USER_INFO_DELIMITERS)
def test_r4_1_a_user_info_with_a_delimiter_is_removed_from_a_url_in_text(delimiter):
    kept, found = request_history.redact_json({"content": f"connect postgres://user:a{delimiter}{SECRET}@db/app now"})
    assert SECRET not in json.dumps(kept)
    assert kept == {"content": "connect [url removed] now"}
    assert "url_parts_removed" in found


@pytest.mark.parametrize("delimiter", USER_INFO_DELIMITERS)
def test_r4_1_b_user_info_with_a_delimiter_is_removed_from_a_url_that_is_a_whole_value(delimiter):
    kept, _ = request_history.redact_json({"dsn": f"postgres://user:a{delimiter}{SECRET}@db/app"})
    assert kept == {"dsn": "[url removed]"}


# Round 6 (R6-1) replaced the rule this test pinned, which cut user info at the last @ anywhere in the URL. Each of these
# URLs cannot be split into its parts now, so the whole URL is replaced (see tests/test_request_history_s5a_r6_fixes.py).
@pytest.mark.parametrize("url", [
    f"postgres://user:pa/{SECRET}@db/app",
    f"postgres://user:12/{SECRET}@db/app",
    f"postgres://user:pa?{SECRET}@db/app",
    f"postgres://user:pa#{SECRET}@db/app",
    f"postgres://user:pa@{SECRET}@db/app",
])
def test_r4_1_c_an_ambiguous_user_info_boundary_removes_the_whole_url(url):
    kept, _ = request_history.redact_json({"dsn": url, "content": f"reach {url} now"})
    assert SECRET not in json.dumps(kept)
    assert kept == {"dsn": "[url removed]", "content": "reach [url removed] now"}


@pytest.mark.parametrize("delimiter", QUERY_DELIMITERS)
def test_r4_1_d_a_query_value_with_a_delimiter_is_removed_from_a_url_in_text(delimiter):
    kept, _ = request_history.redact_json({"content": f"see https://h.test/a?token=abc{delimiter}{SECRET}&x=1 now"})
    assert SECRET not in json.dumps(kept)
    assert kept == {"content": "see https://h.test/a?[removed] now"}


@pytest.mark.parametrize("delimiter", QUERY_DELIMITERS)
def test_r4_1_e_a_query_value_with_a_delimiter_is_removed_from_a_url_that_is_a_whole_value(delimiter):
    kept, _ = request_history.redact_json({"u": f"https://h.test/a?token=abc{delimiter}{SECRET}&x=1"})
    assert SECRET not in json.dumps(kept)
    assert kept == {"u": "https://h.test/a?[removed]"}


def test_r4_1_f_a_quoted_user_info_in_a_message_keeps_no_secret_in_the_row(member_db):
    db, media, person = member_db
    raw = json.dumps({"content": f"connect postgres://user:'{SECRET}'@db/app"}).encode()
    _drive(db, media, person, chunks=[raw], content_type="application/json")
    rows = _kept(media)
    assert len(rows) == 1
    assert SECRET not in rows[0] and "[url removed]" in rows[0]


# ------------------------------------------------------------------ R4-2: JSON too deep to clean is omitted, not kept


def _fullwidth(text: str) -> str:
    """The text in fullwidth forms, as an operator might type it: ``password`` becomes ``ｐａｓｓｗｏｒｄ``."""
    return "".join(chr(ord(char) + 0xFEE0) if "!" <= char <= "~" else char for char in text)


def _nested(depth: int, leaf: Any) -> Any:
    """``leaf`` inside ``depth`` lists, built without recursion."""
    for _ in range(depth):
        leaf = [leaf]
    return leaf


def _nested_text(depth: int, inner: str) -> str:
    return "[" * depth + inner + "]" * depth


def _tool_call(arguments: str) -> dict:
    return {"messages": [{"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "fetch", "arguments": arguments}}]}]}


def _arguments_of(kept: dict) -> Any:
    return kept["messages"][0]["tool_calls"][0]["function"]["arguments"]


@pytest.mark.parametrize("depth", [2000, 100_000])
def test_r4_2_a_tool_arguments_nested_past_the_depth_limit_are_omitted(depth):
    kept, found = request_history.redact_json(_tool_call(_nested_text(depth, f'{{"password":"{SECRET}"}}')))
    assert SECRET not in json.dumps(kept)
    assert _arguments_of(kept) == {"omitted": "json_depth"}
    assert found == ["json_depth"]


@pytest.mark.parametrize("arguments", [
    f'{{"password": "{SECRET}"',
    f'{{"password": "{SECRET}", }}',
    f'  \n[{{"password": "{SECRET}"}}] and then more',
])
def test_r4_2_b_tool_arguments_that_look_like_json_but_do_not_parse_are_omitted(arguments):
    kept, found = request_history.redact_json(_tool_call(arguments))
    assert SECRET not in json.dumps(kept)
    assert _arguments_of(kept) == {"omitted": "unreadable_json"}
    assert found == ["unreadable_json"]


def test_r4_2_c_tool_arguments_that_are_plain_text_are_cleaned_as_text():
    kept, found = request_history.redact_json(_tool_call(f"fetch https://u:{SECRET}@h.test/x now"))
    assert _arguments_of(kept) == "fetch [url removed] now"
    assert found == ["url_parts_removed"]


def test_r4_2_d_tool_arguments_nested_under_the_limit_are_cleaned_as_json():
    kept, found = request_history.redact_json(_tool_call(_nested_text(40, f'{{"password": "{SECRET}", "ok": 1}}')))
    assert SECRET not in json.dumps(kept)
    assert json.loads(_arguments_of(kept)) == _nested(40, {"password": "[removed]", "ok": 1})
    assert found == ["credential_field"]


@pytest.mark.parametrize("depth", [200, 2000])
def test_r4_2_e_a_top_level_body_nested_past_the_depth_limit_is_omitted(depth):
    kept, found = request_history.redact_json({"input": _nested(depth, {"password": SECRET})})
    assert SECRET not in json.dumps(kept)
    assert kept == {"omitted": "json_depth"}
    assert found == ["json_depth"]


def test_r4_2_f_a_body_nested_past_the_depth_limit_keeps_its_row_with_the_body_omitted(member_db):
    db, media, person = member_db
    raw = ('{"model":"m","input":' + _nested_text(2000, f'{{"password":"{SECRET}"}}') + "}").encode()
    _drive(db, media, person, chunks=[raw], content_type="application/json")
    rows = _kept(media)
    assert len(rows) == 1
    assert SECRET not in rows[0] and '"omitted": "json_depth"' in rows[0]


@pytest.mark.parametrize("name", [_fullwidth("password"), _fullwidth("api_key"), _fullwidth("x-api-key"),
                                  _fullwidth("secrets"), _fullwidth("password1")])
def test_r4_2_g_a_fullwidth_credential_name_is_removed(name):
    assert request_history.is_credential_name(name)
    kept, found = request_history.redact_json({"outer": {name: SECRET}})
    assert kept == {"outer": {name: "[removed]"}}
    assert found == ["credential_field"]


def test_r4_2_h_a_fullwidth_name_that_is_not_a_credential_is_kept():
    name = _fullwidth("max_tokens")
    kept, _ = request_history.redact_json({name: 5})
    assert kept == {name: 5}


# ------------------------------------------------------------------ R4-3: a cancelled request gives its slot back


def _scope() -> dict:
    return {"type": "http", "method": "POST", "path": "/v1/responses",
            "headers": [(b"content-type", b"application/json")], "query_string": b"", "state": {}}


def _one_body(raw: bytes) -> Callable:
    """A receive that yields the body once, then reports a disconnect, as a client does after its request."""
    sent = {"body": False}

    async def receive() -> dict:
        if sent["body"]:
            return {"type": "http.disconnect"}
        sent["body"] = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return receive


async def _ignore(message: dict) -> None:
    return None


async def _read_body(receive: Callable) -> None:
    """Read a route's whole body, as a route that parses its JSON reads it."""
    while True:
        message = await receive()
        if not message.get("more_body", False):
            return


def _answer_after_reading(person: str) -> Callable:
    """A route that authenticates, reads its whole body, then answers."""
    async def route(scope, receive, send):
        scope["state"][IDENTITY_STATE_KEY] = _identity(person)
        await _read_body(receive)
        await JSONResponse({"ok": True})(scope, receive, send)

    return route


class _QueuedWriter(ThreadPoolExecutor):
    """A one-thread writer that reports each job as it is queued, so a test knows when a write is waiting."""

    def __init__(self) -> None:
        super().__init__(max_workers=1, thread_name_prefix="request-history-r4")
        self.queued: queue.SimpleQueue[None] = queue.SimpleQueue()

    def submit(self, fn, /, *args, **kwargs):
        self.queued.put(None)
        return super().submit(fn, *args, **kwargs)


@pytest.fixture
def held_writer(monkeypatch):
    """A writer whose only thread is held by a gate. A write queued behind it waits until the test opens the gate."""
    pool = _QueuedWriter()
    gate = threading.Event()
    ThreadPoolExecutor.submit(pool, gate.wait)
    monkeypatch.setattr(request_history_capture, "WRITER", pool)
    yield pool, gate
    gate.set()
    pool.shutdown(wait=True)


@pytest.fixture
def own_writer(monkeypatch):
    pool = _QueuedWriter()
    monkeypatch.setattr(request_history_capture, "WRITER", pool)
    yield pool
    pool.shutdown(wait=True)


def _free_slots() -> int:
    """How many capture admission slots are free now. Takes each one and gives it back."""
    taken = 0
    while request_history_capture._PENDING.acquire(blocking=False):
        taken += 1
    for _ in range(taken):
        request_history_capture._PENDING.release()
    return taken


async def _repeat(step: Callable, times: int) -> None:
    for _ in range(times):
        await step()


def test_r4_3_a_thirty_two_requests_cancelled_while_their_writes_are_queued_give_back_their_slots(member_db,
                                                                                                held_writer):
    db, media, person = member_db
    pool, gate = held_writer
    middleware = RequestHistoryMiddleware(_answer_after_reading(person), admin_path=lambda: db,
                                          media_path=lambda: media)

    async def cancel_once():
        task = asyncio.create_task(middleware(_scope(), _one_body(KEEP_BODY), _ignore))
        await asyncio.to_thread(pool.queued.get)   # the request has reached its write, queued behind the held thread
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    async def scenario():
        await _repeat(cancel_once, MAX_SLOTS)
        gate.set()
        await asyncio.to_thread(pool.shutdown, True)   # the queued writes run now that the thread is free

    asyncio.run(scenario())
    assert _free_slots() == MAX_SLOTS


def test_r4_3_b_thirty_two_requests_cancelled_while_their_bodies_drain_give_back_their_slots(member_db, own_writer):
    db, media, person = member_db

    async def answer_without_reading(scope, receive, send):
        scope["state"][IDENTITY_STATE_KEY] = _identity(person)
        await JSONResponse({"error": {"message": "refused early"}}, status_code=413)(scope, receive, send)

    middleware = RequestHistoryMiddleware(answer_without_reading, admin_path=lambda: db, media_path=lambda: media)

    async def cancel_during_drain():
        stalled = asyncio.Event()
        reads = 0

        async def receive() -> dict:
            nonlocal reads
            reads += 1
            if reads == 1:
                return {"type": "http.request", "body": b'{"model":"m",', "more_body": True}
            stalled.set()
            await asyncio.sleep(3600)   # the rest of the body never arrives; only the cancel below ends this wait
            return {"type": "http.disconnect"}

        task = asyncio.create_task(middleware(_scope(), receive, _ignore))
        await stalled.wait()   # the capture is draining the body, and is waiting for the rest of it
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_repeat(cancel_during_drain, MAX_SLOTS))
    assert _free_slots() == MAX_SLOTS


# ------------------------------------------------------------------ R4-4: a body forget gives its slot back at once

FORGET = {"com.azizam.prompts": {"mode": "forget"}}
FORGET_BODY = json.dumps({"model": "m", "input": "a sentence to forget", "extensions": FORGET}).encode()


async def _forgets_in_flight_then_a_kept_request(db: Any, media: Any, person: str) -> None:
    """Thirty-two body forgets whose routes are still running, then one history-on request while they run."""
    release = asyncio.Event()
    everyone_read = asyncio.Event()
    reads = 0

    async def hold_after_reading(scope, receive, send):
        nonlocal reads
        scope["state"][IDENTITY_STATE_KEY] = _identity(person)
        await _read_body(receive)
        reads += 1
        if reads == MAX_SLOTS:
            everyone_read.set()
        await release.wait()
        await JSONResponse({"ok": True})(scope, receive, send)

    holding = RequestHistoryMiddleware(hold_after_reading, admin_path=lambda: db, media_path=lambda: media)
    keeping = RequestHistoryMiddleware(_answer_after_reading(person), admin_path=lambda: db, media_path=lambda: media)
    forgets = [asyncio.create_task(holding(_scope(), _one_body(FORGET_BODY), _ignore)) for _ in range(MAX_SLOTS)]
    await asyncio.wait_for(everyone_read.wait(), 30)
    await keeping(_scope(), _one_body(KEEP_BODY), _ignore)
    release.set()
    await asyncio.gather(*forgets)


def test_r4_4_a_thirty_two_body_forgets_in_flight_do_not_hold_the_slots_from_a_kept_request(member_db):
    db, media, person = member_db
    asyncio.run(_forgets_in_flight_then_a_kept_request(db, media, person))
    rows = _kept(media)
    assert len(rows) == 1
    assert "a sentence to keep" in rows[0] and "a sentence to forget" not in rows[0]
    assert _free_slots() == MAX_SLOTS


def test_r4_4_b_a_body_forget_stops_its_reply_from_being_buffered(member_db):
    db, media, person = member_db
    request_history.set_member_choice(person, keep=True, retention_id="7_days", parts=["accepted", "received"],
                                      admin_path=db, path=media, now=int(time.time()))
    owner = RequestHistoryMiddleware(None, admin_path=lambda: db, media_path=lambda: media)
    capture = request_history_capture._Capture(owner, state={IDENTITY_STATE_KEY: _identity(person)},
                                               headers={"content-type": "application/json"}, header_forget=False,
                                               limit=1 << 20, route="/v1/responses")

    async def forget_then_reply():
        await capture.receive_wrapped(_one_body(FORGET_BODY))()
        await capture.send_wrapped(_ignore)({"type": "http.response.start", "status": 200,
                                             "headers": [(b"content-type", b"application/json")]})
        await capture.send_wrapped(_ignore)({"type": "http.response.body", "body": b'{"answer":"x"}',
                                             "more_body": False})

    asyncio.run(forget_then_reply())
    assert capture.reply is None
    assert _free_slots() == MAX_SLOTS
