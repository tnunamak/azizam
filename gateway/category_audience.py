"""Who a category change would reach: the owner's audience previews.

Both previews use the runtime's own evaluator (``category_enforcement.refusing_categories``) and the runtime's own identity
builder (``admin_runtime.audience_callers``), so they cannot disagree with a request about a caller they name.

- ``candidate_audience`` previews a candidate category snapshot (the r124 "Turn on these category rules?" review). A caller
  gains or loses an item when the candidate changes whether a category refuses its requests for that item.
- ``plan_reach`` previews a proposed category list for one access plan (the plan editor's "who gains, who loses" view). A
  caller gains or loses an item when that plan is its person plan or its app plan and the change alters the refusal.

Granularity is the item, for each caller, and each entry names the category that changes the item's answer. An item is
refused for a caller when one of its categories refuses that caller (``category_enforcement.refusing_categories``). A gain
is an item the caller could not use and now can; a loss is the reverse. An item that stays refused, by the same or another
category, is not reported, because the caller's use of it has not changed. An item that moves into or out of a category is
reported, because its answer changes.

Coverage is the callers that exist when the preview is made: App keys in use, session Apps and trusted addresses
(``admin_runtime.audience_callers``). A key, App, address or session App created later is not covered. The owner's own
identity is never refused by a category and is left out.

``candidate_audience`` and ``plan_reach`` return ``(result, reason)``: ``result`` holds ``{"gains", "losses", "cannot_tell"}``
and ``reason`` is None; with the admin store unreadable, ``result`` is None and ``reason`` says why. A caller whose person or
app plan is missing or unreadable goes under ``cannot_tell`` and is not compared, so no entry can read as unchanged for it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from gateway import admin_runtime, admin_store, category_policy as policy
from gateway.access_plans import AccessPlan
from gateway.category_enforcement import category_members, is_owner_person, refusing_categories
from gateway.policy import EffectiveAccess
from gateway.runtime_auth import RuntimeIdentity

log = logging.getLogger(__name__)

NOT_CONFIGURED = "admin_store_not_configured"
UNREADABLE = "admin_store_unreadable"


def candidate_audience(before: policy.PolicyVersion | None, after: policy.PolicyVersion, *,
                       admin_path: Path | None) -> tuple[dict[str, list[dict[str, Any]]] | None, str | None]:
    """The audience of a candidate snapshot, or (None, reason) when the admin store cannot be read."""
    if admin_path is None:
        return None, NOT_CONFIGURED
    if not admin_path.exists():  # Reading would create an empty store and report no callers.
        return None, UNREADABLE
    try:
        owner = admin_store.instance_owner_id(path=admin_path)
        callers = admin_runtime.audience_callers(path=admin_path)
    except Exception as exc:  # noqa: BLE001 - an unread store must never read as "no change"
        # Error text can carry request values or credential-bearing paths.
        log.error("category audience unavailable (%s)", type(exc).__name__)
        return None, UNREADABLE
    return audience(before, after, callers, owner), None


def plan_reach(version: policy.PolicyVersion | None, plan_id: str | None, proposed: AccessPlan, *,
               admin_path: Path | None) -> tuple[dict[str, list[dict[str, Any]]] | None, str | None]:
    """Who a proposed category list for one access plan reaches, or (None, reason) when the admin store cannot be read."""
    if admin_path is None:
        return None, NOT_CONFIGURED
    if not admin_path.exists():
        return None, UNREADABLE
    try:
        owner = admin_store.instance_owner_id(path=admin_path)
        callers = admin_runtime.audience_callers(path=admin_path)
    except Exception as exc:  # noqa: BLE001 - an unread store must never read as "no change"
        log.error("category audience unavailable (%s)", type(exc).__name__)
        return None, UNREADABLE
    if plan_id is None:  # A plan not saved yet has no callers, so no caller can gain or lose a category.
        return {"gains": [], "losses": [], "cannot_tell": []}, None
    return plan_audience(version, plan_id, proposed, callers, owner), None


def refusals(version: policy.PolicyVersion | None, identity: RuntimeIdentity,
             owner_id: str | None) -> dict[str, tuple[str, ...]]:
    """Item -> the categories that refuse this identity's requests for that item under ``version``. Only refused items appear.

    Empty for a denied identity, which no category change can admit, and for the owner's own identity, which no category hides.
    """
    if version is None or identity.denial or is_owner_person(identity.person_id, owner_id):
        return {}
    access = identity.access
    found: dict[str, tuple[str, ...]] = {}
    for item, categories in category_members(version).items():
        refused = refusing_categories(categories, access.person_plan, access.app_plan)
        if refused:
            found[item] = refused
    return found


def audience(before: policy.PolicyVersion | None, after: policy.PolicyVersion,
             callers: Iterable[admin_runtime.AudienceCaller], owner_id: str | None) -> dict[str, list[dict[str, Any]]]:
    """Compare each caller's refusals under ``before`` and ``after``. See the module docstring."""
    labels = _labels(before, after)
    changes = _Changes(labels)
    for caller in callers:
        identity = caller.identity
        if identity.denial or is_owner_person(identity.person_id, owner_id):
            continue  # A denied identity gains nothing, and no category hides the owner's own identity.
        changes.record(caller, identity, refusals(before, identity, owner_id), refusals(after, identity, owner_id))
    return changes.result()


def plan_audience(version: policy.PolicyVersion | None, plan_id: str, proposed: AccessPlan,
                  callers: Iterable[admin_runtime.AudienceCaller], owner_id: str | None) -> dict[str, list[dict[str, Any]]]:
    """Compare the callers that use ``plan_id`` under the plan as stored and as ``proposed``, against one snapshot."""
    labels = _labels(version, None)
    changes = _Changes(labels)
    for caller in callers:
        identity = caller.identity
        if identity.denial or is_owner_person(identity.person_id, owner_id):
            continue
        access = identity.access
        if plan_id not in (getattr(access.person_plan, "id", None), getattr(access.app_plan, "id", None)):
            continue  # The change cannot reach a caller that does not use this plan.
        proposed_access = EffectiveAccess(
            person_plan=proposed if getattr(access.person_plan, "id", None) == plan_id else access.person_plan,
            app_plan=proposed if getattr(access.app_plan, "id", None) == plan_id else access.app_plan,
        )
        after_identity = replace(identity, access=proposed_access)
        changes.record(caller, identity, refusals(version, identity, owner_id),
                       refusals(version, after_identity, owner_id))
    return changes.result()


def _labels(before: policy.PolicyVersion | None, after: policy.PolicyVersion | None) -> dict[str, str]:
    labels: dict[str, str] = {}
    for version in (before, after):
        if version is not None:
            labels.update({key: category.label for key, category in version.categories.items()})
    return labels


class _Changes:
    """Accumulates one preview's gains, losses and callers that cannot be compared."""

    def __init__(self, labels: dict[str, str]) -> None:
        self._labels = labels
        self._gains: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._losses: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self._cannot_tell: dict[tuple[str, str], dict[str, Any]] = {}

    def record(self, caller: admin_runtime.AudienceCaller, identity: RuntimeIdentity,
               was: dict[str, tuple[str, ...]], now: dict[str, tuple[str, ...]]) -> None:
        access = identity.access
        person_plan_id = getattr(access.person_plan, "id", None)
        app_plan_id = getattr(access.app_plan, "id", None)
        app_id = identity.app_id or ""
        person_id = identity.accountable_person_id or ""
        if person_plan_id is None or app_plan_id is None:
            missing = [name for name, plan_id in (("person", person_plan_id), ("app", app_plan_id)) if plan_id is None]
            reason = (f"{missing[0]} plan missing or unreadable" if len(missing) == 1
                      else "person and app plans missing or unreadable")
            self._cannot_tell.setdefault((app_id, person_id), {
                "app_id": app_id, "app_name": caller.app_name, "person_id": person_id,
                "person_label": caller.person_label, "person_plan_id": person_plan_id, "app_plan_id": app_plan_id,
                "reason": reason,
            })
            return
        for item in sorted(set(was) | set(now)):
            before_refused, after_refused = was.get(item, ()), now.get(item, ())
            if bool(before_refused) == bool(after_refused):
                continue  # Refused before and after, or usable before and after: the caller's use of the item is unchanged.
            target, categories = (self._gains, before_refused) if before_refused else (self._losses, after_refused)
            for category in categories:
                target.setdefault((item, category, app_id, person_id), {
                    "item_id": item, "category_id": category, "category_label": self._labels.get(category, category),
                    "app_id": app_id, "app_name": caller.app_name, "person_id": person_id,
                    "person_label": caller.person_label,
                    "person_plan_id": person_plan_id, "app_plan_id": app_plan_id,
                })

    def result(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "gains": _ordered(self._gains.values(), ("item_id", "category_id", "app_id", "person_id")),
            "losses": _ordered(self._losses.values(), ("item_id", "category_id", "app_id", "person_id")),
            "cannot_tell": _ordered(self._cannot_tell.values(), ("app_id", "person_id")),
        }


def _ordered(entries: Iterable[dict[str, Any]], keys: tuple[str, ...]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda entry: tuple(entry[key] or "" for key in keys))
