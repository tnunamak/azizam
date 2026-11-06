"""Pure stock feasibility kernel (design §4.2, §14 B2). No IO and no clock: the caller passes `now`.

Stock balance, for the readings of one resource:

    remaining = total_lower − managed − outside_upper − reserve − uncertainty
    outside_upper = max(0, used_upper − V)      V = min(managed, included_managed): managed use the reading provably reflects

    free reading: remaining = free_lower − max(0, managed − V) − reserve − uncertainty

Full managed reservations are always charged. Each reading that is fresh and known gives one
residual, and the smallest one decides. Unknown or stale input never fits.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from gateway.arbitration.types import (
    SCHEMA_VERSION,
    SUPPORTED_CAPABILITIES,
    AccountingKind,
    AuthorityMode,
    BoundType,
    Claim,
    DecisionEnvelope,
    Demand,
    EvidenceRef,
    GuaranteeLevel,
    Known,
    Liability,
    NotApplicable,
    Observation,
    ObservationMeaning,
    Outcome,
    PolicySnapshot,
    Resource,
    ResourceResult,
    Unknown,
)

_GUARANTEE_RANK = {GuaranteeLevel.ADVISORY: 0, GuaranteeLevel.VALIDATED: 1, GuaranteeLevel.ENFORCED: 2}
# Earlier entries win when resources disagree. A definite no-fit beats "unknown".
_OUTCOME_PRIORITY = (Outcome.UNSUPPORTED, Outcome.NO_FIT, Outcome.UNKNOWN, Outcome.FITS)


def evaluate_stock(
    *,
    now: int,
    policy: PolicySnapshot,
    claim: Claim,
    resources: Mapping[str, Resource],
    observations: Sequence[Observation],
    liabilities: Mapping[str, Sequence[Liability]],
    active_classes: Iterable[str],
) -> DecisionEnvelope:
    """Return advice for one claim. Reserves nothing: the result is a value, never a grant."""
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise ValueError("now must be a non-negative int of Unix seconds")
    _check_units(resources, claim, observations, liabilities, policy)

    guarantee = _overall_guarantee(claim, resources)
    unsupported = sorted(claim.required - SUPPORTED_CAPABILITIES)
    if unsupported:
        return _envelope(policy, claim, Outcome.UNSUPPORTED, guarantee, (), (),
                         (f"unsupported capability: {', '.join(unsupported)}",))
    if claim.peak or claim.result or claim.transfers:
        return _envelope(policy, claim, Outcome.UNSUPPORTED, guarantee, (), (),
                         ("phase transitions are reserved and not implemented in this slice",))
    if claim.class_id not in policy.classes:
        return _envelope(policy, claim, Outcome.UNKNOWN_CLASS, guarantee, (), (),
                         ("the claim's class is not in the policy: failing closed",))
    actives = tuple(active_classes)
    if not _compatible(policy, claim.class_id, actives):
        return _envelope(policy, claim, Outcome.INCOMPATIBLE, guarantee, (), (),
                         ("an active holder's class is incompatible with this claim",))

    results: list[ResourceResult] = []
    evidence: list[EvidenceRef] = []
    for demand in sorted(claim.demands, key=lambda item: item.ref.id):
        result, used = _evaluate_demand(
            now, policy, demand, resources.get(demand.ref.id), observations,
            tuple(liabilities.get(demand.ref.id, ())),
        )
        results.append(result)
        evidence.extend(used)

    outcome = next((item for item in _OUTCOME_PRIORITY if any(r.outcome is item for r in results)),
                   Outcome.FITS)
    failing = tuple(r.ref.id for r in results if r.outcome is not Outcome.FITS)
    reasons = tuple(r.reason for r in results)
    facts = tuple(dict.fromkeys(evidence))  # one entry per distinct fact, in first-seen order
    # The effective level of each resource (after any downgrade) bounds the decision.
    guarantee = _weakest(result.guarantee for result in results)
    return _envelope(policy, claim, outcome, guarantee, tuple(results), failing, reasons, facts)


def _evaluate_demand(
    now: int,
    policy: PolicySnapshot,
    demand: Demand,
    resource: Resource | None,
    observations: Sequence[Observation],
    liabilities: tuple[Liability, ...],
) -> tuple[ResourceResult, list[EvidenceRef]]:
    ref = demand.ref
    guarantee = _guarantee(resource, demand.bound_type)

    def stop(outcome: Outcome, reason: str, required: int | None = None,
             residual: int | None = None, used: tuple[EvidenceRef, ...] = ()) -> tuple[ResourceResult, list[EvidenceRef]]:
        return ResourceResult(ref, outcome, required, residual, guarantee, reason), list(used)

    if resource is None:
        return stop(Outcome.UNKNOWN, "resource is not declared")
    if resource.accounting is not AccountingKind.STOCK:
        return stop(Outcome.UNSUPPORTED,
                    f"accounting kind {resource.accounting.value} is not supported in this slice")
    if demand.ref.incarnation != resource.ref.incarnation:
        return stop(Outcome.UNKNOWN, "demand names a different incarnation of the resource")

    readings = [o for o in observations
                if o.ref.id == ref.id and o.ref.incarnation == resource.ref.incarnation]
    totals = [o for o in readings if o.meaning is ObservationMeaning.TOTAL]
    usage = [o for o in readings if o.meaning in (ObservationMeaning.USED, ObservationMeaning.FREE)]
    if len(totals) != 1:
        return stop(Outcome.UNKNOWN, "total reading is missing or repeated")
    total = totals[0]
    total_value = _fresh_value(now, total)
    total_uncertainty = _uncertainty(total)
    if total_value is None or total_uncertainty is None:
        return stop(Outcome.UNKNOWN, "total reading is unknown or stale")

    if isinstance(demand.amount, NotApplicable):
        # An availability predicate: the resource must advertise capacity, but no quantity is charged.
        if total_value == 0:
            return stop(Outcome.NO_FIT, "availability predicate fails: no capacity advertised", None, 0,
                        used=(total.evidence,))
        return stop(Outcome.FITS, "availability predicate holds", used=(total.evidence,))
    if isinstance(demand.amount, Unknown):
        return stop(Outcome.UNKNOWN, demand.amount.reason)

    need = demand.amount.quantity.value
    if not usage:
        return stop(Outcome.UNKNOWN, "no used or free reading")

    managed = sum(item.quantity.value for item in liabilities)
    reserve = policy.limits[ref.id].value if ref.id in policy.limits else 0

    residuals: list[tuple[int, Observation]] = []
    evidence: list[EvidenceRef] = [total.evidence]
    for reading in usage:
        value = _fresh_value(now, reading)
        uncertainty = _uncertainty(reading)
        if value is None or uncertainty is None:
            return stop(Outcome.UNKNOWN, "a used or free reading is unknown or stale")
        # Included use can never exceed the managed total or what the reading itself shows as used.
        shown_used = value if reading.meaning is ObservationMeaning.USED else max(0, total_value - value)
        included = min(managed, _included(reading), shown_used)
        slack = total_uncertainty + uncertainty
        if reading.meaning is ObservationMeaning.USED:
            outside_upper = max(0, value - included)
            residuals.append((total_value - managed - outside_upper - reserve - slack, reading))
        else:
            residuals.append((value - max(0, managed - included) - reserve - slack, reading))
        evidence.append(reading.evidence)

    residual, deciding = min(residuals, key=lambda pair: pair[0])
    reason = f"needs {need}, leaves {residual}"
    if residual < 0:
        reason += ": negative residual is drift"
    # B2: when managed use exists but the deciding reading does not prove how much of it is included,
    # the arithmetic stays conservative and the guarantee is capped at validated.
    if managed > 0 and not isinstance(deciding.included_managed, Known):
        guarantee = _weakest([guarantee, GuaranteeLevel.VALIDATED])
        reason += "; inclusion unproven, so the guarantee is capped at validated"
    outcome = Outcome.FITS if residual >= need else Outcome.NO_FIT
    return ResourceResult(ref, outcome, need, residual, guarantee, reason), evidence


def _fresh_value(now: int, observation: Observation) -> int | None:
    """The reading's value, or None when it is unknown, has no validity horizon, or is stale or future-dated."""
    amount = observation.amount
    if not isinstance(amount, Known):
        return None
    evidence = observation.evidence
    if evidence.validity_s is None:
        return None
    age = now - evidence.observed_at
    if age < 0 or age > evidence.validity_s:
        return None
    return amount.quantity.value


def _included(observation: Observation) -> int:
    """Managed use proven included in the reading. Anything not Known proves nothing, so it is 0."""
    amount = observation.included_managed
    return amount.quantity.value if isinstance(amount, Known) else 0


def _uncertainty(observation: Observation) -> int | None:
    """Uncertainty as a count. None means unknown, which makes the whole decision unknown."""
    amount = observation.uncertainty
    if isinstance(amount, NotApplicable):
        return 0
    if isinstance(amount, Known):
        return amount.quantity.value
    return None


def _guarantee(resource: Resource | None, bound: BoundType) -> GuaranteeLevel:
    if resource is None or bound in (BoundType.ESTIMATE, BoundType.UNKNOWN):
        return GuaranteeLevel.ADVISORY
    if resource.authority is AuthorityMode.SHARED:
        return GuaranteeLevel.ADVISORY
    # A partition is an enforced sub-budget that this installation owns alone (design §3.1, §6).
    if bound is BoundType.ENFORCED and resource.authority in (AuthorityMode.EXCLUSIVE, AuthorityMode.PARTITIONED):
        return GuaranteeLevel.ENFORCED
    return GuaranteeLevel.VALIDATED


def _overall_guarantee(claim: Claim, resources: Mapping[str, Resource]) -> GuaranteeLevel:
    """The declared level across the resources a claim uses. Used only when no decision is evaluated."""
    return _weakest(_guarantee(resources.get(d.ref.id), d.bound_type) for d in claim.demands)


def _weakest(levels: Iterable[GuaranteeLevel]) -> GuaranteeLevel:
    """The weakest level. A claim with no levels bounds nothing."""
    return min(levels, key=lambda level: _GUARANTEE_RANK[level], default=GuaranteeLevel.ADVISORY)


def _compatible(policy: PolicySnapshot, class_id: str, actives: Sequence[str]) -> bool:
    """Direct check against each active holder. Compatibility need not be transitive."""
    for other in actives:
        if other not in policy.classes:
            return False
        if other == class_id:
            if class_id not in policy.self_compatible:
                return False
        elif frozenset((class_id, other)) not in policy.compatible:
            return False
    return True


def _check_units(
    resources: Mapping[str, Resource],
    claim: Claim,
    observations: Sequence[Observation],
    liabilities: Mapping[str, Sequence[Liability]],
    policy: PolicySnapshot,
) -> None:
    """Mismatched units are a caller error. They raise rather than produce a decision."""

    def check(resource_id: str, unit: object) -> None:
        resource = resources.get(resource_id)
        if resource is not None and unit != resource.unit:
            raise ValueError(f"unit mismatch for resource {resource_id!r}")

    for demand in claim.demands:
        if isinstance(demand.amount, Known):
            check(demand.ref.id, demand.amount.quantity.unit)
    for observation in observations:
        for amount in (observation.amount, observation.uncertainty, observation.included_managed):
            if isinstance(amount, Known):
                check(observation.ref.id, amount.quantity.unit)
    for resource_id, items in liabilities.items():
        for item in items:
            check(resource_id, item.quantity.unit)
    for resource_id, reserve in policy.limits.items():
        check(resource_id, reserve.unit)


def _envelope(
    policy: PolicySnapshot,
    claim: Claim,
    outcome: Outcome,
    guarantee: GuaranteeLevel,
    resources: tuple[ResourceResult, ...],
    failing: tuple[str, ...],
    reasons: tuple[str, ...],
    evidence: tuple[EvidenceRef, ...] = (),
) -> DecisionEnvelope:
    return DecisionEnvelope(
        schema_version=SCHEMA_VERSION,
        policy_revision=policy.revision,
        attempt=claim.attempt,
        outcome=outcome,
        guarantee=guarantee,
        resources=resources,
        failing=failing,
        reasons=reasons,
        evidence=evidence,
    )
