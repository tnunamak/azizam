"""Which reviewed adapter answers for a backend record (contract section 4).

A backend record selects its adapter with data, never with a branch on its name.
The record's ``protocol`` (or ``connection.protocol``) names a protocol
description. When that description says ``executor: adapter:<id>``, the factory
registered under ``<id>`` builds the adapter. A record that names no adapter
protocol is answered by the reviewed adapter whose description lists the
record's capability in ``serves``. That is how every record stored before this
field existed keeps working without a migration.

Core calls ``adapter_for_backend(record, capability)``; nothing here knows what
any engine is. The factories below are the reviewed in-tree adapters and the
only place their ids are written next to their code.
"""

from __future__ import annotations

from typing import Any, Mapping

from gateway.engine_protocol import description
from gateway.engine_protocol.adapter import AdapterFactory, AdapterRegistry, EngineAdapter
from gateway.engine_protocol.errors import ProtocolError

_FACTORIES: dict[str, AdapterFactory] = {}
_BUILT: dict[tuple[str, int], tuple[Any, EngineAdapter]] = {}


def register(adapter_id: str, factory: AdapterFactory) -> None:
    """Register (or replace) the reviewed factory for ``adapter:<adapter_id>``."""
    _FACTORIES[adapter_id] = factory
    for key in [key for key in _BUILT if key[0] == adapter_id]:
        del _BUILT[key]


def _inline_tts(_doc: Any) -> EngineAdapter:
    from gateway.engine_protocol import inline_tts
    return inline_tts.InlineTtsAdapter()


def _node_graph(doc: Any) -> EngineAdapter:
    from gateway.engine_protocol import node_graph
    return node_graph.NodeGraphAdapter(doc)


def _chunked_stt(doc: Any) -> EngineAdapter:
    from gateway.engine_protocol import chunked_stt
    return chunked_stt.ChunkedSttAdapter(doc)


# The reviewed in-tree adapters. "builtin" is provenance only: a package adapter registers the same way.
_BUILTIN_FACTORIES: dict[str, AdapterFactory] = {
    "inline-tts": _inline_tts,
    "node-graph": _node_graph,
    "chunked-stt": _chunked_stt,
}


def _named_protocol(record: Mapping[str, Any]) -> str | None:
    connection = record.get("connection")
    for source in (connection if isinstance(connection, Mapping) else {}, record):
        value = source.get("protocol")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def protocol_for_backend(record: Mapping[str, Any], capability: str) -> description.ProtocolDescription:
    """The adapter protocol description that serves ``capability`` for ``record``."""
    documents = description.load_registry()
    named = _named_protocol(record)
    chosen = documents.get(named) if named else None
    if chosen is not None and chosen.adapter_id is not None:
        return chosen
    for protocol_id in sorted(documents):
        candidate = documents[protocol_id]
        if candidate.adapter_id is not None and capability in candidate.serves:
            return candidate
    raise ProtocolError(f"No reviewed adapter is registered for '{capability}'.", code="adapter_not_registered")


def adapter_for_backend(record: Mapping[str, Any], capability: str) -> EngineAdapter:
    """The reviewed adapter a backend record selects for ``capability`` (cached per description)."""
    doc = protocol_for_backend(record, capability)
    adapter_id = doc.adapter_id or ""
    cached = _BUILT.get((adapter_id, id(doc)))
    if cached is not None:
        return cached[1]
    factories = {**_BUILTIN_FACTORIES, **_FACTORIES}
    registry = AdapterRegistry({doc.id: doc})
    for known, factory in factories.items():
        registry.register(known, factory)
    built = registry.adapter_for(doc.id)
    for key in [key for key in _BUILT if key[0] == adapter_id]:
        del _BUILT[key]
    _BUILT[(adapter_id, id(doc))] = (doc, built)
    return built
