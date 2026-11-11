"""T1.1: the per-engine native lane is a mediated dispatch too. A transition-enabled domain leases it
after the request body is read, re-checks the gate under the domain lock, and holds the lease while
every reply byte passes and releases it after the final one."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import proxy
from gateway.transition_gate import GateStore
from tests.support.access import plan_for
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.test_native_passthrough import (  # noqa: F401 - native_gateway is a fixture
    BODY, NATIVE_PATH, _engine, _new_receipt, _post, _receipts_for, _scenario, native_gateway,
)
from tests.test_runtime_route_policy import authenticated_profile

pytestmark = pytest.mark.usefixtures("native_gateway")

GATED = {"domain": "garage-domain", "transition": {"enabled": True, "activation": "pass_through"}}


@pytest.fixture
def leases(monkeypatch, tmp_path):
    store = GateStore(tmp_path / "transition.db")
    store.load()
    monkeypatch.setattr(proxy, "TRANSITION_GATES", store)
    monkeypatch.setattr(proxy, "_service_swap_locks", {})
    monkeypatch.setattr(proxy, "_strict_service_availability_overrides", lambda: {})
    drain = proxy._new_swap_drain()
    minted: list[str] = []
    while_reading: list[int] = []
    per_chunk: list[int] = []
    real_lease = drain.lease
    real_read = proxy._read_capped_body

    def counting(backend, **kwargs):
        lease = real_lease(backend, **kwargs)
        minted.append(lease.key)
        real_hand_off = lease.hand_off

        def recording(stream):
            owned = real_hand_off(stream)  # eager, as the real call is: it marks the hand-off

            async def chunks():
                async for chunk in owned:
                    per_chunk.append(drain.in_flight(lease.key))
                    yield chunk
            return chunks()

        lease.hand_off = recording
        return lease

    async def reading(request, limit):
        while_reading.append(drain.in_flight("garage-domain"))
        return await real_read(request, limit)

    monkeypatch.setattr(drain, "lease", counting)
    monkeypatch.setattr(proxy, "_swap_drain", drain)
    monkeypatch.setattr(proxy, "_read_capped_body", reading)
    return SimpleNamespace(store=store, drain=drain, minted=minted, while_reading=while_reading, per_chunk=per_chunk)


# A refused call must never reach the engine, so its fake allows zero calls (the call list proves zero).
NEVER_CALLED = _scenario({"id": "chat", "match": {"method": "POST", "path": "/v1/chat/completions"},
                          "required": False, "response": {"status": 200, "json": {}}})


def _call(fake_upstreams, scenario, record: dict, body: dict):
    if isinstance(scenario, str):
        scenario = load_scenario(scenario, model="demo-chat", **({"content": "native"} if scenario.endswith("text") else {}))
    engine = fake_upstreams.loopback(scenario)
    profile = plan_for("native-gated", caps=(), engines=["garage"], operations=["v1/chat/completions"])
    with authenticated_profile(profile), patch.dict(
        proxy.config["backends"], {"garage": _engine(engine.base_url, native_passthrough=True, **record)},
    ):
        return engine, _post(NATIVE_PATH, body)


@pytest.mark.parametrize("scenario,body", [("openai-compatible/chat-text", BODY),
                                           ("openai-compatible/chat-stream", {**BODY, "stream": True})])
def test_an_enabled_native_call_holds_one_domain_lease_to_the_end(fake_upstreams, leases, scenario, body):
    engine, response = _call(fake_upstreams, scenario, {"residency": GATED}, body)
    assert response.status_code == 200, response.text
    assert leases.minted == ["garage-domain"]
    assert leases.while_reading == [0], "no lease while the request body is read"
    assert leases.per_chunk and set(leases.per_chunk) == {1}, "the lease is held while each reply chunk passes"
    assert leases.drain.in_flight("garage-domain") == 0, "released after the final byte"
    assert len(engine.calls("chat")) == 1


def test_a_closed_gate_refuses_the_native_call_before_the_engine(fake_upstreams, leases, monkeypatch):
    store, drain, minted = leases.store, leases.drain, leases.minted
    with sqlite3.connect(store.path) as connection:
        connection.execute("INSERT INTO transition_gate (domain, state, op_id, updated, generation) VALUES ('garage-domain', 'closed', NULL, 0, 1)")
    store.load()
    written: list[str] = []
    real_write = proxy.run_receipt_write

    async def spy(function, *args, **kwargs):
        written.append(getattr(function, "__name__", repr(function)))
        return await real_write(function, *args, **kwargs)

    monkeypatch.setattr(proxy, "run_receipt_write", spy)
    before = _receipts_for(NATIVE_PATH)
    engine, response = _call(fake_upstreams, NEVER_CALLED, {"residency": GATED}, BODY)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "engine_transitioning"
    assert response.headers["retry-after"] == "30"
    assert engine.calls("chat") == []
    assert minted == [] and drain.in_flight("garage-domain") == 0
    assert "garage-domain" in proxy._service_swap_locks
    assert not any(lock.locked() for lock in proxy._service_swap_locks.values()), "the domain lock is free after the refusal"
    assert "_refuse_native_transition" in written, "the receipt is written off the event loop, as the other early refusals are"
    receipt = _new_receipt(before, NATIVE_PATH)
    assert receipt["metadata"]["refusal"] == "engine_transitioning"
    assert receipt["status"] == "denied"


def test_a_legacy_native_call_takes_no_lease(fake_upstreams, leases):
    _engine_fake, response = _call(fake_upstreams, "openai-compatible/chat-text", {}, BODY)
    assert response.status_code == 200, response.text
    assert leases.minted == [] and leases.per_chunk == []


def test_a_native_call_never_starts_a_stopped_pass_through_engine(fake_upstreams, leases, monkeypatch, tmp_path):
    """Guards against a FUTURE start call: no code on the native lane starts an engine today (removing the
    pass-through skip in ``_ensure_target`` leaves this test green). It proves the request reached the send."""
    from tests.support.stopped_engine import StoppedEngine, closed_port

    stopped = StoppedEngine(monkeypatch, tmp_path, service_id="garage")
    sends = stopped.watch_sends(monkeypatch)
    profile = plan_for("native-gated", caps=(), engines=["garage"], operations=["v1/chat/completions"])
    record = _engine(f"http://127.0.0.1:{stopped.port}", native_passthrough=True,
                     process=stopped.record()["process"], residency=GATED)
    with authenticated_profile(profile), patch.dict(proxy.config["backends"], {"garage": record}):
        response = _post(NATIVE_PATH, BODY)
    assert response.status_code in (502, 503), response.text
    assert sends, "the request went as far as the send"
    stopped.assert_untouched()
    assert leases.drain.in_flight("garage-domain") == 0
