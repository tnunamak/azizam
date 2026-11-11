"""A graph run pins the service its family selected, keeps it on resume, refuses when it is gone, and waits its timeout.

Two records speak the node-graph protocol. The first one in the config has the worse priority: a creation that took the
first record that speaks the protocol would pin it, where the family's selection is the other one.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import proxy
from gateway.engine_protocol.errors import ProtocolError
from tests.support.graph_service import graph_record

MEDIA_BY_FAMILY = {"images": "image/png", "videos": "video/mp4", "audio.generate": "audio/mpeg"}
FAMILIES = tuple(MEDIA_BY_FAMILY)
BINDING_KEY = "comfyui"
FIRST_TIMEOUT, SECOND_TIMEOUT = 11, 37


def _record(port: int, priority: int, timeout: int, **fields):
    return graph_record(host="203.0.113.7", port=port, priority=priority, timeout=timeout, **fields)


def _publish(monkeypatch, backends):
    monkeypatch.setitem(proxy.config, "backends", backends)


def _entry(family: str):
    return SimpleNamespace(contract={"outputs": {"out": {"media_type": MEDIA_BY_FAMILY[family]}}})


def _binding():
    return SimpleNamespace(adapter_id=BINDING_KEY)


def _created_pin(family: str) -> str:
    return proxy._graph_runner_host().identity(_entry(family), BINDING_KEY, _binding()).service_id


def _two(first_priority=20, second_priority=0):
    return {"first": _record(7001, first_priority, FIRST_TIMEOUT), "second": _record(7002, second_priority, SECOND_TIMEOUT)}


@pytest.mark.parametrize("family", FAMILIES)
def test_new_graph_run_pins_selected_family_service_not_first_protocol_record(monkeypatch, family):
    backends = _two()
    # A higher-priority record of another family is a control: it must not be chosen for these families.
    backends["wrong-family"] = _record(7003, -5, 5, capabilities=["chat"])
    _publish(monkeypatch, backends)
    assert proxy._graph_services_of(backends)[family].service_id == "second"
    assert _created_pin(family) == "second"
    # The same answer whatever the order the records are in.
    _publish(monkeypatch, dict(reversed(list(backends.items()))))
    assert _created_pin(family) == "second"
    # And on a tie, the smaller id.
    _publish(monkeypatch, {"b-tie": _record(7001, 0, 1), "a-tie": _record(7002, 0, 1)})
    assert _created_pin(family) == "a-tie"


def test_a_pin_follows_the_family_of_the_workflow(monkeypatch):
    backends = {
        "pictures": _record(7001, 0, 5, capabilities=["images"]),
        "movies": _record(7002, 0, 5, capabilities=["videos"]),
        "both": _record(7003, 9, 5, capabilities=["images", "videos"]),
    }
    _publish(monkeypatch, backends)
    assert _created_pin("images") == "pictures"
    assert _created_pin("videos") == "movies"


@pytest.mark.parametrize("family", FAMILIES)
def test_resumed_graph_run_keeps_pinned_service_after_priority_flip(monkeypatch, family):
    _publish(monkeypatch, _two(first_priority=0, second_priority=20))
    assert _created_pin(family) == "first"
    # A registry change makes the other service win; the recorded id still names the first.
    _publish(monkeypatch, _two(first_priority=20, second_priority=0))
    assert proxy._graph_services_of(proxy.config["backends"])[family].service_id == "second"
    found = proxy._workflow_engine(BINDING_KEY, "first")
    assert found is not None and found[0] == "first"
    assert found[1]["port"] == 7001


def test_resumed_graph_run_refuses_removed_pinned_service_without_fallback(monkeypatch):
    from gateway.node_graph_runner import NodeGraphRunner
    _publish(monkeypatch, _two())
    contacted: list[str] = []
    monkeypatch.setattr(proxy, "_graph_service_connection", lambda service_id, record: contacted.append(service_id))
    assert proxy._workflow_engine(BINDING_KEY, "gone") is None
    runner = NodeGraphRunner(BINDING_KEY, "adapter", proxy._graph_runner_host())
    ctx = SimpleNamespace(pin=SimpleNamespace(envelope=SimpleNamespace(binding_key=BINDING_KEY, service_id="gone")))
    with pytest.raises(ProtocolError) as refused:
        asyncio.run(runner.run(ctx))
    assert refused.value.code == "adapter_not_registered"
    assert "No reviewed adapter is registered for this job." in str(refused.value)
    assert contacted == []


def test_graph_run_wait_uses_own_service_timeout(monkeypatch):
    _publish(monkeypatch, _two())
    monkeypatch.setattr(proxy, "_require_egress_lease", lambda service_id: None)
    host = proxy._graph_runner_host()
    waits = {}
    for service_id in ("first", "second"):
        _, record, _, _ = proxy._workflow_engine(BINDING_KEY, service_id)
        waits[service_id] = host.connection(service_id, record).timeouts.total_s
    assert waits == {"first": float(FIRST_TIMEOUT), "second": float(SECOND_TIMEOUT)}
    # Flip the winner: the first service's run still waits as long as the first service says.
    _publish(monkeypatch, _two(first_priority=0, second_priority=20))
    _, record, _, _ = proxy._workflow_engine(BINDING_KEY, "first")
    assert host.connection("first", record).timeouts.total_s == float(FIRST_TIMEOUT)
