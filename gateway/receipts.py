"""Route receipt helpers.

Receipts record decisions, routing, usage and spend facts without raw request
bodies or secrets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from time import perf_counter, time
import asyncio
from collections.abc import AsyncIterator
from typing import Any, Callable, Mapping
from uuid import uuid4

from gateway.model_ids import display_model_id
from gateway.resource_decisions import project_decision
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
    "stream",
    "remote",
    "cost",
    "usage",
    "source",
    "source_kind",
    "source_label",
    "client_hint",
    "effective_output_limit",
    "accepted_at_ms",
    "updated_at_ms",
    "refusal",
}

# Machine-readable refusal facts a client may see on its own receipt. Codes
# only: no amounts, provider names or other people's usage.
REFUSAL_CODES = frozenset({"budget_exceeded", "provider_needs_reconciliation"})
REFUSAL_PERIODS = frozenset({"daily", "monthly", "request"})
REFUSAL_SCOPES = frozenset({"plan", "provider", "global"})


def sanitize_refusal(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or value.get("code") not in REFUSAL_CODES:
        return None
    limit = value.get("limit")
    if (isinstance(limit, Mapping) and limit.get("period") in REFUSAL_PERIODS
            and limit.get("scope") in REFUSAL_SCOPES):
        return {"code": value["code"], "limit": {"period": limit["period"], "scope": limit["scope"]}}
    return {"code": value["code"], "limit": None}

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
    "stt",
    "decisions",
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


def _sanitize_decision_metadata(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    count = value.get("question_count")
    if type(count) is not int or count < 0:
        return None
    remote = value.get("remote")
    if type(remote) is not bool:
        return None
    raw_cost = value.get("cost_usd")
    if remote:
        if raw_cost is None:
            cost = None
        elif isinstance(raw_cost, (str, int, float)) and not isinstance(raw_cost, bool):
            try:
                amount = Decimal(str(raw_cost))
            except InvalidOperation:
                return None
            if not amount.is_finite() or amount < 0:
                return None
            cost = str(amount)
        else:
            return None
    else:
        if raw_cost != 0:
            return None
        cost = 0.0
    engine = value.get("engine")
    if engine not in (None, "cpu", "gpu") or (remote and engine is not None):
        return None
    out = {"question_count": count, "engine": engine, "remote": remote, "cost_usd": cost}
    usage = value.get("usage")
    if isinstance(usage, Mapping):
        safe_usage = {key: usage[key] for key in ("input_tokens", "output_tokens")
                      if type(usage.get(key)) is int and usage[key] >= 0}
        if safe_usage:
            out["usage"] = safe_usage
    return out


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
    stt = metadata.get("stt")
    if isinstance(stt, Mapping):
        field_count = stt.get("field_count")
        file_count = stt.get("file_count")
        attempts_started = stt.get("attempts_started")
        chunked = stt.get("chunked")
        if (
            all(isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in (field_count, file_count, attempts_started))
            and isinstance(chunked, bool)
        ):
            out["stt"] = {
                "field_count": field_count,
                "file_count": file_count,
                "attempts_started": attempts_started,
                "chunked": chunked,
            }
    decisions = _sanitize_decision_metadata(metadata.get("decisions"))
    if decisions:
        out["decisions"] = decisions
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
    source_kind: str | None = None
    source_label: str | None = None
    client_hint: str | None = None
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
    stream: bool = False
    remote: bool = False
    cost: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    effective_output_limit: int | None = None
    refusal: dict[str, Any] | None = None
    resource_decision: dict[str, Any] | None = None
    accepted_at_ms: int | None = None
    updated_at_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    _started_at: float = field(default_factory=perf_counter, repr=False)
    owner_key_hash: str | None = None

    def __post_init__(self) -> None:
        from gateway.request_source import current_client_hint, current_gateway_ui

        if self.source_kind is not None:
            return
        if self.parent_request_id:
            self.source_kind = "internal"
            self.source_label = "Gateway internal request"
            return
        if current_gateway_ui():
            self.source_kind = "gateway-ui"
            self.source_label = "Gateway UI"
            return
        self.client_hint = self.client_hint or current_client_hint()
        if self.app_id:
            self.source_kind = "app"
            self.source_label = self.api_key_name or "App"
        else:
            self.source_kind = "local-network"
            self.source_label = "Local network · no key"
        if self.client_hint:
            self.source_label += f" · {self.client_hint}"

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

    def set_refusal(self, refusal: Mapping[str, Any]) -> None:
        """Record why the gateway refused this request (see sanitize_refusal)."""
        self.refusal = sanitize_refusal(refusal)

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

    def begin(self, *, now_ms: int | None = None) -> None:
        """Mark this receipt accepted before any upstream operation begins."""
        timestamp = int(now_ms if now_ms is not None else time() * 1000)
        self.status = "in_progress"
        self.accepted_at_ms = timestamp
        self.updated_at_ms = timestamp

    def finish(
        self,
        status: str,
        latency_ms: float | None = None,
        *,
        now_ms: int | None = None,
    ) -> None:
        self.status = status
        if latency_ms is None:
            latency_ms = (perf_counter() - self._started_at) * 1000
        self.latency_ms = round(latency_ms, 3)
        self.updated_at_ms = int(now_ms if now_ms is not None else time() * 1000)

    def to_dict(self, audience: str = "internal") -> dict[str, Any]:
        # Some facades attach their resolved App after constructing the receipt.
        # Derive its source from that final identity before persisting it.
        if self.app_id and self.source_kind == "local-network":
            self.source_kind = "app"
            self.source_label = self.api_key_name or "App"
            if self.client_hint:
                self.source_label += f" · {self.client_hint}"
        data = {
            "request_id": self.request_id,
            "protocol": self.protocol,
            "path": self.path,
            "intent": self.intent,
            "app_profile": self.app_profile,
            "app_id": self.app_id,
            "source_kind": self.source_kind,
            "source_label": self.source_label,
            "client_hint": self.client_hint,
            "owner_key_hash": self.owner_key_hash,
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
            "stream": self.stream,
            "remote": self.remote,
            "cost": dict(self.cost) if isinstance(self.cost, Mapping) else self.cost,
            "usage": dict(self.usage) if isinstance(self.usage, Mapping) else self.usage,
            "effective_output_limit": self.effective_output_limit,
            "accepted_at_ms": self.accepted_at_ms,
            "updated_at_ms": self.updated_at_ms,
            "metadata": dict(self.metadata),
        }
        if self.resource_decision is not None:
            data["resource_decision"] = self.resource_decision
        if isinstance(self.refusal, Mapping):
            # Present only on refused requests; absent means none recorded.
            data["refusal"] = dict(self.refusal)
        return serialize_receipt_for_audience(data, audience)


def serialize_receipt_for_audience(data: Mapping[str, Any], audience: str = "internal") -> dict[str, Any]:
    if audience not in {"internal", "admin", "client"}:
        raise ValueError("audience must be internal, admin, or client")
    if audience == "internal":
        return dict(data)
    if audience == "admin":
        out = dict(data)
        out.pop("owner_key_hash", None)
        metadata = out.pop("metadata", None)
        out.update(sanitize_admin_metadata(metadata))
        if isinstance(data.get("resource_decision"), Mapping):
            out["resource_decision"] = project_decision(data["resource_decision"], "admin")
        return out
    client_data = {name: data[name] for name in CLIENT_VISIBLE_FIELDS if name in data}
    if isinstance(data.get("resource_decision"), Mapping):
        client_data["resource_decision"] = project_decision(data["resource_decision"])
    for model_field in ("requested_model", "resolved_model"):
        if isinstance(client_data.get(model_field), str):
            client_data[model_field] = display_model_id(client_data[model_field])
    if "refusal" in client_data:
        refusal = sanitize_refusal(client_data.pop("refusal"))
        if refusal is not None:
            client_data["refusal"] = refusal
    cost = client_data.get("cost")
    if isinstance(cost, Mapping):
        client_data["cost"] = {
            name: value
            for name, value in cost.items()
            if name in {"usd", "source", "cost_class", "reconciliation_status"}
        }
    metadata = data.get("metadata")
    decisions = _sanitize_decision_metadata(metadata.get("decisions")) if isinstance(metadata, Mapping) else {}
    if decisions:
        client_data["decisions"] = decisions
    return client_data


class ReceiptLifecycleOwner:
    """Own the terminal lifecycle of one admitted outer receipt.

    ``report`` only records the bounded normal outcome.  ``__aexit__`` and
    ``wrap_stream`` are the sole terminal writers: cancellation always wins
    over a reported normal outcome, ordinary BaseException is failed, and the
    store callback is deliberately best-effort.
    """

    def __init__(
        self,
        receipt: RouteReceipt,
        *,
        begin: Callable[[RouteReceipt], object] | None = None,
        finalize: Callable[[RouteReceipt], object] | None = None,
        log: Callable[[str], object] | None = None,
        on_terminal: Callable[[RouteReceipt, str], object] | None = None,
    ) -> None:
        self.receipt = receipt
        # Delayed import avoids the receipt_store -> receipts import cycle and
        # keeps persistence ownership here rather than in each HTTP route.
        if begin is None or finalize is None:
            from gateway import receipt_store
            begin = begin or receipt_store.begin
            finalize = finalize or receipt_store.finalize
        self._begin = begin
        self._finalize = finalize
        self._log = log or (lambda message: print(f"[proxy] {message}", flush=True))
        self._on_terminal = on_terminal
        self._outcome: tuple[str, str] | None = None
        self._closed = False
        self._stream_transferred = False
        # Admission is deliberately synchronous: SQLite receipt writes are
        # synchronous, and no caller can accidentally put an await boundary
        # between construction and persistence.
        self._begin_once()

    def _begin_once(self) -> None:
        try:
            self._begin(self.receipt)
        except Exception as error:
            self._log(f"receipt begin failed: {error}")

    def report(self, *, status: str, reason: str) -> None:
        """Describe a normal route outcome; this never persists a terminal row."""
        self._outcome = (status, reason)

    def _close(self, status: str, reason: str) -> None:
        if self._closed:
            return
        self._closed = True
        self.receipt.metadata["terminal_reason"] = reason
        if self._on_terminal is not None:
            try:
                self._on_terminal(self.receipt, reason)
            except Exception as error:
                self._log(f"receipt terminal annotation failed: {error}")
        self.receipt.finish(status)
        try:
            self._finalize(self.receipt)
        except Exception as error:
            self._log(f"receipt finalize failed: {error}")

    async def __aenter__(self) -> "ReceiptLifecycleOwner":
        return self

    async def __aexit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        if self._stream_transferred:
            return False
        if isinstance(exc, asyncio.CancelledError):
            self._close("cancelled", "cancelled")
        elif exc is not None:
            self._close("failed", "internal_error")
        else:
            self._close(*(self._outcome or ("failed", "internal_error")))
        return False

    def handoff_stream(self, source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Detach a handler scope and retain ownership in the response body."""
        if self._closed:
            raise RuntimeError("cannot hand off a closed receipt lifecycle")
        self._stream_transferred = True
        return self.wrap_stream(source)

    async def wrap_stream(self, source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Transfer lifecycle ownership past a StreamingResponse return."""
        # The handler's __aexit__ observes this flag and leaves ownership to
        # the generator.  Once the generator starts, its own scope must close.
        self._stream_transferred = False
        async with self:
            async for item in source:
                yield item


def record_validation_result(receipt: RouteReceipt, result: Any) -> None:
    """Attach a manifest validation result without coupling receipt imports."""
    for name in getattr(result, "accepted", {}):
        receipt.accept_input(name)
    for name, reason in getattr(result, "rejected", {}).items():
        receipt.reject_input(name, reason)
