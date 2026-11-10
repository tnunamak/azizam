"""Validated image-graph facts not already owned by manifest input maps.

This module does not read graph _meta or change a graph, registry or route.
Relative graph paths are resolved against the install containing the record's
manifests/workflows directory; in-memory records use this module's install.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.discovery import WorkflowRecord, WorkflowRegistry

_ROOT = Path(__file__).resolve().parents[1]
_GRAPH_PATH = re.compile(r"workflows/images/[^/]+\.json")
# The shipped background-removal processors give the cut-out picture on output 0. Do not infer output
# arity from graph links, or accept another output (for example a mask).
_BACKGROUND_IMAGE_SLOT = 0


def _fail(graph: str, key: str, reason: str) -> None:
    raise ValueError(f"{graph}: {key}: {reason}")


def _object(value: Any, graph: str, key: str, allowed: set[str],
            required: set[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _fail(graph, key, "must be an object")
    for name in value:
        if name not in allowed:
            _fail(graph, f"{key}.{name}", "unknown key")
    for name in sorted(required or ()):
        if name not in value:
            _fail(graph, f"{key}.{name}", "required key is missing")
    return value


def _node(nodes: Mapping[str, Any], node_id: Any, graph: str, key: str) -> Mapping[str, Any]:
    if not isinstance(node_id, str) or not node_id or node_id.startswith("_"):
        _fail(graph, key, "must name a graph node")
    node = nodes.get(node_id)
    if not isinstance(node, Mapping) or not isinstance(node.get("class_type"), str):
        _fail(graph, key, f"node {node_id!r} is missing")
    return node


def _graph_file(graph: str, record: WorkflowRecord) -> Path:
    source = record.source_path.resolve()
    if source.parent.name == "workflows" and source.parent.parent.name == "manifests":
        return source.parent.parent.parent / graph
    return _ROOT / graph


def _targets(binding_map: Mapping[str, Any]) -> set[tuple[str, str]]:
    found = set()
    for targets in binding_map.values():
        if not isinstance(targets, (list, tuple)):
            continue
        for target in targets:
            if not isinstance(target, Mapping):
                continue
            if isinstance(target.get("node"), (str, int)) and isinstance(target.get("input"), str):
                found.add((str(target["node"]), target["input"]))
            elif isinstance(target.get("path"), str):
                parts = target["path"].split(".")
                if len(parts) >= 3 and parts[1] == "inputs":
                    found.add((parts[0], ".".join(parts[2:])))
    return found


def _background(value: Any, graph: str, nodes: Mapping[str, Any],
                manifest: Mapping[str, Any], binding: Mapping[str, Any]) -> None:
    value = _object(value, graph, "background", {"output", "processor", "slot"},
                    {"output", "processor", "slot"})
    output = value["output"]
    outputs, selectors = manifest.get("outputs", {}), binding.get("selectors", {})
    if (not isinstance(output, str) or not output or output not in outputs or output not in selectors):
        _fail(graph, "background.output", "must name a declared manifest output and selector")
    selector = selectors[output]
    if not isinstance(selector, Mapping):
        _fail(graph, "background.output", "selector must be an object")
    sink = _node(nodes, selector.get("node"), graph, "background.output")
    processor = _node(nodes, value["processor"], graph, "background.processor")
    plain = sink.get("inputs", {}).get("images")
    if not isinstance(plain, list) or len(plain) != 2:
        _fail(graph, "background.output", "selector node must consume an image link")
    _node(nodes, plain[0], graph, "background.output")
    if plain not in processor.get("inputs", {}).values():
        _fail(graph, "background.processor", "must process the selected output's plain image")
    slot = value["slot"]
    if type(slot) is not int or slot != _BACKGROUND_IMAGE_SLOT:
        _fail(graph, "background.slot", "out of range for the supported background image output (0)")


def _batch(value: Any, graph: str, nodes: Mapping[str, Any], mapped: set[tuple[str, str]]) -> None:
    if not isinstance(value, (list, tuple)) or len(value) != 1:
        _fail(graph, "batch_size", "must contain exactly one node/input target")
    seen = set()
    for index, target in enumerate(value):
        key = f"batch_size[{index}]"
        target = _object(target, graph, key, {"node", "input"}, {"node", "input"})
        node = _node(nodes, target["node"], graph, f"{key}.node")
        name = target["input"]
        if not isinstance(name, str) or not name:
            _fail(graph, f"{key}.input", "must name a graph input")
        pair = (target["node"], name)
        if pair in mapped:
            _fail(graph, key, "repeats a target already carried by the binding map")
        if name != "batch_size" or name not in node.get("inputs", {}):
            _fail(graph, f"{key}.input", "must name the node's batch_size input")
        if pair in seen:
            _fail(graph, key, "target is repeated")
        seen.add(pair)


def _references(value: Any, graph: str, nodes: Mapping[str, Any],
                manifest: Mapping[str, Any], binding_map: Mapping[str, Any]) -> None:
    if not isinstance(value, (list, tuple)) or not value:
        _fail(graph, "reference_order", "must list reference input names")
    inputs = manifest.get("inputs", {})
    seen = set()
    for index, name in enumerate(value):
        key = f"reference_order[{index}]"
        if not isinstance(name, str) or name not in inputs or name not in binding_map:
            _fail(graph, key, "input is missing from the manifest or its binding map")
        if name in seen:
            _fail(graph, key, f"input {name!r} is repeated")
        seen.add(name)
        if inputs[name].get("type") != "asset":
            _fail(graph, key, "must name an asset input")
        targets = binding_map[name]
        if not isinstance(targets, (list, tuple)) or not targets:
            _fail(graph, key, "reference input has no node targets")
        for target in targets:
            if not isinstance(target, Mapping):
                _fail(graph, key, "reference target must be an object")
            node = _node(nodes, target.get("node"), graph, key)
            if target.get("input") not in node.get("inputs", {}):
                _fail(graph, key, "reference target input is missing from its node")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(child) for key, child in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(child) for child in value)
    return value


def load(images_section: Mapping[str, Any], registry: WorkflowRegistry, binding_key: str) -> Mapping[str, Any]:
    """Return detached, recursively read-only entries; refuse invalid graph/key facts.

    ``binding_key`` names the manifest binding whose ``workflow`` graph and ``map`` the entries describe, so no
    engine name is spelled here.

    The registry is the authority for known graph paths and declared inputs,
    outputs and targets. No node id or role is recovered from graph metadata.
    An absent table is empty, allowing existing installations to adopt it.
    """
    if not isinstance(images_section, Mapping):
        _fail("<images>", "graph_roles", "images section must be an object")
    entries = images_section.get("graph_roles", {})
    if not isinstance(entries, Mapping):
        _fail("<images>", "graph_roles", "must be an object")
    records: dict[str, list[WorkflowRecord]] = {}
    for record in registry.records():
        binding = record.contract.get("bindings", {}).get(binding_key, {})
        path = binding.get("workflow")
        if isinstance(path, str):
            records.setdefault(path, []).append(record)
    for graph, entry in entries.items():
        if (not isinstance(graph, str) or not _GRAPH_PATH.fullmatch(graph)
                or graph not in records):
            _fail(str(graph), "graph_roles", "unknown graph path")
        for record in records[graph]:
            manifest = record.contract
            binding = manifest["bindings"][binding_key]
            binding_map = binding.get("map", {})
            if not isinstance(binding_map, Mapping):
                _fail(graph, "graph_roles", "manifest binding map must be an object")
            if isinstance(entry, Mapping):
                for key in entry:
                    if key in binding_map:
                        _fail(graph, key, "repeats a role already carried by the binding map")
            entry = _object(entry, graph, "graph_roles", {"background", "batch_size", "reference_order"})
            try:
                nodes = json.loads(_graph_file(graph, record).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                _fail(graph, "graph_roles", f"graph cannot be read: {exc}")
            if not isinstance(nodes, Mapping):
                _fail(graph, "graph_roles", "graph must be an object")
            if "background" in entry:
                _background(entry["background"], graph, nodes, manifest, binding)
            if "batch_size" in entry:
                _batch(entry["batch_size"], graph, nodes, _targets(binding_map))
            if "reference_order" in entry:
                _references(entry["reference_order"], graph, nodes, manifest, binding_map)
    return _freeze(entries)
