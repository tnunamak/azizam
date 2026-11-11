"""Fixture helpers for the graph engine's service, which the gateway selects per family by capability.

The gateway never names the service: each of the three families (``images``, ``videos``, ``audio.generate``) selects the
record that declares its capability, so a record may carry any id. ``GRAPH_SERVICE_ID`` is the id the fixtures give it:
the recorded goldens name it, so the fixtures keep it; tests/test_graph_service_identity.py renames it.
A test that needs the engine at an address, or switched on or off, for every family uses ``mirror`` (through
``derived_patch`` or ``set_derived``), which writes the value into each family's view the way the registry would.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Mapping

import pytest

import proxy

GRAPH_SERVICE_ID = "comfyui"
# The families one record serves; every fixture record declares all three.
GRAPH_CAPABILITIES = ["images", "generations", "videos", "audio.generate"]
# The derived names a test used to patch to move the engine, and the view field each one is.
GRAPH_MIRROR = {"COMFY_ENABLED": "enabled", "COMFY_HOST": "host", "COMFY_TIMEOUT": "timeout"}


def graph_record(**fields: Any) -> dict[str, Any]:
    """A service record for the graph engine, declaring every family's capability, with *fields* over it."""
    return {"enabled": True, "protocol": "node-graph", "capabilities": list(GRAPH_CAPABILITIES), **fields}


def mirror(views: Mapping[str, Any], name: str, value: Any) -> dict[str, Any]:
    """*views* (the snapshot's ``GRAPH_SERVICES``) with the derived value *name* set in every family's view.

    A family the base config selects no service for stays empty: a fixture record declares the capabilities it serves
    (``graph_record``), and a test of a missing family leaves one out."""
    field = GRAPH_MIRROR[name]
    return {capability: replace(view, **{field: value}) for capability, view in views.items()}


def set_derived(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    """``monkeypatch.setitem`` of a derived value; one of the engine's (``GRAPH_MIRROR``) reaches every family's view too."""
    derived = proxy.runtime().derived
    monkeypatch.setitem(derived, name, value)
    if name in GRAPH_MIRROR:
        monkeypatch.setitem(derived, "GRAPH_SERVICES", mirror(derived["GRAPH_SERVICES"], name, value))
    if name == "COMFY_TIMEOUT":
        # A run waits as long as the record it was pinned to says, so every graph record in the config carries the value.
        backends = {service_id: {**record, "timeout": value} if record.get("protocol") == "node-graph" or
                    set(GRAPH_CAPABILITIES) & set(record.get("capabilities") or ()) else record
                    for service_id, record in (proxy.config.get("backends") or {}).items()}
        monkeypatch.setitem(proxy.config, "backends", backends)
