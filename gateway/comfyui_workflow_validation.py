"""Pure validation helpers for loaded ComfyUI API workflow graphs."""

from __future__ import annotations

from typing import Any, Mapping


def validate_comfyui_workflow_graph(workflow: Any) -> list[str]:
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
