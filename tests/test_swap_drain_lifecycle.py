"""A model swap must not cut off a request the loaded model is still serving.

These tests drive the real ``proxy.ensure_target``, the real LiteLLM inner hop
(``/internal/litellm/local/{backend}/v1/chat/completions``), the ``current``
route and the real ``SystemdDriver`` against the fake engine in
tests/fake_systemd_engine.py, whose ``systemctl restart`` kills every stream
in flight. Nothing here runs systemctl, touches the real HOME, or opens a
socket.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import lifecycle_drivers as ld  # noqa: E402
from gateway.swap_drain import SwapDrain  # noqa: E402
from tests.fake_systemd_engine import (  # noqa: E402
    MODEL,
    FakeEngine,
    UpstreamStream,
    dropin_text,
    fake_svc,
    install_fake_engine,
)

BACKEND = "fake-llm"
INTERNAL_KEY = "internal-secret"


def _activation(profile: str) -> "proxy.LocalModelActivation":
    return proxy.LocalModelActivation(backend=BACKEND, model=MODEL, profile=profile)


@pytest.fixture
def engine(tmp_path, monkeypatch) -> FakeEngine:
    fake = install_fake_engine(monkeypatch, tmp_path, profile="p-a")

    async def loaded_model(_backend: str) -> str:
        return MODEL

    monkeypatch.setattr(proxy, "_currently_loaded_model_for", loaded_model)
    monkeypatch.setattr(proxy, "_currently_loaded_aux_for", lambda _backend: None)
    _set_fake_backend(monkeypatch, fake_svc())
    monkeypatch.setitem(proxy.config, "llm", {"swap_drain": {"enabled": True, "timeout_s": 5}})
    monkeypatch.setattr(proxy, "_swap_drain", SwapDrain())
    monkeypatch.setattr(proxy, "_service_swap_locks", {})
    monkeypatch.setattr(proxy, "_service_availability_response", lambda *_a, **_k: None)
    monkeypatch.setitem(proxy.LLM_BACKEND_HOSTS, BACKEND, "http://127.0.0.1:59999")
    monkeypatch.setenv("AZIZAM_INTERNAL_LITELLM_KEY", INTERNAL_KEY)
    return fake


def _set_fake_backend(monkeypatch, svc: dict) -> None:
    """Add the fake engine next to the configured backends (aliases still resolve)."""
    monkeypatch.setitem(proxy.config, "backends", {**(proxy.config.get("backends") or {}), BACKEND: svc})


def _set_drain(monkeypatch, **block: Any) -> None:
    monkeypatch.setitem(proxy.config, "llm", {"swap_drain": block})


def _run(coro: Any) -> Any:
    """Run a scenario; a regression that blocks fails here instead of hanging."""
    return asyncio.run(asyncio.wait_for(coro, 10))


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def _until(condition: Any) -> None:
    """Wait for `condition()` to hold, yielding to the loop between checks.

    `_settle` yields a fixed number of times, which cannot cover work that hops
    through a worker thread (the outer hop's availability check): that work
    finishes on the thread's schedule, not the loop's. Poll the state the test
    asserts on, inside the scenario's own 10 s deadline from `_run`."""
    while not condition():
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Kill mid-request: the bug, and the fix
# ---------------------------------------------------------------------------


async def _a_streams_while_b_switches(engine: FakeEngine) -> tuple[UpstreamStream, list[str]]:
    """A is generating on p-a (inner hop). B asks for p-b. Return A and the
    restarts that happened while A was still in flight."""
    ready, lease = await proxy._ensure_target_leased(BACKEND, _activation("p-a"))
    assert ready
    a_stream = engine.serve("A")

    b = asyncio.create_task(proxy.ensure_target(BACKEND, MODEL, None, profile="p-b"))
    await asyncio.sleep(0.05)
    restarts_while_a_in_flight = engine.restarts()

    a_stream.done()
    if lease is not None:
        lease.release()
    assert await asyncio.wait_for(b, 5) is True
    return a_stream, restarts_while_a_in_flight


def test_without_drain_a_swap_kills_the_request_in_flight(engine, monkeypatch) -> None:
    """Reproduces the bug: today's behaviour, kept when the flag is off."""
    _set_drain(monkeypatch, enabled=False)
    a_stream, restarts_while_a_in_flight = _run(_a_streams_while_b_switches(engine))
    assert restarts_while_a_in_flight == ["p-b"]
    assert a_stream.was_killed
    assert engine.killed == ["A"]


def test_with_drain_the_swap_waits_for_the_request_in_flight(engine) -> None:
    a_stream, restarts_while_a_in_flight = _run(_a_streams_while_b_switches(engine))
    assert restarts_while_a_in_flight == [], "no restart while A is generating"
    assert not a_stream.was_killed
    assert engine.restarts() == ["p-b"], "the swap happens after A finishes"
    assert proxy._swap_drain.in_flight(BACKEND) == 0


def test_a_request_for_the_loaded_target_does_not_wait_or_restart(engine) -> None:
    async def scenario() -> float:
        ready, held = await proxy._ensure_target_leased(BACKEND, _activation("p-a"))
        assert ready
        start = time.monotonic()
        ready, second = await proxy._ensure_target_leased(BACKEND, _activation("p-a"))
        elapsed = time.monotonic() - start
        assert ready
        held.release()
        second.release()
        return elapsed

    assert _run(scenario()) < 0.5
    assert engine.restarts() == []


# ---------------------------------------------------------------------------
# Deadlock: chat calls ensure_target on the outer route and again on the inner hop
# ---------------------------------------------------------------------------


async def _outer_hop(profile: str) -> Any:
    """The chat route's activation before LiteLLM. None means ready."""
    return await proxy._ensure_litellm_local_backend_target(
        proxy.NO_AUTH_CONTEXT,
        requested_model=f"fake-{profile}",
        backend=BACKEND,
        target_model=MODEL,
        target_profile=profile,
        enforce_gateway_policy=False,
    )


def test_outer_and_inner_ensure_target_interleave_without_deadlock(engine) -> None:
    """Red-team P1-3 interleave: A passes its OUTER ensure_target, B takes the
    lock to switch, then A's INNER hop needs the same lock. A holds no lease
    between its two hops, so B never waits for A, and A's inner hop only waits
    for B's swap to finish (not for B's request). A design that counted A from
    the outer hop would deadlock here until the drain timeout."""

    async def scenario() -> None:
        # A, outer hop (before LiteLLM): target already loaded.
        assert await _outer_hop("p-a") is None

        # B, outer hop: switches to p-b. Hold its restart so A's inner hop
        # arrives while B owns the lock.
        engine.restart_gate = asyncio.Event()
        b_outer = asyncio.create_task(_outer_hop("p-b"))
        await _until(proxy.swap_lock_for(BACKEND).locked)
        assert proxy._swap_drain.in_flight(BACKEND) == 0, "A is not counted between hops"

        # A, inner hop: blocks on the lock, holding no lease.
        a_inner = asyncio.create_task(proxy._ensure_target_leased(BACKEND, _activation("p-a")))
        await _settle()
        assert not a_inner.done()

        engine.restart_gate.set()
        assert await asyncio.wait_for(b_outer, 1) is None, "B's swap did not wait for A"

        # A's inner hop switches back to p-a (no one is in flight) and leases.
        ready, a_lease = await asyncio.wait_for(a_inner, 1)
        assert ready and a_lease is not None
        a_stream = engine.serve("A")

        # B, inner hop: must wait for A's generation, then switch.
        b_inner = asyncio.create_task(proxy._ensure_target_leased(BACKEND, _activation("p-b")))
        await asyncio.sleep(0.05)
        assert not b_inner.done(), "B waits for A instead of restarting under it"
        a_stream.done()
        a_lease.release()
        ready, b_lease = await asyncio.wait_for(b_inner, 1)
        assert ready and b_lease is not None
        b_lease.release()

    _run(scenario())
    assert engine.restarts() == ["p-b", "p-a", "p-b"]
    assert engine.killed == []


def test_the_lease_is_granted_in_the_same_lock_hold_as_the_swap_that_made_it_ready(engine) -> None:
    """A switches to p-b; B, queued on the lock, wants p-a. No lease exists
    while A's restart runs, and A leases before it releases the lock, so B's
    swap back to p-a must wait for A instead of restarting under it."""

    async def scenario() -> None:
        engine.restart_gate = asyncio.Event()
        a = asyncio.create_task(proxy._ensure_target_leased(BACKEND, _activation("p-b")))
        await _settle()
        assert proxy.swap_lock_for(BACKEND).locked(), "A is inside its restart"
        b = asyncio.create_task(proxy._ensure_target_leased(BACKEND, _activation("p-a")))
        await _settle()
        assert proxy._swap_drain.in_flight(BACKEND) == 0, "no lease before the target is ready"

        engine.restart_gate.set()
        ready, a_lease = await asyncio.wait_for(a, 1)
        assert ready and a_lease is not None
        a_stream = engine.serve("A")
        await asyncio.sleep(0.05)
        assert not b.done(), "B waits for A's lease"
        assert engine.restarts() == ["p-b"]

        a_stream.done()
        a_lease.release()
        ready, b_lease = await asyncio.wait_for(b, 1)
        assert ready and b_lease is not None
        b_lease.release()

    _run(scenario())
    assert engine.restarts() == ["p-b", "p-a"]
    assert engine.killed == []


# ---------------------------------------------------------------------------
# Starvation: new requests for the old target must not hold a pending swap off
# ---------------------------------------------------------------------------


def test_new_old_target_requests_queue_behind_a_pending_swap(engine) -> None:
    async def scenario() -> list[str]:
        order: list[str] = []
        ready, a_lease = await proxy._ensure_target_leased(BACKEND, _activation("p-a"))
        assert ready

        b = asyncio.create_task(proxy.ensure_target(BACKEND, MODEL, None, profile="p-b"))
        await _settle()

        async def c_request() -> None:
            ready, c_lease = await proxy._ensure_target_leased(BACKEND, _activation("p-a"))
            assert ready
            order.append(f"C leased on {engine.loaded} after restarts {engine.restarts()}")
            c_lease.release()

        c = asyncio.create_task(c_request())
        # Keep sending old-target requests while B waits: none may be admitted.
        for _ in range(5):
            await asyncio.sleep(0.01)
            assert proxy._swap_drain.in_flight(BACKEND) == 1, "only A is in flight"
        assert not c.done()

        order.append("A finished")
        a_lease.release()
        assert await asyncio.wait_for(b, 5) is True
        await asyncio.wait_for(c, 5)
        return order

    order = _run(scenario())
    # C got no lease on p-a while B waited; it made p-a ready again after B's swap.
    assert order == ["A finished", "C leased on p-a after restarts ['p-b', 'p-a']"]
    assert engine.restarts() == ["p-b", "p-a"]
    assert engine.killed == []


# ---------------------------------------------------------------------------
# Timeout: the swap is refused, the request gets an honest 503
# ---------------------------------------------------------------------------


def test_drain_timeout_refuses_the_swap_and_returns_503_on_the_outer_route(engine, monkeypatch) -> None:
    _set_drain(monkeypatch, enabled=True, timeout_s=0.1)

    async def scenario() -> Any:
        ready, a_lease = await proxy._ensure_target_leased(BACKEND, _activation("p-a"))
        assert ready
        response = await proxy._ensure_litellm_local_backend_target(
            proxy.NO_AUTH_CONTEXT,
            requested_model="fake-p-b",
            backend=BACKEND,
            target_model=MODEL,
            target_profile="p-b",
            enforce_gateway_policy=False,
        )
        a_lease.release()
        return response

    response = _run(scenario())
    assert response is not None
    assert response.status_code == 503
    assert int(response.headers["Retry-After"]) > 0
    error = json.loads(response.body)["error"]
    assert error["code"] == "model_switch_deferred"
    assert BACKEND in error["message"]
    assert engine.restarts() == [], "a deferred swap must not restart the engine"
    assert engine.loaded == "p-a"
    assert engine.dropin.read_text() == dropin_text("p-a"), "the drop-in is untouched"


@pytest.mark.parametrize("drain_enabled", [False, True])
def test_a_failed_rollback_returns_a_distinct_503_on_the_outer_route(engine, monkeypatch, drain_enabled) -> None:
    """Neither p-b nor the previous p-a starts: the caller learns the backend
    may be down, not just that its model could not be loaded."""
    _set_drain(monkeypatch, enabled=drain_enabled, timeout_s=1)
    engine.restart_codes = [1, 1]
    response = _run(_outer_hop("p-b"))
    assert response is not None and response.status_code == 503
    error = json.loads(response.body)["error"]
    assert error["code"] == "backend_recovery_failed"
    assert "restoring" in error["message"]
    assert proxy._swap_drain.in_flight(BACKEND) == 0
    assert not proxy.swap_lock_for(BACKEND).locked()


def _inner_request(body: dict) -> Request:
    payload = json.dumps(body).encode()
    delivered = False

    async def receive() -> dict:
        nonlocal delivered
        if not delivered:
            delivered = True
            return {"type": "http.request", "body": payload, "more_body": False}
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    path = f"/internal/litellm/local/{BACKEND}/v1/chat/completions"
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"authorization", f"Bearer {INTERNAL_KEY}".encode()),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        },
        receive=receive,
    )


class _UpstreamResponse:
    """The engine's HTTP response for one request."""

    def __init__(self, stream: UpstreamStream, *, sse: bool) -> None:
        self._stream = stream
        self.status_code = 200
        self.headers = {"content-type": "text/event-stream" if sse else "application/json"}
        self.closed = False

    async def aiter_bytes(self):
        yield b'data: {"choices":[{"delta":{"content":"one"}}]}\n\n'
        await self._stream.finish.wait()
        if self._stream.was_killed:
            raise httpx.RemoteProtocolError("peer closed connection: engine restarted")
        yield b"data: [DONE]\n\n"

    async def aread(self) -> bytes:
        await self._stream.finish.wait()
        if self._stream.was_killed:
            raise httpx.RemoteProtocolError("peer closed connection: engine restarted")
        return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    async def aclose(self) -> None:
        self.closed = True
        self._stream.done()


class _UpstreamClient:
    async def aclose(self) -> None:
        pass


def _fake_send(engine: FakeEngine, *, sse: bool, seen_in_flight: list[int]):
    async def send(_execution_request, *, cancellation_token=None):
        seen_in_flight.append(proxy._swap_drain.in_flight(BACKEND))
        return _UpstreamClient(), _UpstreamResponse(engine.serve("A"), sse=sse)

    return send


def test_drain_timeout_returns_503_on_the_inner_hop(engine, monkeypatch) -> None:
    _set_drain(monkeypatch, enabled=True, timeout_s=0.1)
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(engine, sse=False, seen_in_flight=sends))

    async def scenario() -> Any:
        ready, a_lease = await proxy._ensure_target_leased(BACKEND, _activation("p-a"))
        assert ready
        try:
            return await proxy._forward_internal_litellm_local_chat_completions(
                _inner_request({"model": "openai/fake-p-b", "messages": []}),
                backend=BACKEND,
            )
        finally:
            a_lease.release()

    # The inner hop resolves the model name through the backend's model data.
    svc = fake_svc()
    svc["models"] = [{"id": "fake-p-b", "activation_model": MODEL, "activation_profile": "p-b"}]
    _set_fake_backend(monkeypatch, svc)
    response = _run(scenario())
    assert response.status_code == 503
    assert int(response.headers["Retry-After"]) > 0
    assert json.loads(response.body)["error"]["code"] == "model_switch_deferred"
    assert sends == [], "nothing is forwarded upstream"
    assert engine.restarts() == []
    assert proxy._swap_drain.in_flight(BACKEND) == 0


# ---------------------------------------------------------------------------
# The inner hop holds the lease around the upstream forward only
# ---------------------------------------------------------------------------


def _models_for_inner_hop(monkeypatch) -> None:
    svc = fake_svc()
    svc["models"] = [
        {"id": "fake-p-a", "activation_model": MODEL, "activation_profile": "p-a"},
        {"id": "fake-p-b", "activation_model": MODEL, "activation_profile": "p-b"},
    ]
    _set_fake_backend(monkeypatch, svc)


def test_inner_hop_stream_holds_its_lease_until_the_final_byte(engine, monkeypatch) -> None:
    _models_for_inner_hop(monkeypatch)
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(engine, sse=True, seen_in_flight=sends))

    async def scenario() -> list[bytes]:
        response = await proxy._forward_internal_litellm_local_chat_completions(
            _inner_request({"model": "openai/fake-p-a", "messages": [], "stream": True}),
            backend=BACKEND,
        )
        assert isinstance(response, StreamingResponse)
        # The handler has returned; the stream still owns the lease.
        assert proxy._swap_drain.in_flight(BACKEND) == 1
        body = response.body_iterator
        chunks = [await body.__anext__()]

        b = asyncio.create_task(proxy.ensure_target(BACKEND, MODEL, None, profile="p-b"))
        await asyncio.sleep(0.05)
        assert engine.restarts() == [], "B waits while A streams"

        (stream,) = engine.streams
        stream.finish.set()
        async for chunk in body:
            chunks.append(chunk)
        assert proxy._swap_drain.in_flight(BACKEND) == 0
        assert await asyncio.wait_for(b, 5) is True
        return chunks

    chunks = _run(scenario())
    assert sends == [1], "the lease is taken before the upstream send"
    assert chunks[-1] == b"data: [DONE]\n\n"
    assert engine.restarts() == ["p-b"]
    assert engine.killed == []


def test_inner_hop_stream_releases_its_lease_when_cancelled(engine, monkeypatch) -> None:
    _models_for_inner_hop(monkeypatch)
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(engine, sse=True, seen_in_flight=[]))

    async def scenario() -> None:
        response = await proxy._forward_internal_litellm_local_chat_completions(
            _inner_request({"model": "openai/fake-p-a", "messages": [], "stream": True}),
            backend=BACKEND,
        )
        assert isinstance(response, StreamingResponse)

        async def client_reads() -> None:
            async for _ in response.body_iterator:
                pass

        reader = asyncio.create_task(client_reads())
        await asyncio.sleep(0.02)
        assert proxy._swap_drain.in_flight(BACKEND) == 1
        reader.cancel()  # Starlette cancels the body on client disconnect
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert proxy._swap_drain.in_flight(BACKEND) == 0

    _run(scenario())


def test_inner_hop_non_stream_releases_its_lease_on_return(engine, monkeypatch) -> None:
    _models_for_inner_hop(monkeypatch)
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(engine, sse=False, seen_in_flight=sends))

    async def scenario() -> Any:
        task = asyncio.create_task(proxy._forward_internal_litellm_local_chat_completions(
            _inner_request({"model": "openai/fake-p-a", "messages": []}),
            backend=BACKEND,
        ))
        await asyncio.sleep(0.02)
        assert proxy._swap_drain.in_flight(BACKEND) == 1
        (stream,) = engine.streams
        stream.finish.set()
        return await asyncio.wait_for(task, 5)

    response = _run(scenario())
    assert response.status_code == 200
    assert sends == [1]
    assert proxy._swap_drain.in_flight(BACKEND) == 0


def test_inner_hop_releases_its_lease_when_the_upstream_is_unreachable(engine, monkeypatch) -> None:
    _models_for_inner_hop(monkeypatch)

    async def refuse(_execution_request, *, cancellation_token=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(proxy, "send_llm_execution_request", refuse)

    async def scenario() -> Any:
        return await proxy._forward_internal_litellm_local_chat_completions(
            _inner_request({"model": "openai/fake-p-a", "messages": [], "stream": True}),
            backend=BACKEND,
        )

    response = _run(scenario())
    assert response.status_code == 503
    assert proxy._swap_drain.in_flight(BACKEND) == 0


def _current_loaded_route(monkeypatch, engine: FakeEngine, forwarded: list[dict]) -> dict:
    """Serve model "current" from the fake engine with real target resolution.

    The loaded model id names the engine's loaded profile, so a forward that
    used a model resolved before a swap is visible in the forwarded body.
    """
    backends = {}
    for service_id, service in proxy.config["backends"].items():
        service = dict(service)
        if isinstance(service.get("models"), list):
            service["model_policy_schema_version"] = proxy.service_registry.MODEL_POLICY_SCHEMA_VERSION
        backends[service_id] = service
    monkeypatch.setitem(proxy.config, "backends", backends)

    async def active_backend() -> str:
        return BACKEND

    async def loaded_model_id(_backend: str) -> str:
        return f"model-{engine.loaded}"

    async def send(execution_request, *, disconnect_watcher=None, cancellation_token=None, on_signal=None):
        forwarded.append(json.loads(execution_request.body))
        return _UpstreamClient(), _UpstreamResponse(engine.serve("A"), sse=True)

    monkeypatch.setattr(proxy, "get_active_backend", active_backend)
    monkeypatch.setattr(proxy, "current_llm_model_for_backend", loaded_model_id)
    monkeypatch.setattr(proxy, "prepare_downstream_headers", lambda *_a, **_k: {})
    monkeypatch.setattr(proxy, "send_llm_execution_request", send)
    return {"model": "current", "messages": [{"role": "user", "content": "hi"}], "stream": True}


def test_current_loaded_stream_holds_a_lease_that_a_swap_waits_for(engine, monkeypatch) -> None:
    """model "current" is served by whatever is loaded, without ensure_target.
    A swap must still wait for it."""
    body = _current_loaded_route(monkeypatch, engine, [])

    async def scenario() -> None:
        response = await proxy._forward_current_loaded_openai_request(
            _inner_request(body),
            request_path="/v1/chat/completions",
            auth_context=proxy.NO_AUTH_CONTEXT,
            body_json=body,
            enforce_gateway_policy=False,
        )
        assert isinstance(response, StreamingResponse), getattr(response, "body", response)
        assert proxy._swap_drain.in_flight(BACKEND) == 1
        stream = response.body_iterator
        await stream.__anext__()

        b = asyncio.create_task(proxy.ensure_target(BACKEND, MODEL, None, profile="p-b"))
        await asyncio.sleep(0.05)
        assert engine.restarts() == [], "B waits while the current-loaded stream runs"

        (upstream,) = engine.streams
        upstream.finish.set()
        async for _ in stream:
            pass
        assert proxy._swap_drain.in_flight(BACKEND) == 0
        assert await asyncio.wait_for(b, 1) is True

    _run(scenario())
    assert engine.restarts() == ["p-b"]
    assert engine.killed == []


def test_current_forwards_the_model_loaded_when_it_leases_not_when_it_resolved(engine, monkeypatch) -> None:
    """Red-team-3 C2: "current" resolves the loaded model, then a swap lands
    before it leases. It must forward, and report, the model that is loaded
    while it holds the lease, never the one it saw before the swap."""
    forwarded: list[dict] = []
    body = _current_loaded_route(monkeypatch, engine, forwarded)
    real_resolve = proxy._current_loaded_llm_target
    resolved = asyncio.Event()
    resume = asyncio.Event()

    async def resolve_then_pause(*args: Any, **kwargs: Any) -> Any:
        result = await real_resolve(*args, **kwargs)
        if not resolved.is_set():
            resolved.set()
            await resume.wait()
        return result

    monkeypatch.setattr(proxy, "_current_loaded_llm_target", resolve_then_pause)

    async def scenario() -> Any:
        current = asyncio.create_task(proxy._forward_current_loaded_openai_request(
            _inner_request(body),
            request_path="/v1/chat/completions",
            auth_context=proxy.NO_AUTH_CONTEXT,
            body_json=body,
            enforce_gateway_policy=False,
        ))
        await asyncio.wait_for(resolved.wait(), 1)  # "current" saw model-p-a
        assert await proxy.ensure_target(BACKEND, MODEL, None, profile="p-b")
        resume.set()
        response = await asyncio.wait_for(current, 1)
        assert isinstance(response, StreamingResponse), getattr(response, "body", response)
        assert proxy._swap_drain.in_flight(BACKEND) == 1
        (upstream,) = engine.streams
        upstream.finish.set()
        async for _ in response.body_iterator:
            pass
        return response

    response = _run(scenario())
    assert engine.restarts() == ["p-b"]
    assert [sent["model"] for sent in forwarded] == ["model-p-b"]
    assert response.headers["X-Azizam-Resolved-Model"] == "model-p-b"
    assert proxy._swap_drain.in_flight(BACKEND) == 0


def test_current_waits_for_a_swap_in_progress_then_serves_the_new_model(engine, monkeypatch) -> None:
    """A "current" request that arrives while a swap restarts the engine must
    queue on the swap lock and forward the model the swap loaded."""
    forwarded: list[dict] = []
    body = _current_loaded_route(monkeypatch, engine, forwarded)

    async def scenario() -> Any:
        engine.restart_gate = asyncio.Event()
        b = asyncio.create_task(proxy.ensure_target(BACKEND, MODEL, None, profile="p-b"))
        await _settle()
        assert proxy.swap_lock_for(BACKEND).locked()
        current = asyncio.create_task(proxy._forward_current_loaded_openai_request(
            _inner_request(body),
            request_path="/v1/chat/completions",
            auth_context=proxy.NO_AUTH_CONTEXT,
            body_json=body,
            enforce_gateway_policy=False,
        ))
        await _settle()
        assert forwarded == [], "nothing is forwarded while the engine restarts"
        engine.restart_gate.set()
        assert await asyncio.wait_for(b, 1) is True
        response = await asyncio.wait_for(current, 1)
        (upstream,) = engine.streams
        upstream.finish.set()
        async for _ in response.body_iterator:
            pass
        return response

    response = _run(scenario())
    assert [sent["model"] for sent in forwarded] == ["model-p-b"]
    assert response.headers["X-Azizam-Resolved-Model"] == "model-p-b"
    assert engine.killed == []


def test_current_releases_its_lease_when_building_the_forward_fails(engine, monkeypatch) -> None:
    """A lease never expires, so every exit after the grant must release it,
    including a failure while the forwarded request is still being built."""
    body = _current_loaded_route(monkeypatch, engine, [])

    def broken_headers(*_args: Any, **_kwargs: Any) -> dict:
        raise RuntimeError("header preparation failed")

    monkeypatch.setattr(proxy, "prepare_downstream_headers", broken_headers)

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            await proxy._forward_current_loaded_openai_request(
                _inner_request(body),
                request_path="/v1/chat/completions",
                auth_context=proxy.NO_AUTH_CONTEXT,
                body_json=body,
                enforce_gateway_policy=False,
            )

    _run(scenario())
    assert proxy._swap_drain.in_flight(BACKEND) == 0


# ---------------------------------------------------------------------------
# Long requests: a live lease never ages into permission to swap
# ---------------------------------------------------------------------------


def test_a_live_stream_older_than_the_former_max_age_defers_the_swap(engine, monkeypatch) -> None:
    """Red-team-3 C1: a lease used to stop counting after 1800 s, so a swap
    restarted the engine under a long, healthy stream. The drain's timeout_s
    is the only bound: the swap is deferred with 503 and nothing restarts."""
    offset = [0.0]
    monkeypatch.setattr(proxy, "_swap_drain", SwapDrain(clock=lambda: time.monotonic() + offset[0]))
    _set_drain(monkeypatch, enabled=True, timeout_s=0.1)

    async def scenario() -> tuple[Any, UpstreamStream]:
        ready, lease = await proxy._ensure_target_leased(BACKEND, _activation("p-a"))
        assert ready and lease is not None
        a_stream = engine.serve("A")
        offset[0] += 3 * 3600  # A has streamed for three hours and is still going
        response = await proxy._ensure_litellm_local_backend_target(
            proxy.NO_AUTH_CONTEXT,
            requested_model="fake-p-b",
            backend=BACKEND,
            target_model=MODEL,
            target_profile="p-b",
            enforce_gateway_policy=False,
        )
        a_stream.done()
        lease.release()
        return response, a_stream

    response, a_stream = _run(scenario())
    assert response is not None and response.status_code == 503
    assert json.loads(response.body)["error"]["code"] == "model_switch_deferred"
    assert engine.restarts() == []
    assert not a_stream.was_killed


# ---------------------------------------------------------------------------
# swap_required: the drain waits only when swap() would really interrupt
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("profile", "pinned", "expected"),
    [("p-a", False, False), ("p-b", False, True), ("p-b", True, False)],
)
def test_systemd_swap_required_matches_what_swap_would_do(engine, profile, pinned, expected) -> None:
    driver = ld.SystemdDriver()
    activation = driver.build_activation(fake_svc(), model=MODEL, profile=profile)
    ctx = ld.SwapContext(loaded_model=MODEL, target_model=MODEL, target_profile=profile, pin_loaded=pinned)
    assert driver.swap_required(fake_svc(), activation, ctx) is expected
    assert _run(driver.swap(fake_svc(), activation, ctx=ctx)) is True
    assert bool(engine.restarts()) is expected


@pytest.mark.parametrize(
    ("loaded", "pinned", "expected"),
    [("m1", False, False), ("m0", False, True), ("m0", True, True), (None, False, True)],
)
def test_http_load_unload_swap_required_ignores_pin_like_swap(loaded, pinned, expected) -> None:
    ctx = ld.SwapContext(loaded_model=loaded, target_model="m1", pin_loaded=pinned)
    assert ld.HttpLoadUnloadDriver().swap_required({}, {"model_name": "m1"}, ctx) is expected
