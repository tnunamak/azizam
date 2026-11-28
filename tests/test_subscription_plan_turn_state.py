"""Turn state on the plan route: the value the upstream returns at the start of a turn goes back on the later requests
of the same turn (a tool loop), never on a request of another turn, and never to another conversation or caller.

Codex source checked (openai/codex at 78fe0c9): codex-rs/core/src/client.rs ``ModelClientSession.turn_state`` ("we
receive it at turn start, keep sending it unchanged between turn requests (e.g., for retries, incremental appends, or
continuation requests), and must not send it between different turns"); core/tests/suite/turn_state.rs asserts the
first request of a turn sends none, the tool follow-up sends the first value, a later value in the same turn is
ignored, and the next turn sends none.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from dataclasses import replace

import httpx
import pytest

from gateway import receipt_store, receipts, turn_state
from gateway.protocols import responses_plan
from tests.support import access as acc
from tests.support.subscription_fakes import KIND, Upstream, body_of, connect, json_route, make_service, token_body
from tests.support.subscription_routes import execute

MODEL = "chatgpt-plan/gpt-test"
PATH = "/v1/responses"
HEADER = "x-codex-turn-state"
SECRET_STATE = "TS-SECRET-7f3a91"  # every value the fake upstream returns contains this, to search logs and receipts


def run(coro):
    return asyncio.run(coro)


def sse(*events: dict) -> bytes:
    return b"".join(b"event: " + e["type"].encode() + b"\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)


def user(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def tool_output(call_id: str, text: str = "ok") -> dict:
    return {"type": "function_call_output", "call_id": call_id, "output": text}


class ToolLoop:
    """A fake plan upstream: it asks for a tool call while the last input item is a user message, answers in text
    after a tool result, and returns a new turn-state value on every response (``<SECRET_STATE>-<n>``)."""

    def __init__(self, upstream: Upstream) -> None:
        self.bodies: list[dict] = []
        self.sent: list[str | None] = []
        self.fail_next = False
        upstream.routes[("POST", PATH)] = self.answer

    def value(self, n: int) -> str:
        return f"{SECRET_STATE}-{n}"

    def answer(self, request: httpx.Request) -> httpx.Response:
        body = body_of(request)
        self.bodies.append(body)
        self.sent.append(request.headers.get(HEADER))
        n = len(self.bodies)
        if self.fail_next:
            self.fail_next = False
            return httpx.Response(400, json={"error": {"code": "bad"}}, headers={HEADER: self.value(n)})
        last = body["input"][-1]
        if last.get("type") == "function_call_output":
            item = {"type": "message", "role": "assistant", "id": f"msg_{n}",
                    "content": [{"type": "output_text", "text": f"done {n}"}]}
        else:
            item = {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": "lookup", "arguments": "{}"}
        done = {"type": "response.completed", "response": {"id": f"resp_{n}", "status": "completed", "output": [item],
                                                           "usage": {"input_tokens": 10, "output_tokens": 2}}}
        return httpx.Response(200, content=sse({"type": "response.created", "response": {}},
                                               {"type": "response.output_item.done", "item": item}, done),
                              headers={"content-type": "text/event-stream", HEADER: self.value(n)})


@pytest.fixture(autouse=True)
def _receipts_table():
    receipt_store.init_db()


@pytest.fixture()
def plan(tmp_path):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    run(connect(service, jwks, upstream))
    loop = ToolLoop(upstream)
    loop.reconnect = lambda: run(connect(service, jwks, upstream, token=token_body(access="access-2", refresh="refresh-2")))
    plan_ = acc.plan_for("plan_granted", connections=["chatgpt-owner"])
    return service, loop, plan_


def call(service, plan_, surface, body, *, who: str = "app_test", slug: str = "gpt-test"):
    """One request as ``who``; the whole response body is read inside the same event loop."""
    ident = acc.identity(plan_, app_id=who, key_hash=f"sha256:{who}", name=who)

    async def go():
        response = await execute(service, KIND, slug, surface=surface, body=body, identity=ident,
                                 access=acc.access(plan_), app_profile=None,
                                 request_path="/v1/" + ("responses" if surface == "responses" else "chat/completions"))
        chunks = []
        if hasattr(response, "body_iterator"):
            async for chunk in response.body_iterator:
                chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode())
            return response, b"".join(chunks)
        return response, response.body

    return run(go())


def responses_turn(service, plan_, history: list, *, stream: bool = False, key: str | None = None, who: str = "app_test",
                   slug: str = "gpt-test"):
    body = {"model": MODEL.replace("gpt-test", slug), "instructions": "use tools", "input": history, "stream": stream}
    if key:
        body["prompt_cache_key"] = key
    response, raw = call(service, plan_, "responses", body, who=who, slug=slug)
    assert response.status_code == 200, raw
    if stream:
        items = [json.loads(line[5:])["item"] for line in raw.decode().splitlines()
                 if line.startswith("data:") and '"response.output_item.done"' in line]
    else:
        items = json.loads(raw)["output"]
    return response, items


# ------------------------------------------------------------------ the route, end to end

@pytest.mark.parametrize("stream", [False, True])
def test_a_tool_loop_replays_the_first_value_and_a_new_user_turn_sends_none(plan, stream):
    service, loop, plan_ = plan
    history = [user("look it up")]
    first, out = responses_turn(service, plan_, history, stream=stream)            # turn 1 starts: upstream says -1
    history += out + [tool_output(out[0]["call_id"])]
    second, out = responses_turn(service, plan_, history, stream=stream)           # tool result: same turn
    history += out
    history += [user("again")]
    third, out = responses_turn(service, plan_, history, stream=stream)            # turn 2 starts: upstream says -3
    history += out + [tool_output(out[0]["call_id"])]
    fourth, _ = responses_turn(service, plan_, history, stream=stream)
    assert loop.sent == [None, loop.value(1), None, loop.value(3)]
    assert [r.headers["x-azizam-turn-state"] for r in (first, second, third, fourth)] == \
        ["started", "replayed", "started", "replayed"]


def test_a_later_value_in_the_same_turn_does_not_replace_the_first(plan):
    """codex-rs core/tests/suite/turn_state.rs websocket_turn_state_is_stable_within_turn: ts-1, ts-1, not ts-2."""
    service, loop, plan_ = plan
    history = [user("two lookups")]
    _, out = responses_turn(service, plan_, history)
    for n in range(3):
        history += out + [tool_output(out[-1].get("call_id") or f"call_x{n}", f"r{n}")]
        _, out = responses_turn(service, plan_, history)
    assert loop.sent == [None, loop.value(1), loop.value(1), loop.value(1)]


@pytest.mark.parametrize("stream", [False, True])
def test_chat_completions_tool_messages_continue_the_turn(plan, stream):
    service, loop, plan_ = plan
    messages = [{"role": "system", "content": "use tools"}, {"role": "user", "content": "look it up"}]
    base = {"model": MODEL, "stream": stream, "tools": [{"type": "function", "function": {"name": "lookup"}}]}
    first, raw = call(service, plan_, "chat", {**base, "messages": messages})
    assert first.status_code == 200, raw
    messages += [{"role": "assistant", "content": None, "tool_calls": [
                     {"id": "call_1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": "call_1", "content": "42"}]
    second, raw = call(service, plan_, "chat", {**base, "messages": messages})
    assert second.status_code == 200, raw
    messages += [{"role": "assistant", "content": "done 2"}, {"role": "user", "content": "thanks"}]
    third, raw = call(service, plan_, "chat", {**base, "messages": messages})
    assert third.status_code == 200, raw
    assert loop.sent == [None, loop.value(1), None]
    assert [r.headers["x-azizam-turn-state"] for r in (first, second, third)] == ["started", "replayed", "started"]


def test_two_conversations_never_share_a_value(plan):
    service, loop, plan_ = plan
    a = [user("conversation a")]
    b = [user("conversation b")]
    _, out_a = responses_turn(service, plan_, a)
    _, out_b = responses_turn(service, plan_, b)
    a += out_a + [tool_output(out_a[0]["call_id"])]
    b += out_b + [tool_output(out_b[0]["call_id"])]
    responses_turn(service, plan_, b)
    responses_turn(service, plan_, a)
    assert loop.bodies[0]["prompt_cache_key"] != loop.bodies[1]["prompt_cache_key"]
    assert loop.sent == [None, None, loop.value(2), loop.value(1)]


def test_two_callers_with_the_same_client_key_never_share_a_value(plan):
    """Same prompt_cache_key, same input, two apps: each has its own turn."""
    service, loop, plan_ = plan
    history = [user("shared key")]
    _, out = responses_turn(service, plan_, history, key="shared-7", who="app_alice")
    continued = history + out + [tool_output(out[0]["call_id"])]
    responses_turn(service, plan_, continued, key="shared-7", who="app_bob")      # bob never started this turn
    responses_turn(service, plan_, continued, key="shared-7", who="app_alice")
    assert loop.sent == [None, None, loop.value(1)]


def test_switching_the_model_mid_loop_does_not_carry_the_value(tmp_path):
    """Same caller, key and history, another model: the first model's turn value is not sent to the second."""
    upstream = Upstream()
    service, _store, jwks = make_service(tmp_path, upstream)
    upstream.routes[("GET", "/v1/models")] = json_route({"models": [
        {"slug": slug, "visibility": "list", "supported_reasoning_levels": [{"effort": "low", "description": "x"}]}
        for slug in ("gpt-test", "gpt-test-2")]})
    run(connect(service, jwks, upstream))
    loop = ToolLoop(upstream)
    plan_ = acc.plan_for("plan_granted", connections=["chatgpt-owner"])
    history = [user("look it up")]
    _, out = responses_turn(service, plan_, history, key="switch-1")
    history += out + [tool_output(out[0]["call_id"])]
    responses_turn(service, plan_, history, key="switch-1", slug="gpt-test-2")
    assert loop.sent == [None, None]


def test_a_conversation_that_went_on_unseen_starts_a_new_turn(plan):
    """The gateway did not see the request that began turn 2 (it went elsewhere or failed before sending). A tool
    result in turn 2 must not get turn 1's value: the items added since the last request hold a user message."""
    service, loop, plan_ = plan
    history = [user("first")]
    _, out = responses_turn(service, plan_, history)
    history += out + [tool_output(out[0]["call_id"])]
    _, out = responses_turn(service, plan_, history)
    history += out + [user("second"), {"type": "function_call", "call_id": "call_z", "name": "lookup", "arguments": "{}"},
                      tool_output("call_z")]
    responses_turn(service, plan_, history)
    assert loop.sent == [None, loop.value(1), None]


def test_connecting_the_plan_again_drops_the_turn_state(plan):
    """Codex clears turn state when the signed-in account changes. A new connection may be another account."""
    service, loop, plan_ = plan
    history = [user("look it up")]
    _, out = responses_turn(service, plan_, history)
    loop.reconnect()
    history += out + [tool_output(out[0]["call_id"])]
    response, _ = responses_turn(service, plan_, history)
    assert loop.sent == [None, None]
    assert response.headers["x-azizam-turn-state"] == "started"


def test_a_kind_without_the_header_sends_and_discloses_nothing(plan, monkeypatch):
    service, loop, plan_ = plan
    bare = replace(KIND, request={k: v for k, v in KIND.request.items() if k != "turn_state_header"})
    monkeypatch.setattr(service, "kind", lambda _connection_id: bare)
    history = [user("no turn state")]
    first, out = responses_turn(service, plan_, history)
    history += out + [tool_output(out[0]["call_id"])]
    second, _ = responses_turn(service, plan_, history)
    assert loop.sent == [None, None]
    assert "x-azizam-turn-state" not in first.headers and "x-azizam-turn-state" not in second.headers


def test_the_value_is_in_no_log_and_no_receipt(plan, monkeypatch, caplog):
    service, loop, plan_ = plan
    seen = []
    original = receipts.RouteReceipt

    class Spy(original):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen.append(self)

    monkeypatch.setattr(receipts, "RouteReceipt", Spy)
    caplog.set_level(logging.DEBUG)
    history = [user("look it up")]
    _, out = responses_turn(service, plan_, history, stream=True)
    history += out + [tool_output(out[0]["call_id"])]
    response, _ = responses_turn(service, plan_, history, stream=True)
    assert loop.sent[1] == loop.value(1)
    assert SECRET_STATE not in json.dumps(dict(response.headers))
    assert len(seen) == 2
    for receipt in seen:
        assert SECRET_STATE not in json.dumps(dataclasses.asdict(receipt), default=str)
    assert [r.metadata.get("plan_turn_state") for r in seen] == ["started", "replayed"]
    assert all(SECRET_STATE not in record.getMessage() for record in caplog.records)
    stored = b"".join(path.read_bytes() for path in receipt_store.DB_PATH.parent.glob(receipt_store.DB_PATH.name + "*"))
    assert SECRET_STATE.encode() not in stored


def test_an_error_reply_keeps_no_value(plan):
    """Only a reply the upstream accepted sets the turn's value. The retry of a failed tool follow-up sends none."""
    service, loop, plan_ = plan
    loop.fail_next = True
    history = [user("x"), {"type": "function_call", "call_id": "c", "name": "lookup", "arguments": "{}"}, tool_output("c")]
    response, _ = call(service, plan_, "responses", {"model": MODEL, "input": history})
    assert response.status_code == 400
    responses_turn(service, plan_, history)
    assert loop.sent == [None, None]


# ------------------------------------------------------------------ the store and the rule, without the route

def continues(items, start):
    return responses_plan.continues_turn(items, start)


def test_the_rule_reads_the_last_item_and_what_was_added():
    call_ = {"type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"}
    assert continues([user("x"), call_, tool_output("c")], 1)
    assert continues([user("x"), call_, {"type": "custom_tool_call_output", "call_id": "c", "output": "o"}], 1)
    assert not continues([user("x")], 0)
    assert not continues([user("x"), call_, tool_output("c"), user("y")], 1)
    assert not continues([user("x"), call_, tool_output("c")], 0)                     # the user message is new
    developer = {"type": "message", "role": "developer", "content": "note"}
    assert not continues([user("x"), call_, developer, tool_output("c")], 1)
    assert continues([user("x"), call_, tool_output("c")], 3)                         # the same request again


def test_the_store_keeps_the_first_value_of_a_turn_and_drops_it_at_the_next():
    store = turn_state.TurnStates()
    items = [user("x")]
    turn, carried, value = store.outgoing("s", items, continues)
    assert (carried, value) == (False, None)
    store.incoming("s", turn, "v1")
    items = items + [{"type": "function_call", "call_id": "c"}, tool_output("c")]
    turn2, carried, value = store.outgoing("s", items, continues)
    assert (turn2, carried, value) == (turn, True, "v1")
    store.incoming("s", turn2, "v2")                                                    # ignored: the turn has one
    items = items + [tool_output("c")]
    assert store.outgoing("s", items, continues)[2] == "v1"
    turn3, carried, value = store.outgoing("s", items + [user("y")], continues)
    assert (carried, value) == (False, None) and turn3 != turn


def test_a_late_reply_from_an_earlier_turn_is_not_kept():
    store = turn_state.TurnStates()
    old, _, _ = store.outgoing("s", [user("x")], continues)
    store.outgoing("s", [user("x"), user("y")], continues)                             # a new turn began
    store.incoming("s", old, "late")
    assert store.outgoing("s", [user("x"), user("y"), tool_output("c")], continues)[2] is None


def test_a_changed_history_starts_a_new_turn():
    store = turn_state.TurnStates()
    turn, _, _ = store.outgoing("s", [user("x")], continues)
    store.incoming("s", turn, "v1")
    assert store.outgoing("s", [user("other"), tool_output("c")], continues)[1:] == (False, None)


def test_the_store_is_bounded_by_count_and_by_idle_time():
    now = [0.0]
    store = turn_state.TurnStates(clock=lambda: now[0], max_conversations=2, idle_seconds=60)
    for scope in ("a", "b", "c"):
        turn, _, _ = store.outgoing(scope, [user(scope)], continues)
        store.incoming(scope, turn, f"v-{scope}")
    assert len(store) == 2
    follow = [tool_output("c")]
    assert store.outgoing("a", [user("a"), *follow], continues)[2] is None            # "a" was the least recent
    assert store.outgoing("c", [user("c"), *follow], continues)[2] == "v-c"
    now[0] = 61.0
    assert len(store) == 0
    assert store.outgoing("c", [user("c"), *follow, *follow], continues)[2] is None


@pytest.mark.parametrize("value", ["", "a\r\nb", "café", "x" * 4097, None, 7, "two words"])
def test_a_value_that_cannot_be_a_header_is_not_kept(value):
    store = turn_state.TurnStates()
    turn, _, _ = store.outgoing("s", [user("x")], continues)
    store.incoming("s", turn, value)
    assert store.outgoing("s", [user("x"), tool_output("c")], continues)[2] is None


def test_scopes_differ_by_caller_connection_and_key():
    base = turn_state.scope_of("conn", "app", None, "kh", "name", "key")
    assert base == turn_state.scope_of("conn", "app", None, "kh", "name", "key")
    assert len({base, turn_state.scope_of("conn", "app2", None, "kh", "name", "key"),
                turn_state.scope_of("conn2", "app", None, "kh", "name", "key"),
                turn_state.scope_of("conn", "app", None, "kh", "name", "key2")}) == 4
    assert "key" not in base


def test_the_row_names_the_header_and_a_bad_name_is_refused():
    assert responses_plan.turn_state_header(KIND) == HEADER
    good = {"upstream": dict(KIND.upstream), "request": {"turn_state_header": HEADER}}
    responses_plan.validate(good, source="row")
    with pytest.raises(ValueError, match="turn_state_header"):
        responses_plan.validate({**good, "request": {"turn_state_header": "bad header\n"}}, source="row")
