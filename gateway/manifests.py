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


@dataclass
class BackendBindingResult:
    """Result of applying validated inputs to a backend-specific request.

    Carries enough provenance for a route receipt without exposing the raw
    request body payload itself outside of the ``request`` field.
    """

    request: dict[str, Any]
    backend: str
    identity: ManifestIdentity
    applied_inputs: list[str] = field(default_factory=list)
    ignored_inputs: dict[str, str] = field(default_factory=dict)
    backend_metadata: dict[str, Any] = field(default_factory=dict)

    def receipt_provenance(self) -> dict[str, Any]:
        """Provenance summary safe to surface on a route receipt."""
        return {
            "backend": self.backend,
            "manifest_id": self.identity.id,
            "manifest_version": self.identity.version,
            "manifest_hash": self.identity.manifest_hash,
            "manifest_trusted": self.identity.trusted,
            "manifest_source": self.identity.source,
            "applied_inputs": list(self.applied_inputs),
            "ignored_inputs": dict(self.ignored_inputs),
            "backend_metadata": dict(self.backend_metadata),
        }


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
    """Apply validated portable inputs to a backend-specific request copy.

    Backward-compatible thin wrapper around :func:`bind_backend_request`.
    """
    return bind_backend_request(manifest, backend, backend_request, accepted_inputs).request


def bind_backend_request(
    manifest: dict[str, Any],
    backend: str,
    backend_request: dict[str, Any],
    accepted_inputs: dict[str, Any],
) -> BackendBindingResult:
    """Apply validated inputs and return a receipt-ready binding result.

    Accepted inputs with no mapping for the chosen backend are recorded as
    ``ignored`` with a reason. Malformed maps fail fast via :class:`ManifestError`.
    The source ``backend_request`` is not mutated.
    """
    identity = manifest_identity(manifest)
    backend_binding = manifest["bindings"].get(backend)
    if not isinstance(backend_binding, dict):
        raise ManifestError(f"manifest has no binding for backend: {backend}")

    binding_map = backend_binding.get("map", {})
    if not isinstance(binding_map, dict):
        raise ManifestError(f"manifest binding for {backend} has invalid map")

    request = copy.deepcopy(backend_request)
    applied: list[str] = []
    ignored: dict[str, str] = {}

    for input_name, value in accepted_inputs.items():
        if input_name not in binding_map:
            ignored[input_name] = f"no mapping for input under backend {backend}"
            continue
        mappings = binding_map[input_name]
        if not isinstance(mappings, list):
            raise ManifestError(f"binding map for {input_name} must be a list")
        if not mappings:
            ignored[input_name] = f"empty mapping for input under backend {backend}"
            continue
        for mapping in mappings:
            _apply_mapping(request, mapping, value)
        applied.append(input_name)

    backend_metadata = _backend_metadata(backend_binding)

    return BackendBindingResult(
        request=request,
        backend=backend,
        identity=identity,
        applied_inputs=applied,
        ignored_inputs=ignored,
        backend_metadata=backend_metadata,
    )


def _backend_metadata(backend_binding: dict[str, Any]) -> dict[str, Any]:
    """Extract non-payload binding facts safe for route receipts."""
    metadata: dict[str, Any] = {}
    for key, value in backend_binding.items():
        if key == "map":
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            metadata[key] = value
        elif isinstance(value, list) and all(
            isinstance(item, (str, int, float, bool)) for item in value
        ):
            metadata[key] = list(value)
    return metadata


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

    has_node = "node" in mapping
    has_input = "input" in mapping
    if has_node ^ has_input:
        raise ManifestError("binding mapping needs both node and input, or path")

    if has_node and has_input:
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
        if not isinstance(path, str):
            raise ManifestError("binding mapping path must be a dotted string")
        _set_path(request, path.split("."), value)
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
