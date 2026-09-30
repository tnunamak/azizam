"""Image-route extension/manifest binding glue.

Bridges the pure ``gateway.extensions`` contract and the pure
``gateway.manifests`` binding helpers for the OpenAI-compatible image routes.

Why this exists:
    The image routes accept the OpenAI image-generation/edit shape (``prompt``,
    ``model``, ``size``, ``n``, ``response_format``, ``background``, ``image``,
    ``mask``, ``user``). Anything else at the top level used to be silently
    dropped, so a caller could pass ``denoise: 0.35`` or
    ``controlnet_strength: 1.1`` and the request would succeed while doing
    nothing — a "silent no-op". This module makes the contract honest:

    - Standard OpenAI fields stay where they are at the top level. They are
      *not* extensions; this module is not in their path.
    - Gateway-native controls (denoise, controlnet strength, sampler, etc.)
      live under ``extensions.com.vividfish.workflow``. They are validated
      against the resolved workflow manifest's ``inputs`` and applied to the
      backend request via the manifest's bindings.
    - Unknown extension names are rejected. Manifest inputs that exist but the
      current binding has not wired up are ignored *with a reason* on the
      receipt. No silent drops.

The module is pure: it takes a request body dict, a workflow manifest dict, a
base backend request dict, and a receipt. It never reads files, calls
networks, or touches FastAPI types. That keeps it testable and reusable across
backends — ComfyUI node maps and dotted-path maps both work because the
underlying ``bind_backend_request`` already handles both.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from gateway.extensions import (
    ExtensionError,
    ExtensionValidationResult,
    extract_extension_namespace,
    record_extension_result,
    validate_extension_inputs,
)
from gateway.manifests import (
    BackendBindingResult,
    bind_backend_request,
)

# Namespace gateway-native workflow controls live under in the request body's
# ``extensions`` container. Reserved namespaces should be dotted to make
# collisions with caller-owned extensions unambiguous.
WORKFLOW_EXTENSION_NAMESPACE = "com.vividfish.workflow"

# Top-level fields the OpenAI image routes officially accept. Anything else at
# the top level is *not* a valid passthrough and must be flagged so callers
# learn to use the extensions envelope instead.
STANDARD_IMAGE_GENERATE_FIELDS: frozenset[str] = frozenset(
    {
        "prompt",
        "model",
        "n",
        "size",
        "response_format",
        "background",
        "user",
        "quality",
        "style",
        "moderation",
        "output_compression",
        "output_format",
        "partial_images",
        "stream",
        "extensions",
    }
)

STANDARD_IMAGE_EDIT_FIELDS: frozenset[str] = frozenset(
    {
        "prompt",
        "model",
        "n",
        "size",
        "response_format",
        "background",
        "user",
        "image",
        "image[]",
        "images",
        "mask",
        "input_fidelity",
        "moderation",
        "output_compression",
        "output_format",
        "partial_images",
        "quality",
        "stream",
        "extensions",
    }
)

# OpenAI's /v1/images/variations is the narrowest of the three image routes:
# no prompt, no mask, none of the gpt-image-1-only fields (background,
# output_format, output_compression, quality). Deliberately not a superset of
# STANDARD_IMAGE_EDIT_FIELDS even though the gateway routes variations through
# an edit-capable workflow internally — a client sending `prompt` or
# `background` to /v1/images/variations should get "unknown parameter", the
# same as it would from OpenAI itself.
STANDARD_IMAGE_VARIATIONS_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "n",
        "size",
        "response_format",
        "user",
        "image",
        "image[]",
        "images",
    }
)


@dataclass
class ImageExtensionBinding:
    """Outcome of resolving extensions and applying them to a backend request.

    ``validation`` carries the pure validation result from
    :func:`gateway.extensions.validate_extension_inputs`. ``binding`` carries
    the backend-side outcome from :func:`gateway.manifests.bind_backend_request`
    — the same provenance shape used by manifest-only callers.

    ``request`` is a freshly-copied backend request with accepted-and-mapped
    extension inputs applied. When validation rejects any input or no
    accepted inputs were mapped, the source request is still returned
    unmodified (deep-copied), so callers may always use ``binding.request``
    as the authoritative backend payload.
    """

    namespace: str
    validation: ExtensionValidationResult
    binding: BackendBindingResult | None
    unknown_top_level: dict[str, str] = field(default_factory=dict)

    @property
    def request(self) -> dict[str, Any] | None:
        return self.binding.request if self.binding is not None else None

    @property
    def ok(self) -> bool:
        return self.validation.ok and not self.unknown_top_level


def detect_unknown_top_level_fields(
    request_body: Mapping[str, Any],
    standard_fields: Iterable[str],
) -> dict[str, str]:
    """Find top-level keys that are neither standard nor the extensions envelope.

    Returns ``{name: reason}`` for top-level keys outside the standard set so
    the caller can refuse a request like ``{"prompt": "x", "denoise": 0.5}``
    instead of silently dropping ``denoise``.

    This is intentionally strict: extensions belong under
    ``extensions.<namespace>``, not at the top level. The fix for a rejected
    top-level field is to move it into the extensions envelope, not to add a
    new top-level passthrough.
    """
    allowed = set(standard_fields)
    unknown: dict[str, str] = {}
    for key in request_body:
        if key in allowed:
            continue
        unknown[str(key)] = (
            "unknown top-level field; move gateway-native controls under "
            f"extensions.{WORKFLOW_EXTENSION_NAMESPACE}"
        )
    return unknown


def resolve_workflow_extensions(
    request_body: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    namespace: str = WORKFLOW_EXTENSION_NAMESPACE,
    supported_names: Iterable[str] | None = None,
) -> ExtensionValidationResult:
    """Extract and validate the workflow extension block against a manifest.

    ``supported_names`` should be the subset of manifest inputs the current
    backend binding has wired up. When omitted, every manifest input is
    treated as supported.

    Raises ``ExtensionError`` only for obvious shape mistakes (non-mapping
    container, non-mapping namespace block, ``supported_names`` not in
    specs). Per-input failures land on the result as ``rejected`` or
    ``ignored``.
    """
    inputs_spec = manifest.get("inputs")
    if not isinstance(inputs_spec, Mapping):
        raise ExtensionError("manifest must define an 'inputs' mapping")

    supplied = extract_extension_namespace(request_body, namespace)
    return validate_extension_inputs(
        inputs_spec,
        supplied,
        namespace=namespace,
        supported_names=supported_names,
    )


def bind_workflow_extensions(
    request_body: Mapping[str, Any],
    manifest: Mapping[str, Any],
    backend: str,
    backend_request: Mapping[str, Any],
    *,
    namespace: str = WORKFLOW_EXTENSION_NAMESPACE,
    supported_names: Iterable[str] | None = None,
    standard_fields: Iterable[str] | None = None,
) -> ImageExtensionBinding:
    """Full pipeline: extract, validate, bind extensions onto a backend request.

    Returns an :class:`ImageExtensionBinding` whose ``binding.request`` is the
    backend-ready payload. The source ``backend_request`` is never mutated.

    The pipeline:

    1. Detect top-level fields that are neither standard nor the extensions
       envelope (``unknown_top_level``). The caller is expected to reject the
       request when this is non-empty.
    2. Pull the namespaced block from ``extensions`` and validate every
       supplied input against the manifest's input specs and the binding's
       supported-name set.
    3. Apply only the accepted inputs to a deep copy of ``backend_request``
       via :func:`bind_backend_request`. Accepted inputs without a backend
       binding land in ``binding.ignored_inputs`` with a reason.
    """
    standard = (
        set(standard_fields) if standard_fields is not None
        else STANDARD_IMAGE_GENERATE_FIELDS
    )
    unknown_top_level = detect_unknown_top_level_fields(request_body, standard)

    validation = resolve_workflow_extensions(
        request_body,
        manifest,
        namespace=namespace,
        supported_names=supported_names,
    )

    binding = bind_backend_request(
        dict(manifest),
        backend,
        dict(backend_request),
        validation.accepted,
    )

    return ImageExtensionBinding(
        namespace=namespace,
        validation=validation,
        binding=binding,
        unknown_top_level=unknown_top_level,
    )


def record_image_extension_binding(
    receipt: Any,
    result: ImageExtensionBinding,
) -> None:
    """Mirror the full binding outcome onto a route receipt.

    Records, in order:

    - extension validation results (accepted/rejected/ignored), namespaced
    - unknown top-level fields as rejections (no namespace prefix; they are
      flat request-body keys)
    - applied-without-backend-mapping inputs as ignored, namespaced

    Only names and reasons are written. Raw input values never reach the
    receipt — they remain in ``result.validation.accepted`` for the caller
    that already trusts them.
    """
    record_extension_result(receipt, result.validation)

    reject = getattr(receipt, "reject_input", None)
    ignore = getattr(receipt, "ignore_input", None)
    if not (callable(reject) and callable(ignore)):
        raise ExtensionError(
            "receipt must expose reject_input/ignore_input methods"
        )

    for name, reason in result.unknown_top_level.items():
        reject(name, reason)

    if result.binding is None:
        return

    prefix = f"{result.namespace}." if result.namespace else ""
    for name, reason in result.binding.ignored_inputs.items():
        # Skip names already recorded by extension validation as ignored — the
        # binding step only sees inputs that validation accepted, so a name
        # appearing here means "accepted by validation but not wired up by the
        # backend binding map". Namespace-prefix to stay consistent with the
        # validation namespace.
        ignore(f"{prefix}{name}", reason)
