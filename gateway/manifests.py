"""Workflow manifest loading, validation, and backend binding helpers."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ManifestError(ValueError):
    """Raised when a manifest or backend binding is invalid."""


@dataclass
class InputValidationResult:
    accepted: dict[str, Any] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.rejected


@dataclass(frozen=True)
class ManifestIdentity:
    id: str
    version: str | None
    manifest_hash: str
    trusted: bool
    source: str | None


def load_workflow_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path)
    with manifest_path.open("r", encoding="utf-8") as f:
        manifest = yaml.safe_load(f)
    if not isinstance(manifest, dict):
        raise ManifestError(f"manifest {manifest_path} must be a mapping")
    validate_manifest(manifest)
    return manifest


def manifest_identity(manifest: dict[str, Any]) -> ManifestIdentity:
    validate_manifest(manifest)
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return ManifestIdentity(
        id=str(manifest["id"]),
        version=manifest.get("version"),
        manifest_hash=hashlib.sha256(encoded).hexdigest(),
        trusted=bool(manifest.get("trusted", False)),
        source=manifest.get("source"),
    )


def validate_manifest(manifest: dict[str, Any]) -> None:
    for field_name in ("schema_version", "id", "intent", "inputs", "outputs", "bindings"):
        if field_name not in manifest:
            raise ManifestError(f"manifest missing required field: {field_name}")
    if not isinstance(manifest["inputs"], dict):
        raise ManifestError("manifest inputs must be a mapping")
    if not isinstance(manifest["bindings"], dict):
        raise ManifestError("manifest bindings must be a mapping")


def validate_inputs(
    manifest: dict[str, Any],
    supplied: dict[str, Any],
) -> InputValidationResult:
    validate_manifest(manifest)
    specs = manifest["inputs"]
    result = InputValidationResult()

    for name, value in supplied.items():
        spec = specs.get(name)
        if spec is None:
            result.rejected[name] = "unknown input"
            continue
        reason = _validate_value(name, value, spec)
        if reason is None:
            result.accepted[name] = value
        else:
            result.rejected[name] = reason

    for name, spec in specs.items():
        if spec.get("required") and name not in supplied:
            result.rejected[name] = "required input missing"

    return result


def apply_backend_binding(
    manifest: dict[str, Any],
    backend: str,
    backend_request: dict[str, Any],
    accepted_inputs: dict[str, Any],
) -> dict[str, Any]:
    """Apply validated portable inputs to a backend-specific request copy."""
    validate_manifest(manifest)
    backend_binding = manifest["bindings"].get(backend)
    if not isinstance(backend_binding, dict):
        raise ManifestError(f"manifest has no binding for backend: {backend}")

    binding_map = backend_binding.get("map", {})
    if not isinstance(binding_map, dict):
        raise ManifestError(f"manifest binding for {backend} has invalid map")

    request = copy.deepcopy(backend_request)
    for input_name, value in accepted_inputs.items():
        mappings = binding_map.get(input_name, [])
        if not isinstance(mappings, list):
            raise ManifestError(f"binding map for {input_name} must be a list")
        for mapping in mappings:
            _apply_mapping(request, mapping, value)
    return request


def _validate_value(name: str, value: Any, spec: dict[str, Any]) -> str | None:
    expected_type = spec.get("type", "string")
    if expected_type == "string":
        if not isinstance(value, str):
            return f"{name} must be a string"
    elif expected_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{name} must be a number"
    elif expected_type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{name} must be an integer"
    elif expected_type == "boolean":
        if not isinstance(value, bool):
            return f"{name} must be a boolean"
    elif expected_type == "asset":
        if not isinstance(value, (str, dict)):
            return f"{name} must be an asset reference"
    else:
        return f"{name} has unsupported type: {expected_type}"

    enum = spec.get("enum")
    if enum is not None and value not in enum:
        return f"{name} must be one of: {', '.join(map(str, enum))}"

    if expected_type in {"number", "integer"}:
        minimum = spec.get("minimum")
        maximum = spec.get("maximum")
        if minimum is not None and value < minimum:
            return f"{name} must be >= {minimum}"
        if maximum is not None and value > maximum:
            return f"{name} must be <= {maximum}"

    return None


def _apply_mapping(request: dict[str, Any], mapping: dict[str, Any], value: Any) -> None:
    if not isinstance(mapping, dict):
        raise ManifestError("binding mapping must be a mapping")

    if "node" in mapping and "input" in mapping:
        node_id = str(mapping["node"])
        input_name = str(mapping["input"])
        try:
            request[node_id]["inputs"][input_name] = value
        except KeyError as exc:
            raise ManifestError(
                f"ComfyUI binding target missing: node {node_id} input {input_name}"
            ) from exc
        return

    path = mapping.get("path")
    if path:
        _set_path(request, str(path).split("."), value)
        return

    raise ManifestError("binding mapping needs node/input or path")


def _set_path(target: dict[str, Any], parts: list[str], value: Any) -> None:
    if not parts:
        raise ManifestError("binding path cannot be empty")
    cursor: Any = target
    for part in parts[:-1]:
        if isinstance(cursor, dict):
            cursor = cursor.setdefault(part, {})
        else:
            raise ManifestError(f"binding path crosses non-mapping value at {part}")
    if not isinstance(cursor, dict):
        raise ManifestError("binding path target is not a mapping")
    cursor[parts[-1]] = value
