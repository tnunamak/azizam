"""Access plans (design v4, docs/design/ACCESS_PLANS.md).

An access plan is a named, reusable document that the owner assigns to
people and apps. Nothing is allowed unless the plan lists it. This module
holds the plan model and its JSON codec; :mod:`gateway.policy` evaluates
requests against the plans of a person and an app.

Two parts of the design are not in the document yet, and a document that
names them is refused rather than silently ignored:

* ``blocked`` / ``must_match`` (category rules) — they move into plans with
  the categories store.
* ``caps`` — per-person and per-app spending counters need the ledger to
  record the person and app of every charge. Provider and global caps
  (Providers page) still apply to every paid call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from gateway.capability_ids import CAPABILITY_LABELS, INTENT_CAPABILITIES

CAPABILITIES = tuple(CAPABILITY_LABELS)
RECEIPT_DETAIL_LEVELS = ("none", "basic", "standard", "full")
MEDIA_METADATA_LEVELS = ("none", "full")
ALL = "all"
PERSON = "person"

# Discovery is not a capability: any identity with a plan may list, and the
# list is filtered by the same evaluator (design §2.2).
DISCOVERY_INTENTS = frozenset({
    "models.list", "models.show", "models.running", "models.version", "capabilities.list",
})
DIRECT_ENGINE_INTENT = "provider.native"
# Model management through Azizam's own Ollama routes is never grantable: the
# routes answer 501 whatever the plan says (design §2.2), so the plan has no
# opinion and the route gives the honest answer.
UNSUPPORTED_INTENTS = frozenset({"models.pull", "models.push", "models.create", "models.copy", "models.delete"})

_DOCUMENT_FIELDS = frozenset({
    "id", "name", "can_use", "direct_engine_access", "paid_providers",
    "provider_fallback", "can_load_models", "receipt_detail", "media_metadata",
})
_NOT_YET = {
    "blocked": "Blocked categories are not available in access plans yet.",
    "must_match": "Must match rules are not available in access plans yet.",
    "caps": "Spending caps per access plan are not available yet. Use the provider and gateway caps.",
}


class PlanError(ValueError):
    """A plan document is invalid. The message names the field."""


def capability_for_intent(intent: str | None) -> str | None:
    """Return the capability a route intent needs, or None if unknown."""
    if not intent:
        return None
    return INTENT_CAPABILITIES.get(intent)


@dataclass(frozen=True)
class CapabilityGrant:
    """What a plan allows for one capability.

    ``all_items`` grants every model and workflow of the capability, including
    ones the owner adds later. Otherwise only the listed ones are granted.
    """

    all_items: bool = False
    models: frozenset[str] = frozenset()
    workflows: frozenset[str] = frozenset()

    def allows(self, *, model: str | None, workflow: str | None) -> bool:
        """A request names a model (what the client asked for), a workflow (what
        runs), or both; either one being listed grants it."""
        if self.all_items:
            return True
        if model is None and workflow is None:
            # Not resolved yet: the check after resolution names the item.
            return bool(self.models or self.workflows)
        return (model is not None and model in self.models) or (
            workflow is not None and workflow in self.workflows)


@dataclass(frozen=True)
class DirectEngineAccess:
    """Engines an app may call through the engine's own API (design §2.7).

    ``operations`` use the scope syntax ``"METHOD /path"``, ``"METHOD *"``,
    a path for any method, a prefix ending in ``*``, or ``"*"``. An empty
    list grants nothing; there is no implicit "all".
    """

    all_engines: bool = False
    engines: frozenset[str] = frozenset()
    operations: frozenset[str] = frozenset()

    @property
    def granted(self) -> bool:
        return bool((self.all_engines or self.engines) and self.operations)

    def allows_engine(self, engine: str | None) -> bool:
        if engine is None:
            return self.granted
        return self.all_engines or engine in self.engines


@dataclass(frozen=True)
class AccessPlan:
    id: str
    name: str
    can_use: Mapping[str, CapabilityGrant] = field(default_factory=dict)
    direct_engine_access: DirectEngineAccess = field(default_factory=DirectEngineAccess)
    paid_providers: frozenset[str] = frozenset()
    provider_fallback: bool = False
    can_load_models: bool = False
    receipt_detail: str = "basic"
    media_metadata: str = "none"


def _canonical_provider(value: object) -> str:
    from gateway.service_registry import canonical_provider_id

    return canonical_provider_id(value)


def _require(doc: Mapping[str, Any], key: str) -> Any:
    if key not in doc:
        raise PlanError(f"access plan field {key!r} is required")
    return doc[key]


def _strings(value: Any, where: str) -> frozenset[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise PlanError(f"{where} must be a list of names")
    return frozenset(item.strip() for item in value)


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise PlanError(f"{where} must be true or false")
    return value


def _grant_from_document(capability: str, value: Any) -> CapabilityGrant:
    where = f"can_use.{capability}"
    if not isinstance(value, Mapping) or set(value) != {"items"}:
        raise PlanError(f"{where} must be {{\"items\": \"all\" or a list}}")
    items = value["items"]
    if items == ALL:
        return CapabilityGrant(all_items=True)
    if not isinstance(items, list):
        raise PlanError(f"{where}.items must be \"all\" or a list")
    models: set[str] = set()
    workflows: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping) or len(item) != 1:
            raise PlanError(f"{where}.items entries are {{\"model\": name}} or {{\"workflow\": name}}")
        (kind, name), = item.items()
        if kind not in ("model", "workflow") or not isinstance(name, str) or not name.strip():
            raise PlanError(f"{where}.items entries are {{\"model\": name}} or {{\"workflow\": name}}")
        (models if kind == "model" else workflows).add(name.strip())
    return CapabilityGrant(models=frozenset(models), workflows=frozenset(workflows))


def plan_from_document(doc: Any, *, plan_id: str | None = None) -> AccessPlan:
    """Validate a plan document. Every field is required (design §3.1, RT M4)."""
    if not isinstance(doc, Mapping):
        raise PlanError("an access plan must be a JSON object")
    for key, message in _NOT_YET.items():
        if key in doc:
            raise PlanError(message)
    unknown = sorted(set(doc) - _DOCUMENT_FIELDS)
    if unknown:
        raise PlanError(f"unknown access plan field {unknown[0]!r}")
    identifier = plan_id if plan_id is not None else _require(doc, "id")
    if not isinstance(identifier, str) or not identifier.strip():
        raise PlanError("access plan id must be a non-empty string")
    name = _require(doc, "name")
    if not isinstance(name, str) or not name.strip():
        raise PlanError("access plan name must be a non-empty string")
    can_use = _require(doc, "can_use")
    if not isinstance(can_use, Mapping):
        raise PlanError("can_use must be an object keyed by capability")
    grants = {}
    for capability, value in can_use.items():
        if capability not in CAPABILITIES:
            raise PlanError(f"unknown capability {capability!r}; use one of {', '.join(CAPABILITIES)}")
        grants[capability] = _grant_from_document(capability, value)
    direct = _require(doc, "direct_engine_access")
    if not isinstance(direct, Mapping) or set(direct) != {"engines", "operations"}:
        raise PlanError("direct_engine_access must be {\"engines\": ..., \"operations\": [...]}")
    engines = direct["engines"]
    direct_access = DirectEngineAccess(
        all_engines=engines == ALL,
        engines=frozenset() if engines == ALL else _strings(engines, "direct_engine_access.engines"),
        operations=_strings(direct["operations"], "direct_engine_access.operations"),
    )
    receipt_detail = _require(doc, "receipt_detail")
    if receipt_detail not in RECEIPT_DETAIL_LEVELS:
        raise PlanError(f"receipt_detail must be one of {', '.join(RECEIPT_DETAIL_LEVELS)}")
    media_metadata = _require(doc, "media_metadata")
    if media_metadata not in MEDIA_METADATA_LEVELS:
        raise PlanError(f"media_metadata must be one of {', '.join(MEDIA_METADATA_LEVELS)}")
    return AccessPlan(
        id=identifier.strip(),
        name=name.strip(),
        can_use=grants,
        direct_engine_access=direct_access,
        paid_providers=frozenset(
            provider for provider in (
                _canonical_provider(item)
                for item in _strings(_require(doc, "paid_providers"), "paid_providers")
            ) if provider
        ),
        provider_fallback=_bool(_require(doc, "provider_fallback"), "provider_fallback"),
        can_load_models=_bool(_require(doc, "can_load_models"), "can_load_models"),
        receipt_detail=receipt_detail,
        media_metadata=media_metadata,
    )


def plan_to_document(plan: AccessPlan) -> dict[str, Any]:
    can_use: dict[str, Any] = {}
    for capability in CAPABILITIES:
        grant = plan.can_use.get(capability)
        if grant is None:
            continue
        if grant.all_items:
            can_use[capability] = {"items": ALL}
        else:
            can_use[capability] = {"items": [
                *({"model": name} for name in sorted(grant.models)),
                *({"workflow": name} for name in sorted(grant.workflows)),
            ]}
    direct = plan.direct_engine_access
    return {
        "id": plan.id,
        "name": plan.name,
        "can_use": can_use,
        "direct_engine_access": {
            "engines": ALL if direct.all_engines else sorted(direct.engines),
            "operations": sorted(direct.operations),
        },
        "paid_providers": sorted(plan.paid_providers),
        "provider_fallback": plan.provider_fallback,
        "can_load_models": plan.can_load_models,
        "receipt_detail": plan.receipt_detail,
        "media_metadata": plan.media_metadata,
    }


def plan_rules_json(plan: AccessPlan) -> str:
    """Canonical JSON of the rules (no id or name), for comparing plans."""
    doc = plan_to_document(plan)
    doc.pop("id")
    doc.pop("name")
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def _level_max(levels: Iterable[str], order: tuple[str, ...]) -> str:
    return max(levels, key=order.index)


def union_plans(plan_id: str, name: str, plans: Iterable[AccessPlan]) -> AccessPlan:
    """The smallest plan that allows everything any of ``plans`` allows."""
    plans = list(plans)
    can_use: dict[str, CapabilityGrant] = {}
    for plan in plans:
        for capability, grant in plan.can_use.items():
            prior = can_use.get(capability, CapabilityGrant())
            can_use[capability] = CapabilityGrant(
                all_items=prior.all_items or grant.all_items,
                models=frozenset() if prior.all_items or grant.all_items else prior.models | grant.models,
                workflows=frozenset() if prior.all_items or grant.all_items else prior.workflows | grant.workflows,
            )
    all_engines = any(p.direct_engine_access.all_engines for p in plans)
    return AccessPlan(
        id=plan_id,
        name=name,
        can_use=can_use,
        direct_engine_access=DirectEngineAccess(
            all_engines=all_engines,
            engines=frozenset() if all_engines else frozenset().union(*(p.direct_engine_access.engines for p in plans)),
            operations=frozenset().union(*(p.direct_engine_access.operations for p in plans)),
        ),
        paid_providers=frozenset().union(*(p.paid_providers for p in plans)),
        provider_fallback=any(p.provider_fallback for p in plans),
        can_load_models=any(p.can_load_models for p in plans),
        receipt_detail=_level_max((p.receipt_detail for p in plans), RECEIPT_DETAIL_LEVELS) if plans else "none",
        media_metadata=_level_max((p.media_metadata for p in plans), MEDIA_METADATA_LEVELS) if plans else "none",
    )


def plan_summary(plan: AccessPlan) -> str:
    """One line for pickers: "Chat and Images. No paid models." (design §3.4)."""
    if all(plan.can_use.get(cap, CapabilityGrant()).all_items for cap in CAPABILITIES):
        uses = "Everything on this gateway"
    else:
        labels = [CAPABILITY_LABELS[cap] for cap in CAPABILITIES if cap in plan.can_use]
        uses = (", ".join(labels[:-1]) + " and " + labels[-1]) if len(labels) > 1 else (labels[0] if labels else "Nothing")
    paid = "Paid models: " + ", ".join(sorted(plan.paid_providers)) if plan.paid_providers else "No paid models"
    parts = [uses, paid]
    if plan.direct_engine_access.granted:
        parts.append("Direct engine access")
    return ". ".join(parts) + "."


__all__ = [
    "ALL", "AccessPlan", "CAPABILITIES", "CapabilityGrant", "DIRECT_ENGINE_INTENT",
    "DISCOVERY_INTENTS", "DirectEngineAccess", "UNSUPPORTED_INTENTS", "INTENT_CAPABILITIES", "MEDIA_METADATA_LEVELS",
    "PERSON", "PlanError", "RECEIPT_DETAIL_LEVELS", "capability_for_intent", "plan_from_document",
    "plan_rules_json", "plan_summary", "plan_to_document", "union_plans",
]
