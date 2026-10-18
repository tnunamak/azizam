"""The http-v1 executor against a fake upstream: requests, framing, auth, budgets, jobs."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from urllib.parse import quote, quote_plus

import httpx
import pytest

from gateway.engine_protocol import description, executor, transport
from gateway.engine_protocol.assets import MemoryAssetStore
from gateway.engine_protocol.budgets import Budgets
from gateway.engine_protocol.errors import (
    BudgetExceeded, MalformedResponse, MappingError, RedirectRefused, TransportFailure, UnknownOperation, UpstreamError,
)
from gateway.engine_protocol.types import AssetRef, CanonicalRequest, Connection, Credential, JobState, Timeouts
from tests.support.fake_upstream import HandlerTransport

BASE = "http://engine.test:8080"
SECRET = "sk-test-SECRET-1234"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8  # the first bytes of a PNG: assets are sniffed


def desc(operations: dict, *, auth: dict | None = None, errors: list | None = None, budgets: Budgets | None = None):
    doc: dict[str, Any] = {"protocol_version": 1, "id": "t", "executor": "http-v1", "operations": operations}
    if auth is not None:
        doc["auth"] = auth
    if errors is not None:
        doc["errors"] = errors
    return description.parse_description(doc, source="t.yml", **({"budgets": budgets} if budgets else {}))


class Fake:
    """Records requests; answers with a handler. Chunked answers come from ``Chunks``."""

    def __init__(self, handler) -> None:
        self.requests: list[httpx.Request] = []
        self.handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, *, gate=None) -> None:
        self.chunks = chunks
        self.closed = False
        self.sent = 0
        self.gate = gate

    async def __aiter__(self):
        for index, chunk in enumerate(self.chunks):
            if self.gate is not None and index > 0:
                await self.gate.wait()
                self.gate.clear()
            self.sent += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def make(operations, handler, *, auth=None, errors=None, budgets=None, assets=None):
    fake = Fake(handler)
    ex = executor.HttpV1Executor(
        desc(operations, auth=auth, errors=errors, budgets=budgets),
        transport=transport.HttpxTransport.for_tests(HandlerTransport(fake)),
        assets=assets or MemoryAssetStore(),
        **({"budgets": budgets} if budgets else {}),
    )
    return ex, fake


def conn(**kw: Any) -> Connection:
    kw.setdefault("address", BASE)
    return Connection(**kw)


def req(op: str, body: dict | None = None, **kw: Any) -> CanonicalRequest:
    return CanonicalRequest(op=op, body=body if body is not None else {}, **kw)


def run(coro):
    return asyncio.run(coro)


MODELS = {"models": {"class": "inventory", "request": {"method": "GET", "path": "/v1/models"},
                     "response": {"framing": "json", "select": {"ids": "/data/*/id", "first": "/data/0/id", "none": "/zz"}}}}


# --- requests and json -----------------------------------------------------------------------


def test_a_get_with_json_selection() -> None:
    ex, fake = make(MODELS, lambda r: httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]}))
    reply = run(ex.invoke(conn(), req("models")))
    assert reply.status == 200
    assert reply.selected == {"ids": ["a", "b"], "first": "a"}  # an absent plain pointer is not selected
    assert reply.body == {"data": [{"id": "a"}, {"id": "b"}]}
    sent = fake.requests[0]
    assert (sent.method, str(sent.url)) == ("GET", f"{BASE}/v1/models")
    assert "authorization" not in sent.headers
    assert sent.headers["accept-encoding"] == "identity"


def test_an_unknown_operation_is_refused_before_anything_is_sent() -> None:
    ex, fake = make(MODELS, lambda r: httpx.Response(200, json={}))
    with pytest.raises(UnknownOperation):
        run(ex.invoke(conn(), req("nope")))
    assert fake.requests == []


def test_a_json_body_is_built_by_the_map_and_sent_as_exact_bytes() -> None:
    ops = {"chat": {"class": "generate", "request": {"method": "POST", "path": "/api/v3/converse", "body": {"encoding": "json", "map": [
        {"copy": {"from": "/body/model", "to": "/engine/model"}},
        {"copy": {"from": "/body/messages", "to": "/input/messages"}},
        {"set": {"to": "/stream", "value": False}},
        {"when": {"if": {"path": "/body/temperature", "present": True}, "then": [{"copy": {"from": "/body/temperature", "to": "/sampling/t"}}]}},
    ]}}, "response": {"framing": "json"}}}
    ex, fake = make(ops, lambda r: httpx.Response(200, json={"ok": True}))
    run(ex.invoke(conn(), req("chat", {"model": "m", "messages": [{"role": "user", "content": "héllo"}], "temperature": 0.5, "ignored": 1})))
    sent = fake.requests[0]
    assert sent.content == '{"engine":{"model":"m"},"input":{"messages":[{"role":"user","content":"héllo"}]},"stream":false,"sampling":{"t":0.5}}'.encode()
    assert sent.headers["content-type"] == "application/json"


def test_passthrough_bodies_are_sent_without_unmapped_changes() -> None:
    ops = {"echo": {"class": "generate", "request": {"method": "POST", "path": "/e", "body": {"encoding": "json"}}, "response": {"framing": "json"}}}
    ex, fake = make(ops, lambda r: httpx.Response(200, json={}))
    run(ex.invoke(conn(), req("echo", {"a": [1, {"b": None}], "c": "d"})))
    assert json.loads(fake.requests[0].content) == {"a": [1, {"b": None}], "c": "d"}


def test_query_values_come_from_the_request_with_defaults_and_percent_encoding() -> None:
    ops = {"list": {"class": "inventory", "request": {"method": "GET", "path": "/items", "query": [
        {"name": "q", "from": "/params/q"}, {"name": "limit", "from": "/params/limit", "default": 20}, {"name": "skip", "from": "/params/skip"},
        {"name": "flag", "from": "/params/flag"}]}, "response": {"framing": "json"}}}
    ex, fake = make(ops, lambda r: httpx.Response(200, json=[]))
    run(ex.invoke(conn(), req("list", params={"q": "a b&c=d/é", "flag": True})))
    assert str(fake.requests[0].url) == f"{BASE}/items?q=a%20b%26c%3Dd%2F%C3%A9&limit=20&flag=true"


def test_path_parameters_are_percent_encoded_and_cannot_change_the_path() -> None:
    ops = {"get": {"class": "inventory", "request": {"method": "GET", "path": "/items/{name}/info"}, "response": {"framing": "json"}}}
    ex, fake = make(ops, lambda r: httpx.Response(200, json={}))
    run(ex.invoke(conn(), req("get", params={"name": "../../admin?x=1#f"})))
    assert fake.requests[0].url.raw_path == b"/items/..%2F..%2Fadmin%3Fx%3D1%23f/info"
    with pytest.raises(MappingError):
        run(ex.invoke(conn(), req("get", params={})))
    with pytest.raises(MappingError):
        run(ex.invoke(conn(), req("get", params={"name": ""})))


DOT_PATH = {"get": {"class": "inventory", "request": {"method": "GET", "path": "/api/v1/models/{id}/unload"}, "response": {"framing": "json"}}}


@pytest.mark.parametrize("value", ["..", ".", "%2F", "a%2fb", "x%5Cy", "%2e%2e%2F"])
def test_a_path_value_cannot_climb_the_path_or_carry_an_encoded_slash(value: str) -> None:
    ex, fake = make(DOT_PATH, lambda r: httpx.Response(200, json={}))
    with pytest.raises(MappingError):
        run(ex.invoke(conn(), req("get", params={"id": value})))
    assert fake.requests == []


def test_two_placeholders_cannot_join_into_a_dot_segment() -> None:
    ops = {"get": {"class": "inventory", "request": {"method": "GET", "path": "/v1/{a}{b}/x"}, "response": {"framing": "json"}}}
    ex, fake = make(ops, lambda r: httpx.Response(200, json={}))
    with pytest.raises(MappingError):
        run(ex.invoke(conn(), req("get", params={"a": ".", "b": "."})))
    assert fake.requests == []


@pytest.mark.parametrize("value", ["a.b", "..x", "x..", "v1.5", "a/b", "...", "é"])
def test_a_path_value_with_dots_inside_a_name_is_still_sent_encoded(value: str) -> None:
    ex, fake = make(DOT_PATH, lambda r: httpx.Response(200, json={}))
    run(ex.invoke(conn(), req("get", params={"id": value})))
    segments = fake.requests[0].url.raw_path.split(b"/")
    assert len(segments) == 6 and segments[4] not in (b".", b"..")
    assert segments[5] == b"unload"


def test_an_address_with_a_path_prefix_keeps_it() -> None:
    ex, fake = make(MODELS, lambda r: httpx.Response(200, json={}))
    run(ex.invoke(conn(address=BASE + "/openai/"), req("models")))
    assert fake.requests[0].url.path == "/openai/v1/models"


@pytest.mark.parametrize("address", ["ftp://x", "http://u:p@x", "http://x?y=1", "http://x#f", "x:80", ""])
def test_a_connection_address_is_a_plain_http_origin_with_an_optional_path(address: str) -> None:
    with pytest.raises(ValueError):
        conn(address=address)


def test_the_response_map_builds_the_canonical_answer() -> None:
    ops = {"chat": {"class": "generate", "request": {"method": "POST", "path": "/c", "body": {"encoding": "json"}}, "response": {"framing": "json", "map": [
        {"set": {"to": "/choices", "value": [{"index": 0, "message": {"role": "assistant", "content": None}, "finish_reason": None}]}},
        {"copy": {"from": "/body/result/output/text", "to": "/choices/0/message/content"}},
        {"enum": {"from": "/body/result/why", "to": "/choices/0/finish_reason", "table": {"eos": "stop", "len": "length"}, "unmapped": "pass"}},
        {"copy": {"from": "/status", "to": "/upstream_status"}},
    ]}}}
    ex, _ = make(ops, lambda r: httpx.Response(200, json={"result": {"output": {"text": "hi"}, "why": "eos"}}))
    reply = run(ex.invoke(conn(), req("chat", {"x": 1})))
    assert reply.body == {"choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}], "upstream_status": 200}


# --- auth ------------------------------------------------------------------------------------


def authed(kind: dict, op_class: str = "generate"):
    ops = {"x": {"class": op_class, "request": {"method": "GET", "path": "/x", "query": []}, "response": {"framing": "json"}}}
    return make(ops, lambda r: httpx.Response(200, json={}), auth={"inference": kind, "control": kind})


def test_bearer_header_and_overrides_from_the_connection() -> None:
    ex, fake = authed({"kind": "bearer"})
    run(ex.invoke(conn(credentials={"inference": Credential(SECRET)}), req("x")))
    assert fake.requests[0].headers["authorization"] == f"Bearer {SECRET}"
    run(ex.invoke(conn(credentials={"inference": Credential(SECRET, header="X-Key", scheme="")}), req("x")))
    assert fake.requests[1].headers["x-key"] == SECRET and "authorization" not in fake.requests[1].headers


def test_header_and_query_auth() -> None:
    ex, fake = authed({"kind": "header", "name": "X-Engine-Key"})
    run(ex.invoke(conn(credentials={"inference": Credential(SECRET)}), req("x")))
    assert fake.requests[0].headers["x-engine-key"] == SECRET
    ex, fake = authed({"kind": "query", "name": "api_key"})
    run(ex.invoke(conn(credentials={"inference": Credential(SECRET)}), req("x")))
    assert str(fake.requests[0].url) == f"{BASE}/x?api_key={SECRET}"


def test_a_credential_the_protocol_does_not_use_is_never_sent() -> None:
    ex, fake = authed({"kind": "none"})
    run(ex.invoke(conn(credentials={"inference": Credential(SECRET)}), req("x")))
    assert SECRET not in str(fake.requests[0].headers) and SECRET not in str(fake.requests[0].url)


def test_operation_class_picks_the_credential_class() -> None:
    ex, fake = authed({"kind": "bearer"}, op_class="control")
    run(ex.invoke(conn(credentials={"inference": Credential("inf-secret")}), req("x")))
    assert "authorization" not in fake.requests[0].headers  # the inference key is not a control key
    run(ex.invoke(conn(credentials={"inference": Credential("inf-secret"), "control": Credential("ctl-secret")}), req("x")))
    assert fake.requests[1].headers["authorization"] == "Bearer ctl-secret"


@pytest.mark.parametrize("name", ["X-Test\n", "X-Test\r\n", "\nX-Test", "X Test", "X-Test:", ""])
def test_a_connection_header_name_is_a_plain_token_with_no_line_break(name: str) -> None:
    with pytest.raises(ValueError):
        conn(headers={name: "1"})


def test_extra_connection_headers_are_sent_but_credential_like_ones_are_refused() -> None:
    ex, fake = authed({"kind": "none"})
    run(ex.invoke(conn(headers={"X-Client": "home"}), req("x")))
    assert fake.requests[0].headers["x-client"] == "home"
    with pytest.raises(ValueError):
        conn(headers={"X-Api-Key": "k"})


# --- redirects and errors --------------------------------------------------------------------


def test_a_cross_origin_redirect_is_refused_and_the_key_never_leaves() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.host == "engine.test":
            return httpx.Response(302, headers={"location": "http://evil.test/steal"})
        return httpx.Response(200, json={})
    ex, fake = authed({"kind": "bearer"})
    ex2, fake2 = make(
        {"x": {"class": "generate", "request": {"method": "GET", "path": "/x"}, "response": {"framing": "json"}}}, handler,
        auth={"inference": {"kind": "bearer"}},
    )
    with pytest.raises(RedirectRefused) as raised:
        run(ex2.invoke(conn(credentials={"inference": Credential(SECRET)}), req("x")))
    assert [r.url.host for r in fake2.requests] == ["engine.test"]
    assert SECRET not in str(raised.value) and "evil.test" in str(raised.value)


def test_a_same_origin_redirect_is_followed_a_few_times_with_the_key() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/x":
            return httpx.Response(307, headers={"location": "/y"})
        return httpx.Response(200, json={"at": r.url.path})
    ex, fake = make({"x": {"class": "generate", "request": {"method": "GET", "path": "/x"}, "response": {"framing": "json"}}}, handler,
                    auth={"inference": {"kind": "bearer"}})
    reply = run(ex.invoke(conn(credentials={"inference": Credential(SECRET)}), req("x")))
    assert reply.body == {"at": "/y"}
    assert [r.headers.get("authorization") for r in fake.requests] == [f"Bearer {SECRET}"] * 2


def test_a_redirect_loop_is_refused() -> None:
    ex, fake = make({"x": {"class": "generate", "request": {"method": "GET", "path": "/x"}, "response": {"framing": "json"}}},
                    lambda r: httpx.Response(302, headers={"location": "/x"}))
    with pytest.raises(RedirectRefused):
        run(ex.invoke(conn(), req("x")))
    assert len(fake.requests) == Budgets().max_redirects + 1


def test_a_redirect_to_another_scheme_or_port_is_another_origin() -> None:
    for location in ("https://engine.test:8080/x", "http://engine.test:9999/x", "http://engine.test/x"):
        ex, fake = make({"x": {"class": "generate", "request": {"method": "GET", "path": "/x"}, "response": {"framing": "json"}}},
                        lambda r, location=location: httpx.Response(302, headers={"location": location}))
        with pytest.raises(RedirectRefused):
            run(ex.invoke(conn(), req("x")))
        assert len(fake.requests) == 1, location


def test_an_error_status_becomes_a_typed_failure_with_advice_and_no_secret() -> None:
    ex, _ = make(
        {"x": {"class": "generate", "request": {"method": "GET", "path": "/x"}, "response": {"framing": "json"}}},
        lambda r: httpx.Response(401, text=f"bad key {SECRET} and Bearer {SECRET}", headers={"retry-after": "7"}),
        auth={"inference": {"kind": "bearer"}},
        errors=[{"status": [401, 403], "code": "key_rejected", "advice": "Check the key."}],
    )
    with pytest.raises(UpstreamError) as raised:
        run(ex.invoke(conn(credentials={"inference": Credential(SECRET)}), req("x")))
    error = raised.value
    assert (error.status, error.advice_code, error.advice) == (401, "key_rejected", "Check the key.")
    assert SECRET not in str(error) and SECRET.encode() not in error.body
    assert error.headers["retry-after"] == "7"


def test_a_bad_json_answer_is_a_malformed_response() -> None:
    ex, _ = make(MODELS, lambda r: httpx.Response(200, text="<html>not json</html>"))
    with pytest.raises(MalformedResponse):
        run(ex.invoke(conn(), req("models")))


def test_a_compressed_answer_is_refused() -> None:
    ex, _ = make(MODELS, lambda r: httpx.Response(200, stream=Chunks([b"x"]), headers={"content-encoding": "gzip"}))
    with pytest.raises(MalformedResponse):
        run(ex.invoke(conn(), req("models")))


# --- streaming -------------------------------------------------------------------------------

CHAT_STREAM = {"chat": {"class": "generate", "request": {"method": "POST", "path": "/c", "body": {"encoding": "json"}}, "response": {
    "framing": "json",
    "stream": {"when": {"path": "/body/stream", "equals": True}, "framing": "sse", "terminal": {"sentinel": "[DONE]"}}}}}


def collect(reply):
    async def go():
        out = []
        async for event in reply:
            out.append(event)
        return out
    return run(go())


def test_the_same_operation_is_json_or_sse_by_the_request() -> None:
    wire = [b'data: {"a":1}\n\ndata: {"a"', b':2}\n\ndata: [DONE]\n\n']
    ex, _ = make(CHAT_STREAM, lambda r: httpx.Response(200, json={"plain": True}) if not json.loads(r.content).get("stream") else httpx.Response(200, stream=Chunks(wire)))
    plain = run(ex.invoke(conn(), req("chat", {"stream": False})))
    assert plain.body == {"plain": True}
    streamed = run(ex.invoke(conn(), req("chat", {"stream": True})))
    assert collect(streamed) == [b'{"a":1}', b'{"a":2}']  # unmapped events are relayed as raw bytes; the sentinel ends it


@pytest.mark.parametrize("ending", [b"\n\n", b"\r\n\r\n", b"\r\r", b"\n\r", b"\r\n\n"])
def test_an_sse_stream_whose_last_event_ends_the_stream_is_complete_in_every_line_ending_style(ending: bytes) -> None:
    wire = b'data: {"a":1}' + ending + b"data: [DONE]" + ending
    ex, _ = make(CHAT_STREAM, lambda r: httpx.Response(200, content=wire))
    assert collect(run(ex.invoke(conn(), req("chat", {"stream": True})))) == [b'{"a":1}']
    plain = {"chat": {"class": "generate", "request": {"method": "POST", "path": "/c", "body": {"encoding": "json"}}, "response": {"framing": "sse"}}}
    ex, _ = make(plain, lambda r: httpx.Response(200, content=b'data: {"t":"hi"}' + ending + b'data: {"t":"!"}' + ending))
    assert collect(run(ex.invoke(conn(), req("chat", {})))) == [b'{"t":"hi"}', b'{"t":"!"}']


def test_an_sse_stream_cut_inside_its_last_event_is_still_a_failure() -> None:
    plain = {"chat": {"class": "generate", "request": {"method": "POST", "path": "/c", "body": {"encoding": "json"}}, "response": {"framing": "sse"}}}
    for tail in (b'data: {"t":"hi"}\r\rdata: {"t":', b'data: {"t":"hi"}\r\rdata: {"t":"!"}\r'):
        ex, _ = make(plain, lambda r, tail=tail: httpx.Response(200, content=tail))
        with pytest.raises(TransportFailure):
            collect(run(ex.invoke(conn(), req("chat", {}))))


def nested_json(depth: int) -> bytes:
    return b'{"a":' * depth + b"1" + b"}" * depth


DEEP_STREAMS = {
    "sse mapped": ({"framing": "sse", "map": [{"copy": {"from": "/body", "to": "/deep"}}]}, lambda e: b"data: " + e + b"\n\n"),
    "sse terminal path": ({"framing": "sse", "terminal": {"path": "/b", "equals": True}}, lambda e: b"data: " + e + b"\n\n"),
    "ndjson mapped": ({"framing": "ndjson", "map": [{"copy": {"from": "/body", "to": "/deep"}}]}, lambda e: e + b"\n"),
    "ndjson mapped, no final newline": ({"framing": "ndjson", "map": [{"copy": {"from": "/body", "to": "/deep"}}]}, lambda e: e),
    "ndjson terminal path, no final newline": ({"framing": "ndjson", "terminal": {"path": "/b", "equals": True}}, lambda e: e),
}


@pytest.mark.parametrize("name", list(DEEP_STREAMS))
def test_a_stream_event_deeper_than_the_json_depth_budget_is_refused(name: str) -> None:
    response, wire = DEEP_STREAMS[name]
    ops = {"chat": {"class": "generate", "request": {"method": "POST", "path": "/c", "body": {"encoding": "json"}}, "response": response}}
    ex, _ = make(ops, lambda r: httpx.Response(200, content=wire(nested_json(40))), budgets=Budgets(max_json_depth=32))
    with pytest.raises(BudgetExceeded) as raised:
        collect(run(ex.invoke(conn(), req("chat", {}))))
    assert raised.value.budget == "max_json_depth"


def test_a_stream_event_within_the_json_depth_budget_is_mapped() -> None:
    response, wire = DEEP_STREAMS["sse mapped"]
    ops = {"chat": {"class": "generate", "request": {"method": "POST", "path": "/c", "body": {"encoding": "json"}}, "response": response}}
    ex, _ = make(ops, lambda r: httpx.Response(200, content=wire(nested_json(20))), budgets=Budgets(max_json_depth=32))
    events = collect(run(ex.invoke(conn(), req("chat", {}))))
    assert len(events) == 1 and events[0]["deep"]["a"]["a"]


def test_a_stream_map_turns_each_event_into_a_dict() -> None:
    ops = {"chat": {"class": "generate", "request": {"method": "POST", "path": "/c", "body": {"encoding": "json"}}, "response": {
        "framing": "ndjson", "terminal": {"path": "/done", "equals": True},
        "map": [{"set": {"to": "/choices", "value": [{"index": 0, "delta": {"content": None}, "finish_reason": None}]}},
                {"copy": {"from": "/body/delta", "to": "/choices/0/delta/content"}},
                {"when": {"if": {"path": "/body/done", "equals": True}, "then": [{"set": {"to": "/choices/0/finish_reason", "value": "stop"}}]}}]}}}
    wire = [b'{"delta":"he","done":false}\n{"delta":"l', b'lo","done":false}\n{"delta":"","done":true}\n']
    ex, _ = make(ops, lambda r: httpx.Response(200, stream=Chunks(wire)))
    events = collect(run(ex.invoke(conn(), req("chat", {}))))
    assert [e["choices"][0]["delta"]["content"] for e in events] == ["he", "llo", ""]
    assert events[-1]["choices"][0]["finish_reason"] == "stop"


def test_events_are_delivered_while_the_engine_is_still_sending() -> None:
    async def go():
        gate = asyncio.Event()
        stream = Chunks([b'data: {"n":1}\n\n', b'data: {"n":2}\n\n', b"data: [DONE]\n\n"], gate=gate)
        ex, _ = make(CHAT_STREAM, lambda r: httpx.Response(200, stream=stream))
        reply = await ex.invoke(conn(), req("chat", {"stream": True}))
        iterator = reply.__aiter__()
        first = await iterator.__anext__()
        assert first == b'{"n":1}' and stream.sent == 1  # the second chunk was not even produced yet
        gate.set()
        assert await iterator.__anext__() == b'{"n":2}'
        gate.set()
        with pytest.raises(StopAsyncIteration):
            await iterator.__anext__()
        return stream.closed
    assert run(go()) is True


def test_an_abandoned_stream_closes_the_upstream_response() -> None:
    async def go():
        stream = Chunks([b'data: {"n":1}\n\n', b'data: {"n":2}\n\n'])
        ex, _ = make(CHAT_STREAM, lambda r: httpx.Response(200, stream=stream))
        reply = await ex.invoke(conn(), req("chat", {"stream": True}))
        iterator = reply.__aiter__()
        await iterator.__anext__()
        await reply.aclose()
        return stream.closed
    assert run(go()) is True


def test_a_stream_that_ends_before_its_terminal_is_a_failure() -> None:
    ex, _ = make(CHAT_STREAM, lambda r: httpx.Response(200, stream=Chunks([b'data: {"n":1}\n\ndata: {"n":'])))
    reply = run(ex.invoke(conn(), req("chat", {"stream": True})))
    with pytest.raises(TransportFailure) as raised:
        collect(reply)
    assert raised.value.kind == "aborted"


def test_stream_error_status_is_raised_before_any_event() -> None:
    ex, _ = make(CHAT_STREAM, lambda r: httpx.Response(503, text="loading"), errors=[{"status": 503, "code": "loading", "advice": "Wait."}])
    with pytest.raises(UpstreamError) as raised:
        run(ex.invoke(conn(), req("chat", {"stream": True})))
    assert raised.value.advice_code == "loading"


def test_stream_budgets_event_size_and_total_bytes() -> None:
    big = [b"data: " + b"x" * 300 + b"\n\n"]
    ex, _ = make(CHAT_STREAM, lambda r: httpx.Response(200, stream=Chunks(big)), budgets=Budgets(max_event_bytes=100))
    with pytest.raises(BudgetExceeded):
        collect(run(ex.invoke(conn(), req("chat", {"stream": True}))))
    many = [b'data: {"n":1}\n\n'] * 20
    ex, _ = make(CHAT_STREAM, lambda r: httpx.Response(200, stream=Chunks(many)), budgets=Budgets(max_stream_bytes=100))
    with pytest.raises(BudgetExceeded) as raised:
        collect(run(ex.invoke(conn(), req("chat", {"stream": True}))))
    assert raised.value.budget == "max_stream_bytes"


# --- binary, multipart ------------------------------------------------------------------------


def test_a_binary_answer_is_saved_as_an_asset() -> None:
    assets = MemoryAssetStore()
    ops = {"speak": {"class": "generate", "request": {"method": "POST", "path": "/s", "body": {"encoding": "json"}}, "response": {"framing": "binary"}}}
    ex, _ = make(ops, lambda r: httpx.Response(200, content=b"RIFFxxxx", headers={"content-type": "audio/wav"}), assets=assets)
    reply = run(ex.invoke(conn(), req("speak", {"input": "hi"})))
    assert (reply.asset.content_type, reply.asset.size) == ("audio/wav", 8)
    assert assets.get(reply.asset) == b"RIFFxxxx"
    assert reply.body is None


def test_a_binary_answer_over_the_budget_is_stopped_and_nothing_is_kept() -> None:
    assets = MemoryAssetStore()
    ops = {"speak": {"class": "generate", "request": {"method": "POST", "path": "/s", "body": {"encoding": "json"}}, "response": {"framing": "binary"}}}
    ex, _ = make(ops, lambda r: httpx.Response(200, stream=Chunks([b"x" * 60] * 3)), assets=assets, budgets=Budgets(max_asset_bytes=100))
    with pytest.raises(BudgetExceeded):
        run(ex.invoke(conn(), req("speak", {})))
    assert assets.count() == 0


UPLOAD = {"transcribe": {"class": "generate", "request": {"method": "POST", "path": "/t", "body": {"encoding": "multipart", "parts": [
    {"name": "file", "kind": "file", "from": "/files/audio", "required": True},
    {"name": "model", "kind": "field", "from": "/body/model", "required": True},
    {"name": "language", "kind": "field", "from": "/body/language"},
    {"name": "opts", "kind": "json", "from": "/body/opts"},
]}}, "response": {"framing": "json"}}}


def test_multipart_parts_come_from_canonical_assets_and_fields() -> None:
    assets = MemoryAssetStore()
    audio = assets.put(b"\x01\x02audio", "audio/wav", name="clip.wav")
    ex, fake = make(UPLOAD, lambda r: httpx.Response(200, json={"text": "hi"}), assets=assets)
    ex.boundary_factory = lambda: "BND"
    run(ex.invoke(conn(), req("transcribe", {"model": "m", "opts": {"a": 1}}, files={"audio": audio})))
    sent = fake.requests[0]
    assert sent.headers["content-type"] == "multipart/form-data; boundary=BND"
    assert sent.content == (
        b'--BND\r\nContent-Disposition: form-data; name="file"; filename="clip.wav"\r\nContent-Type: audio/wav\r\n\r\n\x01\x02audio\r\n'
        b'--BND\r\nContent-Disposition: form-data; name="model"\r\n\r\nm\r\n'
        b'--BND\r\nContent-Disposition: form-data; name="opts"\r\nContent-Type: application/json\r\n\r\n{"a":1}\r\n'
        b"--BND--\r\n"
    )


def test_a_required_part_that_is_missing_is_refused_before_sending() -> None:
    assets = MemoryAssetStore()
    audio = assets.put(b"x", "audio/wav")
    ex, fake = make(UPLOAD, lambda r: httpx.Response(200, json={}), assets=assets)
    with pytest.raises(MappingError):
        run(ex.invoke(conn(), req("transcribe", {"model": "m"})))
    with pytest.raises(MappingError):
        run(ex.invoke(conn(), req("transcribe", {}, files={"audio": audio})))
    assert fake.requests == []


def test_upload_and_request_size_budgets() -> None:
    assets = MemoryAssetStore()
    audio = assets.put(b"x" * 100, "audio/wav")
    ex, fake = make(UPLOAD, lambda r: httpx.Response(200, json={}), assets=assets, budgets=Budgets(max_upload_bytes=50))
    with pytest.raises(BudgetExceeded):
        run(ex.invoke(conn(), req("transcribe", {"model": "m"}, files={"audio": audio})))
    ops = {"echo": {"class": "generate", "request": {"method": "POST", "path": "/e", "body": {"encoding": "json"}}, "response": {"framing": "json"}}}
    ex, fake = make(ops, lambda r: httpx.Response(200, json={}), budgets=Budgets(max_request_bytes=50))
    with pytest.raises(BudgetExceeded):
        run(ex.invoke(conn(), req("echo", {"big": "x" * 100})))
    assert fake.requests == []


# --- budgets ---------------------------------------------------------------------------------


def test_response_size_depth_and_selection_budgets() -> None:
    ex, _ = make(MODELS, lambda r: httpx.Response(200, json={"data": [{"id": "a"}] * 50}), budgets=Budgets(max_response_bytes=100))
    with pytest.raises(BudgetExceeded) as raised:
        run(ex.invoke(conn(), req("models")))
    assert raised.value.budget == "max_response_bytes"
    ex, _ = make(MODELS, lambda r: httpx.Response(200, content=b"[" * 40 + b"]" * 40), budgets=Budgets(max_json_depth=32))
    with pytest.raises(BudgetExceeded) as raised:
        run(ex.invoke(conn(), req("models")))
    assert raised.value.budget == "max_json_depth"
    ex, _ = make(MODELS, lambda r: httpx.Response(200, json={"data": [{"id": "a"}] * 50}), budgets=Budgets(max_selected=10))
    with pytest.raises(BudgetExceeded):
        run(ex.invoke(conn(), req("models")))


def deep_dict(depth: int) -> dict:
    node: dict = {"x": 1}
    for _ in range(depth - 1):
        node = {"n": node}
    return node


@pytest.mark.parametrize("depth", [500, 1005, 5000, 200_000])
@pytest.mark.parametrize("encoding", ["passthrough with a write", "multipart json part"])
def test_a_very_deep_body_is_a_budget_error_and_never_a_recursion_error(depth: int, encoding: str) -> None:
    if encoding == "passthrough with a write":  # a write below the body makes the map clone it
        body = {"encoding": "json", "base": "passthrough", "map": [{"set": {"to": "/flag", "value": True}}, {"copy": {"from": "/body/n", "to": "/n"}}]}
    else:
        body = {"encoding": "multipart", "parts": [{"name": "opts", "kind": "json", "from": "/body"}]}
    ops = {"chat": {"class": "generate", "request": {"method": "POST", "path": "/v1/chat", "body": body}, "response": {"framing": "json"}}}
    ex, fake = make(ops, lambda r: httpx.Response(200, json={}))
    with pytest.raises(BudgetExceeded) as raised:
        run(ex.invoke(conn(), req("chat", deep_dict(depth))))
    assert raised.value.budget == "max_json_depth"
    assert fake.requests == []


def test_a_very_deep_params_value_cannot_crash_the_request() -> None:
    ops = {"get": {"class": "inventory", "request": {"method": "GET", "path": "/v1/x", "query": [{"name": "q", "from": "/params/q"}]}, "response": {"framing": "json"}}}
    ex, fake = make(ops, lambda r: httpx.Response(200, json={}))
    with pytest.raises((BudgetExceeded, MappingError)):
        run(ex.invoke(conn(), req("get", params={"q": deep_dict(5000)})))
    assert fake.requests == []


ASSET = AssetRef("a1", "text/plain", 3, "f.txt")


@pytest.mark.parametrize("rule", [
    {"copy": {"from": "/files/f", "to": "/file"}},
    {"stringify": {"from": "/files/f", "to": "/file"}},
    {"copy": {"from": "/files", "to": "/all"}},
])
def test_an_asset_copied_into_a_json_body_is_a_typed_mapping_error(rule: dict) -> None:
    ops = {"up": {"class": "generate", "request": {"method": "POST", "path": "/v1/up", "body": {"encoding": "json", "map": [rule]}}, "response": {"framing": "json"}}}
    ex, fake = make(ops, lambda r: httpx.Response(200, json={}))
    with pytest.raises(MappingError):
        run(ex.invoke(conn(), req("up", files={"f": ASSET})))
    assert fake.requests == []


def test_a_value_json_cannot_write_is_a_typed_mapping_error_in_a_body_and_in_a_json_part() -> None:
    ops = {"up": {"class": "generate", "request": {"method": "POST", "path": "/v1/up", "body": {"encoding": "json"}}, "response": {"framing": "json"}},
           "form": {"class": "generate", "request": {"method": "POST", "path": "/v1/form", "body": {"encoding": "multipart", "parts": [
               {"name": "opts", "kind": "json", "from": "/body/opts"}]}}, "response": {"framing": "json"}}}
    ex, fake = make(ops, lambda r: httpx.Response(200, json={}))
    with pytest.raises(MappingError):
        run(ex.invoke(conn(), req("up", {(1, 2): "tuple key"})))
    with pytest.raises(MappingError):
        run(ex.invoke(conn(), req("form", {"opts": {(1, 2): "tuple key"}})))
    assert fake.requests == []


def test_a_mapped_body_that_nests_too_deep_is_refused() -> None:
    ops = {"echo": {"class": "generate", "request": {"method": "POST", "path": "/e", "body": {"encoding": "json"}}, "response": {"framing": "json"}}}
    ex, fake = make(ops, lambda r: httpx.Response(200, json={}), budgets=Budgets(max_json_depth=5))
    deep: Any = 1
    for _ in range(10):
        deep = {"a": deep}
    with pytest.raises(BudgetExceeded):
        run(ex.invoke(conn(), req("echo", deep)))
    assert fake.requests == []


def test_the_total_time_budget_stops_a_silent_engine() -> None:
    async def silent(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={})
    fake = HandlerTransport(silent)
    ex = executor.HttpV1Executor(desc(MODELS), transport=transport.HttpxTransport.for_tests(fake), assets=MemoryAssetStore())
    with pytest.raises(TransportFailure) as raised:
        run(ex.invoke(conn(timeouts=Timeouts(connect_s=1, first_byte_s=1, total_s=0.2)), req("models")))
    assert raised.value.kind == "timeout"


# --- jobs ------------------------------------------------------------------------------------

JOB = {"render": {"class": "generate", "job": {
    "submit": {"request": {"method": "POST", "path": "/jobs", "body": {"encoding": "json", "map": [{"copy": {"from": "/body/prompt", "to": "/spec/prompt"}}]}}, "id": "/job/id"},
    "poll": {"request": {"method": "GET", "path": "/jobs/{id}"}, "status": {"path": "/state", "states": {"queued": "pending", "busy": "running", "ok": "succeeded", "bad": "failed", "gone": "cancelled"}},
             "error": "/error/message", "assets": "/files/*/name", "interval_s": 0.1, "max_wait_s": 5},
    "fetch": {"request": {"method": "GET", "path": "/jobs/{id}/files/{asset}"}},
    "cancel": {"request": {"method": "POST", "path": "/jobs/{id}/cancel"}},
}}}


def job_handler(states):
    queue = list(states)

    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "POST" and r.url.path == "/jobs":
            return httpx.Response(202, json={"job": {"id": 42}})
        if r.method == "GET" and r.url.path == "/jobs/42":
            return httpx.Response(200, json=queue.pop(0))
        if r.url.path.startswith("/jobs/42/files/"):
            name = r.url.path.rsplit("/", 1)[1]
            return httpx.Response(200, content=b"bytes-of-" + name.encode(), headers={"content-type": "image/png"})
        if r.url.path == "/jobs/42/cancel":
            return httpx.Response(204)
        return httpx.Response(404)
    return handler


def test_a_job_is_submitted_polled_fetched_and_cancelled_with_typed_references() -> None:
    assets = MemoryAssetStore()
    ex, fake = make(JOB, job_handler([
        {"state": "queued"}, {"state": "busy"},
        {"state": "ok", "files": [{"name": "a.png"}, {"name": "b c.png"}]},
    ]), assets=assets)

    async def go():
        ref = await ex.submit(conn(), req("render", {"prompt": "a cat"}))
        seen = [(await ex.poll(conn(), ref)).state for _ in range(3)]
        return ref, seen
    ref, seen = run(go())
    assert (ref.op, ref.id) == ("render", "42")
    assert seen == [JobState.pending, JobState.running, JobState.succeeded]
    assert json.loads(fake.requests[0].content) == {"spec": {"prompt": "a cat"}}


def test_finished_job_assets_are_fetched_per_selected_handle() -> None:
    assets = MemoryAssetStore()
    ex, fake = make(JOB, job_handler([{"state": "ok", "files": [{"name": "a.png"}, {"name": "b c.png"}]}]), assets=assets)

    async def go():
        ref = await ex.submit(conn(), req("render", {"prompt": "x"}))
        status = await ex.poll(conn(), ref)
        return status, await ex.fetch_assets(conn(), ref, status)
    status, refs = run(go())
    assert status.state is JobState.succeeded and status.assets == ("a.png", "b c.png")
    assert [assets.get(r) for r in refs] == [b"bytes-of-a.png", b"bytes-of-b c.png"]
    assert all(r.content_type == "image/png" for r in refs)
    assert [r.url.raw_path for r in fake.requests if "/files/" in r.url.path] == [b"/jobs/42/files/a.png", b"/jobs/42/files/b%20c.png"]


def test_a_failed_job_carries_the_engines_reason_and_an_unknown_status_is_loud() -> None:
    ex, _ = make(JOB, job_handler([{"state": "bad", "error": {"message": "out of memory"}}, {"state": "exploded"}]))

    async def go():
        ref = await ex.submit(conn(), req("render", {"prompt": "x"}))
        failed = await ex.poll(conn(), ref)
        with pytest.raises(MalformedResponse) as raised:
            await ex.poll(conn(), ref)
        return failed, raised.value
    failed, error = run(go())
    assert failed.state is JobState.failed and failed.detail == "out of memory"
    assert "exploded" in str(error)


def test_cancel_and_a_missing_job_id() -> None:
    ex, fake = make(JOB, job_handler([{"state": "busy"}]))  # cancel asks the job's state first

    async def go():
        ref = await ex.submit(conn(), req("render", {"prompt": "x"}))
        await ex.cancel(conn(), ref)
    run(go())
    assert [(r.method, r.url.path) for r in fake.requests[-2:]] == [("GET", "/jobs/42"), ("POST", "/jobs/42/cancel")]
    ex2, _ = make(JOB, lambda r: httpx.Response(202, json={"nope": 1}))
    with pytest.raises(MalformedResponse):
        run(ex2.submit(conn(), req("render", {"prompt": "x"})))


class Slow(httpx.AsyncByteStream):
    def __init__(self, count: int, delay: float, size: int = 1024) -> None:
        self.count, self.delay, self.size = count, delay, size

    async def __aiter__(self):
        for _ in range(self.count):
            await asyncio.sleep(self.delay)
            yield b"x" * self.size

    async def aclose(self) -> None:
        pass


def fetch_all(ex, **kw):
    async def go():
        ref = await ex.submit(conn(**kw), req("render", {"prompt": "x"}))
        status = await ex.poll(conn(**kw), ref)
        return await ex.fetch_assets(conn(**kw), ref, status)
    return run(go())


def asset_handler(states, answer):
    base = job_handler(states)
    return lambda r: answer(r) if "/files/" in r.url.path else base(r)


def test_a_slow_asset_body_runs_into_the_total_deadline() -> None:
    assets = MemoryAssetStore()
    ex, _ = make(JOB, asset_handler([{"state": "ok", "files": [{"name": "a.png"}]}],
                                    lambda r: httpx.Response(200, stream=Slow(50, 0.05), headers={"content-type": "image/png"})), assets=assets)
    started = time.perf_counter()
    with pytest.raises(TransportFailure) as raised:
        fetch_all(ex, timeouts=Timeouts(connect_s=1, first_byte_s=1, total_s=0.3))
    assert raised.value.kind == "timeout"
    assert time.perf_counter() - started < 1.5  # the body alone takes 2.5 s
    assert assets.count() == 0


def test_all_the_assets_of_one_fetch_share_one_total_deadline() -> None:
    files = [{"name": f"f{i}.png"} for i in range(4)]
    ex, _ = make(JOB, asset_handler([{"state": "ok", "files": files}],
                                    lambda r: httpx.Response(200, stream=Slow(1, 0.12, 16), headers={"content-type": "image/png"})))
    with pytest.raises(TransportFailure) as raised:
        fetch_all(ex, timeouts=Timeouts(connect_s=1, first_byte_s=1, total_s=0.3))  # 4 x 0.12 s is over 0.3 s together
    assert raised.value.kind == "timeout"


def test_the_assets_of_one_fetch_have_a_total_size_cap() -> None:
    files = [{"name": f"f{i}.png"} for i in range(3)]
    ex, _ = make(JOB, asset_handler([{"state": "ok", "files": files}],
                                    lambda r: httpx.Response(200, content=PNG + b"x" * 52, headers={"content-type": "image/png"})),  # 60 bytes each
                 budgets=Budgets(max_asset_bytes=100, max_fetch_bytes=150))
    with pytest.raises(BudgetExceeded) as raised:
        fetch_all(ex)
    assert raised.value.budget == "max_fetch_bytes"


def test_an_asset_store_that_ignores_the_byte_cap_is_still_capped() -> None:
    class Greedy:
        async def read(self, ref, *, max_bytes):
            raise AssertionError

        async def save(self, chunks, *, content_type, max_bytes, name=None):
            async for _ in chunks:
                pass
            raise AssertionError("the executor should have stopped the stream")
    ex, _ = make(JOB, asset_handler([{"state": "ok", "files": [{"name": "a.png"}]}],
                                    lambda r: httpx.Response(200, stream=Chunks([PNG + b"x" * 200] * 3), headers={"content-type": "image/png"})),
                 assets=Greedy(), budgets=Budgets(max_asset_bytes=100))
    with pytest.raises(BudgetExceeded) as raised:
        fetch_all(ex)
    assert raised.value.budget == "max_asset_bytes"


def fast_job(interval_s: float, max_wait_s: float) -> dict:
    spec = json.loads(json.dumps(JOB))
    spec["render"]["job"]["poll"].update(interval_s=interval_s, max_wait_s=max_wait_s)
    return spec


def timed_job_handler(states, stamps):
    inner = job_handler(states)

    def handler(r: httpx.Request) -> httpx.Response:
        if r.method == "GET" and r.url.path == "/jobs/42":
            stamps.append(time.perf_counter())
        return inner(r)
    return handler


def test_wait_polls_at_the_declared_interval_until_the_job_ends() -> None:
    stamps: list[float] = []
    ex, _ = make(fast_job(0.1, 5), timed_job_handler([{"state": "queued"}, {"state": "busy"}, {"state": "ok", "files": []}], stamps))

    async def go():
        ref = await ex.submit(conn(), req("render", {"prompt": "x"}))
        return await ex.wait(conn(), ref)
    status = run(go())
    assert status.state is JobState.succeeded and len(stamps) == 3
    assert min(b - a for a, b in zip(stamps, stamps[1:])) >= 0.09  # not faster than interval_s


def test_wait_gives_up_at_max_wait_and_stops_polling() -> None:
    stamps: list[float] = []
    ex, _ = make(fast_job(0.1, 1), timed_job_handler([{"state": "busy"}] * 1000, stamps))

    async def go():
        ref = await ex.submit(conn(), req("render", {"prompt": "x"}))
        started = time.perf_counter()
        with pytest.raises(BudgetExceeded) as raised:
            await ex.wait(conn(), ref)
        return raised.value, time.perf_counter() - started
    error, elapsed = run(go())
    assert error.budget == "max_wait_s"
    assert 0.9 <= elapsed < 2.0 and 5 <= len(stamps) <= 12


def test_max_wait_also_bounds_a_poll_that_hangs() -> None:
    async def handler(r: httpx.Request) -> httpx.Response:
        if r.url.path == "/jobs":
            return httpx.Response(202, json={"job": {"id": 42}})
        await asyncio.sleep(5)
        return httpx.Response(200, json={"state": "busy"})
    ex = executor.HttpV1Executor(desc(fast_job(0.1, 1)), transport=transport.HttpxTransport.for_tests(HandlerTransport(handler)), assets=MemoryAssetStore())

    async def go():
        ref = await ex.submit(conn(), req("render", {"prompt": "x"}))
        started = time.perf_counter()
        with pytest.raises(BudgetExceeded) as raised:
            await ex.wait(conn(), ref)
        return raised.value, time.perf_counter() - started
    error, elapsed = run(go())
    assert error.budget == "max_wait_s" and 0.9 <= elapsed < 2.0


@pytest.mark.parametrize("state", ["ok", "bad", "gone"])
def test_cancel_of_a_job_that_already_ended_sends_no_cancel(state: str) -> None:
    ex, fake = make(JOB, job_handler([{"state": state, "files": []}]))

    async def go():
        ref = await ex.submit(conn(), req("render", {"prompt": "x"}))
        await ex.cancel(conn(), ref)
    run(go())
    assert not any(r.url.path.endswith("/cancel") for r in fake.requests)


@pytest.mark.parametrize("state", ["queued", "busy"])
def test_cancel_of_a_live_job_is_sent(state: str) -> None:
    ex, fake = make(JOB, job_handler([{"state": state}]))

    async def go():
        ref = await ex.submit(conn(), req("render", {"prompt": "x"}))
        await ex.cancel(conn(), ref)
    run(go())
    assert fake.requests[-1].url.path == "/jobs/42/cancel"


def test_cancel_of_an_unknown_job_is_the_engines_not_found_and_sends_no_cancel() -> None:
    ex, fake = make(JOB, lambda r: httpx.Response(404, text="no such job") if r.method == "GET" else httpx.Response(202, json={"job": {"id": 42}}))

    async def go():
        ref = await ex.submit(conn(), req("render", {"prompt": "x"}))
        await ex.cancel(conn(), ref)
    with pytest.raises(UpstreamError):
        run(go())
    assert not any(r.url.path.endswith("/cancel") for r in fake.requests)


def test_a_job_operation_is_not_invokable_and_a_plain_one_is_not_submittable() -> None:
    ex, _ = make({**JOB, **MODELS}, lambda r: httpx.Response(200, json={}))
    with pytest.raises(UnknownOperation):
        run(ex.invoke(conn(), req("render")))
    with pytest.raises(UnknownOperation):
        run(ex.submit(conn(), req("models")))


def test_fetch_asset_count_is_bounded() -> None:
    files = [{"name": f"f{i}.png"} for i in range(5)]
    ex, _ = make(JOB, job_handler([{"state": "ok", "files": files}]), budgets=Budgets(max_assets=3))

    async def go():
        ref = await ex.submit(conn(), req("render", {"prompt": "x"}))
        return await ex.poll(conn(), ref)
    with pytest.raises(BudgetExceeded):
        run(go())


# --- query-auth keys never reach a log, an error, a preview or a receipt ---------------------------

QKEY = "k/ey+with space&=é-1234"
FORMS = [QKEY, quote(QKEY), quote(QKEY, safe=""), quote_plus(QKEY)]
QUERY_AUTH = {"inference": {"kind": "query", "name": "api_key"}}
QOPS = {"x": {"class": "generate", "request": {"method": "GET", "path": "/x"}, "response": {"framing": "json"}},
        "s": {"class": "generate", "request": {"method": "GET", "path": "/s"}, "response": {"framing": "sse"}}}


def qconn(**kw: Any) -> Connection:
    return conn(credentials={"inference": Credential(QKEY)}, **kw)


def clean(*texts: Any) -> None:
    for text in texts:
        for form in FORMS:
            assert form not in str(text), (form, str(text))


def test_a_query_key_is_sent_encoded_and_the_preview_never_shows_it_in_any_form() -> None:
    ex, fake = make(QOPS, lambda r: httpx.Response(200, json={}), auth=QUERY_AUTH)
    shown = run(ex.preview(qconn(), req("x")))
    clean(json.dumps(shown))
    assert "api_key=" in shown["url"]
    run(ex.invoke(qconn(), req("x")))
    assert fake.requests[0].url.params["api_key"] == QKEY  # the real request does carry it


def test_a_short_query_key_is_masked_in_the_preview_too() -> None:
    ex, _ = make(QOPS, lambda r: httpx.Response(200, json={}), auth=QUERY_AUTH)
    shown = run(ex.preview(conn(credentials={"inference": Credential("abc")}), req("x")))
    assert "abc" not in shown["url"] and "api_key=" in shown["url"]


def test_an_engine_that_echoes_the_url_cannot_put_the_key_in_an_error() -> None:
    def handler(r: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=f"bad request: {r.url}", headers={"x-trace": str(r.url), "content-type": "text/plain"})
    ex, _ = make(QOPS, handler, auth=QUERY_AUTH)
    with pytest.raises(UpstreamError) as raised:
        run(ex.invoke(qconn(), req("x")))
    error = raised.value
    clean(error, error.body.decode(), error.headers, error.advice, repr(error.args))


def test_a_redirect_refusal_never_holds_the_key() -> None:
    ex, _ = make(QOPS, lambda r: httpx.Response(302, headers={"location": f"http://evil.test/x?api_key={quote(QKEY)}"}), auth=QUERY_AUTH)
    with pytest.raises(RedirectRefused) as raised:
        run(ex.invoke(qconn(), req("x")))
    clean(raised.value)


class Breaking:
    """A transport that fails the way a real one may: with the whole URL in the message."""

    def __init__(self, error) -> None:
        self.error = error

    async def send(self, request, timeouts):
        raise self.error(request.url)


@pytest.mark.parametrize("error", [
    lambda url: TransportFailure("aborted", f"The connection to {url} broke"),
    lambda url: RuntimeError(f"unexpected failure for {url}"),
    lambda url: httpx.InvalidURL(f"Invalid URL {url}"),
])
def test_a_transport_failure_never_holds_the_key_and_is_typed(error) -> None:
    ex = executor.HttpV1Executor(desc(QOPS, auth=QUERY_AUTH), transport=Breaking(error), assets=MemoryAssetStore())
    with pytest.raises(TransportFailure) as raised:
        run(ex.invoke(qconn(), req("x")))
    exc: BaseException | None = raised.value
    while exc is not None:  # also what a log would print from the chain
        clean(exc, repr(exc))
        exc = exc.__cause__ or exc.__context__


def test_a_stream_that_breaks_midway_never_holds_the_key() -> None:
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: {}\n\n"
            raise httpx.ReadError(f"broken at http://engine.test:8080/s?api_key={quote(QKEY)}")

        async def aclose(self) -> None:
            pass
    ex, _ = make(QOPS, lambda r: httpx.Response(200, stream=Broken()), auth=QUERY_AUTH)
    with pytest.raises(TransportFailure) as raised:
        collect(run(ex.invoke(qconn(), req("s"))))
    clean(raised.value, repr(raised.value))


def test_answer_headers_and_a_job_failure_reason_never_hold_the_key() -> None:
    echo = {"x-echo": f"http://engine.test:8080/x?api_key={quote_plus(QKEY)}", "x-plus": quote_plus(QKEY), "x-plain": quote(QKEY)}
    ex, _ = make(QOPS, lambda r: httpx.Response(200, json={}, headers=echo), auth=QUERY_AUTH)
    clean(run(ex.invoke(qconn(), req("x"))).headers)
    jobs = fast_job(0.1, 1)
    ex, _ = make(jobs, job_handler([{"state": "bad", "error": {"message": f"failed: {QKEY}"}}]), auth=QUERY_AUTH)

    async def go():
        ref = await ex.submit(qconn(), req("render", {"prompt": "x"}))
        return await ex.poll(qconn(), ref)
    clean(run(go()).detail)


def test_the_http_client_log_never_holds_the_key(caplog) -> None:
    ex, _ = make(QOPS, lambda r: httpx.Response(200, json={}), auth=QUERY_AUTH)
    with caplog.at_level(logging.DEBUG):
        run(ex.invoke(qconn(), req("x")))
    assert any(record.name == "httpx" for record in caplog.records)  # the client does log each request
    for record in caplog.records:
        clean(record.getMessage(), record.args)


# --- preview, terminals ----------------------------------------------------------------------


def test_preview_shows_the_effective_request_with_the_key_masked_and_sends_nothing() -> None:
    ops = {"chat": {"class": "generate", "request": {"method": "POST", "path": "/api/v3/converse", "query": [{"name": "k", "from": "/params/k"}],
                                                      "body": {"encoding": "json", "map": [{"copy": {"from": "/body/model", "to": "/m"}}]}},
                    "response": {"framing": "json"}}}
    ex, fake = make(ops, lambda r: httpx.Response(200, json={}), auth={"inference": {"kind": "bearer"}})
    shown = run(ex.preview(conn(credentials={"inference": Credential(SECRET)}), req("chat", {"model": "m1"}, params={"k": "v"})))
    assert shown == {
        "method": "POST", "url": f"{BASE}/api/v3/converse?k=v",
        "headers": {"accept-encoding": "identity", "authorization": "<configured>", "content-type": "application/json"},
        "body": '{"m":"m1"}',
    }
    assert fake.requests == [] and SECRET not in json.dumps(shown)


def test_an_sse_event_name_can_end_the_stream_and_an_ndjson_tail_without_a_newline_is_kept() -> None:
    ops = {"s": {"class": "generate", "request": {"method": "GET", "path": "/s"}, "response": {"framing": "sse", "terminal": {"event": "done"}}},
           "n": {"class": "generate", "request": {"method": "GET", "path": "/n"}, "response": {"framing": "ndjson"}}}
    def handler(r):
        if r.url.path == "/s":
            return httpx.Response(200, stream=Chunks([b"event: tok\ndata: 1\n\nevent: done\ndata: {}\n\ndata: after\n\n"]))
        return httpx.Response(200, stream=Chunks([b'{"a":1}\n{"b":', b"2}"]))
    ex, _ = make(ops, handler)
    assert collect(run(ex.invoke(conn(), req("s")))) == [b"1"]
    assert collect(run(ex.invoke(conn(), req("n")))) == [b'{"a":1}', b'{"b":2}']


def test_a_header_value_with_a_line_break_is_never_sent() -> None:
    ex, fake = authed({"kind": "none"})
    with pytest.raises(MappingError):
        run(ex.invoke(conn(headers={"X-Client": "a\r\nInjected: 1"}), req("x")))
    ex, fake = authed({"kind": "bearer"})
    with pytest.raises(MappingError):
        run(ex.invoke(conn(credentials={"inference": Credential("k\nInjected: 1")}), req("x")))
    assert fake.requests == []
