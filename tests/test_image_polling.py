"""Image completion polling latency and wait-budget contracts.

The gateway's ComfyUI round (``proxy._graph_run_round``) against the FK comfyui
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
from tests.support.graph_service import set_derived
from tests.support.config_snapshot import derived_patch
from tests.support.graph_selectors import IMAGE_AT_NODE_1
from tests.support.fake_upstream import FakeUpstream, FakeUpstreams, fake_upstreams, load_scenario, read_deadline  # noqa: F401

WORKFLOW = {"1": {"class_type": "FkNode", "inputs": {"text": "poll test"}}}
HANG_BOUND_SECONDS = 10


class _Round:
    def __init__(self, error, images, sleeps: list[float], fake: FakeUpstream) -> None:
        self.error, self.images, self.sleeps, self.fake = error, images, sleeps, fake
        self.elapsed = sum(sleeps)

    @property
    def history_checks(self) -> list[str]:
        return [request.exchange for request in self.fake.requests if request.path.startswith("/history/")]


def _run_round(fakes: FakeUpstreams, monkeypatch, name: str, *, wire: bool = False, hold_deadline: bool = False) -> _Round:
    scenario = load_scenario(f"comfyui/{name}", text="poll test")
    fake = fakes.loopback(scenario) if wire else fakes.asgi(scenario, host="comfy.fk.test")
    set_derived(monkeypatch, "GRAPH_HOST", fake.base_url)
    deadline = read_deadline(monkeypatch, fakes.clock) if hold_deadline else None
    sleeps: list[float] = []

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async def call():
        timeout = httpx.Timeout(HANG_BOUND_SECONDS, read=60.0)
        async with asyncio.timeout(HANG_BOUND_SECONDS), httpx.AsyncClient(timeout=timeout) as client:
            return await proxy._graph_run_round(client, json.loads(json.dumps(WORKFLOW)), {}, "poll test", max_wait_s=2, capability=proxy._GRAPH_CAPABILITY, **IMAGE_AT_NODE_1)

    with patch.object(proxy.asyncio, "sleep", sleep), derived_patch("GRAPH_TIMEOUT", 100):
        error, images = asyncio.run(call())
    if deadline is not None:
        assert deadline.read_fired == 1
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
    result = _run_round(fake_upstreams, monkeypatch, "image-job-timeout", wire=True, hold_deadline=True)

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
    set_derived(monkeypatch, "GRAPH_HOST", refused)

    async def call():
        async with asyncio.timeout(HANG_BOUND_SECONDS), httpx.AsyncClient() as client:
            return await proxy._graph_run_round(client, json.loads(json.dumps(WORKFLOW)), {}, "refused", max_wait_s=2, capability=proxy._GRAPH_CAPABILITY, **IMAGE_AT_NODE_1)

    error, images = asyncio.run(call())

    assert images == []
    assert (error.status_code, json.loads(error.body)) == (502, {"error": {"message": f"ComfyUI unavailable at {refused}", "type": "api_error"}})


class _Caller:
    """A request whose caller (an auth context, as the routes store it) has the given credential kind; it never disconnects."""

    def __init__(self, auth_kind: str) -> None:
        identity = proxy.AuthIdentity(name="caller", key_hash="h", auth_kind=auth_kind)
        self.state = type("State", (), {"caller": proxy.AuthContext(identity=identity)})()

    async def is_disconnected(self) -> bool:
        return False


@pytest.mark.parametrize(("request_", "kind"), [
    (None, "personal"),
    (_Caller("app_key"), "personal"),
    (_Caller("service_key"), "service"),
])
def test_a_synchronous_round_asks_host_memory_admission_with_the_callers_app_kind(
        fake_upstreams, monkeypatch, request_, kind) -> None:
    """The synchronous round asks admission before it submits, bounded, as the App kind behind the caller's credential."""
    from gateway import host_memory_admission

    asks: list[host_memory_admission.Ask] = []
    adapter = proxy._graph_adapter(proxy._GRAPH_CAPABILITY)

    async def admit_prompt(self, payload, ask=host_memory_admission.Ask()):
        asks.append(ask)

    monkeypatch.setattr(type(adapter), "admit_prompt", admit_prompt)
    scenario = load_scenario("comfyui/image-complete", text="poll test")
    fake = fake_upstreams.asgi(scenario, host="comfy.fk.test")
    set_derived(monkeypatch, "GRAPH_HOST", fake.base_url)

    async def call():
        async with asyncio.timeout(HANG_BOUND_SECONDS), httpx.AsyncClient() as client:
            return await proxy._graph_run_round(client, json.loads(json.dumps(WORKFLOW)), {}, "poll test", request=request_,
                                                max_wait_s=2, capability=proxy._GRAPH_CAPABILITY, **IMAGE_AT_NODE_1)

    with derived_patch("GRAPH_TIMEOUT", 100):
        error, images = asyncio.run(call())

    assert error is None
    assert images == [bytes.fromhex("89504e470d0a1a0a")]
    assert [(ask.app_kind, ask.bounded) for ask in asks] == [(kind, True)]


def test_probe_is_stopped_when_submit_payload_build_raises(monkeypatch):
    class Probe:
        def __init__(self):
            self.started = False
            self.stopped = False
        def start(self):
            self.started = True
        async def stop(self):
            self.stopped = True
    probe = Probe()
    def fail(*_args):
        raise ValueError("cannot build")
    monkeypatch.setattr(proxy, "_build_graph_submit_payload", fail)
    async def call():
        async with httpx.AsyncClient() as client:
            await proxy._graph_run_round(client, {}, {}, "probe", vram_probe=probe, capability=proxy._GRAPH_CAPABILITY)
    with pytest.raises(ValueError, match="cannot build"):
        asyncio.run(call())
    assert probe.started and probe.stopped


def test_protocol_image_job_receives_service_caller(monkeypatch):
    from types import SimpleNamespace
    from gateway.engine_protocol import jobs
    class Stop(Exception):
        pass
    caller = SimpleNamespace(identity=SimpleNamespace(auth_kind="service_key"))
    seen = []
    async def fake_run(*args, **kwargs):
        seen.append(kwargs.get("caller"))
        raise Stop()
    monkeypatch.setattr(jobs, "run", fake_run)
    runtime = SimpleNamespace(target=SimpleNamespace(description=SimpleNamespace(operations={"images": SimpleNamespace(job=True)})))
    with pytest.raises(Stop):
        asyncio.run(proxy._protocol_image_result(runtime, {}, None, caller, SimpleNamespace(job_id="job"), None))
    assert seen == [caller]


def test_identity_scene_passes_service_caller_to_graph_round(monkeypatch):
    from types import SimpleNamespace
    caller = SimpleNamespace(identity=SimpleNamespace(auth_kind="service_key"))
    monkeypatch.setattr(proxy, "_graph_adapter", lambda *_: SimpleNamespace(label="graph"))
    monkeypatch.setattr(proxy, "_graph_service", lambda *_: SimpleNamespace(enabled=True, timeout=10))
    monkeypatch.setattr(proxy, "_graph_service_id", lambda *_: "graph")
    monkeypatch.setattr(proxy, "configured_downstream_headers", lambda *_: {})
    monkeypatch.setattr(proxy, "_graph_media_admission", lambda *a, **k: None)
    monkeypatch.setattr(proxy, "_identity_template", lambda: ({}, {}))
    monkeypatch.setattr(proxy, "_identity_cfg", lambda: {})
    monkeypatch.setattr(proxy.GATEWAY_WORKFLOW_REGISTRY, "get", lambda *_: SimpleNamespace(manifest={}))
    seen = []
    async def fake_round(*args, **kwargs):
        seen.append(kwargs.get("caller"))
        return None, [b"image"]
    monkeypatch.setattr(proxy, "_graph_run_round", fake_round)
    assert asyncio.run(proxy._identity_run_workflow(caller, {}, {})) == b"image"
    assert seen == [caller]
