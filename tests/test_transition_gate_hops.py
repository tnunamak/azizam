"""T1.1 oracle on fakes: a transition-enabled residency domain is leased at the gateway hops.

These drive the real LiteLLM inner hop and the real ``current`` route against the fake engine in
tests/fake_systemd_engine.py, with ``llm.swap_drain`` OFF: an enabled domain takes its lease whatever that
setting says. The services declare ``activation: pass_through``, so the fake's systemd driver must never run.
Oracle items covered: 1 (drain waits for a leased request), 3 (stream endings), 4 (re-check under the lock),
5 (aliases, buffered and streaming, nested activation, positive control) and 10 (independent domains, timing),
plus the step-T1.1 scope: a domain that is not pass-through, and the hops that hold no lease, refuse.
The protocol hop is in tests/test_transition_gate_protocol.py.
"""

from __future__ import annotations

import asyncio
import copy
import gc
import json
import sqlite3
import threading
import time
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from starlette.responses import StreamingResponse

import proxy
from gateway.swap_drain import SwapDrainSettings
from gateway.transition_gate import GateStore
from gateway import engine_egress
from gateway.transition_gate import TransitionUnavailable
from tests.fake_systemd_engine import MODEL, fake_svc
from tests.support.fake_upstream import HandlerTransport
from tests.support.fake_upstream.transport import HostRouter
from tests.support.fake_upstream.async_client_stub import install_async_client_stub, restore_async_client
from tests.support.config_snapshot import pin_inner_hop_hosts
from tests.support.stopped_engine import StoppedEngine
from tests.test_swap_drain_lifecycle import (  # noqa: F401 - engine is a fixture
    BACKEND,
    _current_loaded_route,
    _fake_send,
    _inner_request,
    _run,
    _set_drain,
    _until,
    engine,
)

_REAL_ASYNC_CLIENT = httpx.AsyncClient  # the fake engine below replaces httpx.AsyncClient for most tests here

ALIAS = "fake-llm-b"
OTHER = "other-llm"
DOMAIN = "fake-domain"


def _service(domain: str, *, transition: dict | None) -> dict:
    svc = fake_svc()
    svc["models"] = [{"id": "fake-p-a", "activation_model": MODEL, "activation_profile": "p-a"}]
    svc["residency"] = {"domain": domain, **({"transition": transition} if transition else {})}
    return svc


@pytest.fixture
def gate(engine, monkeypatch, tmp_path):
    enabled = {"enabled": True, "activation": "pass_through", "admission_wait_s": 0.2}
    backends = {**(proxy.config.get("backends") or {}),
                BACKEND: _service(DOMAIN, transition=enabled),
                # The alias declares pass-through too (every member of an enabled domain must); its
                # longer wait shows the domain takes the shortest one.
                ALIAS: _service(DOMAIN, transition={"enabled": True, "activation": "pass_through"}),
                OTHER: _service("other-domain", transition=enabled)}
    monkeypatch.setitem(proxy.config, "backends", backends)
    for name in (ALIAS, OTHER):
        monkeypatch.setitem(proxy.DERIVED.LLM_BACKEND_HOSTS, name, "http://127.0.0.1:59999")
    pin_inner_hop_hosts(monkeypatch, {name: "http://127.0.0.1:59999" for name in (ALIAS, OTHER)})
    _set_drain(monkeypatch, enabled=False)
    store = GateStore(tmp_path / "transition.db")
    store.load()
    monkeypatch.setattr(proxy, "TRANSITION_GATES", store)
    monkeypatch.setattr(proxy, "_swap_drain", proxy._new_swap_drain())
    overrides: dict[str, Any] = {}
    reads: list[int] = []

    def read_overrides() -> dict[str, Any]:
        reads.append(1)
        if overrides.get("__unreadable__"):
            raise sqlite3.OperationalError("database is locked")
        return overrides

    monkeypatch.setattr(proxy, "_strict_service_availability_overrides", read_overrides)
    return SimpleNamespace(engine=engine, store=store, overrides=overrides, reads=reads)


def _set_gate(store: GateStore, domain: str, state: str) -> None:
    with sqlite3.connect(store.path) as connection:
        connection.execute("INSERT OR REPLACE INTO transition_gate VALUES (?, ?, NULL, 0)", (domain, state))
    store.load()


def _stream_request(backend: str = BACKEND, stream: bool = True):
    return proxy._forward_internal_litellm_local_chat_completions(
        _inner_request({"model": "openai/fake-p-a", "messages": [], "stream": stream}), backend=backend)


def _error(response) -> tuple[int, str, str]:
    return response.status_code, json.loads(response.body)["error"]["code"], response.headers.get("Retry-After")


def _no_hold_left() -> None:
    """After a refusal or a cancel: no domain lock is held and no lease is open, in any domain of these tests."""
    for name in (BACKEND, ALIAS, DOMAIN, OTHER):
        assert not proxy.swap_lock_for(name).locked(), f"the lock of {name!r} is still held"
        assert proxy._swap_drain.in_flight(name) == 0, f"a lease on {name!r} is still open"


# -- oracle 1 and 5: the lease, its domain, and the drain that waits for it ----------------------------


def test_an_enabled_stream_holds_one_domain_lease_and_a_drain_waits_for_it(gate, monkeypatch) -> None:
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=True, seen_in_flight=sends))

    async def scenario() -> None:
        # The outer activation (LiteLLM's forward) takes no lease: no duplicate with the inner hop.
        assert await proxy.ensure_target(BACKEND, MODEL, None, profile="p-a") is True
        assert proxy._swap_drain.in_flight(DOMAIN) == 0
        response = await _stream_request()
        assert isinstance(response, StreamingResponse)
        assert [proxy._swap_drain.in_flight(name) for name in (BACKEND, ALIAS, DOMAIN)] == [1, 1, 1]
        assert proxy._swap_drain.in_flight(OTHER) == 0
        body = response.body_iterator
        await body.__anext__()

        async def transition() -> None:
            async with proxy.swap_lock_for(ALIAS):
                await proxy._swap_drain.clear_for_swap(ALIAS, SwapDrainSettings(enabled=True, timeout_s=5))
        drained = asyncio.create_task(transition())
        await asyncio.sleep(0.05)
        assert not drained.done(), "the drain on the alias waits for the stream on its domain"
        (stream,) = gate.engine.streams
        stream.finish.set()
        async for _ in body:
            pass
        await asyncio.wait_for(drained, 2)
        assert proxy._swap_drain.in_flight(DOMAIN) == 0

    _run(scenario())
    assert sends == [1], "the lease exists before the upstream send"
    assert gate.engine.restarts() == [], "pass-through never starts or swaps the process"
    assert proxy.swap_lock_for(BACKEND) is proxy.swap_lock_for(ALIAS) is proxy.swap_lock_for(DOMAIN)


def test_positive_control_without_transition_the_same_stream_holds_no_lease(gate, monkeypatch) -> None:
    """If the lease were removed, the oracle above must fail: show the legacy path leaves it at zero."""
    legacy = copy.deepcopy(proxy.config["backends"])
    for name in (BACKEND, ALIAS):
        legacy[name]["residency"].pop("transition", None)
    monkeypatch.setitem(proxy.config, "backends", legacy)
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=True, seen_in_flight=[]))

    async def scenario() -> None:
        response = await _stream_request()
        assert isinstance(response, StreamingResponse)
        assert proxy._swap_drain.in_flight(DOMAIN) == 0
        (stream,) = gate.engine.streams
        stream.finish.set()
        async for _ in response.body_iterator:
            pass

    _run(scenario())


def test_a_buffered_request_on_the_alias_holds_the_domain_lease_until_it_returns(gate, monkeypatch) -> None:
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=False, seen_in_flight=sends))

    async def scenario() -> Any:
        task = asyncio.create_task(_stream_request(ALIAS, stream=False))
        await _until(lambda: gate.engine.streams)
        assert proxy._swap_drain.in_flight(BACKEND) == 1
        (stream,) = gate.engine.streams
        stream.finish.set()
        return await task

    assert _run(scenario()).status_code == 200
    assert sends == [1]
    assert proxy._swap_drain.in_flight(DOMAIN) == 0


def test_an_unleased_forward_to_an_enabled_domain_is_refused_unsent(gate, monkeypatch) -> None:
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=False, seen_in_flight=sends))
    activation = proxy._litellm_local_activation_target(BACKEND, "fake-p-a")
    response = _run(proxy._forward_internal_litellm_local_upstream(
        _inner_request({}), backend=BACKEND, upstream_base="http://127.0.0.1:59999",
        body={"model": "fake-p-a", "messages": []}, activation=activation, target_model="fake-p-a", lease=None))
    # A code bug (a forward that took no lease) is a lasting refusal, not "the engine is changing state".
    assert _error(response) == (503, "transition_unsupported", "30")
    assert sends == []
    _no_hold_left()


# -- oracle 3: every stream ending releases the lease --------------------------------------------------


def test_stream_endings_release_the_lease(gate, monkeypatch) -> None:
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=True, seen_in_flight=[]))

    async def disconnect() -> None:
        response = await _stream_request()
        reader = asyncio.create_task(_drain(response))
        await asyncio.sleep(0.02)
        assert proxy._swap_drain.in_flight(DOMAIN) == 1
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        assert proxy._swap_drain.in_flight(DOMAIN) == 0

    async def never_started() -> None:
        response = await _stream_request()
        assert proxy._swap_drain.in_flight(DOMAIN) == 1
        del response
        gc.collect()
        assert proxy._swap_drain.in_flight(DOMAIN) == 0

    async def closed_twice() -> None:
        response = await _stream_request()
        body = response.body_iterator
        await body.__anext__()
        await body.aclose()
        await body.aclose()
        assert proxy._swap_drain.in_flight(DOMAIN) == 0

    for scenario in (disconnect, never_started, closed_twice):
        _run(scenario())


def test_an_upstream_failure_releases_the_lease(gate, monkeypatch) -> None:
    async def refuse(_execution_request, *, cancellation_token=None):
        assert proxy._swap_drain.in_flight(DOMAIN) == 1
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(proxy, "send_llm_execution_request", refuse)
    assert _run(_stream_request()).status_code == 503
    assert proxy._swap_drain.in_flight(DOMAIN) == 0


async def _drain(response) -> None:
    async for _ in response.body_iterator:
        pass


# -- oracle 4: a queued request re-checks under the lock -----------------------------------------------


@pytest.mark.parametrize("change", ["gate_closed", "override_on_alias", "transition_turned_off"])
def test_a_queued_request_rechecks_once_it_holds_the_lock(gate, monkeypatch, change: str) -> None:
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=False, seen_in_flight=sends))

    async def scenario() -> Any:
        lock = proxy.swap_lock_for(DOMAIN)
        await lock.acquire()
        task = asyncio.create_task(_stream_request(stream=False))
        await _until(lambda: lock._waiters)  # queued behind the transition, past every earlier check
        if change == "gate_closed":
            _set_gate(gate.store, DOMAIN, "closed")
        elif change == "override_on_alias":
            gate.overrides[ALIAS] = {"state": "maintenance", "retry_after_seconds": 12}
        else:
            backends = copy.deepcopy(proxy.config["backends"])
            for name in (BACKEND, ALIAS):
                backends[name]["residency"].pop("transition")
            monkeypatch.setitem(proxy.config, "backends", backends)
        lock.release()
        if change == "transition_turned_off":
            await _until(lambda: gate.engine.streams)
            next(iter(gate.engine.streams)).finish.set()
        return await task

    response = _run(scenario())
    if change == "transition_turned_off":
        assert response.status_code == 200, "the current config decides: a legacy domain is not gated"
        return
    assert _error(response) == (503, "engine_transitioning", "30" if change == "gate_closed" else "12")
    assert sends == [], "nothing reaches the engine"
    _no_hold_left()
    assert gate.engine.restarts() == []


@pytest.mark.parametrize("state", ["failed_closed", "reconciling"])
def test_a_gate_that_is_not_open_refuses_before_any_send(gate, monkeypatch, state: str) -> None:
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=False, seen_in_flight=sends))
    _set_gate(gate.store, DOMAIN, state)
    assert _error(_run(_stream_request(ALIAS, stream=False)))[:2] == (503, "engine_transitioning")
    assert sends == []
    _no_hold_left()


# -- oracle 10: bounded admission, cancellation, and independent domains -------------------------------


def test_admission_is_bounded_and_another_domain_still_progresses(gate, monkeypatch) -> None:
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=False, seen_in_flight=sends))

    async def scenario() -> tuple[Any, float, Any]:
        lock = proxy.swap_lock_for(DOMAIN)
        await lock.acquire()
        other = asyncio.create_task(_stream_request(OTHER, stream=False))
        await _until(lambda: gate.engine.streams)
        next(iter(gate.engine.streams)).finish.set()
        other_response = await asyncio.wait_for(other, 2)
        started = time.monotonic()
        refused = await _stream_request(stream=False)
        elapsed = time.monotonic() - started
        assert not lock._waiters, "the timed-out request left the queue"
        lock.release()
        return refused, elapsed, other_response

    refused, elapsed, other_response = _run(scenario())
    assert other_response.status_code == 200, "an independent domain is not blocked"
    assert _error(refused) == (503, "engine_transitioning", "1")
    assert 0.18 <= elapsed < 1.5, elapsed
    assert sends == [0], "only the other domain's request was sent; it leased its own domain, not this one"
    assert proxy._swap_drain.in_flight(DOMAIN) == 0


def test_a_request_cancelled_while_queued_mints_no_lease(gate, monkeypatch) -> None:
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=False, seen_in_flight=[]))

    async def scenario() -> None:
        lock = proxy.swap_lock_for(DOMAIN)
        await lock.acquire()
        task = asyncio.create_task(_stream_request(stream=False))
        await _until(lambda: lock._waiters)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        lock.release()
        assert not lock.locked()
        assert proxy._swap_drain.in_flight(DOMAIN) == 0

    _run(scenario())


# -- the current-loaded hop ----------------------------------------------------------------------------


def _current(body: dict):
    return proxy._forward_current_loaded_openai_request(
        _inner_request(body), request_path="/v1/chat/completions", auth_context=proxy.NO_AUTH_CONTEXT,
        body_json=body, enforce_gateway_policy=False)


def test_current_loaded_leases_an_enabled_domain_with_the_drain_off(gate, monkeypatch) -> None:
    body = _current_loaded_route(monkeypatch, gate.engine, [])

    async def scenario() -> None:
        response = await _current(body)
        assert isinstance(response, StreamingResponse), getattr(response, "body", response)
        assert proxy._swap_drain.in_flight(ALIAS) == 1
        next(iter(gate.engine.streams)).finish.set()
        await _drain(response)
        assert proxy._swap_drain.in_flight(DOMAIN) == 0

    _run(scenario())


def test_current_loaded_rechecks_the_gate_under_the_lock(gate, monkeypatch) -> None:
    forwarded: list[dict] = []
    body = _current_loaded_route(monkeypatch, gate.engine, forwarded)
    _set_gate(gate.store, DOMAIN, "closed")
    response = _run(_current(body))
    assert _error(response) == (503, "engine_transitioning", "30")
    assert forwarded == []
    _no_hold_left()


def test_current_loaded_rechecks_a_gate_that_closes_while_it_queues(gate, monkeypatch) -> None:
    forwarded: list[dict] = []
    body = _current_loaded_route(monkeypatch, gate.engine, forwarded)

    async def scenario() -> Any:
        lock = proxy.swap_lock_for(DOMAIN)
        await lock.acquire()
        task = asyncio.create_task(_current(body))
        await _until(lambda: lock._waiters)
        _set_gate(gate.store, DOMAIN, "closed")
        lock.release()
        return await task

    assert _error(_run(scenario())) == (503, "engine_transitioning", "30")
    assert forwarded == []
    _no_hold_left()


# -- fail closed: an unreadable override store, and the gate table's retry -----------------------------


def test_an_unreadable_override_store_refuses_and_is_read_off_the_loop(gate, monkeypatch) -> None:
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=False, seen_in_flight=sends))
    gate.overrides["__unreadable__"] = True
    assert _error(_run(_stream_request(stream=False))) == (503, "engine_transitioning", "5")
    assert sends == [] and gate.reads, "the read ran and its error refused"
    _no_hold_left()


def test_a_slow_override_read_is_bounded_and_refuses_with_a_retry_delay(gate, monkeypatch) -> None:
    """The domain lock is held across the read; a read that does not finish must not hold every request."""
    started, release = threading.Event(), threading.Event()
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=False, seen_in_flight=sends))
    monkeypatch.setattr(proxy, "_ADMISSION_READ_TIMEOUT_S", 0.2)

    def stuck() -> dict[str, Any]:
        started.set()
        release.wait(3)
        return {}

    monkeypatch.setattr(proxy, "_strict_service_availability_overrides", stuck)
    async def scenario() -> tuple[Any, float]:
        began = time.monotonic()
        response = await _stream_request(stream=False)
        return response, time.monotonic() - began

    try:
        response, elapsed = _run(scenario())
        assert _error(response) == (503, "engine_transitioning", "5")
        assert started.is_set() and elapsed < 2, "the wait ended at the bound, not when the read did"
        _no_hold_left()
    finally:
        release.set()
    assert sends == []


def test_an_unreadable_gate_table_retries_and_reopens(tmp_path) -> None:
    now = [0.0]
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the directory should be")
    store = GateStore(blocker / "transition.db", retry_s=5, clock=lambda: now[0])
    store.load()
    assert store.state(DOMAIN) == "reconciling"
    blocker.unlink()
    now[0] = 4.9
    store.refresh_if_due()
    assert store.state(DOMAIN) == "reconciling", "no retry before the interval"
    now[0] = 5.0
    assert store.state(DOMAIN) == "reconciling", "state() reads memory only: it never retries on its own"
    store.refresh_if_due()
    assert store.state(DOMAIN) == "open", "the retry found a readable table"


def test_a_closed_gate_is_not_reread_while_the_table_is_readable(tmp_path) -> None:
    store = GateStore(tmp_path / "transition.db", clock=lambda: 0.0)
    store.load()
    calls: list[int] = []
    real_load = store.load
    store.load = lambda: (calls.append(1), real_load())  # type: ignore[method-assign]
    store.refresh_if_due()
    store.refresh_if_due()
    assert calls == [], "a readable table is never retried"


def test_admission_retries_an_unreadable_gate_table_off_the_loop(gate, monkeypatch, tmp_path) -> None:
    """The retry is blocking I/O: admission runs it in a worker thread, and an admit that finds the table readable proceeds."""
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=False, seen_in_flight=sends))
    now = [0.0]
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the directory should be")
    store = GateStore(blocker / "transition.db", retry_s=5, clock=lambda: now[0])
    store.load()
    monkeypatch.setattr(proxy, "TRANSITION_GATES", store)
    threads: list[str] = []
    real_refresh = store.refresh_if_due

    def spying() -> None:
        threads.append(threading.current_thread().name)
        real_refresh()

    monkeypatch.setattr(store, "refresh_if_due", spying)
    assert _error(_run(_stream_request(stream=False)))[:2] == (503, "engine_transitioning"), "unreadable: refused"
    blocker.unlink()
    now[0] = 6.0

    async def scenario() -> Any:
        task = asyncio.create_task(_stream_request(stream=False))
        await _until(lambda: gate.engine.streams)
        next(iter(gate.engine.streams)).finish.set()
        return await task

    assert _run(scenario()).status_code == 200, "the table is readable again: admitted"
    assert threads and all(name != threading.main_thread().name for name in threads), threads
    assert sends == [1]
    _no_hold_left()


# -- the admission hold: a domain that moved while queued, a cancel mid-read, the override read -------


def test_a_domain_that_moved_while_the_request_queued_is_refused_with_a_short_retry(gate, monkeypatch) -> None:
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=False, seen_in_flight=sends))

    async def scenario() -> Any:
        lock = proxy.swap_lock_for(DOMAIN)
        await lock.acquire()
        task = asyncio.create_task(_stream_request(stream=False))
        await _until(lambda: lock._waiters)
        backends = copy.deepcopy(proxy.config["backends"])
        backends[BACKEND]["residency"]["domain"] = "moved-domain"
        monkeypatch.setitem(proxy.config, "backends", backends)
        lock.release()
        return await task

    response = _run(scenario())
    assert _error(response) == (503, "engine_transitioning", "1"), "the lock it held was not its new domain's"
    assert sends == []
    _no_hold_left()


def test_a_request_cancelled_during_the_override_read_frees_the_lock_and_mints_no_lease(gate, monkeypatch) -> None:
    started, release = threading.Event(), threading.Event()
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=False, seen_in_flight=sends))

    def slow() -> dict[str, Any]:
        started.set()
        release.wait(5)
        return {}

    monkeypatch.setattr(proxy, "_strict_service_availability_overrides", slow)

    async def scenario() -> None:
        task = asyncio.create_task(_stream_request(stream=False))
        await _until(started.is_set)
        assert proxy.swap_lock_for(DOMAIN).locked(), "the read runs inside the hold"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        _no_hold_left()

    try:
        _run(scenario())
    finally:
        release.set()
    assert sends == []


def test_the_override_read_asks_for_the_availability_setting_alone(tmp_path, monkeypatch) -> None:
    from gateway import admin_store
    from gateway.admin_runtime import AdminSQLiteStore

    path = tmp_path / "admin.db"
    admin_store.init_db(path)
    store = AdminSQLiteStore(path)
    assert store.service_availability_overrides() == {}
    owner = SimpleNamespace(subject="owner", actor_kind="user", audit_actor_id="owner", actor_label="owner")
    store.update_service_availability_overrides(data={"x": {"state": "maintenance"}}, actor=owner)

    def whole_document(*_args, **_kwargs):
        raise AssertionError("the whole operator_overrides() document must not be read")

    monkeypatch.setattr(store, "operator_overrides", whole_document)
    monkeypatch.setattr(store, "get_extension_package_index", whole_document)
    assert store.service_availability_overrides() == {"x": {"state": "maintenance"}}
    monkeypatch.setattr(proxy, "ADMIN_STORE", store)
    assert proxy._strict_service_availability_overrides() == {"x": {"state": "maintenance"}}

    def broken(*_args, **_kwargs):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(admin_store, "read_config_section", broken)
    with pytest.raises(sqlite3.OperationalError):
        store.service_availability_overrides()


def test_a_gate_refusal_nothing_caught_never_reaches_the_default_detail_body(gate, monkeypatch) -> None:
    """FastAPI would answer an uncaught HTTPException with {"detail": ...}: the app handler answers in the gateway's shape."""
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=False, seen_in_flight=sends))
    handler = proxy.app.exception_handlers.get(proxy.TransitionUnavailable)
    assert handler is not None, "the gateway app registers a handler for the gate's refusal"
    probe = FastAPI()
    probe.add_exception_handler(proxy.TransitionUnavailable, handler)
    router = APIRouter()

    @router.get("/probe")
    async def probe_route():
        raise proxy.TransitionUnavailable(DOMAIN, reason="gate_closed", retry_after_s=7)

    probe.include_router(router)
    response = TestClient(probe).get("/probe")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "7"
    assert response.json()["error"]["code"] == "engine_transitioning" and "detail" not in response.json()
    unsupported = proxy._transition_error_code(proxy.TransitionUnavailable(DOMAIN, reason="path_not_leased"))
    assert unsupported == "transition_unsupported"


def test_the_outer_activation_answers_a_gate_refusal_in_the_gateway_shape(gate, monkeypatch) -> None:
    """``ensure_target`` raises for a domain whose activation T1.1 cannot gate; the outer hop answers, it does not leak the exception."""
    backends = copy.deepcopy(proxy.config["backends"])
    backends[ALIAS]["residency"]["transition"] = {"enabled": True}
    monkeypatch.setitem(proxy.config, "backends", backends)
    response = _run(proxy._ensure_litellm_local_backend_target(
        proxy.NO_AUTH_CONTEXT, requested_model=ALIAS, backend=ALIAS, target_model=MODEL, target_profile="p-a"))
    assert response is not None and _error(response)[:2] == (503, "transition_unsupported")
    assert "Retry-After" in response.headers
    assert json.loads(response.body).get("detail") is None
    _no_hold_left()


# -- step T1.1 scope: gateway-run activation and unleased hops refuse ----------------------------------


def test_a_domain_that_is_not_pass_through_refuses_before_any_effect(gate, monkeypatch) -> None:
    backends = copy.deepcopy(proxy.config["backends"])
    backends[ALIAS]["residency"]["transition"] = {"enabled": True}
    monkeypatch.setitem(proxy.config, "backends", backends)
    sends: list[int] = []
    monkeypatch.setattr(proxy, "send_llm_execution_request", _fake_send(gate.engine, sse=False, seen_in_flight=sends))
    with pytest.raises(proxy.TransitionUnavailable) as refused:
        _run(proxy.ensure_target(ALIAS, MODEL, None, profile="p-a"))
    assert refused.value.reason == "activation_not_supported"
    assert _error(_run(_stream_request(ALIAS, stream=False)))[:2] == (503, "transition_unsupported")
    assert sends == [] and gate.engine.restarts() == [], "no start, swap or send"
    _no_hold_left()


def test_a_hop_without_a_lease_refuses_an_enabled_domain(gate) -> None:
    """Speech, rerank, realtime and decisions hold no lease in step T1.1: both choke points refuse."""
    refusal = _run(proxy._ensure_speech_service_ready(ALIAS, label="TTS engine"))
    assert _error(refusal)[:2] == (503, "transition_unsupported")
    decision = proxy._decision_dispatch_admission(proxy.NO_AUTH_CONTEXT, ALIAS)
    assert _error(decision)[:2] == (503, "transition_unsupported")
    assert gate.engine.restarts() == []
    assert _run(proxy._ensure_speech_service_ready("not-in-any-domain", label="TTS engine")) is None


def test_the_voice_probe_and_voice_design_keep_the_gate_refusal_code(gate, monkeypatch, tmp_path) -> None:
    """These hops hold no lease: they refuse with transition_unsupported and say so, not 'could not be started'."""
    from gateway import custom_voices

    monkeypatch.setattr(proxy, "_speech_engine_endpoint", lambda engine_id: (ALIAS, "http://127.0.0.1:59999"))
    monkeypatch.setattr(proxy, "_voice_settings", lambda: {"quality_engine": "x", "design_engine": "x"})
    probed = _run(proxy._probe_voice_quality(tmp_path / "clip.wav"))
    assert probed == {"error": "transition_unsupported"}
    with pytest.raises(custom_voices.VoiceError) as refused:
        _run(proxy._design_voice("calm", "Hello there.", 1))
    assert (refused.value.status, refused.value.code) == (503, "transition_unsupported")
    assert refused.value.retry_after and refused.value.retry_after >= 1
    _no_hold_left()


def test_background_removal_leases_an_enabled_domain(gate, monkeypatch) -> None:
    seen: list[int] = []

    async def post(url, headers=None, json=None):
        seen.append(proxy._swap_drain.in_flight(DOMAIN))
        return httpx.Response(200, json={"choices": [{"message": {"content":
            '{"remove_background": true, "confidence": 0.9, "reason": "logo"}'}}]})

    async def active() -> str:
        return BACKEND

    async def model_for(_backend: str) -> str:
        return "fake-p-a"

    monkeypatch.setattr(proxy, "_image_background_removal_enabled", lambda: True)
    monkeypatch.setattr(proxy, "get_active_backend", active)
    monkeypatch.setattr(proxy, "_is_local_swap_backend", lambda name: name == BACKEND)
    monkeypatch.setattr(proxy, "current_llm_model_for_backend", model_for)
    install_async_client_stub(monkeypatch, post)
    decision = _run(proxy.decide_image_background_removal("a logo for a bakery", auth_context=proxy.NO_AUTH_CONTEXT))
    assert decision.source == "llm" and seen == [1], "the call was sent under one domain lease"
    assert proxy._swap_drain.in_flight(DOMAIN) == 0

    _set_gate(gate.store, DOMAIN, "closed")
    seen.clear()
    decision = _run(proxy.decide_image_background_removal("a logo for a bakery", auth_context=proxy.NO_AUTH_CONTEXT))
    assert decision.source == "llm_unavailable" and seen == [], "a closed gate sends nothing"


# -- a stopped pass-through engine is never started by a request ---------------------------------------


@pytest.fixture
def stopped(gate, monkeypatch, tmp_path):
    """The pass-through record of BACKEND replaced by one with a gateway start command and nothing listening.

    The fake engine replaces ``httpx.AsyncClient`` for these tests, so the send is the one the real hop
    makes (``send_llm_execution_request``) answering what a closed port answers: a connection error.
    """
    engine = StoppedEngine(monkeypatch, tmp_path, service_id=BACKEND)
    engine.install(monkeypatch, engine.record())
    monkeypatch.setitem(proxy.DERIVED.LLM_BACKEND_HOSTS, BACKEND, engine.url)
    pin_inner_hop_hosts(monkeypatch, {BACKEND: engine.url})
    engine.sends = []

    async def closed(execution_request, **_kwargs):
        engine.sends.append(execution_request.url)
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(proxy, "send_llm_execution_request", closed)
    return engine


def test_the_start_command_would_run_for_a_domain_that_is_not_pass_through(monkeypatch, tmp_path) -> None:
    """Positive control: the same record and driver do start the engine when nothing says another owner runs it."""
    engine = StoppedEngine(monkeypatch, tmp_path)
    record = engine.record(pass_through=False)
    from gateway import lifecycle_drivers

    _run(lifecycle_drivers._command_driver.ensure_ready(record, 0.2))
    assert engine.marker.exists(), "the start command ran"


def test_the_inner_hop_never_starts_a_stopped_pass_through_engine(stopped) -> None:
    """Guards a CURRENT path: the pass-through skip in ``_ensure_target`` is the only thing that keeps the hop from
    starting the engine, and this test goes red when the skip is removed."""
    response = _run(_stream_request(BACKEND, stream=False))
    assert response.status_code in (502, 503), getattr(response, "body", response)
    assert stopped.sends, "the request went as far as the send"
    stopped.assert_untouched()
    _no_hold_left()


def test_current_loaded_never_starts_a_stopped_pass_through_engine(stopped, monkeypatch) -> None:
    """Guards against a FUTURE start call: the current-loaded route starts nothing today (removing the skip in
    ``_ensure_target`` leaves this test green). It proves the request reached the send."""
    async def active() -> str:
        return BACKEND

    async def loaded(_backend: str) -> str:
        return "fake-p-a"

    monkeypatch.setattr(proxy, "get_active_backend", active)
    monkeypatch.setattr(proxy, "current_llm_model_for_backend", loaded)
    monkeypatch.setattr(proxy, "prepare_downstream_headers", lambda *_a, **_k: {})
    body = {"model": "current", "messages": [{"role": "user", "content": "hi"}]}
    response = _run(proxy._forward_current_loaded_openai_request(
        _inner_request(body), request_path="/v1/chat/completions", auth_context=proxy.NO_AUTH_CONTEXT, body_json=body))
    assert response.status_code in (502, 503), getattr(response, "body", response)
    assert stopped.sends, "the request went as far as the send"
    stopped.assert_untouched()
    _no_hold_left()


def test_the_real_send_of_the_inner_hop_passes_the_send_time_check_only_under_its_lease(gate, monkeypatch) -> None:
    """No fake send: the hop's own client sends to a routed address. The lease is visible to the send, and the same
    destination refuses a send from a task that holds none."""
    restore_async_client(monkeypatch, _REAL_ASYNC_CLIENT)
    seen: list[str] = []

    def engine(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "choices": [], "data": []})

    HostRouter(monkeypatch).add_transport("127.0.0.1:59999", HandlerTransport(engine))
    monkeypatch.setattr(engine_egress, "_live", engine_egress._live)
    engine_egress.publish(proxy.config["backends"])
    assert engine_egress.mapped_destinations()["127.0.0.1:59999"], "the enabled domains' address is mapped"

    response = _run(_stream_request(BACKEND, stream=False))
    assert response.status_code == 200, getattr(response, "body", response)
    assert "/v1/chat/completions" in seen

    async def unleased() -> None:
        async with engine_egress.async_client() as client:
            await client.post("http://127.0.0.1:59999/v1/chat/completions", json={})

    before = list(seen)
    with pytest.raises(TransitionUnavailable):
        _run(unleased())
    assert seen == before, "the unleased send never reached the engine"
    _no_hold_left()
