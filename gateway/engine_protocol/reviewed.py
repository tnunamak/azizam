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

import threading
import time
from pathlib import Path
from typing import Any, Mapping

from gateway.engine_protocol import description
from gateway.engine_protocol.adapter import AdapterFactory, AdapterRegistry, EngineAdapter
from gateway.engine_protocol.errors import ProtocolError

_FACTORIES: dict[str, AdapterFactory] = {}
_BUILT: dict[tuple[str, int], tuple[Any, EngineAdapter]] = {}
_MEMO: dict[tuple[Path | None, str], tuple[float, Mapping[str, description.ProtocolDescription]]] = {}
_LOCK = threading.Lock()
_REGISTRY_TTL_S = 1.0


def register(adapter_id: str, factory: AdapterFactory) -> None:
    """Register (or replace) the reviewed factory for ``adapter:<adapter_id>``."""
    with _LOCK:
        _FACTORIES[adapter_id] = factory
        for key in [key for key in _BUILT if key[0] == adapter_id]:
            _BUILT.pop(key, None)


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


def _adapter_description(documents: Mapping[str, description.ProtocolDescription], record: Mapping[str, Any],
                         capability: str) -> description.ProtocolDescription | None:
    named = _named_protocol(record)
    chosen = documents.get(named) if named else None
    if chosen is not None and chosen.adapter_id is not None:
        return chosen
    for protocol_id in sorted(documents):
        candidate = documents[protocol_id]
        if candidate.adapter_id is not None and capability in candidate.serves:
            return candidate
    return None


def _documents(directory: Path | None) -> Mapping[str, description.ProtocolDescription]:
    """The loaded descriptions, kept for ``_REGISTRY_TTL_S`` so a hot loop does not stat the registry on every call."""
    now = time.monotonic()
    key = (directory, str(description.user_directory()) if directory is None else "")
    with _LOCK:
        memo = _MEMO.get(key)
        if memo is not None and now - memo[0] < _REGISTRY_TTL_S:
            return memo[1]
    loaded = description.load_registry() if directory is None else description.load_registry(directory)
    with _LOCK:
        _MEMO[key] = (now, loaded)
    return loaded


def _candidates(record: Mapping[str, Any], capability: str) -> list[description.ProtocolDescription]:
    """The adapter descriptions that may answer: the active registry's first, then the shipped one as the floor."""
    found: list[description.ProtocolDescription] = []
    for directory in (None, description.REGISTRY_DIR):
        doc = _adapter_description(_documents(directory), record, capability)
        if doc is not None and all(doc is not other for other in found):
            found.append(doc)
    return found


def protocol_for_backend(record: Mapping[str, Any], capability: str) -> description.ProtocolDescription:
    """The adapter protocol description that serves ``capability`` for ``record``.

    The shipped descriptions are the floor: an operator file that replaces or breaks one of them can change what a
    record selects, but it cannot take the media routes down, so when the active registry holds no adapter for the
    capability the shipped one answers.
    """
    found = _candidates(record, capability)
    if not found:
        raise ProtocolError(f"No reviewed adapter is registered for '{capability}'.", code="adapter_not_registered")
    return found[0]


def _build(doc: description.ProtocolDescription) -> EngineAdapter:
    adapter_id = doc.adapter_id or ""
    with _LOCK:
        cached = _BUILT.get((adapter_id, id(doc)))
        if cached is not None:
            return cached[1]
        factories = {**_BUILTIN_FACTORIES, **_FACTORIES}
    registry = AdapterRegistry({doc.id: doc})
    for known, factory in factories.items():
        registry.register(known, factory)
    built = registry.adapter_for(doc.id)
    with _LOCK:
        for key in [key for key in _BUILT if key[0] == adapter_id]:
            _BUILT.pop(key, None)
        _BUILT[(adapter_id, id(doc))] = (doc, built)
    return built


def adapter_for_backend(record: Mapping[str, Any], capability: str) -> EngineAdapter:
    """The reviewed adapter a backend record selects for ``capability`` (cached per description).

    A description that names an adapter nobody registered, or one of another interface version, is skipped for the
    next candidate, ending at the shipped description.
    """
    refusal: ProtocolError | None = None
    for doc in _candidates(record, capability):
        try:
            return _build(doc)
        except ProtocolError as error:
            refusal = error
    raise refusal or ProtocolError(f"No reviewed adapter is registered for '{capability}'.", code="adapter_not_registered")
