"""Access-plan evaluator (design v4, docs/design/ACCESS_PLANS.md §2.5-§2.7).

Pure decision logic. A request runs only if both the accountable person's
plan and the app's plan allow it. A missing plan on either side denies; there
is no built-in default plan. Decisions are receipt-compatible: ``decision``,
``sources`` and ``reasons`` map onto :mod:`gateway.receipts` fields, and
``code``/``side`` carry the data-plane error of design §3.3.

This module has no dependencies on live auth, request handlers or
``proxy.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from gateway.access_plans import (
    DIRECT_ENGINE_INTENT,
    DISCOVERY_INTENTS,
    MEDIA_METADATA_LEVELS,
    UNSUPPORTED_INTENTS,
    RECEIPT_DETAIL_LEVELS,
    AccessPlan,
    capability_for_intent,
)
from gateway.capability_ids import CAPABILITY_LABELS
from gateway.outbound_request import canonical_native_path


ALLOWED = "allowed"
DENIED = "denied"

PERSON_SIDE = "person"
APP_SIDE = "app"

NO_ACCESS_PLAN = "no_access_plan"
NOT_IN_ACCESS_PLAN = "not_in_access_plan"
CANNOT_START_MODEL = "cannot_start_model"


def _canonical_provider_id(value: object) -> str:
    from gateway.service_registry import canonical_provider_id

    return canonical_provider_id(value)


@dataclass(frozen=True)
class PolicyRequest:
    """Inputs the evaluator reasons about for a single request.

    ``intent`` decides the capability. ``workflow`` or ``model`` names the
    item once the route has resolved it. The capability metadata fields are
    request facts kept for receipts; plans do not restrict by them.
    """

    protocol: str | None = None
    intent: str | None = None
    workflow: str | None = None
    model: str | None = None
    backend: str | None = None
    capability_exposure: str | None = None
    capability_trust_tier: str | None = None
    capability_resource_tier: str | None = None
    capability_tags: frozenset[str] | None = None
    requires_cloud_fallback: bool = False
    native_passthrough: bool = False
    native_operation: str | None = None
    native_backend: str | None = None
    remote_provider: str | None = None
    remote_paid_fallback: bool = False

    def __post_init__(self) -> None:
        if self.capability_tags is None:
            return
        if isinstance(self.capability_tags, (str, bytes)):
            raise ValueError("capability_tags must be an iterable of strings")
        tags = frozenset(self.capability_tags)
        if not all(isinstance(tag, str) for tag in tags):
            raise ValueError("capability_tags must be an iterable of strings")
        object.__setattr__(self, "capability_tags", tags)


@dataclass(frozen=True)
class PolicyDecision:
    """Receipt-compatible decision. ``code`` and ``side`` name a denial."""

    decision: str
    sources: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    visibility_level: str = "basic"
    code: str | None = None
    side: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision == ALLOWED

    @property
    def denied(self) -> bool:
        return self.decision == DENIED

    def receipt_fields(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "sources": list(self.sources),
            "reasons": list(self.reasons),
            "visibility_level": self.visibility_level,
        }


def _lower(levels: Iterable[str], order: tuple[str, ...]) -> str:
    return min(levels, key=order.index)


@dataclass(frozen=True)
class EffectiveAccess:
    """The two plans a request runs under (design §2.4, §2.6).

    ``person_plan`` is the accountable person's plan; ``app_plan`` is the
    app's assignment, already resolved ("Same as its person" is the person's
    plan). Either may be None, which denies every request.
    """

    person_plan: AccessPlan | None
    app_plan: AccessPlan | None

    @property
    def complete(self) -> bool:
        return self.person_plan is not None and self.app_plan is not None

    def _plans(self) -> tuple[AccessPlan, ...]:
        return tuple(plan for plan in (self.person_plan, self.app_plan) if plan is not None)

    @property
    def receipt_detail(self) -> str:
        """The lower of the two plans; ``basic`` without a full assignment."""
        if not self.complete:
            return "basic"
        return _lower((plan.receipt_detail for plan in self._plans()), RECEIPT_DETAIL_LEVELS)

    @property
    def media_metadata(self) -> str:
        """A bound: the lower of the two plans (design §2.6, N5)."""
        if not self.complete:
            return "none"
        return _lower((plan.media_metadata for plan in self._plans()), MEDIA_METADATA_LEVELS)

    @property
    def can_load_models(self) -> bool:
        return self.complete and all(plan.can_load_models for plan in self._plans())

    @property
    def paid_providers(self) -> frozenset[str]:
        if not self.complete:
            return frozenset()
        return self.person_plan.paid_providers & self.app_plan.paid_providers  # type: ignore[union-attr]

    @property
    def provider_fallback(self) -> bool:
        return self.complete and all(plan.provider_fallback for plan in self._plans())

    @property
    def lists_items(self) -> bool:
        """True when a plan grants a capability by listed models or workflows."""
        return any(not grant.all_items for plan in self._plans() for grant in plan.can_use.values())

    @property
    def plan_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(plan.id for plan in self._plans()))


NO_ACCESS = EffectiveAccess(person_plan=None, app_plan=None)


def _decision(
    access: EffectiveAccess,
    outcome: str,
    reasons: Iterable[str],
    *,
    code: str | None = None,
    side: str | None = None,
) -> PolicyDecision:
    return PolicyDecision(
        decision=outcome,
        sources=tuple(f"access_plan:{plan_id}" for plan_id in access.plan_ids),
        reasons=tuple(reasons),
        visibility_level=access.receipt_detail,
        code=code,
        side=side,
    )


def matches_operation(value: str, allowed: frozenset[str]) -> bool:
    """Match a direct engine operation ``"METHOD /path"`` against scopes.

    Scopes: exact strings, ``*``, ``"METHOD *"``, a path for any method, and
    prefixes ending in ``*``. An empty scope set matches nothing.
    """
    method: str | None = None
    path = value
    if " " in value:
        method, path = value.split(" ", 1)
        method = method.upper()
    try:
        canonical_native_path(path)
    except ValueError:
        return False
    if not allowed:
        return False
    if "*" in allowed or value in allowed:
        return True
    if method is not None and f"{method} *" in allowed:
        return True
    path_variants = {path, path.lstrip("/"), f"/{path.lstrip('/')}"}
    if path_variants & allowed:
        return True
    for scope in allowed:
        if not scope.endswith("*"):
            continue
        prefix = scope[:-1]
        if value.startswith(prefix):
            return True
        if any(variant.startswith(prefix) or variant.startswith(prefix.lstrip("/")) for variant in path_variants):
            return True
    return False


def _item_label(request: PolicyRequest, capability: str | None) -> str:
    if request.workflow:
        return request.workflow
    if request.model:
        return request.model
    if capability:
        return CAPABILITY_LABELS.get(capability, capability)
    return "This request"


def _not_in_plan(side: str, what: str) -> str:
    if side == PERSON_SIDE:
        return f"{what} isn't in your access plan."
    return (
        f"Your access plan allows {what}, but this app's access plan doesn't. "
        "Ask the gateway owner to change the app's access plan."
    )


def _check_plan(plan: AccessPlan, request: PolicyRequest, side: str) -> tuple[str, str] | None:
    """Return (code, reason) for the first rule ``plan`` fails, else None."""
    intent = request.intent
    if request.native_passthrough or intent == DIRECT_ENGINE_INTENT:
        direct = plan.direct_engine_access
        engine = request.native_backend or request.backend
        if not direct.allows_engine(engine) or not direct.granted:
            return NOT_IN_ACCESS_PLAN, _not_in_plan(side, f"Direct engine access to {engine or 'this engine'}")
        if request.native_operation is not None and not matches_operation(request.native_operation, direct.operations):
            return NOT_IN_ACCESS_PLAN, _not_in_plan(
                side, f"{request.native_operation} on {engine or 'this engine'}")
    elif intent is not None and intent not in DISCOVERY_INTENTS and intent not in UNSUPPORTED_INTENTS:
        capability = capability_for_intent(intent)
        grant = plan.can_use.get(capability) if capability else None
        if grant is None or not grant.allows(model=request.model, workflow=request.workflow):
            return NOT_IN_ACCESS_PLAN, _not_in_plan(side, _item_label(request, capability))
    if request.remote_provider is not None:
        provider = _canonical_provider_id(request.remote_provider)
        if provider not in plan.paid_providers:
            return NOT_IN_ACCESS_PLAN, _not_in_plan(side, f"The paid provider {provider}")
    if (request.requires_cloud_fallback or request.remote_paid_fallback) and not plan.provider_fallback:
        return NOT_IN_ACCESS_PLAN, _not_in_plan(side, "Using a paid provider when the engine is unavailable")
    return None


def evaluate(access: EffectiveAccess, request: PolicyRequest) -> PolicyDecision:
    """Decide a request (design §2.5 steps 3 and 5).

    Step 3: the person has a plan and the app has an assignment. Step 5: the
    person's plan, then the app's plan. When both sides hold the same plan it
    is evaluated once and the explanation says "your access plan".
    """
    if access.person_plan is None:
        return _decision(access, DENIED, [
            "You don't have an access plan yet. Ask the gateway owner to choose one.",
        ], code=NO_ACCESS_PLAN, side=PERSON_SIDE)
    if access.app_plan is None:
        return _decision(access, DENIED, [
            "This app has no access plan. Ask the gateway owner to choose one.",
        ], code=NO_ACCESS_PLAN, side=APP_SIDE)
    sides = [(PERSON_SIDE, access.person_plan)]
    if access.app_plan.id != access.person_plan.id:
        sides.append((APP_SIDE, access.app_plan))
    for side, plan in sides:
        failure = _check_plan(plan, request, side)
        if failure is not None:
            code, reason = failure
            return _decision(access, DENIED, [reason], code=code, side=side)
    return _decision(access, ALLOWED, ["allowed by your access plan"])


def cannot_start_model(access: EffectiveAccess, item: str) -> PolicyDecision:
    """The ``load`` effect was refused: a plan may not start models."""
    return _decision(access, DENIED, [
        f"{item} isn't running, and your access plan can't start it. "
        "Try again when it's running, or ask the gateway owner.",
    ], code=CANNOT_START_MODEL, side=PERSON_SIDE if not (access.person_plan and access.person_plan.can_load_models) else APP_SIDE)


def visibility_for(access: EffectiveAccess) -> str:
    """Return the receipt detail level (none/basic/standard/full)."""
    return access.receipt_detail


def apply_to_receipt(decision: PolicyDecision, receipt: object) -> None:
    """Apply a decision to a receipt-like object without importing receipts."""
    set_decision = getattr(receipt, "set_policy_decision", None)
    if not callable(set_decision):
        raise TypeError("receipt must expose set_policy_decision(decision, ...)")

    sources = list(decision.sources) or [None]
    reasons = list(decision.reasons) or [None]
    pairs = max(len(sources), len(reasons))
    for index in range(pairs):
        set_decision(
            decision.decision,
            reason=reasons[index] if index < len(reasons) else None,
            source=sources[index] if index < len(sources) else None,
        )
    if hasattr(receipt, "visibility"):
        setattr(receipt, "visibility", decision.visibility_level)
