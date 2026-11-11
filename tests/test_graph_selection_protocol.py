"""The graph engine's service is chosen only among records that speak the node-graph protocol.

A record that declares ``images``, ``videos`` or ``audio.generate`` but names another adapter or HTTP protocol never
receives node-graph work, whatever its priority. The inventory (``local_capabilities``) and the runner's
selection (``proxy._graph_services_of``) take the same candidates.
"""
from __future__ import annotations

import copy

import pytest

import proxy
from gateway.local_capabilities import _family_backend
from tests.support.graph_service import graph_record

FAMILIES = ("images", "videos", "audio.generate")
OTHER_PROTOCOLS = ("openai-compatible", "inline-tts", "no-such-protocol")


def _other(priority: int, protocol: str | None, **fields):
    record = {"enabled": True, "host": "203.0.113.9", "port": 9000, "priority": priority,
              "capabilities": ["images", "videos", "audio.generate"], **fields}
    if protocol is not None:
        record["protocol"] = protocol
    return record


def _graph(port: int, priority: int, **fields):
    return graph_record(host="203.0.113.7", port=port, priority=priority, **fields)


def _runner_ids(backends):
    views = proxy._derive_backend_constants({"backends": copy.deepcopy(backends)})["GRAPH_SERVICES"]
    return {family: views[family].service_id for family in FAMILIES}


@pytest.mark.parametrize("protocol", OTHER_PROTOCOLS)
@pytest.mark.parametrize("family", FAMILIES)
def test_a_record_naming_another_protocol_is_not_chosen_for_a_graph_family(protocol, family):
    backends = {"other": _other(1, protocol), "graph-a": _graph(7001, 100)}
    assert _family_backend({}, "section", backends, family) == "graph-a"
    assert _runner_ids(backends)[family] == "graph-a"


@pytest.mark.parametrize("protocol", OTHER_PROTOCOLS)
def test_a_record_naming_another_protocol_alone_leaves_every_family_unselected(protocol):
    backends = {"other": _other(1, protocol)}
    assert all(_family_backend({}, "section", backends, family) is None for family in FAMILIES)
    assert _runner_ids(backends) == {family: "" for family in FAMILIES}


@pytest.mark.parametrize("named", [True, False])
def test_a_second_graph_record_at_a_better_priority_is_chosen(named):
    second = _graph(7002, 1)
    if not named:
        del second["protocol"]
    backends = {"other": _other(5, "openai-compatible"), "graph-a": _graph(7001, 100), "graph-b": second}
    for family in FAMILIES:
        assert _family_backend({}, "section", backends, family) == "graph-b"
    assert set(_runner_ids(backends).values()) == {"graph-b"}


def test_a_record_with_no_protocol_is_inferred_from_the_capability_it_declares():
    backends = {"inferred": {"enabled": True, "host": "203.0.113.7", "port": 1, "capabilities": ["videos"]}}
    assert _family_backend({}, "section", backends, "videos") == "inferred"
    assert _runner_ids(backends)["videos"] == "inferred"
    assert _runner_ids(backends)["images"] == ""


def test_a_graph_record_does_not_serve_a_family_its_protocol_does_not_list():
    backends = {"graph": _graph(7001, 1, capabilities=["chat", "speech"])}
    assert _runner_ids(backends) == {family: "" for family in FAMILIES}


def test_the_protocol_may_be_named_in_the_connection_block():
    backends = {"other": _other(1, None, connection={"protocol": "openai-compatible"}),
                "graph": _graph(7001, 100)}
    assert _runner_ids(backends)["images"] == "graph"
    backends["named"] = _other(2, None, connection={"protocol": "node-graph"})
    assert _runner_ids(backends)["images"] == "named"


@pytest.mark.parametrize("family", FAMILIES)
def test_a_record_with_a_fractional_priority_is_skipped_not_fatal(family):
    # One stored record with a priority the selector refuses (5.7) must not abort the whole local inventory: it is left
    # out, and the valid graph record still serves the family.
    backends = {"bad": _graph(7002, 5.7), "graph-a": _graph(7001, 100)}
    assert _family_backend({}, "section", backends, family) == "graph-a"
