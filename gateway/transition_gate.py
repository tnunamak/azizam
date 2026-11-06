"""The transition gate for residency domains (slot T1).

A residency domain is every service, alias and forwarder that reaches one engine
process. Its key is ``residency.domain`` in the service config; the default is
the service id. Swap locks, serving leases and gate state all use this key, so
two services that share a process share one lock and one lease count.

A domain opts in with ``residency.transition.enabled``. For an enabled domain:

- every mediated dispatch holds a serving lease, minted under the domain lock;
- a lease is minted only when the domain's gate is ``open`` and no manual
  availability override restricts one of the domain's services (``admit``);
- the wait for the lock is bounded by ``admission_wait_s``; then the request
  gets 503 with Retry-After, and a request cancelled while it waits holds no
  lease.

A domain that is not enabled behaves as before: no gate read, no bound.

The invariant: in an enabled domain no request bytes go to the engine unless the
sending task holds an unreleased serving lease for that domain. Each lease is
recorded in the minting task's context (``record_minted_lease``). Every function
that builds a connection, client or URL for a send calls ``require_egress_lease``,
so a hop that forgot to lease is refused where it would send. Read-only health,
readiness and state GETs are exempt: they carry no caller data.

Step T1.1 supports pass-through domains only (``domain_violations``,
``require_supported``). A lease's domain is fixed when it is minted; a
configuration change that moves a service to another domain does not move the
leases it already holds.

A service whose process another owner runs declares
``residency.transition.activation: pass_through``. The gateway then never
starts, stops or swaps it on a request; the request only leases and forwards.

The gate table is the authority for gate state. Step T1.1 only reads it; a
missing row is ``open``. Step T1.2 adds the operation record that writes it.
If the table cannot be read, every enabled domain is ``reconciling``: the gate
fails closed, never open.

Engine-agnostic: domains and services are opaque strings.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from fastapi import HTTPException

from gateway.capability_ids import INNER_HOP_ENDPOINTS
from gateway.engine_contract_view import offerings_config, process_config

GATE_STATES = ("open", "closed", "failed_closed", "reconciling")
DEFAULT_ADMISSION_WAIT_S = 30.0
# A closed gate does not know when its transition ends. 30 s matches the swap
# drain's first retry; it is a hint, not a promise.
GATE_RETRY_AFTER_S = 30


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _residency(service: Mapping[str, Any]) -> Mapping[str, Any]:
    # The same choice as the runtime projection (service_registry.runtime_backend_from_service):
    # a non-empty top-level block wins; an empty one never hides the config's own block.
    residency = service.get("residency")
    if not (isinstance(residency, Mapping) and residency):
        residency = _mapping(service.get("config")).get("residency")
    return _mapping(residency)


def domain_of(service_id: str, service: Mapping[str, Any]) -> str:
    """The residency domain of one service: ``residency.domain``, else the service id."""
    domain = _residency(service).get("domain")
    return domain.strip() if isinstance(domain, str) and domain.strip() else service_id


def _declared_transition(service: Mapping[str, Any]) -> Mapping[str, Any]:
    return _mapping(_residency(service).get("transition"))


def transition_enabled(service: Mapping[str, Any]) -> bool:
    """True when this service record enables transition for its domain."""
    return _declared_transition(service).get("enabled") is True


def declares_pass_through(service: Mapping[str, Any]) -> bool:
    """True when this service enables transition with ``activation: pass_through``."""
    return transition_enabled(service) and _declared_transition(service).get("activation") == "pass_through"


def _positive(value: Any, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


@dataclass(frozen=True)
class TransitionPolicy:
    """What the gate applies to one lock or lease key."""

    domain: str
    enabled: bool = False
    admission_wait_s: float = DEFAULT_ADMISSION_WAIT_S
    services: tuple[str, ...] = ()
    # The named service declares ``activation: pass_through``: the gateway never
    # starts, stops or swaps its process (another owner runs it); a request only
    # leases and forwards.
    pass_through: bool = False


def policy_for(name: str, services: Mapping[str, Any]) -> TransitionPolicy:
    """The policy for ``name``: a service id, or a domain (or exclusive scope) key.

    The domain is enabled when any of its services enables transition. Its
    admission wait is the shortest one those services declare.
    """
    service = services.get(name)
    domain = domain_of(name, service) if isinstance(service, Mapping) else name
    pass_through = isinstance(service, Mapping) and declares_pass_through(service)
    members: list[str] = []
    waits: list[float] = []
    for service_id, record in services.items():
        if not isinstance(record, Mapping) or domain_of(str(service_id), record) != domain:
            continue
        members.append(str(service_id))
        if transition_enabled(record):
            declared = _declared_transition(record)
            waits.append(_positive(declared.get("admission_wait_s"), DEFAULT_ADMISSION_WAIT_S))
    if not waits:
        return TransitionPolicy(domain=domain, services=tuple(sorted(members)))
    return TransitionPolicy(domain=domain, enabled=True, admission_wait_s=min(waits),
                            services=tuple(sorted(members)), pass_through=pass_through)


class TransitionUnavailable(HTTPException):
    """The gate refused a lease. Nothing was dispatched; the caller answers 503."""

    def __init__(self, domain: str, *, reason: str, retry_after_s: int = GATE_RETRY_AFTER_S,
                 service: Optional[str] = None) -> None:
        self.domain = domain
        self.reason = reason
        self.service = service
        self.retry_after_s = max(1, int(retry_after_s))
        super().__init__(status_code=503, detail=f"residency domain {domain!r} is not admitting: {reason}",
                         headers={"Retry-After": str(self.retry_after_s)})


# The leases the current task minted. A tuple, replaced at each mint, so a task spawned later
# inherits the leases its parent held when it was spawned and never one minted after.
_EGRESS_LEASES: ContextVar[tuple[Any, ...]] = ContextVar("transition_egress_leases", default=())


def record_minted_lease(lease: Any) -> None:
    """Remember ``lease`` as held by the current task. Call from the minting task (``SwapDrain(on_mint=...)``)."""
    held = tuple(item for item in _EGRESS_LEASES.get() if not item.released)
    _EGRESS_LEASES.set(held + (lease,))


def holds_lease(domain: str) -> bool:
    """True when the current task holds an unreleased lease minted for ``domain``."""
    return any(item.key == domain and not item.released for item in _EGRESS_LEASES.get())


def require_egress_lease(policy: TransitionPolicy, service: Optional[str] = None) -> None:
    """Refuse a send to an enabled domain unless the sending task holds a lease for it.

    A domain that is not enabled is never refused. The refusal is ``path_not_leased``: the
    hop that is about to send did not take a lease (a missing hop, or a hop this step does
    not lease yet).
    """
    if policy.enabled and not holds_lease(policy.domain):
        raise TransitionUnavailable(policy.domain, reason="path_not_leased", service=service)


def require_supported(policy: TransitionPolicy, service: Optional[str] = None) -> None:
    """Refuse a request that would need a gateway-run activation in an enabled domain.

    Step T1.1 supports pass-through activation only: the gate refuses new leases,
    but it does not yet order the gateway's own start, switch and swap effects
    (step T1.2). The registry refuses such a domain; this is the runtime guard for
    any record that reaches the config another way.
    """
    if policy.enabled and not policy.pass_through:
        raise TransitionUnavailable(policy.domain, reason="activation_not_supported", service=service)


@dataclass(frozen=True)
class ServicePaths:
    """The ways a request reaches one service, split by whether the hop takes a serving lease.

    The names are for the error text only. The registry classifies a record (it knows the
    protocol shapes); this module only requires that nothing is left unleased.
    """

    leased: tuple[str, ...] = ()
    unleased: tuple[str, ...] = ()
    # Capabilities the service declares that the gateway lifecycle hop has no endpoint for.
    unserved_capabilities: tuple[str, ...] = ()


def gateway_owned_lifecycle(service: Mapping[str, Any]) -> Optional[str]:
    """What gateway-owned lifecycle ``service`` declares (a driver the gateway runs, or the gateway's idle
    stop), or None when another owner runs the process."""
    driver = str(process_config(service).get("driver") or "none").strip().lower()
    if driver not in ("none", "external"):
        return f"lifecycle driver {driver!r}"
    if str(_residency(service).get("idle_owner") or "").strip().lower() == "gateway":
        return "residency.idle_owner: gateway"
    return None


def offering_effect(offering: Mapping[str, Any]) -> Optional[str]:
    """What gateway-run effect selecting ``offering`` would need (a switch call, or an explicit load), else None.

    Step T1.1 orders no such effect, so a pass-through service cannot declare one.
    """
    selection = _mapping(offering.get("selection"))
    if selection.get("mode") == "switch":
        return "selection.mode: switch"
    if selection.get("implicit_load") is False:
        return "selection.implicit_load: false"
    return None


def domain_violations(services: Mapping[str, Any], *, paths: Callable[[str, Mapping[str, Any]], ServicePaths]
                      ) -> dict[str, str]:
    """``{service_id: error}`` for each service that an enabled domain cannot hold in step T1.1.

    In a domain that any member enables, a member is allowed only when:

    - it declares ``transition: {enabled: true, activation: pass_through}`` itself;
    - it declares no gateway-owned lifecycle (another owner runs the process);
    - every path to it is a leased hop, and there is at least one: ``paths(id, record)`` lists them.
    """
    enabled_domains = {domain_of(str(service_id), record) for service_id, record in services.items()
                       if isinstance(record, Mapping) and transition_enabled(record)}
    errors: dict[str, str] = {}
    for service_id, record in services.items():
        if not isinstance(record, Mapping):
            continue
        domain = domain_of(str(service_id), record)
        if domain not in enabled_domains:
            continue
        head = f"service {service_id!r} is in residency domain {domain!r}, which enables residency.transition"
        if not declares_pass_through(record):
            errors[str(service_id)] = (
                f"{head}; every service of that domain must declare residency.transition "
                "{enabled: true, activation: pass_through} (gateway-run activation is not supported yet)")
            continue
        owned = gateway_owned_lifecycle(record)
        if owned is not None:
            errors[str(service_id)] = (
                f"{head}; a pass-through service cannot declare a gateway-owned lifecycle ({owned}): "
                "another owner must run its process")
            continue
        for offering in offerings_config(record):
            effect = offering_effect(offering)
            if effect is not None:
                errors[str(service_id)] = (
                    f"{head}; its offering {offering.get('id')!r} declares {effect}, which needs a gateway-run "
                    "effect. A pass-through service cannot declare one")
                break
        if str(service_id) in errors:
            continue
        try:
            found = paths(str(service_id), record)
        except Exception as exc:  # noqa: BLE001 - a record that cannot be classified is never admitted
            errors[str(service_id)] = f"{head}; its paths cannot be classified ({type(exc).__name__})"
            continue
        if found.unleased:
            errors[str(service_id)] = (
                f"{head}; it is reachable by a path that takes no serving lease: {', '.join(found.unleased)}. "
                "Every path must be a leased hop: set config.gateway_lifecycle so the model router reaches it "
                "through the gateway, or declare an engine protocol, or use the native lane")
        elif found.unserved_capabilities:
            errors[str(service_id)] = (
                f"{head}; it declares the capability {', '.join(repr(word) for word in found.unserved_capabilities)}, "
                "which the gateway lifecycle hop has no endpoint for (it serves "
                f"{', '.join(sorted(path for paths in INNER_HOP_ENDPOINTS.values() for path in paths))}). "
                "Remove the capability or serve it another way")
        elif not found.leased:
            errors[str(service_id)] = (
                f"{head}; it has no leased path. Give it config.gateway_lifecycle, an engine protocol, "
                "or native_passthrough")
    return errors


_RESTRICTING_OVERRIDES = frozenset({"disabled", "maintenance", "draining"})


def admit(policy: TransitionPolicy, gate_state: str, overrides: Mapping[str, Any]) -> None:
    """Raise ``TransitionUnavailable`` unless an enabled domain may mint a lease now.

    Call under the domain lock, with the gate state and the manual availability
    overrides read in that hold. A domain that is not enabled is always admitted.
    """
    if not policy.enabled:
        return
    if gate_state != "open":
        raise TransitionUnavailable(policy.domain, reason=f"gate_{gate_state}")
    for service_id in policy.services:
        override = _mapping(overrides.get(service_id))
        state = str(override.get("state") or "available").strip().lower()
        if state in _RESTRICTING_OVERRIDES:
            raw_retry = override.get("retry_after_seconds")
            retry = int(_positive(raw_retry, 10))
            raise TransitionUnavailable(policy.domain, reason=f"service_{state}", retry_after_s=retry,
                                        service=service_id)


async def acquire_bounded(lock: asyncio.Lock, policy: Optional[TransitionPolicy]) -> None:
    """Acquire ``lock``; for an enabled domain, wait at most its ``admission_wait_s``.

    On timeout the lock is not held and ``TransitionUnavailable`` is raised.
    Cancellation while waiting leaves the lock not held (asyncio.Lock semantics).
    """
    if policy is None or not policy.enabled:
        await lock.acquire()
        return
    try:
        await asyncio.wait_for(lock.acquire(), timeout=policy.admission_wait_s)
    except asyncio.TimeoutError:
        raise TransitionUnavailable(policy.domain, reason="admission_timeout",
                                    retry_after_s=max(1, round(policy.admission_wait_s))) from None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS transition_gate (
    domain  TEXT PRIMARY KEY,
    state   TEXT NOT NULL CHECK (state IN ('open', 'closed', 'failed_closed', 'reconciling')),
    op_id   TEXT,
    updated REAL NOT NULL
)
"""


class GateStore:
    """Durable gate state per domain, with an in-process read cache.

    ``state`` reads memory only, so it is safe on the event loop. ``load`` fills the cache; call
    it at startup before the first admission. Each write (T1.2) refreshes the cache in the same
    process. While the table is unreadable, ``refresh_if_due`` retries the load at most every
    ``retry_s`` seconds; it does blocking I/O, so an async caller runs it off the event loop
    (``asyncio.to_thread``). A transient start-up error therefore does not keep enabled domains
    closed for long, and a failed load never fails open.
    """

    def __init__(self, path: Path | str, *, retry_s: float = 5.0, clock: Any = time.monotonic) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cache: dict[str, str] = {}
        self._readable = False
        self._retry_s = retry_s
        self._clock = clock
        self._last_load: Optional[float] = None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def load(self) -> None:
        with self._lock:
            self._last_load = self._clock()
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(_SCHEMA)
                rows = connection.execute("SELECT domain, state FROM transition_gate").fetchall()
        except (OSError, sqlite3.Error) as exc:
            print(f"[transition-gate] gate table unreadable, enabled domains fail closed: {exc}", flush=True)
            with self._lock:
                self._readable = False
                self._cache = {}
            return
        with self._lock:
            # A state this build does not know fails closed.
            self._cache = {str(domain): (state if state in GATE_STATES else "failed_closed")
                           for domain, state in rows}
            self._readable = True

    def refresh_if_due(self) -> None:
        """Retry the load when the table is unreadable and ``retry_s`` has passed. Blocking: not for the event loop."""
        with self._lock:
            due = not self._readable and (self._last_load is None or self._clock() - self._last_load >= self._retry_s)
            if due:
                # Claim the attempt, so concurrent callers do not all hit the table.
                self._last_load = self._clock()
        if due:
            self.load()

    def state(self, domain: str) -> str:
        """The cached gate state; ``reconciling`` while the table has never been read. Memory only."""
        with self._lock:
            if not self._readable:
                return "reconciling"
            return self._cache.get(domain, "open")

    def snapshot(self) -> dict[str, Any]:
        """Read-only diagnostics: every domain whose gate is not ``open``."""
        with self._lock:
            return {"readable": self._readable,
                    "domains": {domain: state for domain, state in sorted(self._cache.items()) if state != "open"}}
