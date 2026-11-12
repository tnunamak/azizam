"""Pure validation helpers for loaded ComfyUI API workflow graphs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping


MAX_REFUSAL_BODY_BYTES = 64 * 1024
MAX_REFUSAL_FACTS = 64
MAX_REFUSAL_IDENTIFIER_CHARS = 128


@dataclass(frozen=True)
class InputRefusal:
    """Protocol facts only: never the rejected value or the engine's explanation."""

    node_id: str
    input_name: str
    validation_type: str


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("ambiguous refusal object")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError("invalid refusal number")


def parse_refusal_body(raw: bytes) -> Mapping[str, Any] | None:
    """Parse a complete, bounded JSON object. Oversize or malformed bodies yield no evidence.

    The body is kept separately from exception text. No prefix is parsed: doing so could hide a second, unattributable
    error. Duplicate keys and non-JSON numeric constants are also ambiguous and fail closed.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_REFUSAL_BODY_BYTES:
        return None
    try:
        body = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None
    return body if isinstance(body, Mapping) else None


def _refusal_identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip() and len(value) <= MAX_REFUSAL_IDENTIFIER_CHARS


def input_refusals(body: Mapping[str, Any] | None) -> tuple[InputRefusal, ...]:
    """Decode only the verified input-choice refusal, atomically across every node and error.

    Unknown validation types may report workflow faults, so one unknown or incomplete entry invalidates all evidence.
    Only identifiers and the validation type leave this protocol module; messages and received values are never used.
    """
    if not isinstance(body, Mapping):
        return ()
    error, nodes = body.get("error"), body.get("node_errors")
    if (not isinstance(error, Mapping) or error.get("type") != "prompt_outputs_failed_validation"
            or not isinstance(nodes, Mapping) or not nodes or len(nodes) > MAX_REFUSAL_FACTS):
        return ()
    facts: list[InputRefusal] = []
    for node_id, node in nodes.items():
        if not _refusal_identifier(node_id) or not isinstance(node, Mapping):
            return ()
        errors = node.get("errors")
        if not isinstance(errors, list) or not errors or len(facts) + len(errors) > MAX_REFUSAL_FACTS:
            return ()
        for item in errors:
            if not isinstance(item, Mapping) or item.get("type") != "value_not_in_list":
                return ()
            extra = item.get("extra_info")
            name = extra.get("input_name") if isinstance(extra, Mapping) else None
            if not _refusal_identifier(name):
                return ()
            facts.append(InputRefusal(node_id, name, item["type"]))
    return tuple(facts)


def validate_workflow_graph(workflow: Any) -> list[str]:
    """Return clear validation errors for a loaded ComfyUI API workflow graph."""
    if not isinstance(workflow, Mapping):
        return ["workflow must be a mapping of node ids to node definitions"]

    node_items = [
        (node_id, node)
        for node_id, node in workflow.items()
        if node_id != "_meta" and not str(node_id).startswith("__")
    ]
    if not node_items:
        return ["workflow must contain at least one node"]

    errors: list[str] = []
    for node_id, node in node_items:
        label = _node_label(node_id)
        if not isinstance(node, Mapping):
            errors.append(f"{label} must be a mapping")
            continue

        class_type = node.get("class_type")
        if not isinstance(class_type, str) or not class_type.strip():
            errors.append(f"{label}.class_type must be a non-empty string")

        inputs = node.get("inputs")
        if not isinstance(inputs, Mapping):
            errors.append(f"{label}.inputs must be a mapping")

    return errors


def _node_label(node_id: Any) -> str:
    return f"node {node_id!r}"
