"""T1.1 at the protocol hop: a transition-enabled domain is leased for every protocol dispatch, switch or not,
a pass-through domain is never sent a selection call, a gate that is not open refuses before any engine call,
and a domain that would need a gateway-run switch refuses (step T1.1 supports pass-through only)."""

from __future__ import annotations

import sqlite3

import httpx
import pytest

import proxy
from gateway.transition_gate import GateStore
from tests.test_engine_protocol_public import chat, engine, switching  # noqa: F401 - engine is a fixture

GATED = {"domain": "prism-domain",
         "transition": {"enabled": True, "activation": "pass_through", "admission_wait_s": 0.5}}


@pytest.fixture
def gated(engine, monkeypatch, tmp_path):
    store = GateStore(tmp_path / "transition.db")
    store.load()
    monkeypatch.setattr(proxy, "TRANSITION_GATES", store)
    monkeypatch.setattr(proxy, "_service_swap_locks", {})
    monkeypatch.setattr(proxy, "_strict_service_availability_overrides", lambda: {})
    monkeypatch.setitem(proxy.config, "backends", {engine.record["id"]: {"residency": GATED}})
    drain = proxy._new_swap_drain()
    minted: list[str] = []
    real_lease = drain.lease

    def counting(backend, **kwargs):
        lease = real_lease(backend, **kwargs)
        minted.append(lease.key)
        return lease

    monkeypatch.setattr(drain, "lease", counting)
    monkeypatch.setattr(proxy, "_swap_drain", drain)
    return store, drain, minted


def test_every_protocol_dispatch_to_an_enabled_domain_holds_one_lease(engine, gated):
    _store, drain, minted = gated
    response = chat(engine)
    assert response.status_code == 200, response.text
    assert minted == ["prism-domain"]
    assert drain.in_flight("prism-domain") == 0
    calls = [(request.method, request.url.path) for request in engine.fake.requests]
    assert calls == [("POST", "/prefix/api/v3/converse")], "pass-through: no selection or control call"


def _implicit_load_off(engine):
    for offering in engine.record["config"]["offerings"]:
        offering["selection"] = {"mode": "request_field", "implicit_load": False}


@pytest.mark.parametrize("effect", [switching, _implicit_load_off], ids=["switch", "no_implicit_load"])
def test_an_offering_that_needs_a_gateway_run_effect_is_refused_unsent_in_a_pass_through_domain(engine, gated, effect):
    """A pass-through member has no gateway-run switch or load: the dispatch answers transition_unsupported, sends nothing."""
    _store, drain, minted = gated
    effect(engine)
    response = chat(engine)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "transition_unsupported"
    assert "retry-after" in response.headers
    assert engine.fake.requests == [], "no selection call and no generation call"
    assert minted == [] and drain.in_flight("prism-domain") == 0
    assert not proxy.swap_lock_for(engine.record["id"]).locked()


def test_a_closed_gate_refuses_the_protocol_dispatch_before_any_engine_call(engine, gated):
    store, drain, minted = gated
    with sqlite3.connect(store.path) as connection:
        connection.execute("INSERT INTO transition_gate VALUES ('prism-domain', 'closed', NULL, 0)")
    store.load()
    response = chat(engine)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "engine_transitioning"
    assert response.headers["retry-after"] == "30"
    assert engine.fake.requests == []
    assert minted == [] and drain.in_flight("prism-domain") == 0
    assert not proxy.swap_lock_for(engine.record["id"]).locked(), "the domain lock is free after the refusal"


def test_a_legacy_protocol_dispatch_takes_no_lease(engine, gated, monkeypatch):
    _store, _drain, minted = gated
    monkeypatch.setitem(proxy.config, "backends", {})
    assert chat(engine).status_code == 200
    assert minted == []


def test_a_domain_that_would_need_a_gateway_run_switch_refuses_unsent(engine, gated, monkeypatch):
    _store, drain, minted = gated
    switching(engine)
    gateway_run = {"domain": "prism-domain", "transition": {"enabled": True}}
    monkeypatch.setitem(proxy.config, "backends", {engine.record["id"]: {"residency": gateway_run}})
    response = chat(engine)
    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "transition_unsupported"
    assert engine.fake.requests == []
    assert minted == [] and drain.in_flight("prism-domain") == 0
    assert not proxy.swap_lock_for(engine.record["id"]).locked(), "the domain lock is free after the refusal"


def test_the_protocol_hop_never_starts_a_stopped_pass_through_engine(engine, gated, monkeypatch, tmp_path):
    """A record with a gateway start command, injected past the registry check, and a closed engine port.

    This guards against a FUTURE start call: no code on the protocol path starts an engine today (removing the
    pass-through skip in ``_ensure_target`` leaves this test green). It would go red if someone added a start call
    to the protocol hop. It proves the request reached the send, so a 503 is the engine's closed port and not an
    early refusal."""
    from gateway.engine_protocol import executor, transport
    from tests.support.fake_upstream import HandlerTransport
    from tests.support.stopped_engine import StoppedEngine

    stopped = StoppedEngine(monkeypatch, tmp_path, service_id=engine.record["id"])
    stopped.install(monkeypatch, stopped.record() | {"residency": GATED})

    sends: list[str] = []

    def refuse(request):
        sends.append(str(request.url))
        raise httpx.ConnectError("connection refused", request=request)

    wire = transport.HttpxTransport.for_tests(HandlerTransport(refuse))
    monkeypatch.setattr(executor, "HttpxTransport", lambda: wire)
    _store, drain, _minted = gated
    response = chat(engine)
    assert response.status_code in (502, 503), response.text
    assert sends, "the request went as far as the send"
    stopped.assert_untouched()
    assert drain.in_flight("prism-domain") == 0
    assert not proxy.swap_lock_for(engine.record["id"]).locked()
