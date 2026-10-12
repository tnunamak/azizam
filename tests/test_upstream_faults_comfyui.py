"""ComfyUI wire protocol: the image round, normal and faulted.

The gateway submits a prompt, polls ``/history/{id}`` and downloads ``/view``
(``proxy._comfy_run_round``). Each case serves one FK scenario
(tests/support/fake_upstream/scenarios/comfyui/) and asserts the complete
result the caller gets plus the forwarded requests. ComfyUI does not stream
SSE, so the "split event" fault is the history JSON arriving in pieces.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

import proxy
from tests.support.gateway import READ_BUDGET_SECONDS
from tests.support.fake_upstream import FakeUpstream, FakeUpstreams, KnownBug, expect_fixed, fake_upstreams, load_scenario  # noqa: F401

WORKFLOW = {"1": {"class_type": "FkNode", "inputs": {"text": "a red fox"}}}
PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")
HANG_BOUND_SECONDS = 10


def _serve(fakes: FakeUpstreams, monkeypatch, name: str, *, wire: bool = False) -> FakeUpstream:
    scenario = load_scenario(f"comfyui/{name}", text="a red fox")
    fake = fakes.loopback(scenario) if wire else fakes.asgi(scenario, host="comfy.fk.test")
    monkeypatch.setattr(proxy, "COMFY_HOST", fake.base_url)
    return fake


async def _round(fakes: FakeUpstreams, *, release_waits: int = 0) -> tuple[Any, list[bytes]]:
    """Run one image round; advance the clock ``release_waits`` times, each once the fake is blocked on it."""

    async def pump() -> None:
        for _ in range(release_waits):
            await fakes.clock.until_waiting()
            fakes.clock.advance(1)

    async with asyncio.timeout(HANG_BOUND_SECONDS), httpx.AsyncClient(timeout=httpx.Timeout(HANG_BOUND_SECONDS, read=READ_BUDGET_SECONDS)) as client:
        releaser = asyncio.create_task(pump())
        result = await proxy._comfy_run_round(client, json.loads(json.dumps(WORKFLOW)), {}, "fk", max_wait_s=2)
        await releaser
        return result


def _error(response) -> tuple[int, dict]:
    return response.status_code, json.loads(response.body)


def _expect_forwarded_round(fake: FakeUpstream) -> None:
    [submit] = fake.calls("prompt")
    assert submit.json["prompt"] == WORKFLOW
    [view] = fake.calls("view")
    assert (view.query["filename"], view.query["type"]) == ("café.png", "output")


@pytest.mark.parametrize("wire", [False, True], ids=["asgi", "loopback"])
def test_image_round_returns_the_downloaded_image(fake_upstreams, monkeypatch, wire) -> None:
    fake = _serve(fake_upstreams, monkeypatch, "image-complete", wire=wire)

    error, images = asyncio.run(_round(fake_upstreams))

    assert error is None
    assert images == [PNG_SIGNATURE]
    _expect_forwarded_round(fake)


@pytest.mark.parametrize("wire", [False, True], ids=["asgi", "loopback"])
def test_history_cut_across_wire_chunks_is_read_whole(fake_upstreams, monkeypatch, wire) -> None:
    """Cut inside 'é' (C3|A9); the rest of the body waits on the clock."""
    fake = _serve(fake_upstreams, monkeypatch, "history-split", wire=wire)

    error, images = asyncio.run(_round(fake_upstreams, release_waits=1))

    assert error is None
    assert images == [PNG_SIGNATURE]
    _expect_forwarded_round(fake)


def test_submit_rate_limit_becomes_a_502_that_carries_the_upstream_body(fake_upstreams, monkeypatch) -> None:
    fake = _serve(fake_upstreams, monkeypatch, "prompt-rate-limited")

    error, images = asyncio.run(_round(fake_upstreams))

    assert images == []
    status, body = _error(error)
    assert status == 502
    upstream_body = json.dumps({"error": {"type": "rate_limited", "message": "too many requests"}})
    assert body == {"error": {"message": f"ComfyUI error: {upstream_body}", "type": "api_error"}}
    [submit] = fake.calls("prompt")
    assert submit.json["prompt"] == WORKFLOW


def test_submit_first_byte_past_the_read_budget_gives_a_504(fake_upstreams, monkeypatch) -> None:
    fake = _serve(fake_upstreams, monkeypatch, "prompt-slow-first-byte", wire=True)

    error, images = asyncio.run(_round(fake_upstreams))

    assert images == []
    assert _error(error) == (504, {"error": {"message": "ComfyUI request timeout", "type": "api_error"}})
    [submit] = fake.calls("prompt")
    assert submit.json["prompt"] == WORKFLOW


def test_download_cut_by_the_upstream_skips_the_image_instead_of_failing_the_round(fake_upstreams, monkeypatch) -> None:
    fake = _serve(fake_upstreams, monkeypatch, "view-disconnect", wire=True)

    try:
        error, images = asyncio.run(_round(fake_upstreams))
    except httpx.RemoteProtocolError as dropped:
        expect_fixed("FK-B7", f"raised {type(dropped).__name__}", "returned")
    else:
        assert (error, images) == (None, [])
    assert len(fake.calls("view")) == 1


def test_history_check_dropped_by_the_upstream_is_polled_again(fake_upstreams, monkeypatch) -> None:
    fake = _serve(fake_upstreams, monkeypatch, "history-disconnect", wire=True)

    try:
        error, images = asyncio.run(_round(fake_upstreams))
    except httpx.RemoteProtocolError as dropped:
        fake.waive("history", "view")  # the bug stops the poll that would follow
        expect_fixed("FK-B7", f"raised {type(dropped).__name__}", "returned")
    else:
        assert error is None and images == [PNG_SIGNATURE]
    assert len(fake.calls("history-dropped")) == 1


def test_submit_dropped_by_the_upstream_gives_a_502(fake_upstreams, monkeypatch) -> None:
    fake = _serve(fake_upstreams, monkeypatch, "prompt-disconnect", wire=True)

    try:
        error, images = asyncio.run(_round(fake_upstreams))
    except httpx.RemoteProtocolError as dropped:
        expect_fixed("FK-B7", f"raised {type(dropped).__name__}", "returned")
    else:
        assert images == []
        assert _error(error) == (502, {"error": {"message": f"ComfyUI connection dropped at {fake.base_url}", "type": "api_error"}})
    assert len(fake.calls("prompt")) == 1
