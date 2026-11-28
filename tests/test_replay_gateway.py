"""RP2: the gateway against recorded transcripts of a real upstream (TEST_STRATEGY 4.7).

Each test serves a committed transcript as the LLM substrate, sends the
transcript's own request through the in-process gateway, and checks that the
client sees what the real upstream said. Transcript replay is strict, so a
gateway that rewrites the forwarded request fails here as well as one that
rewrites the answer.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.support.fake_upstream import FakeUpstreams, fake_upstreams, scenario_from_data  # noqa: F401
from tests.support.fake_upstream.scenario import SCENARIO_DIR
from tests.support.gateway import gateway_client, register_litellm, use_admin_db

HANG_BOUND_SECONDS = 15


def _transcript(name: str) -> dict:
    return json.loads((SCENARIO_DIR / "openai-compatible" / f"rp-{name}.json").read_text(encoding="utf-8"))


def _serve(fakes: FakeUpstreams, monkeypatch, tmp_path: Path, name: str):
    data = _transcript(name)
    fake = fakes.asgi(scenario_from_data(data, source=name), host="litellm.fk.test")
    register_litellm(monkeypatch, use_admin_db(monkeypatch, tmp_path), fake.base_url)
    return fake, data["exchanges"][0]


async def _post(path: str, body: dict):
    async with asyncio.timeout(HANG_BOUND_SECONDS), gateway_client() as client:
        return await client.post(path, json=body)


def _sse(raw: bytes) -> list:
    out = []
    for block in raw.decode("utf-8").split("\n\n"):
        if block:
            assert block.startswith("data: "), block
            payload = block[len("data: "):]
            out.append("[DONE]" if payload == "[DONE]" else json.loads(payload))
    return out


def test_chat_text_transcript_reaches_the_client_unchanged(fake_upstreams, monkeypatch, tmp_path) -> None:
    fake, exchange = _serve(fake_upstreams, monkeypatch, tmp_path, "chat-text")
    response = asyncio.run(_post("/v1/chat/completions", exchange["match"]["json"]))
    assert response.status_code == 200
    assert response.json() == exchange["response"]["json"]
    assert fake.problems() == []


def test_chat_stream_transcript_is_relayed_event_for_event(fake_upstreams, monkeypatch, tmp_path) -> None:
    fake, exchange = _serve(fake_upstreams, monkeypatch, tmp_path, "chat-stream")
    response = asyncio.run(_post("/v1/chat/completions", exchange["match"]["json"]))
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    expected = ["[DONE]" if "done" in part else part["event"] for part in exchange["response"]["body"]]
    assert _sse(response.content) == expected
    assert fake.problems() == []


def test_embeddings_transcript_reaches_the_client_unchanged(fake_upstreams, monkeypatch, tmp_path) -> None:
    fake, exchange = _serve(fake_upstreams, monkeypatch, tmp_path, "embeddings")
    response = asyncio.run(_post("/v1/embeddings", exchange["match"]["json"]))
    assert response.status_code == 200
    assert response.json() == exchange["response"]["json"]
    assert fake.problems() == []
