"""Safe YAML parsing that refuses repeated or unhashable mapping keys."""

from __future__ import annotations

from typing import Any

import yaml


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a repeated mapping key instead of letting the last one win."""


def _construct_unique_mapping(loader: StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    # Flattening erases merge-key provenance, so reject it before expansion.
    for key_node, _ in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise yaml.constructor.ConstructorError(
                None, None, "merge keys are not allowed", key_node.start_mark
            )
    loader.flatten_mapping(node)
    # Python-equal keys must be refused even when YAML assigns different tags:
    # a dict would silently collapse e.g. an integer and a boolean.
    seen: dict[Any, yaml.Node] = {}
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:
            raise yaml.constructor.ConstructorError(None, None, "a mapping key must be a plain value", key_node.start_mark) from None
        if duplicate:
            first = seen[key]
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"duplicate key {key!r} "
                f"(first key {first.value!r} at line {first.start_mark.line + 1}; "
                f"conflicting key {key_node.value!r} at line {key_node.start_mark.line + 1})",
                key_node.start_mark,
            )
        seen[key] = key_node
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def safe_load_strict(text: str) -> Any:
    """Parse safe YAML, refusing repeated or unhashable mapping keys."""
    return yaml.load(text, Loader=StrictLoader)
