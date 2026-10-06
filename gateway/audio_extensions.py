"""Audio-route workflow extension binding glue.

This module keeps ``POST /v1/audio/generate`` strict without coupling the route
to manifest internals. Standard audio request fields stay at the top level;
gateway workflow controls live under ``extensions.com.azizam.workflow`` and
are validated/bound through the workflow manifest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from gateway.extensions import (
    ExtensionError,
    ExtensionValidationResult,
    record_extension_result,
    validate_extension_inputs,
)
from gateway.compat import read_field
from gateway.manifests import BackendBindingResult, bind_backend_request

WORKFLOW_EXTENSION_NAMESPACE = "com.azizam.workflow"


STANDARD_AUDIO_GENERATE_FIELDS: frozenset[str] = frozenset(
    {
        "prompt",
        "input",
        "tags",
        "lyrics",
        "model",
        "duration",
        "seed",
        "type",
        "response_format",
        "user",
        "extensions",
    }
)

ROUTE_MANAGED_AUDIO_INPUTS: frozenset[str] = frozenset(
    {
        "prompt",
        "tags",
        "lyrics",
        "duration",
        "seed",
    }
)


@dataclass
class AudioExtensionBinding:
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


def detect_unknown_audio_fields(
    request_body: Mapping[str, Any],
    standard_fields: Iterable[str] = STANDARD_AUDIO_GENERATE_FIELDS,
) -> dict[str, str]:
    standard = set(standard_fields)
    return {
        name: (
            "unknown top-level field; gateway workflow controls belong under "
            f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}"
        )
        for name in request_body
        if name not in standard
    }


def _extract_workflow_block(
    request_body: Mapping[str, Any],
    namespace: str,
) -> dict[str, Any]:
    container = request_body.get("extensions")
    if container is None:
        return {}
    if not isinstance(container, Mapping):
        raise ExtensionError("extensions must be a mapping")
    block = read_field(container, namespace)
    if block is None:
        return {}
    if not isinstance(block, Mapping):
        raise ExtensionError(
            f"extensions.{namespace} must be a mapping of input names to values"
        )
    return dict(block)


def _route_managed_rejections(
    supplied: Mapping[str, Any],
    route_managed: Iterable[str],
) -> tuple[dict[str, Any], dict[str, str]]:
    managed = set(route_managed)
    filtered: dict[str, Any] = {}
    rejected: dict[str, str] = {}
    for name, value in supplied.items():
        if name in managed:
            rejected[name] = f"{name} is managed by the standard top-level request field"
        else:
            filtered[name] = value
    return filtered, rejected


def bind_audio_workflow_extensions(
    request_body: Mapping[str, Any],
    manifest: Mapping[str, Any],
    backend: str,
    backend_request: Mapping[str, Any],
    *,
    namespace: str = WORKFLOW_EXTENSION_NAMESPACE,
    standard_fields: Iterable[str] = STANDARD_AUDIO_GENERATE_FIELDS,
) -> AudioExtensionBinding:
    inputs_spec = manifest.get("inputs")
    if not isinstance(inputs_spec, Mapping):
        raise ExtensionError("manifest must define an 'inputs' mapping")

    supported_names = set(inputs_spec) - ROUTE_MANAGED_AUDIO_INPUTS
    supplied, route_managed_rejected = _route_managed_rejections(
        _extract_workflow_block(request_body, namespace),
        ROUTE_MANAGED_AUDIO_INPUTS,
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
        backend,
        dict(backend_request),
        validation.accepted,
    )
    return AudioExtensionBinding(
        namespace=namespace,
        validation=validation,
        binding=binding,
        unknown_top_level=detect_unknown_audio_fields(request_body, standard_fields),
    )


def record_audio_extension_binding(receipt: Any, result: AudioExtensionBinding) -> None:
    record_extension_result(receipt, result.validation)

    reject = getattr(receipt, "reject_input", None)
    ignore = getattr(receipt, "ignore_input", None)
    if not (callable(reject) and callable(ignore)):
        raise ExtensionError(
            "receipt must expose reject_input/ignore_input methods"
        )

    for name, reason in result.unknown_top_level.items():
        reject(name, reason)

    prefix = f"{result.namespace}." if result.namespace else ""
    for name, reason in result.binding.ignored_inputs.items():
        ignore(f"{prefix}{name}", reason)
