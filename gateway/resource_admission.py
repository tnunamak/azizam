"""Pure resource availability advice for gateway route admission.

This module intentionally does not manage services, queues, or workers. Callers
provide the current facts they already know, and receive a small decision object
that can be logged, surfaced, or translated into an HTTP response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from gateway.resource_decisions import CapacityFact, DecisionDetails, DecisionReason, Quality


class AdmissionOutcome(str, Enum):
    """Route-level resource admission outcome."""

    ALLOWED = "allowed"
    DEGRADED = "degraded"
    DENIED = "denied"


@dataclass(frozen=True)
class ServiceAvailability:
    """Caller-supplied service availability facts."""

    available: bool | None = None
    degraded: bool = False
    retry_after: int | None = None
    reasons: tuple[str, ...] = ()
    observed_at: float | None = None
    quality: Quality = "declared"

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", _clean_reasons(self.reasons))
        object.__setattr__(self, "retry_after", _clean_retry_after(self.retry_after))


@dataclass(frozen=True)
class ResourceEstimate:
    """Resources a request expects to need."""

    intent: str
    app_id: str | None = None
    resource_class: str | None = None
    queue_key: str | None = None
    requires_gpu: bool = False
    required_tokens: tuple[str, ...] = ()
    optional_tokens: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent", self.intent.strip())
        object.__setattr__(self, "app_id", _clean_optional(self.app_id))
        object.__setattr__(self, "resource_class", _clean_optional(self.resource_class))
        object.__setattr__(self, "queue_key", _clean_optional(self.queue_key))
        object.__setattr__(self, "required_tokens", _clean_tokens(self.required_tokens))
        object.__setattr__(self, "optional_tokens", _clean_tokens(self.optional_tokens))
        object.__setattr__(self, "reasons", _clean_reasons(self.reasons))


@dataclass(frozen=True)
class AdmissionConstraints:
    """Current mode/profile limits relevant to a single admission check."""

    disabled_intents: tuple[str, ...] = ()
    disabled_resource_classes: tuple[str, ...] = ()
    reserved_tokens: tuple[str, ...] = ()
    max_queue_depth: int | None = None
    max_app_concurrency: Mapping[str, int] = field(default_factory=dict)
    retry_after: int | None = None
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "disabled_intents", _clean_tokens(self.disabled_intents))
        object.__setattr__(
            self,
            "disabled_resource_classes",
            _clean_tokens(self.disabled_resource_classes),
        )
        object.__setattr__(self, "reserved_tokens", _clean_tokens(self.reserved_tokens))
        object.__setattr__(self, "max_queue_depth", _clean_limit(self.max_queue_depth))
        object.__setattr__(
            self,
            "max_app_concurrency",
            {
                _clean_key(key): _clean_limit(value)
                for key, value in self.max_app_concurrency.items()
            },
        )
        object.__setattr__(self, "retry_after", _clean_retry_after(self.retry_after))
        object.__setattr__(self, "reasons", _clean_reasons(self.reasons))


@dataclass(frozen=True)
class CurrentLoad:
    """Caller-supplied queue and per-app load counters."""

    queue_depths: Mapping[str, int | None] = field(default_factory=dict)
    active_by_app: Mapping[str, int | None] = field(default_factory=dict)
    retry_after: int | None = None
    observed_at: float | None = None
    quality: Quality = "declared"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "queue_depths",
            {_clean_key(key): None if value is None else max(0, int(value)) for key, value in self.queue_depths.items()},
        )
        object.__setattr__(
            self,
            "active_by_app",
            {_clean_key(key): None if value is None else max(0, int(value)) for key, value in self.active_by_app.items()},
        )
        object.__setattr__(self, "retry_after", _clean_retry_after(self.retry_after))


@dataclass(frozen=True)
class ResourceAdmissionDecision:
    """Data-only admission result."""

    outcome: AdmissionOutcome
    retry_after: int | None = None
    reasons: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()
    details: DecisionDetails = field(default_factory=DecisionDetails)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", _clean_reasons(self.reasons))
        object.__setattr__(self, "reason_codes", _clean_tokens(self.reason_codes))

    @property
    def allowed(self) -> bool:
        return self.outcome is not AdmissionOutcome.DENIED

    @property
    def degraded(self) -> bool:
        return self.outcome is AdmissionOutcome.DEGRADED

    @property
    def denied(self) -> bool:
        return self.outcome is AdmissionOutcome.DENIED

    def as_dict(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "allowed": self.allowed,
            "retry_after": self.retry_after,
            "reasons": self.reasons,
            "reason_codes": self.reason_codes,
        }


def advise_resource_admission(
    service: ServiceAvailability,
    estimate: ResourceEstimate,
    constraints: AdmissionConstraints | None = None,
    load: CurrentLoad | None = None,
) -> ResourceAdmissionDecision:
    """Return an admission decision from explicit caller-supplied facts."""

    constraints = constraints or AdmissionConstraints()
    load = load or CurrentLoad()
    denied: list[str] = []
    denied_codes: list[str] = []
    degraded: list[str] = []
    degraded_codes: list[str] = []
    reasons: list[DecisionReason] = []
    facts: list[CapacityFact] = []
    retry_after_values: list[int] = []

    def record(code: str, effect: str, resource: str, rules: tuple[str, ...],
               observed_at: float | None = None, quality: Quality = "declared") -> None:
        (denied if effect == "rejection" else degraded).extend(rules)
        (denied_codes if effect == "rejection" else degraded_codes).append(code)
        reasons.extend(DecisionReason(code, effect, resource, rule, observed_at, quality) for rule in rules)

    availability = CapacityFact("available", "service", service.available, service.observed_at, service.quality)
    facts.append(availability)
    facts.append(CapacityFact("degraded", "service", service.degraded, service.observed_at, service.quality))
    if availability.value is None:
        record("service_unknown", "rejection", "service", ("service availability is not advertised",),
               service.observed_at, "unavailable")
        _append_retry_after(retry_after_values, service.retry_after)
    elif not availability.value:
        record("service_unavailable", "rejection", "service", service.reasons or ("service unavailable",),
               service.observed_at, service.quality)
        _append_retry_after(retry_after_values, service.retry_after)
    elif service.degraded:
        record("service_degraded", "delay", "service", service.reasons or ("service degraded",),
               service.observed_at, service.quality)
        _append_retry_after(retry_after_values, service.retry_after)

    for name, value in (("disabled_intents", constraints.disabled_intents),
                        ("disabled_resource_classes", constraints.disabled_resource_classes),
                        ("reserved_tokens", constraints.reserved_tokens),
                        ("required_tokens", estimate.required_tokens),
                        ("optional_tokens", estimate.optional_tokens),
                        ("requires_gpu", estimate.requires_gpu)):
        facts.append(CapacityFact(name, "policy", value))
    if estimate.intent in constraints.disabled_intents:
        record("intent_disabled", "rejection", "policy", (f"intent disabled: {estimate.intent}",))
    if estimate.resource_class and estimate.resource_class in constraints.disabled_resource_classes:
        record("resource_class_disabled", "rejection", "policy", (f"resource class disabled: {estimate.resource_class}",))
    reserved_required = _intersection(estimate.required_tokens, constraints.reserved_tokens)
    if reserved_required:
        record("required_resource_reserved", "rejection", "resource",
               ("required resource reserved: " + ", ".join(reserved_required),))
    reserved_optional = _intersection(estimate.optional_tokens, constraints.reserved_tokens)
    if reserved_optional:
        record("optional_resource_reserved", "delay", "resource",
               ("optional resource reserved: " + ", ".join(reserved_optional),))
    if estimate.requires_gpu and "gpu" in constraints.reserved_tokens:
        record("gpu_reserved", "rejection", "gpu", ("gpu reserved",))

    for resource, key, limit, counters, full_code, full_rule in (
        ("queue", estimate.queue_key, constraints.max_queue_depth, load.queue_depths,
         "queue_full", f"queue full: {estimate.queue_key}"),
        ("app", estimate.app_id, constraints.max_app_concurrency.get(estimate.app_id), load.active_by_app,
         "app_concurrency_full", f"app concurrency full: {estimate.app_id}"),
    ):
        if key is None or limit is None:
            continue
        count = CapacityFact(f"{resource}_active", resource, counters.get(key), load.observed_at, load.quality)
        facts.extend((count, CapacityFact(f"{resource}_limit", resource, limit)))
        if count.value is None:
            record(f"{resource}_load_unknown", "rejection", resource, (f"{resource} load is not advertised",),
                   load.observed_at, "unavailable")
            _append_retry_after(retry_after_values, load.retry_after)
        elif count.value >= limit:
            record(full_code, "rejection", resource, (full_rule,), load.observed_at, load.quality)
            _append_retry_after(retry_after_values, load.retry_after)

    _append_retry_after(retry_after_values, constraints.retry_after)
    reasons.extend(DecisionReason("estimate_reason", "info", "resource", rule) for rule in estimate.reasons)
    reasons.extend(DecisionReason("constraint_reason", "info", "policy", rule) for rule in constraints.reasons)
    details = DecisionDetails(tuple(reasons), tuple(facts))
    # Preserve the legacy summaries, including their precedence and prose.
    # All concurrent causes live in details, including on a denied decision.
    if denied:
        if constraints.reasons:
            denied_codes.append("constraint_reason")
        return ResourceAdmissionDecision(AdmissionOutcome.DENIED, _smallest_retry_after(retry_after_values),
                                         _unique((*denied, *constraints.reasons)), _unique(tuple(denied_codes)), details)
    if degraded:
        if estimate.reasons:
            degraded_codes.append("estimate_reason")
        if constraints.reasons:
            degraded_codes.append("constraint_reason")
        return ResourceAdmissionDecision(AdmissionOutcome.DEGRADED, _smallest_retry_after(retry_after_values),
                                         _unique((*degraded, *estimate.reasons, *constraints.reasons)),
                                         _unique(tuple(degraded_codes)), details)
    return ResourceAdmissionDecision(AdmissionOutcome.ALLOWED, None, estimate.reasons or ("resources available",),
                                     ("estimate_reason",) if estimate.reasons else ("resources_available",), details)


def _append_retry_after(values: list[int], retry_after: int | None) -> None:
    if retry_after is not None:
        values.append(retry_after)


def _smallest_retry_after(values: list[int]) -> int | None:
    return min(values) if values else None


def _intersection(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    right_set = set(right)
    return tuple(item for item in left if item in right_set)


def _clean_tokens(values: tuple[str, ...]) -> tuple[str, ...]:
    return _unique(tuple(value.strip() for value in values if value and value.strip()))


def _clean_reasons(values: tuple[str, ...]) -> tuple[str, ...]:
    return _clean_tokens(values)


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _clean_key(value: str) -> str:
    return value.strip()


def _clean_limit(value: int | None) -> int | None:
    if value is None:
        return None
    return max(0, int(value))


def _clean_retry_after(value: int | None) -> int | None:
    if value is None:
        return None
    return max(0, int(value))


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)
