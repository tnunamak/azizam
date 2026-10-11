"""Apply the active owner category rules to runtime requests (design v3).

The gate adds a second, stricter decision after the existing access-plan
decision allowed a request. It never widens access.

Migration (design v3, R3): a request is governed only when its person or its
app appears in the active snapshot's plans. Requests from people and apps the
owner has not listed keep the existing access plans alone until the owner
adds them. A governed request needs a plan for BOTH its person and its app
(A10) and a service in the snapshot whose ID stem is the requested workflow
or model; anything else is denied with the kernel's stable reason.

Services are named by their runtime target: a snapshot service ``stem@rev``
governs requests whose workflow (or, without one, model) is ``stem``.

If an active snapshot exists but cannot be read, every request is denied
with ``POLICY_UNAVAILABLE`` (fail closed); with no active snapshot the gate
does nothing.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from gateway import category_policy as kernel
from gateway.category_store import (
    CategoryStore, CorruptSnapshotError, NoActiveSnapshotError,
)
from gateway.policy import DENIED, PolicyDecision, PolicyRequest

log = logging.getLogger(__name__)

SOURCE = "policy:categories"


def _stem(service_id: str) -> str:
    return service_id.rsplit("@", 1)[0] if "@" in service_id else service_id


class CategoryGate:
    """Caches the active snapshot by the store's activation counter."""

    def __init__(self, store_path: Path):
        self._path = Path(store_path)
        self._lock = threading.Lock()
        self._counter: int | None = None
        self._version: kernel.PolicyVersion | None = None
        self._failed = False

    def _active(self) -> tuple[kernel.PolicyVersion | None, bool]:
        """Return (snapshot or None, unavailable)."""
        if not self._path.exists():
            return None, False
        from gateway.category_api import snapshot_from_json  # codec lives with the API
        try:
            store = CategoryStore(self._path)
            counter = store.active_revision_counter
            with self._lock:
                if counter == self._counter and not self._failed:
                    return self._version, False
            try:
                row = store.get_active()
            except NoActiveSnapshotError:
                version = None
            else:
                version = snapshot_from_json(row.body)
            with self._lock:
                self._counter, self._version, self._failed = counter, version, False
            return version, False
        except (CorruptSnapshotError, kernel.PolicyError, ValueError, OSError) as exc:
            log.error("category rules unavailable: %s", exc)
            with self._lock:
                self._failed = True
            return None, True
        except Exception as exc:  # noqa: BLE001 - fail closed on any store error
            log.exception("category rules unavailable: %s", exc)
            with self._lock:
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
        target = request.workflow or request.model or ""
        service = next((key for key in version.services if _stem(key) == target), "")
        if not service:
            return PolicyDecision(
                decision=DENIED, sources=(SOURCE,),
                reasons=(f"category rules: NO_GRANT (no service for {target or 'this request'})",))
        decision = kernel.decision_for(version, {
            "person": person, "app": app, "service": service,
            "capability": request.intent or "",
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
    return PolicyDecision(decision=DENIED, sources=decision.sources + denial.sources,
                          reasons=decision.reasons + denial.reasons,
                          visibility_level=decision.visibility_level)
