"""The reviewed ``check_targets`` hooks of the adapters that have a binding description (CODE_TO_DATA_S3, D7).

Core (``binding_descriptors``) checks the type and the bound of an engine-side reference. An adapter's hook checks what the
reference points at, with the rules of that adapter's wire. A hook is called as
``check(field=, kind=, value=, at=, inputs=, template=, errors=)`` and adds to ``errors``; it reads ``template`` only when
it was given, and skips the part that needs it otherwise.

- ``graph``: for adapters that send their inputs into a graph. Each map target is a node and input, or a dotted path, and
  a target node must exist in the template.
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


def _node_exists(template: Mapping[str, Any] | None, node: Any) -> bool:
    return template is None or (isinstance(node, str) and node in template)


def check_graph_targets(*, field: str, kind: str, value: Any, at: tuple[PathPart, ...], inputs: Mapping[str, Any] | None,
                        template: Mapping[str, Any] | None, errors: ErrorCollector) -> None:
    """Targets that write into a graph. The same rules as ``manifests.bind_backend_request`` applies at run, said at save."""
    if kind == "input_map":
        for logical, targets in value.items():
            here = at + (str(logical),)
            if not isinstance(targets, list):
                errors.add(here, "type_mismatch", "an input's targets must be a list")
                continue
            for index, mapping in enumerate(targets):
                _check_graph_mapping(mapping, here + (index,), template, errors)
    elif kind == "target":
        if not _node_exists(template, value):
            errors.add(at, "target_missing", "the template has no such node")
    elif kind == "target_list":
        for index, node in enumerate(value):
            if not _node_exists(template, node):
                errors.add(at + (index,), "target_missing", "the template has no such node")


def _check_graph_mapping(mapping: Any, at: tuple[PathPart, ...], template: Mapping[str, Any] | None,
                         errors: ErrorCollector) -> None:
    if not isinstance(mapping, Mapping):
        errors.add(at, "type_mismatch", "a target must be a mapping")
        return
    has_node, has_input = "node" in mapping, "input" in mapping
    if has_node != has_input:
        errors.add(at, "mapping_incomplete", "a target needs both node and input, or path")
    elif has_node:
        node = str(mapping["node"])
        entry = template.get(node) if template is not None else None
        if not _node_exists(template, node):
            errors.add(at + ("node",), "target_missing", "the template has no such node")
        elif isinstance(entry, Mapping) and str(mapping["input"]) not in (entry.get("inputs") or {}):
            errors.add(at + ("input",), "target_missing", "the template node has no such input")
    elif not isinstance(mapping.get("path"), str) or not mapping["path"]:
        errors.add(at, "mapping_incomplete", "a target needs node and input, or path")


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
