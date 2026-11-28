"""Plan models over several turns: the request carries what Codex CLI's Responses request carries, the prefix and the
cache key stay stable, reasoning items round-trip on /v1/responses, and usage is counted the way the upstream counts it.

Codex source checked (openai/codex at e0a6d05): codex-rs/core/src/client.rs ``build_responses_request`` sends
store=false, stream=true, include=["reasoning.encrypted_content"], reasoning={effort,...}, prompt_cache_key and the
whole input every turn.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from gateway import subscription_execution as execution
from tests.support import access as acc
from tests.support.subscription_routes import execute
from gateway import receipts
from tests.support.subscription_fakes import KIND, Upstream, body_of, connect, make_service

MODEL = "chatgpt-plan/gpt-test"
PATH = "/v1/responses"
REASONING_ITEM = {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "ENC-1"}


def run(coro):
    return asyncio.run(coro)


def sse(*events: dict) -> bytes:
    return b"".join(b"event: " + e["type"].encode() + b"\ndata: " + json.dumps(e).encode() + b"\n\n" for e in events)


class Chat:
    """A fake plan upstream that counts cache hits the way a prefix cache would: the cached part is the input
    prefix it saw on the previous turn."""

    def __init__(self, upstream: Upstream) -> None:
        self.upstream = upstream
        self.bodies: list[dict] = []
        self.headers: list[httpx.Headers] = []
        upstream.routes[("POST", PATH)] = self.answer

    def answer(self, request: httpx.Request) -> httpx.Response:
        body = body_of(request)
        self.bodies.append(body)
        self.headers.append(request.headers)
        turn = len(self.bodies)
        cached = 0 if turn == 1 else 1000 * (turn - 1)
        output = [dict(REASONING_ITEM, id=f"rs_{turn}", encrypted_content=f"ENC-{turn}"),
                  {"type": "message", "role": "assistant", "id": f"msg_{turn}",
                   "content": [{"type": "output_text", "text": f"answer {turn}"}]}]
        usage = {"input_tokens": 1000 * turn + 50, "output_tokens": 40 + turn, "total_tokens": 1000 * turn + 90 + turn,
                 "input_tokens_details": {"cached_tokens": cached}, "output_tokens_details": {"reasoning_tokens": 7 * turn}}
        done = {"type": "response.completed", "response": {"id": f"resp_{turn}", "status": "completed", "output": output, "usage": usage}}
        return httpx.Response(200, content=sse({"type": "response.created", "response": {}}, done),
                              headers={"content-type": "text/event-stream"})


@pytest.fixture(autouse=True)
def _receipts_table():
    from gateway import receipt_store
    receipt_store.init_db()


@pytest.fixture()
def plan(tmp_path):
    upstream = Upstream()
    service, store, jwks = make_service(tmp_path, upstream)
    run(connect(service, jwks, upstream))
    chat = Chat(upstream)
    plan_ = acc.plan_for("plan_granted", connections=["chatgpt-owner"])
    return service, chat, acc.identity(plan_), acc.access(plan_)


def send(service, ident, access, surface, body):
    return run(execute(service, KIND, "gpt-test", surface=surface, body=body, identity=ident, access=access,
                                 app_profile=None, request_path="/v1/" + ("responses" if surface == "responses" else "chat/completions")))


def prefix_bytes(items: list, count: int) -> bytes:
    return json.dumps(items[:count], sort_keys=False, separators=(",", ":")).encode()


# ------------------------------------------------------------------ /v1/responses (the path under acceptance)

def test_responses_turns_round_trip_reasoning_and_keep_one_cache_key(plan):
    service, chat, ident, access = plan
    first_input = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}]
    reply1 = json.loads(send(service, ident, access, "responses", {
        "model": MODEL, "instructions": "be brief", "input": first_input, "reasoning": {"effort": "low"}}).body)
    assert reply1["output"][0]["encrypted_content"] == "ENC-1"  # the upstream reply is returned unchanged
    # Turn 2: the client sends the whole conversation again, including the returned reasoning item.
    second_input = first_input + reply1["output"] + [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "and then?"}]}]
    reply2 = json.loads(send(service, ident, access, "responses", {
        "model": MODEL, "instructions": "be brief", "input": second_input, "reasoning": {"effort": "low"}}).body)
    third_input = second_input + reply2["output"] + [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "thanks"}]}]
    send(service, ident, access, "responses", {
        "model": MODEL, "instructions": "be brief", "input": third_input, "reasoning": {"effort": "low"}})

    one, two, three = chat.bodies
    for body in chat.bodies:
        assert body["store"] is False and body["stream"] is True
        assert body["reasoning"] == {"effort": "low"}
        assert "reasoning.encrypted_content" in body["include"]
        assert body["model"] == "gpt-test"
    assert one["prompt_cache_key"] == two["prompt_cache_key"] == three["prompt_cache_key"]
    # The reasoning items come back to the upstream exactly as it sent them, and the earlier input is byte-identical.
    assert REASONING_ITEM["type"] in [item["type"] for item in two["input"]]
    assert next(item for item in two["input"] if item["type"] == "reasoning")["encrypted_content"] == "ENC-1"
    assert prefix_bytes(two["input"], len(first_input)) == prefix_bytes(one["input"], len(first_input))
    assert prefix_bytes(three["input"], len(second_input)) == prefix_bytes(two["input"], len(second_input))
    assert one["instructions"] == two["instructions"] == three["instructions"]


def test_a_client_key_is_kept_and_include_entries_are_merged(plan):
    service, chat, ident, access = plan
    send(service, ident, access, "responses", {
        "model": MODEL, "prompt_cache_key": "client-key", "include": ["message.output_text.logprobs"],
        "input": [{"type": "message", "role": "user", "content": "hi"}]})
    body = chat.bodies[0]
    assert body["prompt_cache_key"] == "client-key"
    assert body["include"] == ["reasoning.encrypted_content", "message.output_text.logprobs"]


def test_a_chat_client_key_is_kept(plan):
    # OpenAI Chat Completions takes prompt_cache_key too; a Chat caller that sends one keeps its conversations apart.
    service, chat, ident, access = plan
    send(service, ident, access, "chat", {
        "model": MODEL, "prompt_cache_key": "client-key", "messages": [{"role": "user", "content": "hi"}]})
    assert chat.bodies[0]["prompt_cache_key"] == "client-key"


def test_different_conversations_get_different_keys(plan):
    service, chat, ident, access = plan
    for text in ("one", "two"):
        send(service, ident, access, "responses", {"model": MODEL, "input": [{"type": "message", "role": "user", "content": text}]})
    assert chat.bodies[0]["prompt_cache_key"] != chat.bodies[1]["prompt_cache_key"]


def test_an_unadvertised_effort_is_a_named_400_and_nothing_is_sent(plan):
    service, chat, ident, access = plan
    for surface, body in (("responses", {"model": MODEL, "reasoning": {"effort": "ultra"}, "input": [{"type": "message", "role": "user", "content": "x"}]}),
                          ("chat", {"model": MODEL, "reasoning_effort": "ultra", "messages": [{"role": "user", "content": "x"}]})):
        response = send(service, ident, access, surface, body)
        error = json.loads(response.body)["error"]
        assert response.status_code == 400 and error["code"] == "plan_unsupported_reasoning_effort"
        assert "low" in error["message"] and error["param"] == "reasoning.effort"
    assert chat.bodies == []


def test_the_receipt_counts_input_cached_output_and_reasoning(plan, monkeypatch):
    service, chat, ident, access = plan
    seen = []
    original = receipts.RouteReceipt

    class Spy(original):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            seen.append(self)

    monkeypatch.setattr(receipts, "RouteReceipt", Spy)
    for turn in range(2):
        send(service, ident, access, "responses", {"model": MODEL, "input": [{"type": "message", "role": "user", "content": f"t{turn}"}]})
    assert seen[0].usage == {"input_tokens": 1050, "cached_input_tokens": 0, "output_tokens": 41, "reasoning_tokens": 7}
    assert seen[1].usage["cached_input_tokens"] == 1000 and seen[1].cost["usd"] is None and seen[1].cost["mode"] == "plan"


# ------------------------------------------------------------------ /v1/chat/completions (second)

def messages_for(turns: int) -> list[dict]:
    out = [{"role": "system", "content": "be brief"}]
    for turn in range(1, turns + 1):
        out.append({"role": "user", "content": f"question {turn}"})
        if turn < turns:
            out.append({"role": "assistant", "content": f"answer {turn}"})
    return out


def test_chat_turns_keep_a_byte_stable_prefix_and_one_cache_key(plan):
    service, chat, ident, access = plan
    replies = [json.loads(send(service, ident, access, "chat", {
        "model": MODEL, "reasoning_effort": "low", "messages": messages_for(turn)}).body) for turn in (1, 2, 3)]
    one, two, three = chat.bodies
    assert all(body["reasoning"] == {"effort": "low"} for body in chat.bodies)
    assert one["prompt_cache_key"] == two["prompt_cache_key"] == three["prompt_cache_key"]
    assert one["instructions"] == two["instructions"] == three["instructions"] == "be brief"
    assert prefix_bytes(two["input"], len(one["input"])) == prefix_bytes(one["input"], len(one["input"]))
    assert prefix_bytes(three["input"], len(two["input"])) == prefix_bytes(two["input"], len(two["input"]))
    assert all("ENC" not in json.dumps(body["input"]) for body in chat.bodies)  # chat cannot carry reasoning items
    usage = replies[1]["usage"]
    assert usage["prompt_tokens"] == 2050 and usage["completion_tokens"] == 42
    assert usage["prompt_tokens_details"]["cached_tokens"] == 1000
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 14
    assert replies[0]["usage"]["prompt_tokens_details"]["cached_tokens"] == 0


def test_chat_and_responses_share_the_cache_key_for_the_same_conversation(plan):
    service, chat, ident, access = plan
    send(service, ident, access, "chat", {"model": MODEL, "messages": [{"role": "system", "content": "be brief"},
                                                                         {"role": "user", "content": "hello"}]})
    send(service, ident, access, "responses", {"model": MODEL, "instructions": "be brief", "input": [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}]})
    assert chat.bodies[0]["prompt_cache_key"] == chat.bodies[1]["prompt_cache_key"]


def test_upstream_4xx_never_exposes_its_distinctive_text(plan):
    service, chat, ident, access = plan
    from tests.support.subscription_fakes import json_route
    distinctive = "DISTINCTIVE-UPSTREAM-VALIDATION-TEXT-DO-NOT-ECHO"
    chat.upstream.routes[("POST", PATH)] = json_route({"error": {"message": distinctive, "code": "invalid_request"}}, 400)
    response = send(service, ident, access, "responses", {
        "model": MODEL, "input": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 400
    assert distinctive not in response.body.decode()
    assert chat.upstream.count(PATH) == 1


AFFINITY = ("session-id", "thread-id", "x-client-request-id")


def test_every_turn_carries_the_cache_key_in_the_cache_affinity_headers(plan):
    """ChatGPT-account traffic picks its cache shard from the session-id header, not the body key (codex-rs client;
    Codex PR #44862). Every turn of one conversation sends the same key in the header and in the body."""
    service, chat, ident, access = plan
    first = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}]
    reply = json.loads(send(service, ident, access, "responses", {"model": MODEL, "instructions": "be brief", "input": first}).body)
    second = first + reply["output"] + [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "more"}]}]
    send(service, ident, access, "responses", {"model": MODEL, "instructions": "be brief", "input": second})
    send(service, ident, access, "chat", {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})
    keys = [body["prompt_cache_key"] for body in chat.bodies]
    for headers, key in zip(chat.headers, keys):
        assert [headers.get(name) for name in AFFINITY] == [key, key, key]
    assert keys[0] == keys[1] and keys[2] != keys[0]


def test_a_client_cache_key_is_the_header_value_too(plan):
    service, chat, ident, access = plan
    send(service, ident, access, "responses", {"model": MODEL, "prompt_cache_key": "my-thread-7",
                                               "input": [{"role": "user", "content": "hi"}]})
    assert chat.headers[0]["session-id"] == "my-thread-7"


def test_a_kind_without_affinity_headers_sends_none():
    from dataclasses import replace
    from gateway.protocols import responses_plan
    bare = replace(KIND, request={k: v for k, v in KIND.request.items() if k != "cache_affinity_headers"})
    assert responses_plan._cache_affinity_headers(bare, {"prompt_cache_key": "k"}) == {}
    assert responses_plan._cache_affinity_headers(KIND, {}) == {}
    assert responses_plan._cache_affinity_headers(KIND, {"prompt_cache_key": "k"}) == dict.fromkeys(AFFINITY, "k")


def _tool_output(content):
    from gateway.protocols import responses_plan
    body = {"model": "m", "messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "shot", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": content}]}
    items = responses_plan._chat_to_responses(body, KIND, "gpt-6.1-terra")["input"]
    return next(item["output"] for item in items if item["type"] == "function_call_output")


def test_an_image_tool_result_goes_to_the_plan_as_input_image():
    # Responses function_call_output.output may be a list of input_text and input_image items (OpenAI input items).
    out = _tool_output([{"type": "text", "text": "the page"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}])
    assert out == [{"type": "input_text", "text": "the page"},
                   {"type": "input_image", "image_url": "data:image/png;base64,YQ=="}]


def test_a_text_tool_result_stays_one_string():
    assert _tool_output("done") == "done"
    assert _tool_output([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "ab"
