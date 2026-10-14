"""Speech (TTS) engine protocol: the streamed PCM relay, normal and faulted.

The gateway forwards an engine's ``POST /v1/audio/speech`` PCM stream to its
client (``proxy._stream_tts_upstream``). Each case serves one FK scenario
(tests/support/fake_upstream/scenarios/openai-compatible/speech-*.json) and
asserts what the caller gets plus the request the engine received.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import proxy
from tests.support.fake_upstream import FakeUpstream, FakeUpstreams, fake_upstreams, load_scenario, read_deadline  # noqa: F401

HANG_BOUND_SECONDS = 10
REQUEST_BODY = b'{"input":"hello"}'


def _serve(fakes: FakeUpstreams, name: str, *, wire: bool = False) -> FakeUpstream:
    scenario = load_scenario(f"openai-compatible/{name}")
    return fakes.loopback(scenario) if wire else fakes.asgi(scenario)


async def _open(fake: FakeUpstream):
    async with asyncio.timeout(HANG_BOUND_SECONDS):
        return await proxy._stream_tts_upstream(
            url=f"{fake.base_url}/v1/audio/speech",
            headers={},
            body=REQUEST_BODY,
            stream_format="audio",
            response_format="pcm",
            native_rate=24000,
        )


async def _drain(response, fakes: FakeUpstreams | None = None, *, release_waits: int = 0) -> list[bytes]:
    async def pump() -> None:
        for _ in range(release_waits):
            await fakes.clock.until_waiting()
            fakes.clock.advance(1)

    releaser = asyncio.create_task(pump())
    async with asyncio.timeout(HANG_BOUND_SECONDS):
        chunks = [chunk async for chunk in response.body_iterator]
    await releaser
    return chunks


def _error(response) -> tuple[int, dict]:
    return response.status_code, json.loads(response.body)


def _expect_forwarded(fake: FakeUpstream) -> None:
    [call] = fake.calls("speech")
    assert call.body == REQUEST_BODY


@pytest.mark.parametrize("wire", [False, True], ids=["asgi", "loopback"])
def test_a_sample_cut_across_wire_chunks_is_delivered_whole(fake_upstreams, wire) -> None:
    fake = _serve(fake_upstreams, "speech-split-sample", wire=wire)

    async def call() -> list[bytes]:
        response = await _open(fake)
        return await _drain(response, fake_upstreams, release_waits=1)

    chunks = asyncio.run(call())

    assert b"".join(chunks) == bytes.fromhex("01000200")
    assert all(len(chunk) % 2 == 0 for chunk in chunks), chunks  # a client never gets half a 16-bit sample
    _expect_forwarded(fake)


def test_an_engine_that_drops_before_any_audio_gives_a_503(fake_upstreams) -> None:
    fake = _serve(fake_upstreams, "speech-disconnect-before-audio")

    response = asyncio.run(_open(fake))

    status, body = _error(response)
    assert status == 503
    assert body["error"]["code"] == "tts_engine_stream_failed"
    _expect_forwarded(fake)


def test_an_engine_that_drops_mid_stream_aborts_the_client_stream(fake_upstreams) -> None:
    """The first chunk is already committed as a 200; a drop after it must not look like a finished clip."""
    fake = _serve(fake_upstreams, "speech-disconnect-mid-stream")

    async def call() -> None:
        response = await _open(fake)
        assert response.status_code == 200
        await _drain(response)

    with pytest.raises(Exception) as dropped:
        asyncio.run(call())
    assert "peer closed connection" in str(dropped.value)
    _expect_forwarded(fake)


def test_a_rate_limited_engine_status_and_retry_after_reach_the_caller(fake_upstreams) -> None:
    fake = _serve(fake_upstreams, "speech-rate-limited")

    response = asyncio.run(_open(fake))

    assert response.status_code == 429
    assert json.loads(response.body)["error"]["type"] == "rate_limited"
    assert response.headers.get("retry-after") == "7"
    _expect_forwarded(fake)


def test_an_engine_that_never_answers_ends_at_the_read_budget(fake_upstreams, monkeypatch) -> None:
    fake = _serve(fake_upstreams, "speech-slow-first-byte", wire=True)
    deadline = read_deadline(monkeypatch, fake_upstreams.clock)

    response = asyncio.run(_open(fake))

    assert _error(response)[0] == 504
    assert deadline.read_fired == 1
    _expect_forwarded(fake)
