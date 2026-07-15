"""Route receipt helpers.

Receipts are compact diagnostics. They record decisions and routing facts, not
raw request bodies or secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Mapping
from uuid import uuid4

from gateway.backends.request_shape import (
    TERMINAL_REASONS,
    sanitize_request_shape_fields_for_admin,
    sanitize_request_shape_stages_for_admin,
)

CLIENT_VISIBLE_FIELDS = {
    "request_id",
    "protocol",
    "path",
    "intent",
    "requested_model",
    "resolved_model",
    "requested_workflow",
    "resolved_workflow",
    "accepted_inputs",
    "rejected_inputs",
    "ignored_inputs",
    "fallback_reasons",
    "warnings",
    "status",
    "latency_ms",
    "job_id",
}

# Metadata keys promoted to top-level fields for the "admin" audience even
# though the rest of ``metadata`` is hidden there (a free-text ops note is
# not assumed operator-safe just because a caller happened to write one
# today). These specific keys are written only by the request-shape/staged
# observability path (gateway/backends/request_shape.py) and its terminal
# outcome — always structural, never a raw note — so they're safe to
# special-case rather than trust the whole metadata dict.
ADMIN_VISIBLE_METADATA_FIELDS = {
    "request_shape",
    "request_shape_stages",
    "terminal_reason",
    "litellm",
    "status_code",
}

# Canonical allowlist for metadata["litellm"] — mirrors the keys
# proxy.py's _attach_litellm_response_metadata already restricts itself to
# (small bounded scalars: a model name/group, a cost, a duration, a
# retry/fallback count). Deliberately excludes any call-id/URL-shaped key —
# see independent-gate-3.md finding 4 (admin_model_api_base,
# admin_call_id_length). Re-declared here, not imported from proxy.py, so
# this module has no dependency on the application entrypoint; the two
# lists are kept in sync manually since call sites for each rarely change.
_LITELLM_ADMIN_SAFE_FIELDS = frozenset(
    {
        "model_id",
        "model_group",
        "response_cost",
        "response_duration_ms",
        "attempted_retries",
        "attempted_fallbacks",
    }
)
_LITELLM_ADMIN_MAX_VALUE_LENGTH = 128


def _sanitize_litellm_metadata_for_admin(value: Any) -> dict[str, str] | None:
    if not isinstance(value, Mapping):
        return None
    out: dict[str, str] = {}
    for key in _LITELLM_ADMIN_SAFE_FIELDS:
        raw = value.get(key)
        if isinstance(raw, str) and raw:
            out[key] = raw[:_LITELLM_ADMIN_MAX_VALUE_LENGTH]
    return out or None


def sanitize_admin_metadata(metadata: Any) -> dict[str, Any]:
    """Re-parse ``RouteReceipt.metadata`` for the admin audience.

    This is the single trust boundary for what an admin-audience receipt
    view may expose from ``metadata`` — every field is re-derived from a
    closed per-field schema, independent of whatever internal callers
    happened to write into ``metadata``. See
    ``gateway/backends/request_shape.py`` for the stage/shape schemas this
    delegates to, and independent-gate-3.md finding 4 for the exploit this
    replaces (verbatim promotion of unvalidated nested metadata).
    """
    if not isinstance(metadata, Mapping):
        return {}
    out: dict[str, Any] = {}
    shape = sanitize_request_shape_fields_for_admin(metadata.get("request_shape"))
    if shape:
        out["request_shape"] = shape
    stages = sanitize_request_shape_stages_for_admin(metadata.get("request_shape_stages"))
    if stages:
        out["request_shape_stages"] = stages
    terminal_reason = metadata.get("terminal_reason")
    if isinstance(terminal_reason, str) and terminal_reason in TERMINAL_REASONS:
        out["terminal_reason"] = terminal_reason
    status_code = metadata.get("status_code")
    if isinstance(status_code, int) and not isinstance(status_code, bool):
        out["status_code"] = status_code
    litellm_meta = _sanitize_litellm_metadata_for_admin(metadata.get("litellm"))
    if litellm_meta:
        out["litellm"] = litellm_meta
    return out


def new_request_id() -> str:
    return f"req_{uuid4().hex}"


@dataclass
class RouteReceipt:
    protocol: str
    intent: str
    path: str = ""
    request_id: str = field(default_factory=new_request_id)
    app_profile: str | None = None
    # ``app_id`` is the durable id of the owning App (when the request was
    # authenticated by an App credential). It is the safe owner key for the
    # user-facing activity feed: ``app_profile`` is a shared permission-profile
    # id and ``api_key_name`` is a non-unique App name, so neither can scope a
    # feed to one user. None when there is no owning App (unauthenticated,
    # legacy config keys, or provider-native passthrough without identity).
    app_id: str | None = None
    api_key_name: str | None = None
    requested_model: str | None = None
    resolved_model: str | None = None
    requested_workflow: str | None = None
    resolved_workflow: str | None = None
    backend: str | None = None
    manifest_id: str | None = None
    manifest_version: str | None = None
    manifest_hash: str | None = None
    job_id: str | None = None
    parent_request_id: str | None = None
    visibility: str = "internal"
    policy_decision: str | None = None
    policy_sources: list[str] = field(default_factory=list)
    policy_reasons: list[str] = field(default_factory=list)
    accepted_inputs: list[str] = field(default_factory=list)
    rejected_inputs: dict[str, str] = field(default_factory=dict)
    ignored_inputs: dict[str, str] = field(default_factory=dict)
    fallback_reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    status: str | None = None
    latency_ms: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    _started_at: float = field(default_factory=perf_counter, repr=False)

    def accept_input(self, name: str) -> None:
        if name not in self.accepted_inputs:
            self.accepted_inputs.append(name)
        self.rejected_inputs.pop(name, None)
        self.ignored_inputs.pop(name, None)

    def reject_input(self, name: str, reason: str) -> None:
        if name in self.accepted_inputs:
            self.accepted_inputs.remove(name)
        self.rejected_inputs[name] = reason
        self.ignored_inputs.pop(name, None)

    def ignore_input(self, name: str, reason: str) -> None:
        if name in self.accepted_inputs:
            self.accepted_inputs.remove(name)
        self.ignored_inputs[name] = reason
        self.rejected_inputs.pop(name, None)

    def add_warning(self, warning: str) -> None:
        self.warnings.append(warning)

    def add_fallback_reason(self, reason: str) -> None:
        self.fallback_reasons.append(reason)

    def set_policy_decision(
        self,
        decision: str,
        *,
        reason: str | None = None,
        source: str | None = None,
    ) -> None:
        if decision not in {"allowed", "denied", "not_evaluated"}:
            raise ValueError("policy decision must be allowed, denied, or not_evaluated")
        self.policy_decision = decision
        if reason:
            self.policy_reasons.append(reason)
        if source:
            self.policy_sources.append(source)

    def attach_manifest(
        self,
        *,
        manifest_id: str,
        manifest_version: str | None = None,
        manifest_hash: str | None = None,
    ) -> None:
        self.manifest_id = manifest_id
        self.manifest_version = manifest_version
        self.manifest_hash = manifest_hash

    def finish(self, status: str, latency_ms: float | None = None) -> None:
        self.status = status
        if latency_ms is None:
            latency_ms = (perf_counter() - self._started_at) * 1000
        self.latency_ms = round(latency_ms, 3)

    def to_dict(self, audience: str = "internal") -> dict[str, Any]:
        if audience not in {"internal", "admin", "client"}:
            raise ValueError("audience must be internal, admin, or client")

        data = {
            "request_id": self.request_id,
            "protocol": self.protocol,
            "path": self.path,
            "intent": self.intent,
            "app_profile": self.app_profile,
            "app_id": self.app_id,
            "api_key_name": self.api_key_name,
            "requested_model": self.requested_model,
            "resolved_model": self.resolved_model,
            "requested_workflow": self.requested_workflow,
            "resolved_workflow": self.resolved_workflow,
            "backend": self.backend,
            "manifest_id": self.manifest_id,
            "manifest_version": self.manifest_version,
            "manifest_hash": self.manifest_hash,
            "job_id": self.job_id,
            "parent_request_id": self.parent_request_id,
            "visibility": self.visibility,
            "policy_decision": self.policy_decision,
            "policy_sources": list(self.policy_sources),
            "policy_reasons": list(self.policy_reasons),
            "accepted_inputs": list(self.accepted_inputs),
            "rejected_inputs": dict(self.rejected_inputs),
            "ignored_inputs": dict(self.ignored_inputs),
            "fallback_reasons": list(self.fallback_reasons),
            "warnings": list(self.warnings),
            "status": self.status,
            "latency_ms": self.latency_ms,
            "metadata": dict(self.metadata),
        }
        if audience == "internal":
            return data
        if audience == "admin":
            data = dict(data)
            metadata = data.pop("metadata", None)
            data.update(sanitize_admin_metadata(metadata))
            return data
        return {name: data[name] for name in CLIENT_VISIBLE_FIELDS if name in data}


def record_validation_result(receipt: RouteReceipt, result: Any) -> None:
    """Attach a manifest validation result without coupling receipt imports."""
    for name in getattr(result, "accepted", {}):
        receipt.accept_input(name)
    for name, reason in getattr(result, "rejected", {}).items():
        receipt.reject_input(name, reason)
