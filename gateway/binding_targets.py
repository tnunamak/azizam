"""The reviewed ``check_targets`` hooks of the adapters that have a binding description (CODE_TO_DATA_S3, D7).

Core (``binding_descriptors``) checks the type and the bound of an engine-side reference. An adapter's hook checks what the
reference points at, with the rules of that adapter's wire. A hook is called as
``check(field=, kind=, value=, at=, inputs=, template=, errors=)`` and adds to ``errors``; it reads ``template`` only when
it was given, and skips the part that needs it otherwise.

- ``graph``: for adapters that send their inputs into a graph. Each map target is a node and input, or a dotted path, and
  a target node must exist in the template and be a mapping; a path must start at such a node. Two targets may not
  write one place, and a node list may not repeat a node.
- ``form``: for adapters that send their inputs as form fields. A target is a plain field name, used once, and an object
  input has no form field (the rules ``mesh_manifest`` applied to its map).

A description picks its hook by name with ``check_targets``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from gateway import binding_registry
from gateway.manifests import ErrorCollector, PathPart

_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _node_problem(template: Mapping[str, Any] | None, node: Any) -> tuple[str, str] | None:
    """Why ``node`` cannot be written to, as ``(code, message)``, or ``None`` when it can or when no template was given. A
    node of the template must be a mapping, so a malformed template node is an error and never a crash."""
    if template is None:
        return None
    if not isinstance(node, str) or node not in template:
        return "target_missing", "the template has no such node"
    if not isinstance(template[node], Mapping):
        return "template_node_invalid", "this template node is not a mapping"
    return None


def check_graph_targets(*, field: str, kind: str, value: Any, at: tuple[PathPart, ...], inputs: Mapping[str, Any] | None,
                        template: Mapping[str, Any] | None, errors: ErrorCollector) -> None:
    """Targets that write into a graph. The same rules as ``manifests.bind_backend_request`` applies at run, said at save."""
    if template is not None and not isinstance(template, Mapping):
        # A template that is not a mapping of nodes says nothing about any target: report it, then check what needs no template.
        errors.add(at, "template_node_invalid", "the template is not a mapping of nodes")
        template = None
    if kind == "input_map":
        written: dict[tuple[str, ...], tuple[PathPart, ...]] = {}   # where each write lands, and who wrote it first
        for logical, targets in value.items():
            here = at + (str(logical),)
            if not isinstance(targets, list):
                errors.add(here, "type_mismatch", "an input's targets must be a list")
                continue
            for index, mapping in enumerate(targets):
                _check_graph_mapping(mapping, here + (index,), template, written, errors)
    elif kind == "target":
        problem = _node_problem(template, value)
        if problem:
            errors.add(at, *problem)
    elif kind == "target_list":
        seen: set[str] = set()
        for index, node in enumerate(value):
            problem = _node_problem(template, node)
            if problem:
                errors.add(at + (index,), *problem)
            elif node in seen:
                errors.add(at + (index,), "target_duplicate", "this node is already in the list")
            seen.add(node)


def _check_graph_mapping(mapping: Any, at: tuple[PathPart, ...], template: Mapping[str, Any] | None,
                         written: dict[tuple[str, ...], tuple[PathPart, ...]], errors: ErrorCollector) -> None:
    if not isinstance(mapping, Mapping):
        errors.add(at, "type_mismatch", "a target must be a mapping")
        return
    has_node, has_input = "node" in mapping, "input" in mapping
    if has_node != has_input:
        errors.add(at, "mapping_incomplete", "a target needs both node and input, or path")
        return
    if has_node:
        node, name = str(mapping["node"]), str(mapping["input"])
        problem = _node_problem(template, node)
        if problem:
            errors.add(at + ("node",), *problem)
        elif template is not None:
            node_inputs = template[node].get("inputs")
            if node_inputs is not None and not isinstance(node_inputs, Mapping):
                errors.add(at + ("node",), "template_node_invalid", "the inputs of this template node are not a mapping")
            elif node_inputs is None or name not in node_inputs:
                errors.add(at + ("input",), "target_missing", "the template node has no such input")
        _note_write(written, (node, "inputs", name), at, errors)
        return
    path = mapping.get("path")
    if not isinstance(path, str) or not path:
        errors.add(at, "mapping_incomplete", "a target needs node and input, or path")
        return
    parts = path.split(".")
    if not all(parts):
        errors.add(at + ("path",), "path_invalid", "a path cannot hold an empty part")
        return
    problem = _path_problem(template, parts)
    if problem:
        errors.add(at + ("path",), *problem)
    _note_write(written, tuple(parts), at, errors)


def _path_problem(template: Mapping[str, Any] | None, parts: list[str]) -> tuple[str, str] | None:
    """Walk a dotted path through the template as far as it goes. The first part is a node and must exist; a part the
    path crosses must be a mapping. Where the path leaves the template, the writer creates the rest."""
    problem = _node_problem(template, parts[0])
    if problem or template is None:
        return problem
    cursor: Any = template[parts[0]]
    for part in parts[1:-1]:
        if part not in cursor:
            return None
        cursor = cursor[part]
        if not isinstance(cursor, Mapping):
            return "path_invalid", "this path crosses a value that is not a mapping"
    return None


def _note_write(written: dict[tuple[str, ...], tuple[PathPart, ...]], place: tuple[str, ...], at: tuple[PathPart, ...],
                errors: ErrorCollector) -> None:
    """Two targets that write one place keep only the later one; refuse the second."""
    if place in written:
        errors.add(at, "target_duplicate", "another target writes this node and input")
    else:
        written[place] = at


def check_form_targets(*, field: str, kind: str, value: Any, at: tuple[PathPart, ...], inputs: Mapping[str, Any] | None,
                       template: Mapping[str, Any] | None, errors: ErrorCollector) -> None:
    """Targets that name form fields of a multipart POST."""
    if kind != "input_map":
        return
    used: set[str] = set()
    for logical, target in value.items():
        here = at + (str(logical),)
        if inputs is not None and isinstance(inputs.get(logical), Mapping) and inputs[logical].get("type") == "object":
            errors.add(here, "input_not_mappable", "an object input cannot be mapped to a form field")
        elif not isinstance(target, str) or not _FIELD_NAME.fullmatch(target):
            errors.add(here, "name_invalid", "a form field target must be a plain field name")
        elif target in used:
            errors.add(here, "target_duplicate", "this form field is the target of another input")
        else:
            used.add(target)


binding_registry.register_target_check("graph", check_graph_targets)
binding_registry.register_target_check("form", check_form_targets)
