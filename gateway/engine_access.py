"""Who can use an engine's models: the answer Add engine shows before Create.

New models become usable under the access plans that already exist. A plan
that allows all chat models includes a new chat model the moment it is saved;
a plan that lists models includes only the ones it lists. This module asks the
evaluator every request meets (:func:`gateway.policy.evaluate`) about each
plan, each model and each kind of work the engine does, so the answer is what
the gateway will enforce. Deny by default: work no plan can grant, and plans
that grant other work or list other models, include nothing.

It names no engine and no plan; plans come from the caller.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from gateway.access_plans import AccessPlan
from gateway.capability_ids import CAPABILITY_LABELS
from gateway.policy import RULE_ALLOWS_ALL, RULE_LISTS_ITEM, EffectiveAccess, PolicyRequest, evaluate

PLANS_LINK = "/people/?tab=plans"


def plan_link(plan_id: str) -> str:
    return f"/people/?plan={plan_id}"


def _why(rules: Iterable[tuple[str, str | None]]) -> str:
    """"allows all chat models", "lists qwen3-32b", or both, from the deciding rules."""
    kinds: list[str] = []
    listed: list[str] = []
    for code, subject in rules:
        if code == RULE_ALLOWS_ALL and subject:
            label = CAPABILITY_LABELS.get(subject, subject).lower()
            if label not in kinds:
                kinds.append(label)
        elif code == RULE_LISTS_ITEM and subject and subject not in listed:
            listed.append(subject)
    parts = []
    if kinds:
        parts.append(f"allows all {_join(kinds)} models")
    if listed:
        parts.append(f"lists {_join(listed)}")
    return "; ".join(parts) or "allows them"


def _join(words: Sequence[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def who_can_use(
    models: Sequence[str],
    works: Sequence[str],
    plans: Sequence[AccessPlan],
    *,
    holders: Mapping[str, Mapping[str, Any]],
    viewer_plan_id: str | None,
    category_rules: bool = False,
) -> dict[str, Any]:
    """Which plans include ``models`` for ``works`` (engine capability ids), and one sentence saying so.

    ``holders`` maps a plan id to who holds it (``people`` and ``apps`` lists).
    ``viewer_plan_id`` is the asking person's own plan, for "you".
    ``category_rules`` says active category rules govern some people or apps:
    a new model is in no category yet, so they cannot use it until it is added.
    """
    models = list(dict.fromkeys(name for name in models if name))
    included: list[dict[str, Any]] = []
    for plan in plans:
        access = EffectiveAccess(person_plan=plan, app_plan=plan)
        allowed: list[str] = []
        rules: list[tuple[str, str | None]] = []
        for model in models:
            for work in works:
                decision = evaluate(access, PolicyRequest(intent=work, model=model))
                if decision.allowed:
                    if model not in allowed:
                        allowed.append(model)
                    if decision.rule is not None:
                        rules.append((decision.rule.code, decision.rule.subject))
        if not allowed:
            continue
        held = holders.get(plan.id) or {}
        included.append({
            "id": plan.id,
            "name": plan.name,
            "models": allowed,
            "why": _why(rules),
            "people": len(held.get("people") or ()),
            "apps": len(held.get("apps") or ()),
            "link": plan_link(plan.id),
        })
    covered = {model for row in included for model in row["models"]}
    not_included = [model for model in models if model not in covered]
    you = any(row["id"] == viewer_plan_id for row in included)
    return {
        "plans": included,
        "you": you,
        "not_included": not_included,
        "category_rules": category_rules,
        "sentence": _sentence(models, included, not_included, you=you, viewer_plan_id=viewer_plan_id,
                              category_rules=category_rules),
        "link": PLANS_LINK,
    }


def _sentence(models: Sequence[str], included: Sequence[Mapping[str, Any]], not_included: Sequence[str], *,
              you: bool, viewer_plan_id: str | None, category_rules: bool) -> str:
    if not models:
        return "No models yet. When it has some, this shows who can use them."
    if not included:
        text = ("No access plan includes these yet, so nobody can use them, you included. "
                "Add them to a plan in Access plans.")
    else:
        others = [f"the {row['name']} plan ({row['why']})" for row in included if row["id"] != viewer_plan_id]
        if you and others:
            text = f"Usable by: you, and {_join(others)}."
        elif you:
            mine = next(row for row in included if row["id"] == viewer_plan_id)
            text = f"Usable by: you ({mine['name']} plan: {mine['why']})."
        else:
            text = f"Usable by: {_join(others)}. Your own plan doesn’t include them."
        if not_included:
            text += f" Not in any plan yet: {', '.join(not_included)}."
    if category_rules:
        text += " People and apps under category rules can’t use them until you add them to a category."
    return text
