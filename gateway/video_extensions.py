"""Video-route workflow extension binding glue."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from gateway.extensions import (
    ExtensionError,
    ExtensionValidationResult,
    extract_extension_namespace,
    record_extension_result,
    validate_extension_inputs,
)
from gateway.image_extensions import (
    WORKFLOW_EXTENSION_NAMESPACE,
    detect_unknown_top_level_fields,
)
from gateway.manifests import BackendBindingResult, bind_backend_request
from gateway.extension_catalog import INPUT_CONTRACT

STANDARD_VIDEO_CREATE_FIELDS: frozenset[str] = frozenset(
    {
        "prompt",
        "model",
        "size",
        "seconds",
        "seed",
        "input_reference",
        "image_strength",
        "negative_prompt",
        "response_format",
        "user",
        "extensions",
        "inputs",
        # Long video (gateway extensions): storyboard and overlap window.
        "shots",
        "context_seconds",
    }
)

# POST /v1/videos/extensions: Sora's extend shape plus gateway fields. `video`
# is read separately.
STANDARD_VIDEO_EXTEND_FIELDS: frozenset[str] = frozenset(
    {
        "prompt",
        "model",
        "size",
        "seconds",
        "seed",
        "negative_prompt",
        "context_seconds",
        "shots",
        "input_reference",
        "response_format",
        "user",
        "extensions",
        "inputs",
    }
)

# Compatibility exports are release data, shared with callers that list extension inputs.
ROUTE_MANAGED_VIDEO_HINTS = INPUT_CONTRACT.extension_hints
ROUTE_MANAGED_VIDEO_INPUTS = frozenset(ROUTE_MANAGED_VIDEO_HINTS)


def _selected_binding(manifest: Mapping[str, Any], backend: str | None) -> tuple[str, Mapping[str, Any]]:
    bindings = manifest.get("bindings") or {}
    if backend is None:
        if len(bindings) != 1:
            raise ExtensionError("select a backend binding for workflow extensions")
        backend = next(iter(bindings))
    block = bindings.get(backend)
    if not isinstance(block, Mapping):
        from gateway.manifests import refusal
        raise refusal(("bindings", backend), "binding_missing", f"manifest has no binding for backend: {backend}")
    return backend, block


@dataclass
class VideoExtensionBinding:
    namespace: str
    validation: ExtensionValidationResult
    binding: BackendBindingResult
    unknown_top_level: dict[str, str] = field(default_factory=dict)

    @property
    def request(self) -> dict[str, Any]:
        return self.binding.request

    @property
    def ok(self) -> bool:
        return self.validation.ok and not self.unknown_top_level


def _request_with_workflow_namespace(
    request_body: Mapping[str, Any],
    namespace: str,
) -> dict[str, Any]:
    copied = dict(request_body)
    workflow_inputs = copied.get("inputs")
    if workflow_inputs is None:
        return copied
    if not isinstance(workflow_inputs, Mapping):
        raise ExtensionError("inputs must be a mapping of workflow input names to values")

    extensions = copied.get("extensions")
    if extensions is None:
        copied["extensions"] = {namespace: dict(workflow_inputs)}
        return copied
    if not isinstance(extensions, Mapping):
        raise ExtensionError("extensions must be a mapping")
    if namespace in extensions:
        raise ExtensionError(
            f"use either inputs or extensions.{namespace}, not both"
        )

    merged_extensions = dict(extensions)
    merged_extensions[namespace] = dict(workflow_inputs)
    copied["extensions"] = merged_extensions
    return copied


def merge_workflow_inputs(
    request_body: Mapping[str, Any],
    *,
    namespace: str = WORKFLOW_EXTENSION_NAMESPACE,
) -> dict[str, Any]:
    """Return the body with a top-level ``inputs`` block moved under ``extensions``."""
    merged = _request_with_workflow_namespace(request_body, namespace)
    merged.pop("inputs", None)
    return merged


def _reject_route_managed_inputs(
    supplied: Mapping[str, Any],
    managed: frozenset[str],
    hints: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    filtered: dict[str, Any] = {}
    rejected: dict[str, str] = {}
    for name, value in supplied.items():
        if name in managed:
            hint = hints.get(name, "the standard request shape")
            rejected[name] = (
                f"{name} is managed by the standard {hint} request field; "
                "do not pass it as a workflow extension"
            )
        else:
            filtered[name] = value
    return filtered, rejected


def validate_video_workflow_extensions(
    request_body: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    namespace: str = WORKFLOW_EXTENSION_NAMESPACE,
    backend: str | None = None,
) -> tuple[ExtensionValidationResult, dict[str, str]]:
    """Validate caller inputs without resolving assets or binding a graph."""
    normalized_body = _request_with_workflow_namespace(request_body, namespace)
    unknown_top_level = detect_unknown_top_level_fields(
        normalized_body,
        STANDARD_VIDEO_CREATE_FIELDS,
    )

    inputs_spec = manifest.get("inputs")
    if not isinstance(inputs_spec, Mapping):
        raise ExtensionError("workflow manifest inputs must be a mapping")

    _, block = _selected_binding(manifest, backend)
    binding_map = block.get("map") or {}
    managed = frozenset(block.get("route_bound_inputs", ROUTE_MANAGED_VIDEO_INPUTS))
    hints = {**ROUTE_MANAGED_VIDEO_HINTS, **(manifest.get("input_fields") or {})}
    supported_names = set(binding_map) - managed if isinstance(binding_map, Mapping) else set()
    supplied, route_managed_rejected = _reject_route_managed_inputs(
        extract_extension_namespace(normalized_body, namespace), managed, hints,
    )
    validation = validate_extension_inputs(
        inputs_spec,
        supplied,
        namespace=namespace,
        supported_names=supported_names,
    )
    validation.rejected.update(route_managed_rejected)
    return validation, unknown_top_level


def bind_video_workflow_extensions(
    request_body: Mapping[str, Any],
    manifest: Mapping[str, Any],
    backend_request: Mapping[str, Any],
    *,
    namespace: str = WORKFLOW_EXTENSION_NAMESPACE,
    backend: str | None = None,
) -> VideoExtensionBinding:
    """Validate workflow extensions and bind them through the selected binding."""
    backend, _ = _selected_binding(manifest, backend)
    validation, unknown_top_level = validate_video_workflow_extensions(
        request_body, manifest, namespace=namespace, backend=backend,
    )
    binding = bind_backend_request(
        dict(manifest),
        backend,
        dict(backend_request),
        validation.accepted,
    )
    return VideoExtensionBinding(
        namespace=namespace,
        validation=validation,
        binding=binding,
        unknown_top_level=unknown_top_level,
    )


def record_video_extension_binding(receipt: Any, result: VideoExtensionBinding) -> None:
    record_extension_result(receipt, result.validation)
    for name, reason in result.unknown_top_level.items():
        receipt.reject_input(name, reason)
    for name, reason in result.binding.ignored_inputs.items():
        receipt.ignore_input(f"{result.namespace}.{name}", reason)
