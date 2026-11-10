"""Immutable image input write targets from one of a workflow's binding maps."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

__all__ = ["targets"]

_Pairs = tuple[tuple[str, str], ...]
_FIELDS = ("prompt", "negative_prompt", "seed", "width", "height", "batch_size", "lora_strength")


@dataclass(frozen=True, slots=True)
class _Targets:
    prompt: _Pairs
    negative_prompt: _Pairs
    seed: _Pairs
    width: _Pairs
    height: _Pairs
    batch_size: _Pairs
    lora_strength: _Pairs
    assets: Mapping[str, _Pairs]
    path_mappings: Mapping[str, tuple[str, ...]]


def targets(contract: Mapping[str, Any], binding_key: str) -> _Targets:
    """Read a validated workflow contract's ``binding_key`` binding map without loading or inspecting its graph.

    The caller names the binding (the record's selected backend), so no engine name is spelled here.

    Scalar fields and each declared type: asset input contain node/input
    pairs in binding-map order, including duplicates. Unmapped inputs have an
    empty tuple. The assets mapping is keyed by contract input name.
    The path_mappings field keeps dotted path strings separately, keyed by
    their binding input name (including inputs outside the scalar fields).

    Like manifests._apply_mapping, node/input values are converted to strings
    and a complete node/input pair takes precedence over a path on the same
    mapping. This reader consumes a validated contract; it is not a validator.
    All returned containers are immutable and detached from the contract.
    Graph metadata, selectors, defaults and other backends supply no targets.
    """
    binding = contract.get("bindings", {}).get(binding_key, {})
    binding_map = binding.get("map", {})
    pairs_by_input: dict[str, _Pairs] = {}
    paths_by_input: dict[str, tuple[str, ...]] = {}
    for name, mappings in binding_map.items():
        pairs: list[tuple[str, str]] = []
        paths: list[str] = []
        for mapping in mappings:
            if "node" in mapping and "input" in mapping:
                pairs.append((str(mapping["node"]), str(mapping["input"])))
            elif "path" in mapping:
                paths.append(mapping["path"])
        pairs_by_input[name] = tuple(pairs)
        if paths:
            paths_by_input[name] = tuple(paths)

    assets = {
        name: pairs_by_input.get(name, ())
        for name, spec in contract.get("inputs", {}).items()
        if spec.get("type") == "asset"
    }
    return _Targets(
        **{name: pairs_by_input.get(name, ()) for name in _FIELDS},
        assets=MappingProxyType(assets),
        path_mappings=MappingProxyType(paths_by_input),
    )
