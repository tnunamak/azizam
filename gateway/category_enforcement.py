"""Apply the active owner category rules to runtime requests (design v3).

The gate adds a second, stricter decision after the existing access-plan
decision allowed a request. It never widens access.

Migration (design v3, R3): a request is governed only when its person or its
app appears in the active snapshot's plans. Requests from people and apps the
owner has not listed keep the existing access plans alone until the owner
adds them. A governed request needs a plan for BOTH its person and its app
(A10) and a service in the snapshot whose ID stem is the requested workflow
or model; anything else is denied with the kernel's stable reason. A check
that names no workflow or model (an early route check before resolution)
passes only if some service is allowed for the capability; every dispatch
path checks again with the resolved target.

Services are named by their runtime target: a snapshot service ``stem@rev``
governs requests whose workflow (or, without one, model) is ``stem``.

Categories are labels; access plans choose them (docs/design/CATEGORIES_AS_LABELS.md).
An item in one or more categories is usable only when the request's person plan
AND app plan both include every one of those categories. This is checked before
that, for everyone, listed or not, and the owner acting as themself is exempt. An
item in no category is governed by the plans' other settings only.

If an active snapshot exists but cannot be read, every request is denied
with ``POLICY_UNAVAILABLE`` (fail closed); with no active snapshot the gate
does nothing.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from gateway import category_policy as kernel, request_connections
from gateway.access_plans import AccessPlan, CategoryLabels, includes_category
from gateway.category_store import (
    CategoryStore, CorruptSnapshotError, NoActiveSnapshotError,
)
from gateway.policy import (
    DENIED,
    RULE_CATEGORY_HIDDEN as RULE_HIDDEN,
    RULE_CATEGORY_NO_GRANT as RULE_NO_GRANT,
    RULE_CATEGORY_NOT_ALLOWED as RULE_NOT_ALLOWED,
    RULE_CATEGORY_UNAVAILABLE as RULE_UNAVAILABLE,
    DecidingRule,
    PolicyDecision,
    PolicyRequest,
)

log = logging.getLogger(__name__)

SOURCE = "policy:categories"
HIDDEN = "CATEGORY_HIDDEN"  # The reason code of an item a category hides from this request.
UNAVAILABLE = "POLICY_UNAVAILABLE"

# The rule codes (``RULE_*``) are defined in gateway.policy beside the others.


def _rule(code: str, category: str = "") -> DecidingRule:
    return DecidingRule(code, "category", category or None)


def _stem(service_id: str) -> str:
    return service_id.rsplit("@", 1)[0] if "@" in service_id else service_id


def refusing_categories(item_categories: Iterable[str], person_plan: AccessPlan | None,
                        app_plan: AccessPlan | None) -> tuple[str, ...]:
    """The item's categories that a request with these person and app plans may not use, sorted.

    The one check every boundary uses (runtime, listings, plan summaries, the reach preview). A category is
    usable only when BOTH plans include it; an item in no category is never refused here. The owner exception
    is separate (``is_owner_person``).
    """
    return tuple(sorted(category for category in set(item_categories)
                        if not (includes_category(person_plan, category)
                                and includes_category(app_plan, category))))


def is_owner_person(person_id: str | None, owner_id: str | None) -> bool:
    """True when a request carries the instance owner's own identity: the only owner exception."""
    return bool(person_id) and person_id == owner_id


def category_members(version: kernel.PolicyVersion) -> dict[str, frozenset[str]]:
    """Service stem -> ids of the categories it belongs to: the gate's member index.

    Every category restricts its members now (access plans decide who may use them), so every category counts.
    """
    index: dict[str, frozenset[str]] = {}
    if version.categories:
        for key, service in version.services.items():
            found = frozenset(
                assertion.category
                for subject in kernel.resolve(service)
                for assertion in subject.assertions
                if assertion.accepted and assertion.category in version.categories)
            if found:
                index[_stem(key)] = found
    return index


class CategoryGate:
    """Caches the active snapshot until the store changes.

    A persistent read connection asks SQLite for ``PRAGMA data_version``,
    which changes when another connection commits. Only then does the gate
    reload the activation counter and snapshot, so a check costs one cheap
    query and an activation applies on the next check.
    """

    def __init__(self, store_path: Path, owner_id: Callable[[], str | None] | None = None):
        self._path = Path(store_path)
        self._owner_id = owner_id
        self._member_index: tuple[kernel.PolicyVersion, dict[str, frozenset[str]]] | None = None
        self._lock = threading.Lock()
        self._store: CategoryStore | None = None
        self._watch: sqlite3.Connection | None = None
        self._data_version: int | None = None
        self._counter: int | None = None
        self._version: kernel.PolicyVersion | None = None
        self._failed = False

    def _active(self) -> tuple[kernel.PolicyVersion | None, bool]:
        """Return (snapshot or None, unavailable), read once per read request.

        A listing checks one request per model, capability and workflow, so
        the store was asked for its version hundreds of times in a request.
        A read request now sees one snapshot; an activation applies on the
        next request. Outside a read request (inference, tests) every check
        asks the store, as before.
        """
        return request_connections.memo(("category-gate", id(self)), self._active_now)

    def _active_now(self) -> tuple[kernel.PolicyVersion | None, bool]:
        if not self._path.exists():
            if self._store is None:
                return None, False
            # The store this process has been enforcing is gone: fail closed.
            log.error("category rules unavailable: previously opened store is missing")
            return None, True
        from gateway.category_api import snapshot_from_json  # codec lives with the API
        with self._lock:
            try:
                if self._store is None:
                    self._store = CategoryStore(self._path)
                    self._watch = sqlite3.connect(str(self._path), timeout=5.0,
                                                  check_same_thread=False)
                data_version = self._watch.execute("PRAGMA data_version").fetchone()[0]
                if data_version == self._data_version and not self._failed:
                    return self._version, False
                counter = self._store.active_revision_counter
                if counter != self._counter or self._failed:
                    try:
                        row = self._store.get_active()
                    except NoActiveSnapshotError:
                        self._version = None
                    else:
                        version = snapshot_from_json(row.body)
                        kernel.validate_snapshot(version)
                        self._version, counter = version, row.active_counter
                    self._counter = counter
                self._data_version, self._failed = data_version, False
                return self._version, False
            except (CorruptSnapshotError, kernel.PolicyError, ValueError, OSError, sqlite3.Error) as exc:
                # Errors can contain request values or credential-bearing URLs.
                log.error("category rules unavailable (%s)", type(exc).__name__)
                self._failed = True
                return None, True
            except Exception as exc:  # noqa: BLE001 - fail closed on any store error
                log.error("category rules unavailable (%s)", type(exc).__name__)
                self._failed = True
                return None, True

    def _members_by_stem(self, version: kernel.PolicyVersion) -> dict[str, frozenset[str]]:
        """Service stem -> ids of the categories it belongs to, cached per snapshot."""
        cached = self._member_index
        if cached is not None and cached[0] is version:
            return cached[1]
        index = category_members(version)
        self._member_index = (version, index)
        return index

    def _is_owner(self, identity: Any) -> bool:
        person = getattr(identity, "person_id", None)
        if not person or self._owner_id is None:
            return False
        try:
            owner = request_connections.memo(("category-owner", id(self)), self._owner_id)
        except Exception as exc:  # noqa: BLE001 - an unreadable owner record never grants
            log.error("category rules: owner lookup failed (%s)", type(exc).__name__)
            return False
        return is_owner_person(person, owner)

    def _hidden_category(self, version: kernel.PolicyVersion, identity: Any,
                         request: PolicyRequest) -> str:
        """The id of a category that hides this item from the caller, else ''.

        The plans are the identity's own: the same snapshot of the admin store the access decision read.
        Without a plan, nothing is included, so a categorized item is hidden (fail closed).
        """
        index = self._members_by_stem(version)
        if not index:
            return ""
        categories: set[str] = set()
        # Both spellings: an alias must not reach what its model's category hides.
        for name in (request.workflow, request.model, request.requested_model):
            categories |= index.get(name or "", frozenset())
        if not categories:
            return ""
        access = getattr(identity, "access", None)
        refused = refusing_categories(categories, getattr(access, "person_plan", None),
                                      getattr(access, "app_plan", None))
        # The owner lookup costs a query, so ask it only when a plan check refused.
        if not refused or self._is_owner(identity):
            return ""
        return refused[0]

    def check(self, identity: Any, request: PolicyRequest) -> PolicyDecision | None:
        """Return a denial when the active rules refuse this request, else None."""
        version, unavailable = self._active()
        if unavailable:
            return PolicyDecision(decision=DENIED, sources=(SOURCE,),
                                  reasons=(f"category rules: {UNAVAILABLE}",),
                                  rule=_rule(RULE_UNAVAILABLE))
        if version is None:
            return None
        hidden = self._hidden_category(version, identity, request)
        if hidden:
            return PolicyDecision(decision=DENIED, sources=(SOURCE,),
                                  reasons=(f"category rules: {HIDDEN} {hidden}",),
                                  rule=_rule(RULE_HIDDEN, hidden))
        person = getattr(identity, "person_id", None) or ""
        app = getattr(identity, "app_id", None) or ""
        if person not in version.people and app not in version.apps:
            return None  # Not governed yet: the existing access plans decide.
        capability = request.intent or ""
        target = request.workflow or request.model or ""
        if not target:
            # Early route check, before the request names a service: pass only if
            # some service is allowed for this capability. The check after
            # resolution decides the exact service.
            decisions = [kernel.decision_for(version, {
                "person": person, "app": app, "service": key, "capability": capability,
            }) for key in version.services]
            if any(decision.outcome == "allow" for decision in decisions):
                return None
            if not decisions:
                return PolicyDecision(
                    decision=DENIED, sources=(SOURCE,),
                    reasons=(f"category rules: NO_GRANT (no service grants {capability or 'this request'})",),
                    rule=_rule(RULE_NO_GRANT))
            decision = decisions[0]
        else:
            service = next((key for key in version.services if _stem(key) == target), "")
            if not service:
                return PolicyDecision(
                    decision=DENIED, sources=(SOURCE,),
                    reasons=(f"category rules: NO_GRANT (no service for {target})",),
                    rule=_rule(RULE_NO_GRANT))
            decision = kernel.decision_for(version, {
                "person": person, "app": app, "service": service, "capability": capability,
            })
            if decision.outcome == "allow":
                return None
        detail = " ".join(part for part in (decision.rule, decision.category, decision.member) if part)
        return PolicyDecision(decision=DENIED, sources=(SOURCE,),
                              reasons=(f"category rules: {decision.reason} {detail}".strip(),),
                              rule=_rule(RULE_UNAVAILABLE if decision.reason == UNAVAILABLE else RULE_NOT_ALLOWED,
                                         "" if decision.reason == UNAVAILABLE else decision.category))


_GATE: CategoryGate | None = None


def configure(store_path: Path | None, owner_id: Callable[[], str | None] | None = None) -> None:
    """Install the process-wide gate (proxy startup); None disables it.

    ``owner_id`` returns the instance owner's person id; without it nobody is
    exempt from a category.
    """
    global _GATE
    _GATE = CategoryGate(store_path, owner_id) if store_path is not None else None


def apply(identity: Any, request: PolicyRequest, decision: PolicyDecision) -> PolicyDecision:
    """Tighten an allowed decision with the active category rules.

    A decision the access plans already deny keeps its reason, unless a category hides the item: then the
    caller gets the hidden answer, as everyone outside the category does, so a refusal never confirms the item exists.
    """
    if _GATE is None:
        return decision
    denial = _GATE.check(identity, request)
    if denial is None or not (decision.allowed or is_hidden(denial.reasons)):
        return decision
    # The access plan's allow reasons do not explain a denial; callers show reasons[0].
    return PolicyDecision(decision=DENIED, sources=decision.sources + denial.sources,
                          reasons=denial.reasons, visibility_level=decision.visibility_level,
                          rule=denial.rule)


def visible(identity: Any, request: PolicyRequest) -> bool:
    """Show a discovery entry only when its runtime category check passes."""
    return _GATE is None or _GATE.check(identity, request) is None


def rules_state() -> str:
    """``"none"``, ``"governs"`` when the active rules list a person or an app, or ``"unreadable"``.

    People and apps the rules list need a category for each service they use,
    so a model the owner adds is closed to them until it is put in a category.
    Rules that cannot be read refuse every request.
    """
    if _GATE is None:
        return "none"
    version, unavailable = _GATE._active()
    if unavailable:
        return "unreadable"
    return "governs" if version is not None and (version.people or version.apps) else "none"


def is_hidden(reasons: Any) -> bool:
    """True when a policy view says a category hides the item.

    Discovery lists drop such items instead of showing them as unavailable.
    """
    return any(f"category rules: {HIDDEN}" in str(reason) for reason in reasons or ())


def category_labels() -> CategoryLabels | None:
    """Category id -> the label the owner gave it, from the active rules.

    Read from the snapshot the checks read, so a summary and a decision agree. Empty when the gate is off or
    no rules are active; None when the rules cannot be read, which a plan summary must say. A label is a
    courtesy: the decisions themselves report an unreadable store (``category.rules_unavailable``).
    """
    if _GATE is None:
        return {}
    version, unavailable = _GATE._active()
    if unavailable:
        return None
    if version is None:
        return {}
    return {key: category.label for key, category in version.categories.items()}
