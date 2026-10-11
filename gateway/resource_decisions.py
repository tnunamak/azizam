"""Evidence and audience projection shared by the two admission evaluators.

Legacy summaries remain separate. Only trusted server code supplies evidence or
next actions; a decision is advice, never a reservation or consent to execute.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Mapping

Quality = Literal["measured", "declared", "estimated", "unavailable"]


@dataclass(frozen=True)
class CapacityFact:
    name: str
    resource_class: str
    value: object
    observed_at: float | None = None  # Unix seconds; None means no observation time supplied.
    quality: Quality = "declared"

    def __post_init__(self) -> None:
        if self.quality not in {"measured", "declared", "estimated", "unavailable"}:
            raise ValueError("invalid fact quality")
        if self.value is None or self.quality == "unavailable":
            object.__setattr__(self, "value", None)
            object.__setattr__(self, "quality", "unavailable")


@dataclass(frozen=True)
class DecisionReason:
    code: str
    effect: Literal["rejection", "delay", "info"]
    resource_class: str
    rule: str
    observed_at: float | None = None
    quality: Quality = "declared"


@dataclass(frozen=True)
class DecisionDetails:
    reasons: tuple[DecisionReason, ...] = ()
    facts: tuple[CapacityFact, ...] = ()

    def as_dict(self, outcome: str) -> dict[str, object]:
        return {"outcome": outcome, "reasons": [asdict(r) for r in self.reasons],
                "facts": [asdict(f) for f in self.facts]}


# One dictionary for HTTP, receipts, and their REST/MCP readers. Never use
# operator-authored prose or identifiers as a caller-visible fallback.
CALLER_REASONS = {
    "service_unavailable": "This service is not available right now.",
    "service_unknown": "Service availability is not known yet.",
    "service_degraded": "This service has reduced availability.",
    "intent_disabled": "This kind of work is disabled.",
    "resource_class_disabled": "This kind of resource is disabled.",
    "required_resource_reserved": "A required resource is reserved.",
    "optional_resource_reserved": "An optional resource is reserved.",
    "gpu_reserved": "GPU capacity is reserved.",
    "queue_full": "Shared capacity is busy.",
    "queue_load_unknown": "Shared capacity is not known yet.",
    "app_concurrency_full": "Your app has reached its concurrency limit.",
    "app_load_unknown": "Your app's current load is not known yet.",
    "profile_concurrency_full": "Your access plan's concurrency limit has been reached.",
    "profile_load_unknown": "Access-plan capacity is not known yet.",
    "lock_conflict": "A required resource is in use.",
    "locks_unknown": "Required resource availability is not known yet.",
    "capacity_insufficient": "There is not enough capacity for this work right now.",
    "capacity_unknown": "Required capacity is not known yet.",
    "gpu_unavailable": "GPU capacity is not available.",
    "resources_available": "Resources are available.",
    "estimate_reason": "The request has resource requirements.",
    "constraint_reason": "An operator rule applies to this work.",
}
_RESOURCE_CLASSES = {"service", "policy", "queue", "app", "profile", "lock", "gpu", "vram", "cpu", "ram", "disk", "resource"}


def project_decision(value: Mapping[str, object], audience: str = "client") -> dict[str, object]:
    """Re-project stored evidence; unknown audiences always get the safe view."""
    owner = audience in {"admin", "internal", "owner"}
    reasons = []
    for raw in value.get("reasons", ()):
        if not isinstance(raw, Mapping):
            continue
        code = raw.get("code")
        resource_class = raw.get("resource_class")
        item = {"code": code if code in CALLER_REASONS else "constraint_reason",
                "effect": raw.get("effect") if raw.get("effect") in {"rejection", "delay", "info"} else "info",
                "resource_class": resource_class if resource_class in _RESOURCE_CLASSES else "resource",
                "message": CALLER_REASONS.get(code, "An operator rule applies to this work.")}
        if owner:
            item.update({key: raw.get(key) for key in ("rule", "observed_at", "quality")})
        reasons.append(item)
    actions = []
    deadline = value.get("wait_deadline")
    if type(deadline) in {int, float} and deadline > 0 and any(r["effect"] == "delay" or r["code"] in {
            "service_unavailable", "service_unknown", "queue_full", "queue_load_unknown",
            "app_concurrency_full", "app_load_unknown", "profile_concurrency_full", "profile_load_unknown",
            "required_resource_reserved", "gpu_reserved"} for r in reasons):
        actions.append({"action": "wait", "deadline": deadline,
                        "message": "Retry until this deadline, then stop waiting. This is not a start-time estimate."})
    # These options must already be authorized for this caller by the route.
    # No live route supplies them until it can prove ownership/permission.
    if value.get("can_cancel_own_work") is True:
        actions.append({"action": "cancel_own_work"})
    for key, action in (("permitted_options", "use_option"), ("authorized_paid_options", "opt_in_paid_route")):
        for option in value.get(key, ()):
            if isinstance(option, str) and option:
                item = {"action": action, "option": option}
                if key == "authorized_paid_options":
                    item["requires_explicit_consent"] = True
                actions.append(item)
    out = {"outcome": value.get("outcome"), "reasons": reasons, "next_actions": actions}
    if owner:
        out["facts"] = value.get("facts", [])
    return out
