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

If an active snapshot exists but cannot be read, every request is denied
with ``POLICY_UNAVAILABLE`` (fail closed); with no active snapshot the gate
does nothing.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from gateway import category_policy as kernel, request_connections
from gateway.category_store import (
    CategoryStore, CorruptSnapshotError, NoActiveSnapshotError,
)
from gateway.policy import DENIED, PolicyDecision, PolicyRequest

log = logging.getLogger(__name__)

SOURCE = "policy:categories"


def _stem(service_id: str) -> str:
    return service_id.rsplit("@", 1)[0] if "@" in service_id else service_id


class CategoryGate:
    """Caches the active snapshot until the store changes.

    A persistent read connection asks SQLite for ``PRAGMA data_version``,
    which changes when another connection commits. Only then does the gate
    reload the activation counter and snapshot, so a check costs one cheap
    query and an activation applies on the next check.
    """

    def __init__(self, store_path: Path):
        self._path = Path(store_path)
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
            log.error("category rules unavailable: %s is missing", self._path)
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
                log.error("category rules unavailable: %s", exc)
                self._failed = True
                return None, True
            except Exception as exc:  # noqa: BLE001 - fail closed on any store error
                log.exception("category rules unavailable: %s", exc)
                self._failed = True
                return None, True

    def check(self, identity: Any, request: PolicyRequest) -> PolicyDecision | None:
        """Return a denial when the active rules refuse this request, else None."""
        version, unavailable = self._active()
        if unavailable:
            return PolicyDecision(decision=DENIED, sources=(SOURCE,),
                                  reasons=("category rules: POLICY_UNAVAILABLE",))
        if version is None:
            return None
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
                    reasons=(f"category rules: NO_GRANT (no service grants {capability or 'this request'})",))
            decision = decisions[0]
        else:
            service = next((key for key in version.services if _stem(key) == target), "")
            if not service:
                return PolicyDecision(
                    decision=DENIED, sources=(SOURCE,),
                    reasons=(f"category rules: NO_GRANT (no service for {target})",))
            decision = kernel.decision_for(version, {
                "person": person, "app": app, "service": service, "capability": capability,
            })
            if decision.outcome == "allow":
                return None
        detail = " ".join(part for part in (decision.rule, decision.category, decision.member) if part)
        return PolicyDecision(decision=DENIED, sources=(SOURCE,),
                              reasons=(f"category rules: {decision.reason} {detail}".strip(),))


_GATE: CategoryGate | None = None


def configure(store_path: Path | None) -> None:
    """Install the process-wide gate (proxy startup); None disables it."""
    global _GATE
    _GATE = CategoryGate(store_path) if store_path is not None else None


def apply(identity: Any, request: PolicyRequest, decision: PolicyDecision) -> PolicyDecision:
    """Tighten an allowed decision with the active category rules."""
    if _GATE is None or not decision.allowed:
        return decision
    denial = _GATE.check(identity, request)
    if denial is None:
        return decision
    # The access plan's allow reasons do not explain a denial; callers show reasons[0].
    return PolicyDecision(decision=DENIED, sources=decision.sources + denial.sources,
                          reasons=denial.reasons, visibility_level=decision.visibility_level)


def visible(identity: Any, request: PolicyRequest) -> bool:
    """Show a discovery entry only when its runtime category check passes."""
    return _GATE is None or _GATE.check(identity, request) is None
