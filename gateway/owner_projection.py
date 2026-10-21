"""Unserved owner-view schema (OWNER_VISIBILITY v3 section 3.3).

Build new typed values, including nested values and envelopes. Names must come
from account/catalog lookups supplied by the caller, never receipt free text.
No route uses this projection until the step-3 visibility switch.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Annotated, Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from gateway.capability_ids import INTENT_CAPABILITIES
from gateway.discovery_metadata import PUBLIC_PROTOCOLS
from gateway.resource_decisions import CALLER_REASONS

OpaqueId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")]
Count = Annotated[int, Field(ge=0)]
Number = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Capability = Literal["chat", "image", "video", "speech", "transcribe", "music", "sfx", "audio", "decide", "embed", "rerank"]
Status = Literal["queued", "running", "succeeded", "failed", "canceled", "unknown"]
Terminal = Literal["cancelled", "completed", "connect_error", "disconnected", "failed", "idle_timeout", "internal_error", "read_error", "unknown"]
_PROTOCOL_VALUES = frozenset({record["id"] for record in PUBLIC_PROTOCOLS} | {"mcp", "gateway", "unknown"})
Protocol = Enum("Protocol", {value: value for value in sorted(_PROTOCOL_VALUES)}, type=str)
ResourceCode = Enum("ResourceCode", {code: code for code in sorted(CALLER_REASONS)}, type=str)


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class NamedIdentity(Schema):
    id: OpaqueId
    name: str  # account lookup only


class CatalogIdentity(Schema):
    catalog_id: OpaqueId | None = None
    name: str = "Unknown model"  # catalog lookup only


class Usage(Schema):
    input_tokens: Count | None = None
    output_tokens: Count | None = None
    images: Count | None = None
    audio_seconds: Number | None = None
    video_seconds: Number | None = None
    characters: Count | None = None


class Cost(Schema):
    amount_usd: Number | None = None
    quality: Literal["measured", "estimated", "unknown"] = "unknown"
    source: Literal["provider", "price_table", "none"] = "none"
    reconciled: bool = False


class Refusal(Schema):
    code: Literal["none", "plan_denied", "resource_denied", "rate_limited", "unknown"] = "none"


class Policy(Schema):
    decision: Literal["allow", "deny"]
    reason: Literal["allowed", "person_disabled", "app_disabled", "plan_denied", "capability_denied", "provider_denied", "spend_limit", "unknown"]


class ResourceDecision(Schema):
    code: ResourceCode
    effect: Literal["rejection", "delay", "info"]
    resource_class: Literal["service", "policy", "queue", "app", "profile", "lock", "gpu", "vram", "cpu", "ram", "disk", "resource"]


class Retention(Schema):
    kind: Literal["library_prompt", "conversation", "request_history", "debug_capture"]
    decision: Literal["kept", "not_kept", "deleted", "expired", "not_requested", "disabled", "unknown"]


class ReceiptView(Schema):
    kind: Literal["receipt"] = "receipt"
    request_id: OpaqueId | None = None
    job_id: OpaqueId | None = None
    attempt: Count | None = None
    created_at: Number | None = None
    queued_at: Number | None = None
    started_at: Number | None = None
    first_output_at: Number | None = None
    finished_at: Number | None = None
    person: NamedIdentity | None = None
    app: NamedIdentity | None = None
    source_kind: Literal["app_key", "session"] | None = None
    protocol: Protocol = Protocol("unknown")
    stream: bool = False
    capability: Capability | None = None
    model: CatalogIdentity = CatalogIdentity()
    workflow: CatalogIdentity = CatalogIdentity()
    route: Literal["local", "provider"] | None = None
    provider: OpaqueId | None = None
    status: Status = "unknown"
    status_code: Count | None = None
    terminal_reason: Terminal = "unknown"
    layer: Literal["request", "plan", "gateway", "engine", "provider", "unknown"] = "unknown"
    latency_ms: Count | None = None
    queue_ms: Count | None = None
    execution_ms: Count | None = None
    first_output_ms: Count | None = None
    usage: Usage = Usage()
    cost: Cost = Cost()
    refusal: Refusal = Refusal()
    policy: Policy | None = None
    resource_decision: tuple[ResourceDecision, ...] = ()
    retention: tuple[Retention, ...] = ()


class JobView(Schema):
    kind: Literal["job"] = "job"
    job_id: OpaqueId | None = None
    capability: Capability | None = None
    model: CatalogIdentity = CatalogIdentity()
    workflow: CatalogIdentity = CatalogIdentity()
    status: Status = "unknown"
    phase: Literal["queued", "running", "preparing", "generating", "saving", "complete", "unknown"] = "unknown"
    attempt: Count | None = None
    created_at: Number | None = None
    updated_at: Number | None = None
    latency_ms: Count | None = None
    count: Count | None = None
    width: Count | None = None
    height: Count | None = None
    duration_s: Number | None = None
    size_bytes: Count | None = None


class StorageView(Schema):
    kind: Literal["storage"] = "storage"
    person: NamedIdentity | None = None
    app: NamedIdentity | None = None
    category: Literal["person", "service_app", "unattributed", "gateway", "disk"]
    media_kind: Literal["image", "video", "audio", "voice", "other"] | None = None
    count: Count | None = None
    size_bytes: Count | None = None
    free_bytes: Count | None = None
    total_bytes: Count | None = None
    reserve_bytes: Count | None = None
    leased_bytes: Count | None = None
    measured_at: Number | None = None
    state: Literal["healthy", "low", "paused", "unknown"] = "unknown"
    stale: bool = False


class ReceiptListView(Schema):
    items: tuple[ReceiptView, ...] = ()
    coverage_start: Number | None = None
    aggregate_complete: bool = False
    unknown_paid_count: Count = 0
    history_note: Literal["complete", "partial", "unavailable"] = "unavailable"


@dataclass(frozen=True)
class ProjectionNames:
    people: Mapping[str, str] = field(default_factory=dict)
    apps: Mapping[str, str] = field(default_factory=dict)
    models: Mapping[str, str] = field(default_factory=dict)
    workflows: Mapping[str, str] = field(default_factory=dict)
    providers: frozenset[str] = frozenset()


def _number(value: Any, *, integer=False):
    import math
    expected = (int,) if integer else (int, float)
    return value if type(value) in expected and value >= 0 and math.isfinite(value) else None


def _enum(value: Any, choices, default=None):
    return value if isinstance(value, str) and value in choices else default


def _identity(value, names):
    return NamedIdentity(id=value, name=names[value]) if isinstance(value, str) and value in names else None


def _catalog(value, names):
    if isinstance(value, str) and value in names:
        return CatalogIdentity(catalog_id=value, name=names[value])
    return CatalogIdentity()


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def _layer(reason, status_code, route):
    if reason == "completed":
        return "unknown"
    if reason in {"connect_error", "read_error", "idle_timeout"}:
        return "provider" if route == "provider" else "engine"
    if reason == "internal_error":
        return "gateway"
    if status_code in {400, 413, 415, 422}:
        return "request"
    if status_code in {401, 403, 429}:
        return "plan"
    return "unknown"


def project(kind: Literal["receipt", "job", "storage"], record: Mapping[str, Any], *, names: ProjectionNames = ProjectionNames()):
    """Project trusted record identity/quantities; unknown free text never falls back.

    ``person_id`` must be the immutable spending subject at submission, not the
    App's current owner. A caller must resolve ids before supplying lookup maps.
    """
    common = {
        "model": _catalog(record.get("resolved_model"), names.models),
        "workflow": _catalog(record.get("workflow_id"), names.workflows),
        "capability": _enum(INTENT_CAPABILITIES.get(_enum(record.get("intent"), INTENT_CAPABILITIES)), get_args(Capability)),
        "status": _enum(record.get("status"), get_args(Status), "unknown"),
        "attempt": _number(record.get("attempt_number"), integer=True),
        "created_at": _number(record.get("created_at")),
        "latency_ms": _number(record.get("latency_ms"), integer=True),
    }
    if kind == "job":
        return JobView(job_id=record.get("job_id"), **common,
                       phase=_enum(record.get("phase"), {"queued", "running", "preparing", "generating", "saving", "complete"}, "unknown"),
                       updated_at=_number(record.get("updated_at")),
                       **{key: _number(record.get(key), integer=key != "duration_s")
                          for key in ("count", "width", "height", "duration_s", "size_bytes")})
    if kind == "storage":
        return StorageView(person=_identity(record.get("person_id"), names.people),
                           app=_identity(record.get("app_id"), names.apps), category=record["category"],
                           media_kind=_enum(record.get("media_kind"), {"image", "video", "audio", "voice", "other"}),
                           state=_enum(record.get("state"), {"healthy", "low", "paused"}, "unknown"),
                           stale=record.get("stale") is True,
                           **{key: _number(record.get(key), integer=key != "measured_at") for key in
                              ("count", "size_bytes", "free_bytes", "total_bytes", "reserve_bytes", "leased_bytes", "measured_at")})
    if kind != "receipt":
        raise ValueError("unknown operational record kind")
    route = _enum(record.get("route"), {"local", "provider"})
    reason = _enum(record.get("terminal_reason"), get_args(Terminal), "unknown")
    status_code = _number(record.get("status_code"), integer=True)
    usage = _mapping(record.get("usage"))
    cost = _mapping(record.get("cost"))
    policy = _mapping(record.get("policy"))
    reasons = _mapping(record.get("resource_decision")).get("reasons", ())
    decisions = []
    for item in reasons if isinstance(reasons, (list, tuple)) else ():
        item = _mapping(item)
        code = _enum(item.get("code"), CALLER_REASONS)
        effect = _enum(item.get("effect"), {"rejection", "delay", "info"})
        resource = _enum(item.get("resource_class"), {"service", "policy", "queue", "app", "profile", "lock", "gpu", "vram", "cpu", "ram", "disk", "resource"})
        if code and effect and resource:
            decisions.append(ResourceDecision(code=ResourceCode(code), effect=effect, resource_class=resource))
    retained = []
    retention = record.get("retention")
    for item in retention if isinstance(retention, (list, tuple)) else ():
        item = _mapping(item)
        rkind = _enum(item.get("kind"), {"library_prompt", "conversation", "request_history", "debug_capture"})
        decision = _enum(item.get("decision"), {"kept", "not_kept", "deleted", "expired", "not_requested", "disabled", "unknown"})
        if rkind and decision:
            retained.append(Retention(kind=rkind, decision=decision))
    policy_decision = _enum(policy.get("decision"), {"allow", "deny"})
    policy_reason = _enum(policy.get("reason"), {"allowed", "person_disabled", "app_disabled", "plan_denied", "capability_denied", "provider_denied", "spend_limit"}, "unknown")
    return ReceiptView(request_id=record.get("request_id"), job_id=record.get("job_id"), **common,
                       person=_identity(record.get("person_id"), names.people), app=_identity(record.get("app_id"), names.apps),
                       source_kind=_enum(record.get("source_kind"), {"app_key", "session"}),
                       protocol=Protocol(_enum(record.get("protocol"), _PROTOCOL_VALUES, "unknown")),
                       stream=record.get("stream") is True, route=route,
                       provider=_enum(record.get("provider"), names.providers),
                       status_code=status_code, terminal_reason=reason, layer=_layer(reason, status_code, route),
                       **{key: _number(record.get(key)) for key in ("queued_at", "started_at", "first_output_at", "finished_at")},
                       **{key: _number(record.get(key), integer=True) for key in ("queue_ms", "execution_ms", "first_output_ms")},
                       usage=Usage(**{key: _number(usage.get(key), integer=key not in {"audio_seconds", "video_seconds"}) for key in Usage.model_fields}),
                       cost=Cost(amount_usd=_number(cost.get("amount_usd")),
                                 quality=_enum(cost.get("quality"), {"measured", "estimated"}, "unknown"),
                                 source=_enum(cost.get("source"), {"provider", "price_table"}, "none"), reconciled=cost.get("reconciled") is True),
                       refusal=Refusal(code=_enum(_mapping(record.get("refusal")).get("code"), {"none", "plan_denied", "resource_denied", "rate_limited"}, "unknown")),
                       policy=Policy(decision=policy_decision, reason=policy_reason) if policy_decision else None,
                       resource_decision=tuple(decisions), retention=tuple(retained))


def project_receipt_list(records, envelope: Mapping[str, Any], *, names: ProjectionNames = ProjectionNames()):
    return ReceiptListView(items=tuple(project("receipt", record, names=names) for record in records),
                           coverage_start=_number(envelope.get("coverage_start")),
                           aggregate_complete=envelope.get("aggregate_complete") is True,
                           unknown_paid_count=_number(envelope.get("unknown_paid_count"), integer=True) or 0,
                           history_note=_enum(envelope.get("history_note"), {"complete", "partial"}, "unavailable"))
