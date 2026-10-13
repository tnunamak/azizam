"""Runtime identity and the bridge from request handlers to the evaluator.

A :class:`RuntimeIdentity` is what an authenticated data-plane request runs
as: an App (by key or by a person's browser session), its accountable person
and the two access plans that bound it (:class:`gateway.policy.EffectiveAccess`).
Identities come only from the admin store (``gateway.admin_runtime``); there
is no keyless or config-file identity.

This module never imports :mod:`proxy`, never reads files and never touches
``os.environ``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Mapping

from gateway.policy import (
    NO_ACCESS,
    EffectiveAccess,
    PolicyDecision,
    PolicyRequest,
    apply_to_receipt,
    evaluate,
)

__all__ = [
    "RuntimeIdentity",
    "discovery_allows",
    "plan_ref",
    "discovery_audience_for_access",
    "evaluate_and_apply",
    "hash_api_key",
    "make_policy_request",
    "normalize_key_hash",
]


def hash_api_key(api_key: str) -> str:
    """Hash a client-facing proxy key for storage and lookup."""
    return f"sha256:{hashlib.sha256(api_key.encode('utf-8')).hexdigest()}"


def normalize_key_hash(key_hash: str) -> str:
    """Ensure a stored hash carries the ``sha256:`` prefix."""
    if key_hash.startswith("sha256:"):
        return key_hash
    return f"sha256:{key_hash}"


@dataclass(frozen=True)
class RuntimeIdentity:
    """Identity carried with an authenticated request.

    ``person_id`` is the data person: the person whose saved prompts and
    Library a request may use. Only a personal App's key made by its owner,
    or that person's browser session, has one (``person_for_app_credential``).
    ``accountable_person_id`` is the policy and spending subject: the person
    whose plan bounds the App (design §2.4). ``denial`` names why the App or
    person cannot run (``app_disabled``, ``person_disabled``).
    """

    name: str
    key_hash: str
    downstream: Mapping[str, object] = field(default_factory=dict)
    access: EffectiveAccess = NO_ACCESS
    app_id: str | None = None
    person_id: str | None = None
    accountable_person_id: str | None = None
    denial: str | None = None


def plan_ref(identity: "RuntimeIdentity | None") -> str | None:
    """The receipt's plan field: the plan ids a request ran under, joined by '+'."""
    access = getattr(identity, "access", None)
    ids = getattr(access, "plan_ids", ())
    return "+".join(ids) if ids else None


def make_policy_request(
    *,
    protocol: str | None = None,
    intent: str | None = None,
    workflow: str | None = None,
    model: str | None = None,
    backend: str | None = None,
    capability_exposure: str | None = None,
    capability_trust_tier: str | None = None,
    capability_resource_tier: str | None = None,
    capability_tags: Iterable[str] | None = None,
    requires_cloud_fallback: bool = False,
    native_passthrough: bool = False,
    native_operation: str | None = None,
    native_backend: str | None = None,
    remote_provider: str | None = None,
) -> PolicyRequest:
    """Construct a :class:`PolicyRequest` for a route."""
    if isinstance(capability_tags, (str, bytes)):
        raise ValueError("capability_tags must be an iterable of strings")
    return PolicyRequest(
        protocol=protocol,
        intent=intent,
        workflow=workflow,
        model=model,
        backend=backend,
        capability_exposure=capability_exposure,
        capability_trust_tier=capability_trust_tier,
        capability_resource_tier=capability_resource_tier,
        capability_tags=(
            frozenset(capability_tags) if capability_tags is not None else None
        ),
        requires_cloud_fallback=requires_cloud_fallback,
        native_passthrough=native_passthrough,
        native_operation=native_operation,
        native_backend=native_backend,
        remote_provider=remote_provider,
    )


def evaluate_and_apply(
    identity: RuntimeIdentity | None,
    request: PolicyRequest,
    receipt: object | None = None,
) -> PolicyDecision:
    """Evaluate a request for an identity and apply the decision to a receipt.

    ``identity`` None (no authenticated App) denies: there is no default plan.
    """
    access = identity.access if identity is not None else NO_ACCESS
    decision = evaluate(access, request)
    # Owner category rules can only tighten an allowed decision.
    from gateway import category_enforcement
    decision = category_enforcement.apply(identity, request, decision)
    if receipt is not None:
        apply_to_receipt(decision, receipt)
    return decision


def discovery_allows(identity: RuntimeIdentity | None, request: PolicyRequest) -> bool:
    """Discovery lists an item only when the evaluator allows it (design §2.13)."""
    return evaluate_and_apply(identity, request).allowed


# Map receipt detail -> RouteReceipt audience tier.
# - none / basic -> client (least information)
# - standard     -> admin
# - full         -> internal
_VISIBILITY_TO_AUDIENCE = {
    "none": "client",
    "basic": "client",
    "standard": "admin",
    "full": "internal",
}


def discovery_audience_for_access(access: EffectiveAccess | None) -> str:
    """Return the receipt audience tier for the lower receipt detail of two plans."""
    if access is None:
        return "client"
    return _VISIBILITY_TO_AUDIENCE.get(access.receipt_detail, "client")
