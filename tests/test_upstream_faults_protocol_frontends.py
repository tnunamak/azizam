"""Anthropic and Ollama frontends over a normal and a faulted LLM substrate.

The gateway is never an Anthropic or Ollama *client*: both frontends translate
to the chat route over the gateway's own loopback hop, and the chat route
calls the LLM substrate (openai-compatible). So each upstream fault is served
by the openai-compatible FK scenarios, and these cases assert what an
Anthropic or Ollama client sees. The loopback hop runs in process behind a
tap, so a test releases the next upstream chunk only after the hop relayed
the previous one.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import httpx
import pytest

import proxy
from tests.support.fake_upstream import FakeUpstreams, KnownBug, expect_fixed, fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.support.gateway import READ_BUDGET_SECONDS, ResponseTap, gateway_client, register_litellm, use_admin_db

MODEL = "fk-model"
MESSAGES = [{"role": "user", "content": "hi"}]
HANG_BOUND_SECONDS = 10
INTERRUPTED = "The LLM execution stream ended before the response was complete."


def _anthropic_events(raw: bytes) -> list[tuple[str, Any]]:
    text = raw.decode("utf-8")
    assert text.endswith("\n\n"), text[-80:]
    out = []
    for block in text[:-2].split("\n\n"):
        event, data = block.split("\n")
        assert event.startswith("event: ") and data.startswith("data: "), block
        out.append((event[len("event: "):], json.loads(data[len("data: "):])))
    return out


def _ollama_lines(raw: bytes) -> list[Any]:
    """NDJSON lines; created_at and total_duration are wall-clock values, checked then fixed."""
    text = raw.decode("utf-8")
    assert text.endswith("\n"), text[-80:]
    out = []
    for line in text[:-1].split("\n"):
        item = json.loads(line)
        if "created_at" in item:
            assert isinstance(item["created_at"], str) and item["created_at"].endswith("Z")
            item["created_at"] = "<wall clock>"
        if "total_duration" in item:
            assert isinstance(item["total_duration"], int) and item["total_duration"] >= 0
            item["total_duration"] = "<wall clock>"
        out.append(item)
    return out


def _anthropic_stream(message_id: str, deltas: list[str], tail: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    return [
        ("message_start", {"type": "message_start", "message": {"id": message_id, "type": "message", "role": "assistant", "model": MODEL, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        *[("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}) for text in deltas],
        *tail,
    ]


def _anthropic_end(usage: dict) -> list[tuple[str, Any]]:
    return [
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": usage}),
        ("message_stop", {"type": "message_stop"}),
    ]


def _ollama_chunk(content: str) -> dict:
    return {"model": MODEL, "created_at": "<wall clock>", "message": {"role": "assistant", "content": content}, "done": False}


def _ollama_done(prompt: int, completion: int) -> dict:
    return {
        "model": MODEL, "created_at": "<wall clock>", "message": {"role": "assistant", "content": ""}, "done": True,
        "done_reason": "stop", "total_duration": "<wall clock>", "load_duration": 0, "prompt_eval_count": prompt,
        "prompt_eval_duration": 0, "eval_count": completion, "eval_duration": 0,
    }


FRONTENDS: dict[str, dict[str, Any]] = {
    "anthropic": {
        "path": "/v1/messages",
        "body": lambda stream: {"model": MODEL, "max_tokens": 16, "stream": stream, "messages": MESSAGES},
        "parse": _anthropic_events,
    },
    "ollama": {
        "path": "/api/chat",
        "body": lambda stream: {"model": MODEL, "stream": stream, "messages": MESSAGES},
        "parse": _ollama_lines,
    },
}


@pytest.fixture
def frontend_gateway(fake_upstreams: FakeUpstreams, monkeypatch, tmp_path):
    hop = ResponseTap(proxy.app)
    fake_upstreams.router.add(f"127.0.0.1:{proxy.PROXY_PORT}", hop)

    def serve(name: str, *, wire: bool = False, **values):
        scenario = load_scenario(f"openai-compatible/{name}", model=MODEL, **values)
        fake = fake_upstreams.loopback(scenario) if wire else fake_upstreams.asgi(scenario, host="litellm.fk.test")
        register_litellm(monkeypatch, use_admin_db(monkeypatch, tmp_path), fake.base_url)
        return fake

    return serve, hop


def _run(fakes: FakeUpstreams, hop: ResponseTap, frontend: str, *, stream: bool, release_after_events: list[int] = ()) -> tuple[httpx.Response, bytes]:
    """Call the frontend; release one upstream wait each time the hop has relayed N SSE events."""
    spec = FRONTENDS[frontend]

    async def pump() -> None:
        for count in release_after_events:
            await hop.until_sent(b"\n\n", count)
            await fakes.clock.until_waiting()
            fakes.clock.advance(1)

    async def call() -> tuple[httpx.Response, bytes]:
        async with asyncio.timeout(HANG_BOUND_SECONDS), gateway_client() as client:
            releaser = asyncio.create_task(pump())
            async with client.stream("POST", spec["path"], json=spec["body"](stream)) as response:
                raw = await response.aread()
            await releaser
            return response, raw

    return asyncio.run(call())


@pytest.mark.parametrize("frontend", list(FRONTENDS))
def test_text_answer(frontend_gateway, fake_upstreams, frontend) -> None:
    serve, hop = frontend_gateway
    fake = serve("chat-text", content="Hello there")

    response, raw = _run(fake_upstreams, hop, frontend, stream=False)

    assert response.status_code == 200, raw
    body = json.loads(raw)
    if frontend == "anthropic":
        assert body == {"id": "msg_chatcmpl-fk-text", "type": "message", "role": "assistant", "model": MODEL, "content": [{"type": "text", "text": "Hello there"}], "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 7, "output_tokens": 3}}
    else:
        assert _ollama_lines(raw + b"\n") == [{**_ollama_done(7, 3), "message": {"role": "assistant", "content": "Hello there"}}]
    [forwarded] = fake.calls("chat")
    assert forwarded.json["messages"] == MESSAGES and forwarded.json["stream"] is False


@pytest.mark.parametrize("frontend", list(FRONTENDS))
def test_streamed_answer(frontend_gateway, fake_upstreams, frontend) -> None:
    serve, hop = frontend_gateway
    fake = serve("chat-stream")

    response, raw = _run(fake_upstreams, hop, frontend, stream=True)

    assert response.status_code == 200, raw
    if frontend == "anthropic":
        assert _anthropic_events(raw) == _anthropic_stream("msg_chatcmpl-fk-stream", ["Hello", " café"], _anthropic_end({"output_tokens": 2, "input_tokens": 7}))
    else:
        assert _ollama_lines(raw) == [_ollama_chunk("Hello"), _ollama_chunk(" café"), _ollama_done(7, 2)]
    assert fake.calls("chat")[0].json["stream_options"] == {"include_usage": True}


def _forwarded(fake, **extra) -> None:
    """Assert the one chat call the frontend forwarded: translated messages, max tokens, stream and usage options."""
    [forwarded] = fake.calls("chat")
    body = forwarded.json
    assert body["model"] == MODEL and body["messages"] == MESSAGES
    for key, value in extra.items():
        assert body[key] == value, (key, body)


def _forwarded_stream(fake, frontend: str) -> None:
    _forwarded(fake, stream=True, stream_options={"include_usage": True}, **({"max_tokens": 16} if frontend == "anthropic" else {}))


@pytest.mark.parametrize("frontend", list(FRONTENDS))
def test_events_cut_across_wire_chunks_reach_the_client_whole(frontend_gateway, fake_upstreams, frontend) -> None:
    serve, hop = frontend_gateway
    fake = serve("stream-split-event", wire=True)

    response, raw = _run(fake_upstreams, hop, frontend, stream=True, release_after_events=[1, 2, 4])

    assert response.status_code == 200, raw
    _forwarded_stream(fake, frontend)
    if frontend == "anthropic":
        expected = _anthropic_stream("msg_chatcmpl-fk-split", ["Hello", " café"], _anthropic_end({"output_tokens": 0}))
        events = _anthropic_events(raw)
        assert [name for name, _ in events] == [name for name, _ in expected]
    else:
        expected = [_ollama_chunk("Hello"), _ollama_chunk(" café"), _ollama_done(0, 0)]
        events = _ollama_lines(raw)
        assert len(events) == len(expected)
    expect_fixed("FK-B4", events, expected)


@pytest.mark.parametrize("frontend", list(FRONTENDS))
def test_upstream_drop_mid_stream_ends_with_a_protocol_error(frontend_gateway, fake_upstreams, frontend) -> None:
    serve, hop = frontend_gateway
    fake = serve("stream-disconnect", wire=True)

    response, raw = _run(fake_upstreams, hop, frontend, stream=True)

    assert response.status_code == 200, raw
    _forwarded_stream(fake, frontend)
    if frontend == "anthropic":
        expected = _anthropic_stream("msg_chatcmpl-fk-drop", ["Partial", " answer"], [
            ("error", {"type": "error", "error": {"type": "api_error", "message": INTERRUPTED}}),
        ])
        events, kept = _anthropic_events(raw), 4
    else:
        expected = [_ollama_chunk("Partial"), _ollama_chunk(" answer"), {"error": INTERRUPTED}]
        events, kept = _ollama_lines(raw), 2
    assert events[:kept] == expected[:kept]
    expect_fixed("FK-B5", events[kept:], expected[kept:])


@pytest.mark.xfail(strict=True, raises=KnownBug, reason="FK-B6: the frontends drop the upstream Retry-After header (fix/fk-upstream-faults)")
@pytest.mark.parametrize("stream", [False, True], ids=["text", "stream"])
@pytest.mark.parametrize("frontend", list(FRONTENDS))
def test_rate_limit_keeps_status_retry_after_and_message(frontend_gateway, fake_upstreams, frontend, stream) -> None:
    serve, hop = frontend_gateway
    fake = serve("rate-limited")

    response, raw = _run(fake_upstreams, hop, frontend, stream=stream)

    assert response.status_code == 429
    assert response.headers["content-type"].startswith("application/json")
    expected = (
        {"type": "error", "error": {"type": "rate_limit_error", "message": "Rate limit reached for requests"}}
        if frontend == "anthropic" else {"error": "Rate limit reached for requests"}
    )
    assert json.loads(raw) == expected
    [forwarded] = fake.calls("chat")
    assert forwarded.json["model"] == MODEL and forwarded.json["messages"] == MESSAGES and forwarded.json["stream"] is stream
    expect_fixed("FK-B6", response.headers.get("retry-after"), "7")


@pytest.mark.xfail(strict=True, raises=KnownBug, reason="FK-B3: the LLM substrate read timeout answers {'detail': ...}, which the frontends pass on as the message (fix/fk-upstream-faults)")
@pytest.mark.parametrize("stream", [False, True], ids=["text", "stream"])
@pytest.mark.parametrize("frontend", list(FRONTENDS))
def test_first_byte_past_the_read_budget_gives_a_504(frontend_gateway, fake_upstreams, monkeypatch, frontend, stream) -> None:
    serve, hop = frontend_gateway
    fake = serve("slow-first-byte", wire=True)
    monkeypatch.setattr(proxy, "UPSTREAM_READ_TIMEOUT", READ_BUDGET_SECONDS)

    response, raw = _run(fake_upstreams, hop, frontend, stream=stream)

    assert response.status_code == 504
    assert response.headers["content-type"].startswith("application/json")
    [forwarded] = fake.calls("chat")
    assert forwarded.json["model"] == MODEL and forwarded.json["messages"] == MESSAGES and forwarded.json["stream"] is stream
    expected = (
        {"type": "error", "error": {"type": "api_error", "message": "LiteLLM execution timed out."}}
        if frontend == "anthropic" else {"error": "LiteLLM execution timed out."}
    )
    expect_fixed("FK-B3", json.loads(raw), expected)


def test_frontend_sse_reader_reassembles_a_cut_utf8_character(fake_upstreams) -> None:
    """The frontends' SSE reader on its own, against the wire, without the chat hop's event alignment."""
    fake = fake_upstreams.loopback(load_scenario("openai-compatible/stream-split-event", model=MODEL))

    async def read() -> list[dict]:
        events: list[dict] = []
        async with asyncio.timeout(HANG_BOUND_SECONDS), httpx.AsyncClient() as client:
            async with client.stream("POST", f"{fake.base_url}/v1/chat/completions", json={"model": MODEL, "stream": True}) as response:
                async for event in proxy._iter_openai_sse_json(response):
                    events.append(event)
                    if len(events) in (1, 2, 4):
                        await fake_upstreams.clock.until_waiting()
                        fake_upstreams.clock.advance(1)
        return events

    events = asyncio.run(read())

    deltas = [event["choices"][0]["delta"] for event in events]
    assert len(deltas) == 4 and deltas[:2] == [{"role": "assistant", "content": ""}, {"content": "Hello"}]
    expect_fixed("FK-B4", deltas[2:], [{"content": " café"}, {}])
