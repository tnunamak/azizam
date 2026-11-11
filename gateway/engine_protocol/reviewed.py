"""Which reviewed adapter answers for a backend record (contract section 4).

A backend record selects its adapter with data, never with a branch on its name.
The record's ``protocol`` (or ``connection.protocol``) names a protocol
description. Its ``executor: adapter:<id>`` declares identity; its ``implementation``
selects a factory in the reviewed code allow-list. Explicit factory registrations
may replace that factory by identity. A record that names no adapter
protocol is answered by the reviewed adapter whose description lists the
record's capability in ``serves``. That is how every record stored before this
field existed keeps working without a migration.

Core calls ``adapter_for_backend(record, capability)``; nothing here knows what
any engine is. The factories below are the reviewed in-tree adapters and the
only place implementation symbols are written next to their code.
"""

from __future__ import annotations

import inspect
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


def _node_graph(doc: Any, assets: Any = None) -> EngineAdapter:
    from gateway.engine_protocol import node_graph
    return node_graph.NodeGraphAdapter(doc, assets=assets)


def _chunked_stt(doc: Any) -> EngineAdapter:
    from gateway.engine_protocol import chunked_stt
    return chunked_stt.ChunkedSttAdapter(doc)


# The reviewed in-tree adapters. "builtin" is provenance only: a package adapter registers the same way.
_BUILTIN_FACTORIES: dict[str, AdapterFactory] = {
    "inline_speech": _inline_tts,
    "graph_submission": _node_graph,
    "chunked_transcription": _chunked_stt,
}


def is_reviewed_implementation(name: str) -> bool:
    """Whether a description may select this code, including the separate binding runner."""
    return name in _BUILTIN_FACTORIES or name == "multipart_post"


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


def adapter_serves(capability: str) -> bool:
    """Whether any reviewed adapter description (active registry, then the shipped floor) lists ``capability`` in ``serves``."""
    return any(
        doc.adapter_id is not None and capability in doc.serves
        for directory in (None, description.REGISTRY_DIR) for doc in _documents(directory).values()
    )


def speaks_serving_adapter(record: Mapping[str, Any], capability: str) -> bool:
    """Whether ``record`` speaks a reviewed adapter protocol that serves ``capability``.

    A record that names a protocol speaks it only when that is an adapter description listing the capability in
    ``serves``; a record that names none speaks the adapter whose ``serves`` lists the capability (the inference
    ``protocol_for_backend`` makes). A record naming another adapter, an HTTP protocol or an unknown one does not.
    The active registry answers first and the shipped one is the floor, as in ``protocol_for_backend``.
    """
    named = _named_protocol(record)
    for directory in (None, description.REGISTRY_DIR):
        documents = _documents(directory)
        candidates = [documents.get(named)] if named else list(documents.values())
        if any(doc is not None and doc.adapter_id is not None and capability in doc.serves for doc in candidates):
            return True
    return False


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


def _with_assets(factory: AdapterFactory, assets: Any) -> AdapterFactory:
    """``factory`` as a factory of adapters that keep results in ``assets``, when the factory takes a store."""
    if "assets" not in inspect.signature(factory).parameters:
        return factory
    return lambda doc: factory(doc, assets=assets)  # type: ignore[call-arg]


def _factories_for(doc: description.ProtocolDescription) -> dict[str, AdapterFactory]:
    factories = dict(_FACTORIES)
    implementation = doc.implementation
    if not implementation:
        # Existing operator descriptions may predate this field. Their identity can inherit a shipped registration.
        defaults = [row for row in description.load_registry(description.REGISTRY_DIR).values()
                    if row.adapter_id == doc.adapter_id and row.implementation]
        implementation = defaults[0].implementation if len(defaults) == 1 else ""
    if implementation in _BUILTIN_FACTORIES:
        factories.setdefault(doc.adapter_identity, _BUILTIN_FACTORIES[implementation])
    return factories


def _build(doc: description.ProtocolDescription, assets: Any = None) -> EngineAdapter:
    adapter_id = doc.adapter_id or ""
    # Explicit implementation symbols never fall through to an identity-based registration.
    if doc.implementation and doc.implementation not in _BUILTIN_FACTORIES:
        raise ProtocolError(f"Unknown reviewed implementation '{doc.implementation}'.", code="adapter_not_registered")
    if assets is not None:      # an adapter bound to one caller's store is never shared
        registry = AdapterRegistry({doc.id: doc}, assets=assets)
        for known, factory in _factories_for(doc).items():
            registry.register(known, _with_assets(factory, assets))
        return registry.adapter_for(doc.id)
    with _LOCK:
        cached = _BUILT.get((adapter_id, id(doc)))
        if cached is not None:
            return cached[1]
        factories = _factories_for(doc)
    registry = AdapterRegistry({doc.id: doc})
    for known, factory in factories.items():
        registry.register(known, factory)
    built = registry.adapter_for(doc.id)
    with _LOCK:
        for key in [key for key in _BUILT if key[0] == adapter_id]:
            _BUILT.pop(key, None)
        _BUILT[(adapter_id, id(doc))] = (doc, built)
    return built


def adapter_for_backend(record: Mapping[str, Any], capability: str, *, assets: Any = None) -> EngineAdapter:
    """The reviewed adapter a backend record selects for ``capability`` (cached per description).
    With ``assets``, a fresh adapter that keeps the result files in that store (not cached).

    A description that names an adapter nobody registered, or one of another interface version, is skipped for the
    next candidate, ending at the shipped description. An explicit unknown implementation is refused immediately.
    """
    refusal: ProtocolError | None = None
    for doc in _candidates(record, capability):
        try:
            return _build(doc, assets)
        except ProtocolError as error:
            if doc.implementation and doc.implementation not in _BUILTIN_FACTORIES:
                raise
            refusal = error
    raise refusal or ProtocolError(f"No reviewed adapter is registered for '{capability}'.", code="adapter_not_registered")
