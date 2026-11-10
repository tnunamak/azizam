"""Closed, data-owned gateway extension controls and pure wire validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from string import Formatter
from types import MappingProxyType
from typing import Any, Mapping

import yaml

@dataclass(frozen=True)
class ControlDescriptor:
    """One validated primitive control, addressed by its public field."""

    field: str
    type: str
    default: bool


@dataclass(frozen=True)
class ControlGroup:
    """An owned immutable schema; logical IDs never leak into wire results."""

    namespace: str
    controls: Mapping[str, ControlDescriptor]
    errors: Mapping[str, str]
    public_fields: frozenset[str] = field(init=False)

    def __post_init__(self) -> None:
        owned = dict(self.controls)
        object.__setattr__(self, "controls", MappingProxyType(owned))
        object.__setattr__(self, "errors", MappingProxyType(dict(self.errors)))
        object.__setattr__(self, "public_fields", frozenset(item.field for item in owned.values()))


class ControlValidationError(ValueError):
    """A request control failed validation at the public dotted parameter."""

    def __init__(self, message: str, *, param: str) -> None:
        super().__init__(message)
        self.param = param


class _ControlLoader(yaml.SafeLoader):
    """Safe YAML without duplicate or merged mappings."""


def _unique_mapping(loader: _ControlLoader, node: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise ValueError("extension controls do not accept merged mappings")
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key in result:
            raise ValueError("extension controls require unique string mapping keys")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_ControlLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping,
)


def _mapping(value: Any, label: str, keys: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) or not key for key in value):
        raise ValueError(f"{label}: expected nonempty string mapping keys")
    if keys is not None and set(value) != keys:
        raise ValueError(f"{label}: unknown or missing fields")
    return value


def _logical_mapping(value: Any, label: str) -> dict[str, Any]:
    result = _mapping(value, label)
    if not result or any(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key) is None for key in result):
        raise ValueError(f"{label}: expected nonempty mapping of logical IDs")
    return result


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label}: expected nonempty text")
    return value


def _template(value: Any, token: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}: expected a message template")
    try:
        for _literal, name, spec, conversion in Formatter().parse(value):
            if name is not None and (name != token or spec or conversion is not None):
                raise ValueError("undeclared token, conversion or format specification")
    except ValueError as exc:
        raise ValueError(f"{label}: invalid message template") from exc
    return value


def load_extension_controls(path: Path) -> Mapping[str, ControlGroup]:
    """Load the descriptor-owned public contract without importing its consumers."""
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_ControlLoader)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise ValueError(f"{path}: invalid extension control YAML") from exc
    document = _mapping(document, str(path), {"schema_version", "groups"})
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError(f"{path}: unsupported schema_version")
    groups: dict[str, ControlGroup] = {}
    namespaces: set[str] = set()
    for name, raw in _logical_mapping(document["groups"], "groups").items():
        raw = _mapping(raw, name, {"namespace", "controls", "errors"})
        namespace = _text(raw["namespace"], f"{name}.namespace")
        if namespace in namespaces:
            raise ValueError(f"{name}: duplicate namespace {namespace}")
        namespaces.add(namespace)
        descriptors: dict[str, ControlDescriptor] = {}
        public_fields: set[str] = set()
        group_label = f"{name} ({namespace})"
        for logical_id, definition in _logical_mapping(raw["controls"], f"{group_label}.controls").items():
            label = f"{group_label}.controls.{logical_id}"
            definition = _mapping(definition, label, {"field", "type", "default"})
            public_field = _text(definition["field"], f"{label}.field")
            if public_field in public_fields:
                raise ValueError(f"{label}: duplicate public field")
            public_fields.add(public_field)
            if definition["type"] != "boolean":
                raise ValueError(f"{label}: unsupported control type")
            if type(definition["default"]) is not bool:
                raise ValueError(f"{label}: expected boolean default")
            descriptors[logical_id] = ControlDescriptor(public_field, definition["type"], definition["default"])
        tokens = {"block_not_object": "namespace", "unknown_control": "field", "invalid_value": "field"}
        raw_errors = _mapping(raw["errors"], f"{name}.errors", set(tokens))
        errors = {
            key: _template(raw_errors[key], token, f"{name}.errors.{key}")
            for key, token in tokens.items()
        }
        group = ControlGroup(namespace, descriptors, errors)
        groups[name] = group
    return MappingProxyType(groups)


def extension_controls_schema(
    groups: Mapping[str, ControlGroup] | None = None,
) -> dict[str, Any]:
    """Build a fresh public schema from loaded descriptors, closing each namespace."""
    if groups is None:
        groups = _CONTROL_GROUPS
    return {
        "type": "object",
        "properties": {
            group.namespace: {
                "type": "object",
                "properties": {
                    descriptor.field: {"type": descriptor.type, "default": descriptor.default}
                    for descriptor in group.controls.values()
                },
                "additionalProperties": False,
            }
            for group in groups.values()
        },
        "additionalProperties": True,
    }


def control_group(name: str) -> ControlGroup:
    """Return an immutable startup descriptor; unknown names raise KeyError."""
    return _CONTROL_GROUPS[name]


def parse_control_group(body: Mapping[str, Any], group: ControlGroup) -> dict[str, Any]:
    """Validate one wire block, checking all supplied names before any value."""
    extensions = body.get("extensions") if isinstance(body, Mapping) else None
    if not isinstance(extensions, Mapping):
        return {}
    block = extensions.get(group.namespace)
    if block is None:
        return {}
    param = f"extensions.{group.namespace}"
    if not isinstance(block, Mapping):
        raise ControlValidationError(
            group.errors["block_not_object"].format(namespace=group.namespace), param=param,
        )
    for name in block:
        if name not in group.public_fields:
            raise ControlValidationError(
                group.errors["unknown_control"].format(field=name), param=f"{param}.{name}",
            )
    result: dict[str, Any] = {}
    for descriptor in group.controls.values():
        value = block.get(descriptor.field, descriptor.default)
        if type(value) is not bool:
            raise ControlValidationError(
                group.errors["invalid_value"].format(field=descriptor.field),
                param=f"{param}.{descriptor.field}",
            )
        result[descriptor.field] = value
    return result


_CONTROL_GROUPS = load_extension_controls(
    Path(__file__).resolve().parent.parent / "registry" / "extension_controls.yml",
)
