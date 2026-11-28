"""Round-6 fixes for request history slice 5a (docs/design/REQUEST_RETENTION.md, slice 5a review fixes).

Each test names the round-6 finding it pins (R6-n). The round-4 fixes, whose user info rule round 6 replaces, are in
``tests/test_request_history_s5a_r4_fixes.py``.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest

from gateway import request_history, request_history_capture
from gateway.request_history_capture import IDENTITY_STATE_KEY, RequestHistoryMiddleware
from tests.test_request_history_s5a_r4_fixes import (  # noqa: F401 - fixtures and helpers shared with round 4
    FORGET_BODY,
    KEEP_BODY,
    MAX_SLOTS,
    _free_slots,
    _ignore,
    _one_body,
    _read_body,
    _scope,
)
from tests.test_review_retention_s5a_probes import _identity, _kept, member_db  # noqa: F401 - fixture

SECRET = "SYNTHETIC_MARKER"


# ------------------------------------------------------------------ R6-1: the authority ends at the first /, ? or #


# R7-1 replaced this rule: an @ anywhere in a URL now removes the whole URL, so every case below is replaced whole.
@pytest.mark.parametrize("url", [
    f"https://h.test/#token=prefix@{SECRET}",
    f"https://h.test/?token=prefix@{SECRET}",
    f"https://h.test?token=prefix@{SECRET}",
    f"https://h.test#token=prefix@{SECRET}",
    f"https://h.test/a?x=1&token=prefix@{SECRET}#frag@{SECRET}",
    "https://" + "u:pw" + "@" + f"h.test/a?token=prefix@{SECRET}",
])
def test_r6_1_a_an_at_sign_after_the_authority_never_makes_its_text_a_host(url):
    kept, found = request_history.redact_json({"u": url})
    assert SECRET not in json.dumps(kept)
    assert kept == {"u": "[url removed]"}
    assert "url_parts_removed" in found


@pytest.mark.parametrize("url", [
    f"https://h.test/#token=prefix@{SECRET}",
    f"https://h.test/?token=prefix@{SECRET}",
])
def test_r6_1_b_the_same_rule_holds_for_a_url_inside_a_sentence(url):
    kept, _ = request_history.redact_json({"content": f"see {url} now"})
    assert SECRET not in json.dumps(kept)
    assert kept == {"content": "see [url removed] now"}


@pytest.mark.parametrize("url", [
    f"https://u:pa?{SECRET}@h.test/x",
    f"https://u:pa#{SECRET}@h.test/x",
    f"https://u:pa/{SECRET}@h.test/x",
    f"https://u:12/{SECRET}@h.test/x",
    f"https://u:p@{SECRET}@h.test/x",
    f"https://h.test\\@{SECRET}/x",
    f"https://u:p@h.test:notaport/{SECRET}",
    f"https://h.test:99999/{SECRET}",
    f"https://[::1/{SECRET}",
])
def test_r6_1_c_a_url_that_cannot_be_split_is_replaced_whole_and_no_part_of_it_is_kept(url):
    kept, found = request_history.redact_json({"dsn": url, "content": f"reach {url} now"})
    assert SECRET not in json.dumps(kept)
    assert kept == {"dsn": "[url removed]", "content": "reach [url removed] now"}
    assert "url_parts_removed" in found


@pytest.mark.parametrize(("url", "kept_as"), [
    ("https://" + "user:pw" + "@" + "h.test:8443/a?x=1", "[url removed]"),
    ("https://" + "u:p" + "@" + "[::1]:8080/x", "[url removed]"),
    ("https://" + "u:p" + "@" + "h.test/x@y", "[url removed]"),
    ("https://h.test:8443/a?x=1", "https://h.test:8443/a?[removed]"),
])
def test_r6_1_d_user_info_replaces_the_whole_url_and_a_url_without_it_keeps_its_port(url, kept_as):
    kept, _ = request_history.redact_json({"u": url})
    assert kept == {"u": kept_as}


# ------------------------------------------------------------------ R6-2: JSON held in a string literal is cleaned too


LIMIT = 1 << 20


def _message(role: str, content: Any) -> dict:
    return {"messages": [{"role": role, "content": content}]}


def _content_of(kept: dict) -> Any:
    return kept["messages"][0]["content"]


def _tool_call(arguments: str) -> dict:
    return {"messages": [{"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "fetch", "arguments": arguments}}]}]}


def _wrapped(value: Any, levels: int) -> str:
    """``value`` as JSON text, then JSON-encoded again ``levels`` times: a string whose content is JSON, wrapped."""
    text = json.dumps(value)
    for _ in range(levels):
        text = json.dumps(text)
    return text


def _unwrapped(text: str, levels: int) -> Any:
    for _ in range(levels):
        text = json.loads(text)
    return json.loads(text)


@pytest.mark.parametrize("levels", [1, 2, 3, 4])
def test_r6_2_a_json_wrapped_in_string_literals_is_cleaned_at_every_level(levels):
    kept, found = request_history.redact_json(_message("tool", _wrapped({"password": SECRET, "ok": 1}, levels)))
    assert SECRET not in json.dumps(kept)
    assert _unwrapped(_content_of(kept), levels) == {"password": "[removed]", "ok": 1}
    assert found == ["credential_field"]


def test_r6_2_b_a_tool_call_whose_arguments_are_a_json_string_literal_is_cleaned():
    arguments = json.dumps(json.dumps({"password": SECRET}))
    kept, found = request_history.redact_json(_tool_call(arguments))
    assert SECRET not in json.dumps(kept)
    assert json.loads(kept["messages"][0]["tool_calls"][0]["function"]["arguments"]) == '{"password":"[removed]"}'
    assert found == ["credential_field"]


def test_r6_2_c_a_string_literal_of_plain_text_is_kept_as_it_was():
    text = json.dumps("plain prose, quoted")
    kept, found = request_history.redact_json(_message("user", text))
    assert _content_of(kept) == text
    assert found == []


def test_r6_2_d_a_string_literal_holding_a_credential_url_keeps_no_secret():
    kept, found = request_history.redact_json(_message("user", json.dumps(f"see https://user:{SECRET}@h.test/x now")))
    assert SECRET not in json.dumps(kept)
    assert json.loads(_content_of(kept)) == "see [url removed] now"
    assert found == ["url_parts_removed"]


def test_r6_2_e_a_string_literal_of_bracketed_prose_is_cleaned_as_text():
    text = json.dumps(f"[note] see https://user:{SECRET}@h.test/x now")
    kept, _ = request_history.redact_json(_message("user", text))
    assert SECRET not in json.dumps(kept)
    assert json.loads(_content_of(kept)) == "[note] see [url removed] now"


@pytest.mark.parametrize(("depth", "omitted"), [(60, False), (63, True)])
def test_r6_2_f_a_string_literal_nested_past_the_depth_limit_is_omitted(depth, omitted):
    """The literal is a value inside ``depth`` lists, so the JSON it wraps sits two levels past the literal's list."""
    leaf = json.dumps(json.dumps({"password": SECRET}))
    for _ in range(depth):
        leaf = [leaf]
    kept, found = request_history.redact_json(leaf)
    assert SECRET not in json.dumps(kept)
    assert ("json_depth" in found) is omitted


def test_r6_2_g_the_json_budget_is_shared_by_every_nested_decode_of_a_record():
    budget = request_history.WorkBudget(max_bytes=40)
    body = {"messages": [{"role": "tool", "content": '{"password":"x"}'} for _ in range(5)]}
    kept, found = request_history.redact_json(body, budget=budget)
    contents = [message["content"] for message in kept["messages"]]
    assert contents[:2] == ['{"password":"[removed]"}'] * 2
    assert contents[2:] == [{"omitted": "json_budget"}] * 3
    assert found == ["credential_field", "json_budget"]
    assert budget.decoded_bytes <= 40


def test_r6_2_h_two_bodies_of_one_record_draw_on_one_budget():
    budget = request_history.WorkBudget(max_bytes=20)
    first = request_history.BodyCapture("application/json", limit=LIMIT, budget=budget)
    first.feed(json.dumps({"a": '{"password":"x"}'}).encode())
    second = request_history.BodyCapture("application/json", limit=LIMIT, budget=budget)
    second.feed(json.dumps({"b": '{"password":"x"}'}).encode())
    assert first.result() == ({"a": '{"password":"[removed]"}'}, ["credential_field"])
    assert second.result() == ({"b": {"omitted": "json_budget"}}, ["json_budget"])


def test_r6_2_i_the_node_budget_caps_the_values_one_record_visits():
    budget = request_history.WorkBudget(max_bytes=LIMIT, max_nodes=10)
    values = [json.dumps({"k": index}) for index in range(20)]
    kept, found = request_history.redact_json({"items": values}, budget=budget)
    assert budget.nodes <= 10
    assert found == ["json_budget"]
    assert len(kept["items"]) == 20
    assert kept["items"][0] == '{"k":0}'
    assert {"omitted": "json_budget"} in kept["items"]


def test_r6_2_j_a_one_mebibyte_body_of_small_json_strings_stays_within_the_node_budget():
    """Bounded work, not timing: a body of 100,000 small JSON strings, each three values once decoded, has about
    300,000 values. The node counter stops at its limit, and the strings past it are omitted."""
    body = json.dumps({"items": ["[1,2]"] * 100_000}).encode()
    assert len(body) <= LIMIT
    budget = request_history.WorkBudget(max_bytes=LIMIT)
    capture = request_history.BodyCapture("application/json", limit=LIMIT, budget=budget)
    capture.feed(body)
    kept, found = capture.result()
    assert budget.nodes <= request_history.JSON_NODE_LIMIT
    assert budget.decoded_bytes <= LIMIT
    assert found == ["json_budget"]
    assert len(kept["items"]) == 100_000
    assert kept["items"][0] == "[1,2]"
    assert kept["items"][-1] == {"omitted": "json_budget"}


# ------------------------------------------------------------------ R6-3: a body whole before the caller is known


@pytest.fixture(autouse=True)
def own_admission_limit(monkeypatch):
    """Each test has its own admission limit, so a slot that a failing test leaves taken cannot reach the next test."""
    monkeypatch.setattr(request_history_capture, "_PENDING", threading.BoundedSemaphore(MAX_SLOTS))


def _reads_then_authenticates(person: str, reply: list[bytes], free_slots_seen: list[int]) -> Callable:
    """A route that reads its whole body before it knows the caller, then streams ``reply``. After each chunk it records
    how many admission slots are free."""
    async def route(scope, receive, send):
        await _read_body(receive)
        scope["state"][IDENTITY_STATE_KEY] = _identity(person)
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        for index, chunk in enumerate(reply):
            await send({"type": "http.response.body", "body": chunk, "more_body": index < len(reply) - 1})
            free_slots_seen.append(_free_slots())

    return route


def _members_keep_history(db, media, person) -> None:
    request_history.set_member_choice(person, keep=True, retention_id="7_days", parts=["accepted", "received"],
                                      admin_path=db, path=media, now=int(time.time()))


def test_r6_3_a_a_body_forget_whole_before_the_caller_is_known_gives_its_slot_back_before_the_reply(member_db):
    db, media, person = member_db
    _members_keep_history(db, media, person)
    free_slots_seen: list[int] = []
    route = _reads_then_authenticates(person, [b"x" * 65536] * 16, free_slots_seen)
    middleware = RequestHistoryMiddleware(route, admin_path=lambda: db, media_path=lambda: media)
    asyncio.run(middleware(_scope(), _one_body(FORGET_BODY), _ignore))
    assert free_slots_seen == [MAX_SLOTS] * 16
    assert _kept(media) == []
    assert _free_slots() == MAX_SLOTS


def test_r6_3_b_a_body_whole_before_the_caller_is_known_is_kept_with_its_reply(member_db):
    db, media, person = member_db
    _members_keep_history(db, media, person)
    free_slots_seen: list[int] = []
    route = _reads_then_authenticates(person, [b"reply" * 1000] * 4, free_slots_seen)
    middleware = RequestHistoryMiddleware(route, admin_path=lambda: db, media_path=lambda: media)
    asyncio.run(middleware(_scope(), _one_body(KEEP_BODY), _ignore))
    assert free_slots_seen == [MAX_SLOTS - 1] * 4
    rows = _kept(media)
    assert len(rows) == 1 and "a sentence to keep" in rows[0]
    assert _free_slots() == MAX_SLOTS


# ------------------------------------------------------------------ R6-4: every default-ignorable character is dropped from a name


#: Default_Ignorable_Code_Point ranges that are not format characters (category Cf), as the round-6 finding lists them,
#: plus the unassigned ones the property also covers (U+2065, U+FFF0-U+FFF8).
DEFAULT_IGNORABLE_RANGES = [
    (0x034F, 0x034F), (0x115F, 0x1160), (0x17B4, 0x17B5), (0x180B, 0x180F), (0x2065, 0x2065), (0x3164, 0x3164),
    (0xFE00, 0xFE0F), (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0xE0000, 0xE0FFF),
]


def test_r6_4_a_every_code_point_in_the_default_ignorable_ranges_is_dropped_from_a_credential_name():
    for low, high in DEFAULT_IGNORABLE_RANGES:
        for code in range(low, high + 1):
            assert request_history.is_credential_name("pass" + chr(code) + "word"), hex(code)


def test_r6_4_b_the_grapheme_joiner_inside_password_is_still_a_credential_name():
    assert request_history.is_credential_name("pass\u034fword")


@pytest.mark.parametrize("char", ["\u034f", "\u115f", "\u180b", "\u3164", "\ufe0f", "\uffa0", "\U000e0001", "\u200b"])
def test_r6_4_c_a_default_ignorable_character_inside_a_credential_word_is_removed(char):
    name = "pass" + char + "word"
    kept, found = request_history.redact_json({name: SECRET})
    assert SECRET not in json.dumps(kept)
    assert kept == {name: "[removed]"}
    assert found == ["credential_field"]


def test_r6_4_d_a_default_ignorable_character_does_not_make_a_name_a_credential():
    name = "max\u034ftokens"
    assert not request_history.is_credential_name(name)
    kept, found = request_history.redact_json({name: 5})
    assert kept == {name: 5}
    assert found == []


def test_r6_2_k_a_string_literal_that_wraps_json_spends_the_budget_too():
    budget = request_history.WorkBudget(max_bytes=10)
    text = json.dumps(json.dumps({"a": 1}))  # 12 characters: a literal around 8 characters of JSON
    kept, found = request_history.redact_json({"x": text}, budget=budget)
    assert kept == {"x": {"omitted": "json_budget"}}
    assert found == ["json_budget"]
