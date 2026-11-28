"""Small readers over the schema 2 workflow contract (CODE_TO_DATA_S3, S3.4).

Every consumer reads the contract through these or through plain keys of the v2 grammar. Nothing here names any workflow,
engine or modality.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def backend_ids(contract: Mapping[str, Any]) -> list[str]:
    """The backends a contract binds: the service a binding names, else the binding's own key (an adapter that is itself
    the backend)."""
    bindings = contract.get("bindings") or {}
    return [str(spec.get("service") or key) if isinstance(spec, Mapping) else str(key) for key, spec in bindings.items()]


def requirements_of(contract: Mapping[str, Any]) -> dict[str, list[Any]] | None:
    """What the workflow needs to be present, gathered from the bindings that declare it: each requirement list
    (``services``, ``workflows``, ``custom_nodes``, ``models``) in binding order, without repeats. ``None`` when no binding
    declares any."""
    merged: dict[str, list[Any]] | None = None
    for spec in (contract.get("bindings") or {}).values():
        required = spec.get("requires") if isinstance(spec, Mapping) else None
        if not isinstance(required, Mapping):
            continue
        merged = {} if merged is None else merged
        for key, items in required.items():
            bucket = merged.setdefault(str(key), [])
            for item in items if isinstance(items, (list, tuple)) else ():
                if item not in bucket:
                    bucket.append(item)
    return merged


def settable_inputs(contract: Mapping[str, Any]) -> dict[str, Any]:
    """The inputs a caller may set: every input except those marked ``settable: false``, which only the gateway fills."""
    inputs = contract.get("inputs")
    if not isinstance(inputs, Mapping):
        return {}
    return {name: spec for name, spec in inputs.items() if not (isinstance(spec, Mapping) and spec.get("settable") is False)}
