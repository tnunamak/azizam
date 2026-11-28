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
    # Seconds a refused caller is told to wait (Retry-After). The shortest one a member declares (decision G8).
    retry_after_s: float = float(GATE_RETRY_AFTER_S)
    # The reviewed transition profile that runs this domain's release and load (T1.2). None: no gateway-run effect.
    profile: Optional[str] = None

    @property
    def lease_only(self) -> bool:
        """A request here only leases and forwards: another owner runs the process (pass-through), or the
        transition executor does (a profile). It never starts, stops or swaps the process itself."""
        return self.pass_through or self.profile is not None


def declared_profile(service: Mapping[str, Any]) -> Optional[str]:
    """The reviewed transition profile id this service names, else None."""
    profile = _declared_transition(service).get("profile")
    return profile.strip() if transition_enabled(service) and isinstance(profile, str) and profile.strip() else None


# Keys an owner may not set in a service's residency block: the reviewed profile holds them (D3 "Attestation is
# not owner data"). A config that carries one is refused at load.
PROFILE_ONLY_KEYS = frozenset({"attested", "failure_semantics", "idle_evidence", "operations", "verify", "residual",
                               "servable_check", "placement_keys", "fresh_within_s"})


def forbidden_profile_keys(service: Mapping[str, Any]) -> tuple[str, ...]:
    """The profile-only keys found anywhere under this service's residency block (sorted, without repeats)."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, inner in value.items():
                if key in PROFILE_ONLY_KEYS:
                    found.add(str(key))
                walk(inner)
        elif isinstance(value, (list, tuple)):
            for inner in value:
                walk(inner)

    walk(_residency(service))
    return tuple(sorted(found))


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
    retries: list[float] = []
    profiles: list[str] = []
    for service_id, record in services.items():
        if not isinstance(record, Mapping) or domain_of(str(service_id), record) != domain:
            continue
        members.append(str(service_id))
        if transition_enabled(record):
            declared = _declared_transition(record)
            waits.append(_positive(declared.get("admission_wait_s"), DEFAULT_ADMISSION_WAIT_S))
            retries.append(_positive(declared.get("retry_after_s"), float(GATE_RETRY_AFTER_S)))
            profile = declared_profile(record)
            if profile:
                profiles.append(profile)
    if not waits:
        return TransitionPolicy(domain=domain, services=tuple(sorted(members)))
    return TransitionPolicy(domain=domain, enabled=True, admission_wait_s=min(waits),
                            services=tuple(sorted(members)), pass_through=pass_through,
                            retry_after_s=min(retries), profile=sorted(profiles)[0] if profiles else None)


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
    if policy.enabled and not policy.pass_through and policy.profile is None:
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


def domain_violations(services: Mapping[str, Any], *, paths: Callable[[str, Mapping[str, Any]], ServicePaths],
                      profile_known: Optional[Callable[[str], bool]] = None) -> dict[str, str]:
    """``{service_id: error}`` for each service that an enabled domain cannot hold in step T1.1.

    In a domain that any member enables, a member is allowed only when:

    - it declares ``transition: {enabled: true, activation: pass_through}`` itself, or names a reviewed
      ``transition.profile`` (the transition executor then runs the domain's release and load);
    - it sets none of the profile-only keys (``attested``, ``failure_semantics``, ...) and, when
      ``profile_known`` is given, names a profile it knows;
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
        forbidden = forbidden_profile_keys(record)
        if forbidden:
            errors[str(service_id)] = (
                f"{head}; its residency block sets {', '.join(forbidden)}. Those belong to a reviewed transition "
                "profile, not to the service config: name the profile with residency.transition.profile")
            continue
        profile = declared_profile(record)
        if profile is not None and profile_known is not None and not profile_known(profile):
            errors[str(service_id)] = f"{head}; it names the transition profile {profile!r}, which is not a reviewed profile"
            continue
        if not (declares_pass_through(record) or profile is not None):
            errors[str(service_id)] = (
                f"{head}; every service of that domain must declare residency.transition "
                "{enabled: true, activation: pass_through} or name a reviewed residency.transition.profile")
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


def admit(policy: TransitionPolicy, gate_state: str, overrides: Mapping[str, Any], *, parked: bool = False) -> None:
    """Raise ``TransitionUnavailable`` unless an enabled domain may mint a lease now.

    Call under the domain lock, with the gate state and the manual availability
    overrides read in that hold. A domain that is not enabled is always admitted.
    A parked domain (its last finished operation is a park) serves only after a wake.
    """
    if not policy.enabled:
        return
    if gate_state != "open":
        raise TransitionUnavailable(policy.domain, reason=f"gate_{gate_state}", retry_after_s=policy.retry_after_s)
    if parked:
        raise TransitionUnavailable(policy.domain, reason="parked", retry_after_s=policy.retry_after_s)
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
    updated REAL NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0
)
"""

# The operation record (D2). ``OperationStore`` (gateway/transition_store.py) is its only writer; the DDL sits
# here so that opening the gate table also creates it, and ``load`` can read which domains are parked.
OPERATION_SCHEMA = """
CREATE TABLE IF NOT EXISTS transition_op (
    op_id           TEXT PRIMARY KEY,
    domain          TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('select', 'park', 'wake')),
    idempotency_key TEXT NOT NULL,
    request_hash    TEXT NOT NULL,
    target          TEXT,
    incarnation     TEXT,
    config_revision INTEGER NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('running', 'succeeded', 'aborted', 'failed_closed', 'reconciling')),
    step            TEXT,
    step_intent     TEXT,
    step_outcome    TEXT,
    evidence_ref    TEXT,
    snapshot_handle TEXT,
    compensates     TEXT,
    stock           TEXT,
    created         REAL NOT NULL,
    updated         REAL NOT NULL,
    seq             INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS transition_op_key ON transition_op (domain, idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS transition_op_one_unfinished ON transition_op (domain)
    WHERE state IN ('running', 'reconciling');
CREATE INDEX IF NOT EXISTS transition_op_domain_seq ON transition_op (domain, seq)
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
        self._parked: dict[str, str] = {}
        self._generations: dict[str, int] = {}
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
                connection.executescript(OPERATION_SCHEMA)
                # Serialize the migration, including concurrent first loads of an older database.
                connection.execute("BEGIN IMMEDIATE")
                columns = {row[1] for row in connection.execute("PRAGMA table_info(transition_gate)")}
                if "generation" not in columns:
                    connection.execute(
                        "ALTER TABLE transition_gate ADD COLUMN generation INTEGER NOT NULL DEFAULT 0")
                connection.commit()
                # Both queries must describe the same committed gate/park snapshot.
                connection.execute("BEGIN")
                rows = connection.execute("SELECT domain, state, generation FROM transition_gate").fetchall()
                # A domain is parked when its latest finished operation is a successful park (decision G5).
                last = connection.execute(
                    "SELECT domain, kind, op_id FROM transition_op WHERE state = 'succeeded' AND seq = "
                    "(SELECT MAX(seq) FROM transition_op AS later WHERE later.domain = transition_op.domain "
                    "AND later.state = 'succeeded')").fetchall()
        except (OSError, sqlite3.Error) as exc:
            print(f"[transition-gate] gate table unreadable, enabled domains fail closed: {exc}", flush=True)
            with self._lock:
                self._readable = False
                # Retain versions and holds while unreadable; state() still fails closed.
            return
        with self._lock:
            parked = {str(domain): str(op_id) for domain, kind, op_id in last if kind == "park"}
            for domain, state, generation in rows:
                domain = str(domain)
                # Missing rows in an older snapshot cannot erase a newer domain either.
                if int(generation) <= self._generations.get(domain, -1):
                    continue
                self._generations[domain] = int(generation)
                self._cache[domain] = state if state in GATE_STATES else "failed_closed"
                if domain in parked:
                    self._parked[domain] = parked[domain]
                else:
                    self._parked.pop(domain, None)
            self._readable = True

    def note_write(self, domain: str, state: str, parked_by: Optional[str] = None, *, clears_park: bool = False,
                   generation: int) -> None:
        """Refresh the cache after a committed gate write in this process (T1.2). Call after the commit.

        ``parked_by`` marks the domain parked by that operation; ``clears_park`` marks it not parked. A cache
        that was never read is loaded first, so a write never hides what the table holds.
        ``generation`` is the one the writer committed with the row; every caller supplies it, so no write
        can skip the fence. Equal generations are duplicates, including after a conservative in-memory hold.
        """
        with self._lock:
            readable = self._readable
        if not readable:
            self.load()
        with self._lock:
            if self._readable:
                if generation <= self._generations.get(domain, -1):
                    return
                self._generations[domain] = generation
                self._cache[domain] = state if state in GATE_STATES else "failed_closed"
                if parked_by is not None:
                    self._parked[domain] = parked_by
                elif clears_park:
                    self._parked.pop(domain, None)

    def force(self, domain: str, state: str, *, generation: Optional[int] = None) -> None:
        """Ignore stale versioned holds, just as note_write ignores stale publications.

        Delayed holds must pass a generation from a versioned unfinished observation.
        Omission retains immediate unconditional local holds for existing callers.

        Only non-open gate states may be forced; opening requires a newer durable publication.
        """
        if state not in GATE_STATES or state == "open":
            raise ValueError(f"residency domain {domain!r} cannot force gate state {state!r}; expected a hold state")
        with self._lock:
            if generation is not None and generation != self._generations.get(domain, 0):
                return
            self._generations.setdefault(domain, 0)
            self._cache[domain] = state

    def readable(self) -> bool:
        with self._lock:
            return self._readable

    def parked(self, domain: str) -> Optional[str]:
        """The op_id of the park that parked ``domain``, else None. Memory only."""
        with self._lock:
            return self._parked.get(domain)

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
