"""Pure resource availability advice for gateway route admission.

This module intentionally does not manage services, queues, or workers. Callers
provide the current facts they already know, and receive a small decision object
that can be logged, surfaced, or translated into an HTTP response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class AdmissionOutcome(str, Enum):
    """Route-level resource admission outcome."""

    ALLOWED = "allowed"
    DEGRADED = "degraded"
    DENIED = "denied"


@dataclass(frozen=True)
class ServiceAvailability:
    """Caller-supplied service availability facts."""

    available: bool = True
    degraded: bool = False
    retry_after: int | None = None
    reasons: tuple[str, ...] = ()

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

    queue_depths: Mapping[str, int] = field(default_factory=dict)
    active_by_app: Mapping[str, int] = field(default_factory=dict)
    retry_after: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "queue_depths",
            {_clean_key(key): max(0, int(value)) for key, value in self.queue_depths.items()},
        )
        object.__setattr__(
            self,
            "active_by_app",
            {_clean_key(key): max(0, int(value)) for key, value in self.active_by_app.items()},
        )
        object.__setattr__(self, "retry_after", _clean_retry_after(self.retry_after))


@dataclass(frozen=True)
class ResourceAdmissionDecision:
    """Data-only admission result."""

    outcome: AdmissionOutcome
    retry_after: int | None = None
    reasons: tuple[str, ...] = ()

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
    degraded: list[str] = []
    retry_after_values: list[int] = []

    if not service.available:
        denied.extend(service.reasons or ("service unavailable",))
        _append_retry_after(retry_after_values, service.retry_after)
    elif service.degraded:
        degraded.extend(service.reasons or ("service degraded",))
        _append_retry_after(retry_after_values, service.retry_after)

    if estimate.intent in constraints.disabled_intents:
        denied.append(f"intent disabled: {estimate.intent}")
    if estimate.resource_class and estimate.resource_class in constraints.disabled_resource_classes:
        denied.append(f"resource class disabled: {estimate.resource_class}")

    reserved_required = _intersection(estimate.required_tokens, constraints.reserved_tokens)
    if reserved_required:
        denied.append("required resource reserved: " + ", ".join(reserved_required))

    reserved_optional = _intersection(estimate.optional_tokens, constraints.reserved_tokens)
    if reserved_optional:
        degraded.append("optional resource reserved: " + ", ".join(reserved_optional))

    if estimate.requires_gpu and "gpu" in constraints.reserved_tokens:
        denied.append("gpu reserved")

    _check_queue_depth(estimate, constraints, load, denied, retry_after_values)
    _check_app_concurrency(estimate, constraints, load, denied, retry_after_values)

    _append_retry_after(retry_after_values, constraints.retry_after)

    if denied:
        return ResourceAdmissionDecision(
            AdmissionOutcome.DENIED,
            _smallest_retry_after(retry_after_values),
            _unique((*denied, *constraints.reasons)),
        )
    if degraded:
        return ResourceAdmissionDecision(
            AdmissionOutcome.DEGRADED,
            _smallest_retry_after(retry_after_values),
            _unique((*degraded, *estimate.reasons, *constraints.reasons)),
        )
    return ResourceAdmissionDecision(
        AdmissionOutcome.ALLOWED,
        None,
        estimate.reasons or ("resources available",),
    )


def _check_queue_depth(
    estimate: ResourceEstimate,
    constraints: AdmissionConstraints,
    load: CurrentLoad,
    denied: list[str],
    retry_after_values: list[int],
) -> None:
    if constraints.max_queue_depth is None or estimate.queue_key is None:
        return
    queue_depth = load.queue_depths.get(estimate.queue_key, 0)
    if queue_depth >= constraints.max_queue_depth:
        denied.append(f"queue full: {estimate.queue_key}")
        _append_retry_after(retry_after_values, load.retry_after)


def _check_app_concurrency(
    estimate: ResourceEstimate,
    constraints: AdmissionConstraints,
    load: CurrentLoad,
    denied: list[str],
    retry_after_values: list[int],
) -> None:
    if estimate.app_id is None:
        return
    limit = constraints.max_app_concurrency.get(estimate.app_id)
    if limit is None:
        return
    active = load.active_by_app.get(estimate.app_id, 0)
    if active >= limit:
        denied.append(f"app concurrency full: {estimate.app_id}")
        _append_retry_after(retry_after_values, load.retry_after)


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
