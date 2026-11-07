"""The read-only request that tells whether an added engine is ready, declared by its definition.

An engine definition's primary probe (``registry/engine_definitions.json``, ``probes.primary``) is a GET the
definition's contract promises the server answers, for example its model list. That same read is the readiness read:
the Add engine check runs it, a new record stores its server-root path as ``health_endpoint``, and an older record
with no stored endpoint falls back to it when the default health path is absent (``gateway.lifecycle_drivers``).
This module names no engine; the path is data.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from gateway.engine_definitions import EngineDefinitionError, load_engine_catalog


def readiness_path(definition: Mapping[str, Any] | None, config: Mapping[str, Any]) -> str | None:
    """The server-root path of the definition's readiness read for a record's connection, or ``None``.

    A probe written from the address sits under the record's ``base_path`` (which already holds the path prefix);
    one written from the origin does not.
    """
    probe = (definition or {}).get("probes", {}).get("primary") if definition else None
    if not isinstance(probe, Mapping) or probe.get("method") != "GET":
        return None
    path = str(probe.get("path") or "")
    if not path.startswith("/"):
        return None
    if probe.get("from") == "address":
        base = str(config.get("base_path") or "").rstrip("/")
        return (base if base.startswith("/") or not base else "/" + base) + path
    return path


def record_readiness_path(config: Mapping[str, Any]) -> str | None:
    """``readiness_path`` for a saved record's config, found by the definition id it was added with."""
    definition_id = config.get("definition")
    if not isinstance(definition_id, str) or not definition_id:
        return None
    try:
        definition = load_engine_catalog().definition(definition_id)
    except EngineDefinitionError:
        return None
    return readiness_path(definition, config)
