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

Restricted categories (a category with ``visible_to`` set) are checked before
that, for everyone, listed or not. An item in a restricted category is visible
only to the owner (acting as themself) and to requests whose person plan and
app plan are both named by the category. This is what lets the owner mark a
workflow "Experimental" or "Adult" with one category row and no per-person rules.

If an active snapshot exists but cannot be read, every request is denied
with ``POLICY_UNAVAILABLE`` (fail closed); with no active snapshot the gate
does nothing.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gateway import category_policy as kernel, request_connections
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
HIDDEN = "CATEGORY_HIDDEN"  # The reason code of a restricted category (see ``visible_to``).
UNAVAILABLE = "POLICY_UNAVAILABLE"

# The rule codes (``RULE_*``) are defined in gateway.policy beside the others.


def _rule(code: str, category: str = "") -> DecidingRule:
    return DecidingRule(code, "category", category or None)


def _stem(service_id: str) -> str:
    return service_id.rsplit("@", 1)[0] if "@" in service_id else service_id


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
        self._restricted_index: tuple[kernel.PolicyVersion, dict[str, frozenset[str]]] | None = None
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

    def _restricted_by_stem(self, version: kernel.PolicyVersion) -> dict[str, frozenset[str]]:
        """Service stem -> ids of the restricted categories it belongs to."""
        cached = self._restricted_index
        if cached is not None and cached[0] is version:
            return cached[1]
        restricted = {key for key, category in version.categories.items() if category.restricted}
        index: dict[str, frozenset[str]] = {}
        if restricted:
            for key, service in version.services.items():
                found = frozenset(
                    assertion.category
                    for subject in kernel.resolve(service)
                    for assertion in subject.assertions
                    if assertion.accepted and assertion.category in restricted)
                if found:
                    index[_stem(key)] = found
        self._restricted_index = (version, index)
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
        return owner == person

    def _hidden_category(self, version: kernel.PolicyVersion, identity: Any,
                         request: PolicyRequest) -> str:
        """The id of a restricted category that hides this item from the caller, else ''."""
        index = self._restricted_by_stem(version)
        if not index:
            return ""
        categories: set[str] = set()
        # Both spellings: an alias must not reach what its model's category hides.
        for name in (request.workflow, request.model, request.requested_model):
            categories |= index.get(name or "", frozenset())
        if not categories:
            return ""
        access = getattr(identity, "access", None)
        plans = {getattr(getattr(access, name, None), "id", None)
                 for name in ("person_plan", "app_plan")}
        refused = [category_id for category_id in sorted(categories)
                   if None in plans or not plans <= set(version.categories[category_id].visible_to or ())]
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
    exempt from a restricted category.
    """
    global _GATE
    _GATE = CategoryGate(store_path, owner_id) if store_path is not None else None


def apply(identity: Any, request: PolicyRequest, decision: PolicyDecision) -> PolicyDecision:
    """Tighten an allowed decision with the active category rules."""
    if _GATE is None or not decision.allowed:
        return decision
    denial = _GATE.check(identity, request)
    if denial is None:
        return decision
    # The access plan's allow reasons do not explain a denial; callers show reasons[0].
    return PolicyDecision(decision=DENIED, sources=decision.sources + denial.sources,
                          reasons=denial.reasons, visibility_level=decision.visibility_level,
                          rule=denial.rule)


def visible(identity: Any, request: PolicyRequest) -> bool:
    """Show a discovery entry only when its runtime category check passes."""
    return _GATE is None or _GATE.check(identity, request) is None


def governs_anyone() -> bool:
    """True when the active rules list a person or an app, or cannot be read.

    Those people and apps need a category for each service they use, so a model
    the owner adds is closed to them until it is put in a category.
    """
    if _GATE is None:
        return False
    version, unavailable = _GATE._active()
    return unavailable or (version is not None and bool(version.people or version.apps))


def is_hidden(reasons: Any) -> bool:
    """True when a policy view says a restricted category hides the item.

    Discovery lists drop such items instead of showing them as unavailable.
    """
    return any(f"category rules: {HIDDEN}" in str(reason) for reason in reasons or ())


def category_labels() -> dict[str, str]:
    """Category id -> the label the owner gave it, from the active rules.

    Empty when the gate is off, no rules are active or they cannot be read:
    a label is a courtesy for Check access, and an unreadable store is already
    reported by the decisions themselves (``category.rules_unavailable``).
    """
    if _GATE is None:
        return {}
    version, unavailable = _GATE._active()
    if unavailable or version is None:
        return {}
    return {key: category.label for key, category in version.categories.items()}
