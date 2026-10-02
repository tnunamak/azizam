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
    }
)

ROUTE_MANAGED_VIDEO_INPUTS: frozenset[str] = frozenset(
    {
        "prompt",
        "negative_prompt",
        "width",
        "height",
        "length",
        "frame_rate",
        "seed",
        "image",
        "image_strength",
    }
)

ROUTE_MANAGED_VIDEO_HINTS: dict[str, str] = {
    "prompt": "prompt",
    "negative_prompt": "negative_prompt",
    "width": "size",
    "height": "size",
    "length": "seconds",
    "frame_rate": "workflow configuration",
    "seed": "seed",
    "image": "input_reference",
    "image_strength": "image_strength",
}


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
) -> tuple[dict[str, Any], dict[str, str]]:
    filtered: dict[str, Any] = {}
    rejected: dict[str, str] = {}
    for name, value in supplied.items():
        if name in ROUTE_MANAGED_VIDEO_INPUTS:
            hint = ROUTE_MANAGED_VIDEO_HINTS.get(name, "the standard request shape")
            rejected[name] = (
                f"{name} is managed by the standard {hint} request field; "
                "do not pass it as a workflow extension"
            )
        else:
            filtered[name] = value
    return filtered, rejected


def bind_video_workflow_extensions(
    request_body: Mapping[str, Any],
    manifest: Mapping[str, Any],
    backend_request: Mapping[str, Any],
    *,
    namespace: str = WORKFLOW_EXTENSION_NAMESPACE,
) -> VideoExtensionBinding:
    """Validate video workflow extensions and bind accepted inputs to ComfyUI."""
    normalized_body = _request_with_workflow_namespace(request_body, namespace)
    unknown_top_level = detect_unknown_top_level_fields(
        normalized_body,
        STANDARD_VIDEO_CREATE_FIELDS,
    )

    inputs_spec = manifest.get("inputs")
    if not isinstance(inputs_spec, Mapping):
        raise ExtensionError("workflow manifest inputs must be a mapping")

    binding_map = (
        ((manifest.get("bindings") or {}).get("comfyui") or {}).get("map") or {}
    )
    supported_names = (
        set(binding_map) - ROUTE_MANAGED_VIDEO_INPUTS
        if isinstance(binding_map, Mapping)
        else set()
    )
    supplied, route_managed_rejected = _reject_route_managed_inputs(
        extract_extension_namespace(normalized_body, namespace)
    )
    validation = validate_extension_inputs(
        inputs_spec,
        supplied,
        namespace=namespace,
        supported_names=supported_names,
    )
    validation.rejected.update(route_managed_rejected)
    binding = bind_backend_request(
        dict(manifest),
        "comfyui",
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
