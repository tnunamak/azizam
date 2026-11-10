"""Safe YAML parsing that refuses repeated or unhashable mapping keys."""

from __future__ import annotations

from typing import Any

import yaml


class StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a repeated mapping key instead of letting the last one win."""


def _construct_unique_mapping(loader: StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict:
    loader.flatten_mapping(node)
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:
            raise yaml.constructor.ConstructorError(None, None, "a mapping key must be a plain value", key_node.start_mark) from None
        if duplicate:
            raise yaml.constructor.ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def safe_load_strict(text: str) -> Any:
    """Parse safe YAML, refusing repeated or unhashable mapping keys."""
    return yaml.load(text, Loader=StrictLoader)
