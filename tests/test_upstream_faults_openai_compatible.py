"""Chat over the LLM substrate's openai-compatible wire, normal and faulted.

The gateway's chat route forwards to the registry's LLM substrate (an
openai-compatible upstream). Each case serves one FK scenario
(tests/support/fake_upstream/scenarios/openai-compatible/) and asserts the
complete client-visible result plus the forwarded request. Wire faults run on
a loopback port; application semantics run in process.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

import proxy
from tests.support.fake_upstream import FakeUpstream, FakeUpstreams, KnownBug, expect_fixed, fake_upstreams, load_scenario  # noqa: F401
from tests.support.gateway import READ_BUDGET_SECONDS, LITELLM_MASTER_KEY, gateway_client, register_litellm, use_admin_db

MODEL = "fk-model"
MESSAGES = [{"role": "user", "content": "hi"}]
# Bounds a hang that only a broken gateway produces; clean code never waits on it.
HANG_BOUND_SECONDS = 10
CHUNK = {"object": "chat.completion.chunk", "created": 1767225600, "model": MODEL}


def _serve(fakes: FakeUpstreams, monkeypatch, tmp_path, name: str, *, wire: bool = False, **values: Any) -> FakeUpstream:
    scenario = load_scenario(f"openai-compatible/{name}", model=MODEL, **values)
    fake = fakes.loopback(scenario) if wire else fakes.asgi(scenario, host="litellm.fk.test")
    register_litellm(monkeypatch, use_admin_db(monkeypatch, tmp_path), fake.base_url)
    return fake


def _events(raw: bytes) -> list[Any]:
    """Parse an SSE body into data payloads; ``[DONE]`` stays a string."""
    text = raw.decode("utf-8")
    assert text.endswith("\n\n"), f"stream does not end on an event boundary: {text[-80:]!r}"
    out: list[Any] = []
    for block in text[:-2].split("\n\n"):
        lines = block.split("\n")
        assert all(line.startswith("data: ") for line in lines), f"not an SSE data event: {block!r}"
        payload = "\n".join(line[len("data: "):] for line in lines)
        out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


def _delta(chunk_id: str, delta: dict, finish_reason: str | None = None) -> dict:
    return {"id": chunk_id, **CHUNK, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}


async def _stream(fakes: FakeUpstreams, *, release_after: list[int]) -> tuple[httpx.Response, bytes]:
    """POST a streamed chat; advance the clock once the client holds N complete events."""
    received = b""
    async with asyncio.timeout(HANG_BOUND_SECONDS), gateway_client() as client:
        async with client.stream("POST", "/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES, "stream": True}) as response:
            async for chunk in response.aiter_raw():
                received += chunk
                while release_after and received.count(b"\n\n") >= release_after[0]:
                    await fakes.clock.until_waiting()
                    fakes.clock.advance(1)
                    release_after.pop(0)
    return response, received


def test_chat_text_returns_the_upstream_answer_and_forwards_the_request(fake_upstreams, monkeypatch, tmp_path) -> None:
    fake = _serve(fake_upstreams, monkeypatch, tmp_path, "chat-text", content="Hello there")

    async def call() -> httpx.Response:
        async with gateway_client() as client:
            return await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES, "temperature": 0.2})

    response = asyncio.run(call())

    assert response.status_code == 200
    assert response.json() == {
        "id": "chatcmpl-fk-text",
        "object": "chat.completion",
        "created": 1767225600,
        "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "Hello there"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }
    assert response.headers["x-azizam-llm-execution"] == "litellm"
    [forwarded] = fake.calls("chat")
    assert forwarded.json == {"model": MODEL, "messages": MESSAGES, "temperature": 0.2}
    assert forwarded.headers["x-litellm-key"] == f"Bearer {LITELLM_MASTER_KEY}"
    assert "authorization" not in forwarded.headers
    assert forwarded.headers["x-azizam-request-id"] == response.headers["x-azizam-request-id"]


def test_chat_stream_relays_every_event_and_the_done_marker(fake_upstreams, monkeypatch, tmp_path) -> None:
    fake = _serve(fake_upstreams, monkeypatch, tmp_path, "chat-stream")

    response, raw = asyncio.run(_stream(fake_upstreams, release_after=[]))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert _events(raw) == [
        _delta("chatcmpl-fk-stream", {"role": "assistant", "content": ""}),
        _delta("chatcmpl-fk-stream", {"content": "Hello"}),
        _delta("chatcmpl-fk-stream", {"content": " café"}),
        _delta("chatcmpl-fk-stream", {}, "stop"),
        {"id": "chatcmpl-fk-stream", **CHUNK, "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}},
        "[DONE]",
    ]
    [forwarded] = fake.calls("chat")
    assert forwarded.json == {"model": MODEL, "messages": MESSAGES, "stream": True}


def test_events_cut_across_wire_chunks_reach_the_client_whole(fake_upstreams, monkeypatch, tmp_path) -> None:
    """Cuts inside JSON, inside 'é' (C3|A9) and inside [DONE]; the upstream then keeps the socket open."""
    fake = _serve(fake_upstreams, monkeypatch, tmp_path, "stream-split-event", wire=True)

    response, raw = asyncio.run(_stream(fake_upstreams, release_after=[1, 2, 4]))

    assert response.status_code == 200
    assert _events(raw) == [
        _delta("chatcmpl-fk-split", {"role": "assistant", "content": ""}),
        _delta("chatcmpl-fk-split", {"content": "Hello"}),
        _delta("chatcmpl-fk-split", {"content": " café"}),
        _delta("chatcmpl-fk-split", {}, "stop"),
        "[DONE]",
    ]
    assert _forwarded_stream(fake) == {"model": MODEL, "messages": MESSAGES, "stream": True}


def _forwarded_stream(fake: FakeUpstream) -> dict:
    [forwarded] = fake.calls("chat")
    return forwarded.json


def test_think_leak_cut_across_wire_chunks_is_repaired_like_an_uncut_event(fake_upstreams, monkeypatch, tmp_path) -> None:
    fake = _serve(fake_upstreams, monkeypatch, tmp_path, "stream-split-think", wire=True)

    response, raw = asyncio.run(_stream(fake_upstreams, release_after=[1]))

    assert response.status_code == 200
    assert _forwarded_stream(fake) == {"model": MODEL, "messages": MESSAGES, "stream": True}
    events = _events(raw)
    expected = [
        _delta("chatcmpl-fk-think", {"role": "assistant", "content": ""}),
        _delta("chatcmpl-fk-think", {"content": "Answer"}),
        _delta("chatcmpl-fk-think", {}, "stop"),
        "[DONE]",
    ]
    # Everything but the repaired middle event is already right and stays asserted.
    assert [events[0], *events[-2:]] == [expected[0], *expected[-2:]]
    expect_fixed("FK-B1", events[1:-2], expected[1:-2])


def test_upstream_drop_mid_stream_ends_with_an_error_event(fake_upstreams, monkeypatch, tmp_path) -> None:
    fake = _serve(fake_upstreams, monkeypatch, tmp_path, "stream-disconnect", wire=True)

    response, raw = asyncio.run(_stream(fake_upstreams, release_after=[]))

    assert response.status_code == 200
    assert _forwarded_stream(fake) == {"model": MODEL, "messages": MESSAGES, "stream": True}
    events = _events(raw)
    assert events[:2] == [
        _delta("chatcmpl-fk-drop", {"role": "assistant", "content": "Partial"}),
        _delta("chatcmpl-fk-drop", {"content": " answer"}),
    ]
    expect_fixed("FK-B2", events[2:], [{"error": {
        "message": "The LLM execution stream ended before the response was complete.",
        "type": "api_error",
        "param": None,
        "code": "upstream_stream_interrupted",
    }}])


@pytest.mark.parametrize("stream", [False, True], ids=["text", "stream"])
def test_rate_limit_keeps_status_retry_after_and_error_body(fake_upstreams, monkeypatch, tmp_path, stream: bool) -> None:
    fake = _serve(fake_upstreams, monkeypatch, tmp_path, "rate-limited")

    async def call() -> httpx.Response:
        async with gateway_client() as client:
            return await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES, "stream": stream})

    response = asyncio.run(call())

    assert response.status_code == 429
    assert response.headers["retry-after"] == "7"
    assert response.json() == {"error": {"message": "Rate limit reached for requests", "type": "requests", "param": None, "code": "rate_limit_exceeded"}}
    [forwarded] = fake.calls("chat")
    assert forwarded.json == {"model": MODEL, "messages": MESSAGES, "stream": stream}


@pytest.mark.parametrize("stream", [False, True], ids=["text", "stream"])
def test_first_byte_past_the_read_budget_gives_a_504_error_envelope(fake_upstreams, monkeypatch, tmp_path, stream: bool) -> None:
    """One real-clock wiring case: the read budget is the gateway's own setting, shortened."""
    fake = _serve(fake_upstreams, monkeypatch, tmp_path, "slow-first-byte", wire=True)
    monkeypatch.setattr(proxy, "UPSTREAM_READ_TIMEOUT", READ_BUDGET_SECONDS)

    async def call() -> httpx.Response:
        async with asyncio.timeout(HANG_BOUND_SECONDS), gateway_client() as client:
            return await client.post("/v1/chat/completions", json={"model": MODEL, "messages": MESSAGES, "stream": stream})

    response = asyncio.run(call())

    assert response.status_code == 504
    assert response.headers["content-type"].startswith("application/json")
    assert _forwarded_stream(fake) == {"model": MODEL, "messages": MESSAGES, "stream": stream}
    expect_fixed("FK-B3", response.json(), {"error": {"message": "LiteLLM execution timed out.", "type": "api_error", "param": None, "code": "timeout"}})
