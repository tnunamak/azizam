"""Wires the transition executor to the running gateway (design D2 and D4, step T1.2).

``TransitionRuntime`` builds the operation store, the steps and the executor once from what the gateway passes in
(no import of ``proxy``): the gate store, the residency domains' services, the per-domain swap lock, the lease
registry and a ``protocol_invoke`` that sends a declared operation of an engine record through the http-v1 path.
The adapter for a domain is the one generic ``DeclaredOperationAdapter`` for the reviewed profile the domain's
services name. No engine name appears here.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence

from gateway import transition_profiles
from gateway.swap_drain import SwapDrain
from gateway.transition_adapter import DeclaredOperationAdapter
from gateway.transition_executor import DEFAULTS, Executor, core_steps
from gateway.transition_gate import GateStore, TransitionPolicy, policy_for
from gateway.transition_ops import Step, StepName
from gateway.transition_store import SqliteOperationStore

ProtocolInvoke = Callable[[str, str, Mapping[str, Any]], Awaitable[Any]]
"""``(service_id, protocol_op, body)`` -> an http-v1 ``Reply``."""


class NoProfile(Exception):
    """The domain has no usable reviewed profile, so the gateway cannot run a transition for it."""


def transition_settings(services: Mapping[str, Any], policy: TransitionPolicy) -> dict[str, float]:
    """The deadlines and waits the owner declared under ``residency.transition``, by key in ``DEFAULTS``.
    Several services of one domain: the shortest value wins, as for ``admission_wait_s``."""
    found: dict[str, float] = {"retry_after_s": policy.retry_after_s}
    for service_id in policy.services:
        record = services.get(service_id)
        residency = record.get("residency") if isinstance(record, Mapping) else None
        declared = residency.get("transition") if isinstance(residency, Mapping) else None
        if not isinstance(declared, Mapping):
            continue
        for key in DEFAULTS:
            try:
                value = float(declared[key])
            except (KeyError, TypeError, ValueError):
                continue
            if value > 0:
                found[key] = min(value, found.get(key, value))
    return found


def member_for(services: Mapping[str, Any], policy: TransitionPolicy, *, target: Optional[str] = None) -> Optional[str]:
    """The service whose record a declared operation is sent to: the target when it belongs to the domain, else the
    first member (by id) that names the profile."""
    if target is not None and target in policy.services:
        return target
    for service_id in policy.services:
        record = services.get(service_id)
        residency = record.get("residency") if isinstance(record, Mapping) else None
        declared = residency.get("transition") if isinstance(residency, Mapping) else None
        if isinstance(declared, Mapping) and declared.get("profile") == policy.profile:
            return service_id
    return None


class TransitionRuntime:
    def __init__(self, *, gates: Callable[[], GateStore], services: Callable[[], Mapping[str, Any]],
                 lock_for: Callable[[str], Any], drain: Callable[[], SwapDrain], protocol_invoke: ProtocolInvoke,
                 config_revision: Callable[[], int] = lambda: 0, extra_steps: Optional[Mapping[StepName, Step]] = None,
                 snapshots: Any = None, startup: Sequence[Callable[[], None]] = ()) -> None:
        self._gates = gates
        self._services = services
        self._lock_for = lock_for
        self._drain = drain
        self._protocol_invoke = protocol_invoke
        self._config_revision = config_revision
        self._extra_steps = dict(extra_steps or {})
        self._snapshots = snapshots
        self._startup = tuple(startup)
        self._built: Optional[tuple[GateStore, Executor]] = None

    def policy(self, domain: str) -> TransitionPolicy:
        return policy_for(domain, self._services())

    def settings(self, domain: str) -> Mapping[str, object]:
        """``StepContext.policy``: the owner's deadlines and waits at the top level, ``checkpoint_policy`` and
        ``deadlines`` (owner arguments), and ``profile``: the reviewed profile mapping, the only source of attested
        fields (``failure_semantics``, ``servable_check``, ``placement_keys``, ``fresh_within_s``)."""
        services = self._services()
        held = policy_for(domain, services)
        found: dict[str, object] = dict(transition_settings(services, held))
        profile = transition_profiles.get(held.profile) if held.profile else None
        if profile is not None:
            found["profile"] = profile.as_policy()
        found["deadlines"] = {key[: -len("_deadline_s")]: value for key, value in found.items()
                              if key.endswith("_deadline_s") and isinstance(value, float)}
        found["checkpoint_policy"] = self._checkpoint_policy(services, held)
        return found

    @staticmethod
    def _checkpoint_policy(services: Mapping[str, Any], policy: TransitionPolicy) -> str:
        """``required`` when any service of the domain asks for it, else ``best_effort`` (design D2)."""
        for service_id in policy.services:
            record = services.get(service_id)
            residency = record.get("residency") if isinstance(record, Mapping) else None
            declared = residency.get("transition") if isinstance(residency, Mapping) else None
            if isinstance(declared, Mapping) and declared.get("checkpoint_policy") == "required":
                return "required"
        return "best_effort"

    def adapter(self, domain: str) -> DeclaredOperationAdapter:
        policy = self.policy(domain)
        profile = transition_profiles.get(policy.profile) if policy.profile else None
        if profile is None:
            raise NoProfile(domain)

        async def invoke(_domain: str, op: str, body: Mapping[str, Any]) -> Any:
            services = self._services()
            held = policy_for(domain, services)
            service_id = member_for(services, held, target=str(body["target"]) if "target" in body else None)
            if service_id is None:
                raise NoProfile(domain)
            return await self._protocol_invoke(service_id, op, body)

        return DeclaredOperationAdapter(profile, invoke)

    def executor(self) -> Executor:
        gates = self._gates()
        if self._built is not None and self._built[0] is gates:
            return self._built[1]
        store = SqliteOperationStore(gates)
        executor = Executor(store=store, steps={**core_steps(self._drain()), **self._extra_steps}, adapter_for=self.adapter,
                            policy_of=self.settings, lock_for=self._lock_for, config_revision=self._config_revision,
                            snapshots=self._snapshots)
        self._built = (gates, executor)
        return executor

    async def reconcile_startup(self) -> None:
        """Before any admission: the startup hooks (the snapshot store's ``startup_reconcile``), then the executor's
        recovery of unfinished operations. A hook that raises is logged and does not stop the gateway; the
        operations are still held by their gates."""
        for hook in self._startup:
            try:
                await asyncio.to_thread(hook)
            except Exception as exc:  # noqa: BLE001
                print(f"[transition-gate] startup hook failed ({type(exc).__name__}: {exc})", flush=True)
        await self.executor().reconcile_startup()

    def leases(self, domain: str) -> tuple[int, Optional[float]]:
        """``(active_leases, last_lease_at)`` for the domain, read-only from the lease registry. The count is the
        leases held right now; ``last_lease_at`` is epoch seconds of the last mint since the process started."""
        drain = self._drain()
        return drain.in_flight(domain), drain.last_lease_at(domain)

    def enabled(self, domain: str) -> bool:
        policy = self.policy(domain)
        return policy.enabled and policy.profile is not None and transition_profiles.known(policy.profile)
