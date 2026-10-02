"""Pure request-extension contract for gateway-native workflow controls.

This module gives the gateway a single, data-oriented surface for validating
namespaced request extensions like::

    {
        "extensions": {
            "com.vividfish.workflow": {
                "denoise": 0.35,
                "controlnet_strength": 1.1
            }
        }
    }

The contract has three goals:

- Never silently accept inputs that do nothing. Unknown inputs are rejected
  with a reason; known-but-unsupported inputs are recorded as ignored with a
  reason.
- Stay portable. Input specs follow the JSON-Schema-ish shape used by
  ``gateway.manifests`` (``type``, ``minimum``, ``maximum``, ``enum``,
  ``required``) so a workflow manifest's ``inputs`` block can drive extension
  validation without translation.
- Stay safe to log. Only input names and reasons cross the trust boundary into
  receipts; values stay in the validated result for the adapter that asked for
  validation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from gateway.manifests import validate_input_value
from gateway.provenance import (
    RESERVED_GATEWAY_CONTROLS,
    WORKFLOW_EXTENSION_NAMESPACE as _WORKFLOW_NAMESPACE,
    MetadataModeError,
    parse_metadata_mode,
)


class ExtensionError(ValueError):
    """Raised for caller mistakes (bad envelope shape, bad specs)."""


@dataclass
class ExtensionValidationResult:
    """Outcome of validating one namespace's extension inputs.

    ``accepted`` holds validated values keyed by name. ``rejected`` and
    ``ignored`` hold reasons keyed by name. Names are safe to log; values
    are not stored anywhere except ``accepted``.

    ``gateway_controls`` holds validated gateway-level controls (today only
    ``metadata``). They are not workflow inputs: they never reach a binding.
    """

    namespace: str
    accepted: dict[str, Any] = field(default_factory=dict)
    gateway_controls: dict[str, Any] = field(default_factory=dict)
    rejected: dict[str, str] = field(default_factory=dict)
    ignored: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.rejected


def extract_extension_namespace(
    request_body: Mapping[str, Any],
    namespace: str,
    *,
    container_key: str = "extensions",
) -> dict[str, Any]:
    """Pull one namespaced extension block out of a request body.

    Returns an empty dict if the request carried no ``extensions`` block, or no
    block for this namespace. Raises ``ExtensionError`` if the shapes are
    obviously wrong so callers fail fast instead of silently dropping inputs.
    """
    container = request_body.get(container_key)
    if container is None:
        return {}
    if not isinstance(container, Mapping):
        raise ExtensionError(f"{container_key} must be a mapping")
    block = container.get(namespace)
    if block is None:
        return {}
    if not isinstance(block, Mapping):
        raise ExtensionError(
            f"{container_key}.{namespace} must be a mapping of input names to values"
        )
    return dict(block)


def validate_extension_inputs(
    specs: Mapping[str, Mapping[str, Any]],
    supplied: Mapping[str, Any],
    *,
    namespace: str,
    supported_names: Iterable[str] | None = None,
) -> ExtensionValidationResult:
    """Validate one namespace's extension inputs against typed specs.

    ``specs`` is the workflow manifest's ``inputs`` block (or anything with the
    same JSON-Schema-ish shape). ``supplied`` is the user-provided block for
    one namespace.

    ``supported_names`` lets the caller narrow which spec'd inputs are wired up
    at the current binding. A name that appears in ``specs`` but not in
    ``supported_names`` is recorded as **ignored** with a reason, instead of
    being silently dropped or treated as unknown. This is how a manifest can
    advertise an input that a particular backend binding has not yet wired up.
    """
    try:
        spec_names = set(specs.keys())
    except AttributeError as exc:
        raise ExtensionError("specs must be a mapping of name -> spec") from exc

    supported = spec_names if supported_names is None else set(supported_names)
    unknown_supported = supported - spec_names
    if unknown_supported:
        raise ExtensionError(
            f"supported_names references inputs not in specs: {sorted(unknown_supported)}"
        )

    result = ExtensionValidationResult(namespace=namespace)

    for name, value in supplied.items():
        if namespace == _WORKFLOW_NAMESPACE and name in RESERVED_GATEWAY_CONTROLS:
            # Gateway-level control: valid on every workflow, owned by no manifest.
            try:
                result.gateway_controls[name] = parse_metadata_mode(value)
            except MetadataModeError as exc:
                result.rejected[name] = str(exc)
            continue
        spec = specs.get(name)
        if spec is None:
            result.rejected[name] = "unknown input"
            continue
        if name not in supported:
            result.ignored[name] = "input not supported by current binding"
            continue
        if not isinstance(spec, Mapping):
            raise ExtensionError(f"spec for input {name!r} must be a mapping")
        reason = validate_input_value(name, value, dict(spec))
        if reason is None:
            result.accepted[name] = value
        else:
            result.rejected[name] = reason

    for name, spec in specs.items():
        if name in supplied:
            continue
        if not isinstance(spec, Mapping):
            raise ExtensionError(f"spec for input {name!r} must be a mapping")
        if spec.get("required") and name in supported:
            result.rejected[name] = "required input missing"

    return result


def record_extension_result(receipt: Any, result: ExtensionValidationResult) -> None:
    """Mirror an extension validation result onto a route receipt.

    Uses duck typing against ``RouteReceipt``'s ``accept_input``,
    ``reject_input``, ``ignore_input`` methods so this module does not import
    receipts. Names are namespaced (``"<namespace>.<name>"``) so multiple
    extension blocks on one request stay distinguishable in receipts without
    leaking values.
    """
    accept = getattr(receipt, "accept_input", None)
    reject = getattr(receipt, "reject_input", None)
    ignore = getattr(receipt, "ignore_input", None)
    if not (callable(accept) and callable(reject) and callable(ignore)):
        raise ExtensionError(
            "receipt must expose accept_input/reject_input/ignore_input methods"
        )

    prefix = f"{result.namespace}." if result.namespace else ""
    for name in (*result.accepted, *result.gateway_controls):
        accept(f"{prefix}{name}")
    for name, reason in result.rejected.items():
        reject(f"{prefix}{name}", reason)
    for name, reason in result.ignored.items():
        ignore(f"{prefix}{name}", reason)
