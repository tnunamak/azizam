"""Image completion polling latency and wait-budget contracts.

The gateway's ComfyUI round (``proxy._comfy_run_round``) against the FK comfyui
fake. The poll back-off sleeps through ``proxy.asyncio.sleep``; the tests
record those sleeps instead of waiting (the gateway has no clock seam).
"""

from __future__ import annotations

import asyncio
import json
import socket
from unittest.mock import patch

import httpx
import pytest

import proxy
from tests.support.gateway import READ_BUDGET_SECONDS
from tests.support.fake_upstream import FakeUpstream, FakeUpstreams, fake_upstreams, load_scenario  # noqa: F401

WORKFLOW = {"1": {"class_type": "FkNode", "inputs": {"text": "poll test"}}}
HANG_BOUND_SECONDS = 10


class _Round:
    def __init__(self, error, images, sleeps: list[float], fake: FakeUpstream) -> None:
        self.error, self.images, self.sleeps, self.fake = error, images, sleeps, fake
        self.elapsed = sum(sleeps)

    @property
    def history_checks(self) -> list[str]:
        return [request.exchange for request in self.fake.requests if request.path.startswith("/history/")]


def _run_round(fakes: FakeUpstreams, monkeypatch, name: str, *, wire: bool = False, host: str | None = None) -> _Round:
    scenario = load_scenario(f"comfyui/{name}", text="poll test")
    fake = fakes.loopback(scenario) if wire else fakes.asgi(scenario, host="comfy.fk.test")
    monkeypatch.setattr(proxy, "COMFY_HOST", host or fake.base_url)
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async def call():
        timeout = httpx.Timeout(HANG_BOUND_SECONDS, read=READ_BUDGET_SECONDS)
        async with asyncio.timeout(HANG_BOUND_SECONDS), httpx.AsyncClient(timeout=timeout) as client:
            return await proxy._comfy_run_round(client, json.loads(json.dumps(WORKFLOW)), {}, "poll test", max_wait_s=2)

    with patch.object(proxy.asyncio, "sleep", sleep), patch.object(proxy, "COMFY_TIMEOUT", 100):
        error, images = asyncio.run(call())
    return _Round(error, images, sleeps, fake)


def test_fast_completion_returns_without_sleep(fake_upstreams, monkeypatch) -> None:
    result = _run_round(fake_upstreams, monkeypatch, "image-complete")

    assert result.error is None
    assert result.images == [bytes.fromhex("89504e470d0a1a0a")]
    assert result.sleeps == []
    assert result.history_checks == ["history"]


def test_slow_completion_keeps_polling(fake_upstreams, monkeypatch) -> None:
    result = _run_round(fake_upstreams, monkeypatch, "image-slow-completion")

    assert result.error is None
    assert result.images == [b"generated image"]
    assert result.history_checks == ["pending"] * 5 + ["complete"]
    assert result.sleeps == [0.05, 0.1, 0.2, 0.4, 0.5]
    assert result.elapsed == pytest.approx(1.25)


def test_slow_job_times_out_with_original_wait_budget(fake_upstreams, monkeypatch) -> None:
    """A 503 and a check that gets no answer within the read budget are polled past; the 2 s budget then ends the job."""
    result = _run_round(fake_upstreams, monkeypatch, "image-job-timeout", wire=True)

    assert result.images == []
    assert result.error.status_code == 504
    body = json.loads(result.error.body)
    assert body["error"]["code"] == "image_generation_timeout"
    assert body["error"]["queue_position"] == 1
    assert result.elapsed == pytest.approx(2)
    assert result.history_checks[:2] == ["history-unavailable", "history-no-answer"]
    assert len(result.history_checks) > 4 and set(result.history_checks[2:]) == {"pending"}
    assert result.sleeps[:3] == [0.05, 0.1, 0.2] and max(result.sleeps) <= 0.5
    assert len(result.fake.calls("cancel")) == 1


def test_a_refused_submit_connection_gives_a_502(fake_upstreams, monkeypatch) -> None:
    """The ComfyUI host refuses the connection (a port that was just free), so the submit fails."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        refused = f"http://127.0.0.1:{probe.getsockname()[1]}"
    monkeypatch.setattr(proxy, "COMFY_HOST", refused)

    async def call():
        async with asyncio.timeout(HANG_BOUND_SECONDS), httpx.AsyncClient() as client:
            return await proxy._comfy_run_round(client, json.loads(json.dumps(WORKFLOW)), {}, "refused", max_wait_s=2)

    error, images = asyncio.run(call())

    assert images == []
    assert (error.status_code, json.loads(error.body)) == (502, {"error": {"message": f"ComfyUI unavailable at {refused}", "type": "api_error"}})
