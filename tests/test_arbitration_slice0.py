"""Resource arbitration slice 0: the stock kernel, holdings invariants, and the boundary rules (design §11, §14)."""

from __future__ import annotations

import ast
import dataclasses
import re
from pathlib import Path

import pytest

from gateway.arbitration.holdings import (
    Authority,
    Health,
    Holding,
    Lifecycle,
    OwnerKind,
    SettlementEvidence,
    SettlementKind,
    charged,
    duplicate,
    grant,
    join,
    leave,
    settle,
    timeout,
    transfer_to_residency,
    transport_done,
)
from gateway.arbitration.kernel import evaluate_stock
from gateway.arbitration.types import (
    AccountingKind,
    AttemptId,
    AuthorityMode,
    BoundType,
    Claim,
    Demand,
    EvidenceQuality,
    EvidenceRef,
    GuaranteeLevel,
    HoldingId,
    Known,
    Liability,
    NotApplicable,
    Observation,
    ObservationMeaning,
    OperationId,
    Outcome,
    PolicySnapshot,
    Quantity,
    Resource,
    ResourceRef,
    Unit,
    Unknown,
)

ROOT = Path(__file__).resolve().parent.parent
ARB = ROOT / "gateway" / "arbitration"
UNIT = Unit("unit", 1)
NOW = 1000


def ref(name: str, incarnation: str = "1") -> ResourceRef:
    return ResourceRef(name, incarnation)


def evidence(observed_at: int = NOW, validity_s: int | None = 60, sequence: int = 1) -> EvidenceRef:
    return EvidenceRef("source", 1, sequence, observed_at, validity_s, EvidenceQuality.MEASURED)


def reading(resource: ResourceRef, meaning: ObservationMeaning, value: int | None, *,
            observed_at: int = NOW, validity_s: int | None = 60, uncertainty: int | None = None,
            included: int | None = None) -> Observation:
    amount = Unknown("not advertised") if value is None else Known(Quantity(UNIT, value))
    unc = NotApplicable() if uncertainty is None else Known(Quantity(UNIT, uncertainty))
    inc = NotApplicable() if included is None else Known(Quantity(UNIT, included))
    return Observation(resource, meaning, amount, unc, inc, evidence(observed_at, validity_s))


def stock(resource: ResourceRef, authority: AuthorityMode = AuthorityMode.EXCLUSIVE,
          accounting: AccountingKind = AccountingKind.STOCK) -> Resource:
    return Resource(resource, UNIT, accounting, authority)


def demand(resource: ResourceRef, value: int, bound: BoundType = BoundType.ENFORCED) -> Demand:
    return Demand(resource, Known(Quantity(UNIT, value)), bound)


def liability(value: int, sequence: int = 1) -> Liability:
    return Liability(Quantity(UNIT, value), sequence)


def policy(classes=("A", "B", "C"), compatible=(), self_compatible=(), limits=None, revision: int = 7) -> PolicySnapshot:
    return PolicySnapshot(
        revision=revision,
        classes=frozenset(classes),
        compatible=frozenset(frozenset(pair) for pair in compatible),
        self_compatible=frozenset(self_compatible),
        limits={} if limits is None else limits,
    )


def decide(claims: Claim, resources: dict, observations, *, liabilities=None, active=(),
           pol: PolicySnapshot | None = None, now: int = NOW):
    return evaluate_stock(
        now=now,
        policy=pol or policy(),
        claim=claims,
        resources={r.ref.id: r for r in resources.values()} if isinstance(resources, dict) else resources,
        observations=observations,
        liabilities=liabilities or {},
        active_classes=active,
    )


def claim_of(*demands: Demand, class_id: str = "A", attempt: str = "att-1", required=frozenset()) -> Claim:
    return Claim(AttemptId(attempt), tuple(demands), class_id, required)


def residual_of(envelope, name: str) -> int | None:
    return next(r.residual for r in envelope.resources if r.ref.id == name)


# --- Unknown is never zero, never fits -------------------------------------------------------------

@pytest.mark.parametrize("total,used", [
    (reading(ref("r"), ObservationMeaning.TOTAL, None), reading(ref("r"), ObservationMeaning.USED, 10)),
    (reading(ref("r"), ObservationMeaning.TOTAL, 100), reading(ref("r"), ObservationMeaning.USED, None)),
])
def test_unknown_reading_gives_unknown_even_for_zero_demand(total, used) -> None:
    r = ref("r")
    envelope = decide(claim_of(demand(r, 0)), {"r": stock(r)}, [total, used])
    assert envelope.outcome is Outcome.UNKNOWN


def test_stale_and_future_readings_are_unknown() -> None:
    r = ref("r")
    stale_total = reading(r, ObservationMeaning.TOTAL, 100, observed_at=NOW - 61, validity_s=60)
    used = reading(r, ObservationMeaning.USED, 10)
    assert decide(claim_of(demand(r, 1)), {"r": stock(r)}, [stale_total, used]).outcome is Outcome.UNKNOWN

    future_total = reading(r, ObservationMeaning.TOTAL, 100, observed_at=NOW + 5)
    assert decide(claim_of(demand(r, 1)), {"r": stock(r)}, [future_total, used]).outcome is Outcome.UNKNOWN


def test_reading_without_declared_validity_is_unknown() -> None:
    r = ref("r")
    total = reading(r, ObservationMeaning.TOTAL, 100, validity_s=None)
    used = reading(r, ObservationMeaning.USED, 10)
    assert decide(claim_of(demand(r, 1)), {"r": stock(r)}, [total, used]).outcome is Outcome.UNKNOWN


def test_missing_total_or_undeclared_resource_is_unknown() -> None:
    r = ref("r")
    used = reading(r, ObservationMeaning.USED, 10)
    assert decide(claim_of(demand(r, 1)), {"r": stock(r)}, [used]).outcome is Outcome.UNKNOWN
    assert decide(claim_of(demand(r, 1)), {}, [used]).outcome is Outcome.UNKNOWN


# --- §14 B2: the 80-reserved / 20-used counterexample ---------------------------------------------

def counterexample_inputs(included: int | None):
    r = ref("pool")
    observations = [
        reading(r, ObservationMeaning.TOTAL, 100),
        reading(r, ObservationMeaning.USED, 50, included=included),  # aggregate used 50
    ]
    # One holding reserved at 80, of which 20 has materialized and is reflected in the reading.
    liabilities = {"pool": [liability(80, sequence=1)]}
    return r, observations, liabilities


def test_counterexample_outside_upper_is_30_and_next_claim_does_not_fit() -> None:
    r, observations, liabilities = counterexample_inputs(included=20)
    envelope = decide(claim_of(demand(r, 20)), {"pool": stock(r)}, observations, liabilities=liabilities)

    assert residual_of(envelope, "pool") == -10  # 100 − 80 − (50 − 20)
    assert envelope.outcome is Outcome.NO_FIT
    assert envelope.failing == ("pool",)


def test_counterexample_fits_only_when_the_total_really_has_room() -> None:
    r = ref("pool")
    observations = [
        reading(r, ObservationMeaning.TOTAL, 150),
        reading(r, ObservationMeaning.USED, 50, included=20),
    ]
    liabilities = {"pool": [liability(80, sequence=1)]}
    assert residual_of(decide(claim_of(demand(r, 40)), {"pool": stock(r)}, observations, liabilities=liabilities), "pool") == 40
    assert decide(claim_of(demand(r, 40)), {"pool": stock(r)}, observations, liabilities=liabilities).outcome is Outcome.FITS
    assert decide(claim_of(demand(r, 41)), {"pool": stock(r)}, observations, liabilities=liabilities).outcome is Outcome.NO_FIT


def test_unproven_inclusion_counts_the_materialized_use_twice_and_is_more_conservative() -> None:
    r, observations, liabilities = counterexample_inputs(included=None)
    envelope = decide(claim_of(demand(r, 1)), {"pool": stock(r)}, observations, liabilities=liabilities)
    assert residual_of(envelope, "pool") == 100 - 80 - 50  # V = 0 proves nothing is included


def test_a_reservation_ceiling_is_never_treated_as_included_use() -> None:
    # The round-2 review's trap: claiming the whole 80 reservation is in the reading would make
    # outside use look like 0 and admit 20. Only the attested 20 counts as included.
    r, observations, liabilities = counterexample_inputs(included=80)
    envelope = decide(claim_of(demand(r, 20)), {"pool": stock(r)}, observations, liabilities=liabilities)
    # V = min(managed 80, included 80) = 80, outside = 0: the adapter over-attested. The kernel cannot
    # detect that alone, so the attestation is the adapter's contract; with honest input (20) it refuses.
    assert residual_of(envelope, "pool") == 20
    honest = decide(claim_of(demand(r, 20)), {"pool": stock(r)}, *counterexample_inputs(included=20)[1:2],
                    liabilities=liabilities)
    assert honest.outcome is Outcome.NO_FIT


def test_full_managed_reservations_are_always_charged() -> None:
    r = ref("pool")
    observations = [reading(r, ObservationMeaning.TOTAL, 100), reading(r, ObservationMeaning.USED, 0, included=70)]
    liabilities = {"pool": [liability(70, sequence=3)]}
    assert residual_of(decide(claim_of(demand(r, 30)), {"pool": stock(r)}, observations, liabilities=liabilities), "pool") == 30
    assert decide(claim_of(demand(r, 31)), {"pool": stock(r)}, observations, liabilities=liabilities).outcome is Outcome.NO_FIT


# --- paired readings: total/free and total/used give the same decision -------------------------

# Consistent data only: the included managed part cannot exceed the used or managed totals.
CONSISTENT_PAIRS = [
    (total, managed, included, used)
    for total in (100, 150)
    for managed, included in ((0, 0), (20, 0), (20, 20), (80, 20), (80, 80))
    for used in (20, 50, 90)
    if used >= included and total >= used
]


@pytest.mark.parametrize("total,managed,included,used", CONSISTENT_PAIRS)
def test_paired_total_free_and_total_used_readings_agree(total, managed, included, used) -> None:
    r = ref("pool")
    liabilities = {"pool": [liability(managed, sequence=1)]} if managed else {}
    free = total - used
    as_used = [reading(r, ObservationMeaning.TOTAL, total), reading(r, ObservationMeaning.USED, used, included=included)]
    as_free = [reading(r, ObservationMeaning.TOTAL, total), reading(r, ObservationMeaning.FREE, free, included=included)]
    for want in (1, 10, 40, 200):
        by_used = decide(claim_of(demand(r, want)), {"pool": stock(r)}, as_used, liabilities=liabilities)
        by_free = decide(claim_of(demand(r, want)), {"pool": stock(r)}, as_free, liabilities=liabilities)
        assert by_used.outcome is by_free.outcome
        assert residual_of(by_used, "pool") == residual_of(by_free, "pool")


def test_two_readings_of_one_resource_use_the_more_conservative_residual() -> None:
    r = ref("pool")
    observations = [
        reading(r, ObservationMeaning.TOTAL, 100),
        reading(r, ObservationMeaning.USED, 30),  # residual 70
        reading(r, ObservationMeaning.FREE, 50),  # residual 50
    ]
    assert residual_of(decide(claim_of(demand(r, 1)), {"pool": stock(r)}, observations), "pool") == 50


# --- atomic multi-resource feasibility ---------------------------------------------------------

def test_two_resources_are_all_or_none_and_failures_are_named() -> None:
    a, b = ref("alpha"), ref("beta")
    observations = [
        reading(a, ObservationMeaning.TOTAL, 100), reading(a, ObservationMeaning.USED, 0),
        reading(b, ObservationMeaning.TOTAL, 10), reading(b, ObservationMeaning.USED, 0),
    ]
    resources = {"alpha": stock(a), "beta": stock(b)}

    fits = decide(claim_of(demand(a, 50), demand(b, 10)), resources, observations)
    assert fits.outcome is Outcome.FITS and fits.failing == ()

    partial = decide(claim_of(demand(a, 50), demand(b, 11)), resources, observations)
    assert partial.outcome is Outcome.NO_FIT
    assert partial.failing == ("beta",)
    assert {r.ref.id: r.outcome for r in partial.resources} == {"alpha": Outcome.FITS, "beta": Outcome.NO_FIT}


# --- availability predicate and guarantee levels ---------------------------------------------

def test_not_applicable_is_an_availability_predicate_not_a_zero_demand() -> None:
    r = ref("gate")
    available = [reading(r, ObservationMeaning.TOTAL, 5), reading(r, ObservationMeaning.USED, None)]
    no_capacity = [reading(r, ObservationMeaning.TOTAL, 0), reading(r, ObservationMeaning.USED, None)]
    unknown_total = [reading(r, ObservationMeaning.TOTAL, None), reading(r, ObservationMeaning.USED, None)]
    predicate = claim_of(Demand(r, NotApplicable(), BoundType.ESTIMATE))

    assert decide(predicate, {"gate": stock(r)}, available).outcome is Outcome.FITS
    assert decide(predicate, {"gate": stock(r)}, no_capacity).outcome is Outcome.NO_FIT
    assert decide(predicate, {"gate": stock(r)}, unknown_total).outcome is Outcome.UNKNOWN


def test_guarantee_is_the_weakest_across_resources() -> None:
    a, b = ref("alpha"), ref("beta")
    observations = [
        reading(a, ObservationMeaning.TOTAL, 100), reading(a, ObservationMeaning.USED, 0),
        reading(b, ObservationMeaning.TOTAL, 100), reading(b, ObservationMeaning.USED, 0),
    ]
    resources = {"alpha": stock(a), "beta": stock(b)}

    enforced = decide(claim_of(demand(a, 1), demand(b, 1)), resources, observations)
    assert enforced.guarantee is GuaranteeLevel.ENFORCED

    mixed = decide(claim_of(demand(a, 1), demand(b, 1, BoundType.ESTIMATE)), resources, observations)
    assert mixed.guarantee is GuaranteeLevel.ADVISORY
    assert {r.ref.id: r.guarantee for r in mixed.resources} == {
        "alpha": GuaranteeLevel.ENFORCED, "beta": GuaranteeLevel.ADVISORY}


@pytest.mark.parametrize("authority,bound,level", [
    (AuthorityMode.EXCLUSIVE, BoundType.ENFORCED, GuaranteeLevel.ENFORCED),
    (AuthorityMode.PARTITIONED, BoundType.ENFORCED, GuaranteeLevel.ENFORCED),
    (AuthorityMode.EXCLUSIVE, BoundType.CONSERVATIVE, GuaranteeLevel.VALIDATED),
    (AuthorityMode.EXCLUSIVE, BoundType.ESTIMATE, GuaranteeLevel.ADVISORY),
    (AuthorityMode.EXCLUSIVE, BoundType.UNKNOWN, GuaranteeLevel.ADVISORY),
    (AuthorityMode.SHARED, BoundType.ENFORCED, GuaranteeLevel.ADVISORY),
])
def test_guarantee_levels_follow_authority_and_bound(authority, bound, level) -> None:
    r = ref("pool")
    observations = [reading(r, ObservationMeaning.TOTAL, 100), reading(r, ObservationMeaning.USED, 0)]
    envelope = decide(claim_of(demand(r, 1, bound)), {"pool": stock(r, authority)}, observations)
    assert envelope.guarantee is level


def test_availability_decision_names_the_total_reading_it_used() -> None:
    r = ref("gate")
    for total, outcome in ((5, Outcome.FITS), (0, Outcome.NO_FIT)):
        total_reading = reading(r, ObservationMeaning.TOTAL, total)
        observations = [total_reading, reading(r, ObservationMeaning.USED, None)]
        envelope = decide(claim_of(Demand(r, NotApplicable(), BoundType.ESTIMATE)), {"gate": stock(r)}, observations)
        assert envelope.outcome is outcome
        assert total_reading.evidence in envelope.evidence


# --- §6 / M3: unproven inclusion caps the guarantee at validated -------------------------------

def used_with_inclusion(resource: ResourceRef, value: int, included) -> Observation:
    return Observation(resource, ObservationMeaning.USED, Known(Quantity(UNIT, value)), NotApplicable(),
                       included, evidence())


def test_unproven_inclusion_with_managed_use_lowers_the_guarantee_to_validated() -> None:
    r = ref("pool")
    observations = [reading(r, ObservationMeaning.TOTAL, 100),
                    used_with_inclusion(r, 20, Unknown("inclusion not attested"))]
    envelope = decide(claim_of(demand(r, 10)), {"pool": stock(r)}, observations,
                      liabilities={"pool": [liability(20)]})
    assert envelope.outcome is Outcome.FITS
    assert residual_of(envelope, "pool") == 60  # the conservative arithmetic is kept
    assert envelope.resources[0].guarantee is GuaranteeLevel.VALIDATED
    assert envelope.guarantee is GuaranteeLevel.VALIDATED
    assert "inclusion unproven" in envelope.resources[0].reason


def test_proven_inclusion_keeps_the_enforced_level() -> None:
    r = ref("pool")
    observations = [reading(r, ObservationMeaning.TOTAL, 100),
                    used_with_inclusion(r, 20, Known(Quantity(UNIT, 20)))]
    envelope = decide(claim_of(demand(r, 10)), {"pool": stock(r)}, observations,
                      liabilities={"pool": [liability(20)]})
    assert envelope.outcome is Outcome.FITS
    assert residual_of(envelope, "pool") == 80
    assert envelope.guarantee is GuaranteeLevel.ENFORCED


def test_unproven_inclusion_without_managed_use_is_not_downgraded() -> None:
    r = ref("pool")
    observations = [reading(r, ObservationMeaning.TOTAL, 100),
                    used_with_inclusion(r, 20, Unknown("inclusion not attested"))]
    envelope = decide(claim_of(demand(r, 10)), {"pool": stock(r)}, observations)
    assert envelope.guarantee is GuaranteeLevel.ENFORCED


def test_one_unproven_resource_sets_the_overall_level() -> None:
    a, b = ref("alpha"), ref("beta")
    observations = [
        reading(a, ObservationMeaning.TOTAL, 100), used_with_inclusion(a, 20, Unknown("inclusion not attested")),
        reading(b, ObservationMeaning.TOTAL, 100), reading(b, ObservationMeaning.USED, 0),
    ]
    envelope = decide(claim_of(demand(a, 10), demand(b, 10)), {"alpha": stock(a), "beta": stock(b)}, observations,
                      liabilities={"alpha": [liability(20)]})
    assert envelope.outcome is Outcome.FITS
    assert {r.ref.id: r.guarantee for r in envelope.resources} == {
        "alpha": GuaranteeLevel.VALIDATED, "beta": GuaranteeLevel.ENFORCED}
    assert envelope.guarantee is GuaranteeLevel.VALIDATED


# --- outcome priority across resources: UNSUPPORTED > NO_FIT > UNKNOWN > FITS --------------------

def test_no_fit_beats_unknown_across_two_resources() -> None:
    a, b = ref("alpha"), ref("beta")
    observations = [reading(a, ObservationMeaning.TOTAL, 10), reading(a, ObservationMeaning.USED, 0),
                    reading(b, ObservationMeaning.TOTAL, None), reading(b, ObservationMeaning.USED, 0)]
    envelope = decide(claim_of(demand(a, 50), demand(b, 1)), {"alpha": stock(a), "beta": stock(b)}, observations)
    assert {r.ref.id: r.outcome for r in envelope.resources} == {"alpha": Outcome.NO_FIT, "beta": Outcome.UNKNOWN}
    assert envelope.outcome is Outcome.NO_FIT


def test_unsupported_beats_no_fit_across_two_resources() -> None:
    a, b = ref("alpha"), ref("beta")
    observations = [reading(a, ObservationMeaning.TOTAL, 100), reading(a, ObservationMeaning.USED, 0),
                    reading(b, ObservationMeaning.TOTAL, 10), reading(b, ObservationMeaning.USED, 0)]
    resources = {"alpha": stock(a, accounting=AccountingKind.WINDOW_BUDGET), "beta": stock(b)}
    envelope = decide(claim_of(demand(a, 1), demand(b, 50)), resources, observations)
    assert {r.ref.id: r.outcome for r in envelope.resources} == {"alpha": Outcome.UNSUPPORTED, "beta": Outcome.NO_FIT}
    assert envelope.outcome is Outcome.UNSUPPORTED


# --- compatibility: a direct, non-transitive relation with a self rule -----------------------

def test_compatibility_is_pairwise_not_transitive() -> None:
    r = ref("pool")
    observations = [reading(r, ObservationMeaning.TOTAL, 100), reading(r, ObservationMeaning.USED, 0)]
    pol = policy(compatible=[("A", "B"), ("B", "C")])
    resources = {"pool": stock(r)}

    assert decide(claim_of(demand(r, 1), class_id="A"), resources, observations, active=["B"], pol=pol).outcome is Outcome.FITS
    assert decide(claim_of(demand(r, 1), class_id="C"), resources, observations, active=["B"], pol=pol).outcome is Outcome.FITS
    assert decide(claim_of(demand(r, 1), class_id="A"), resources, observations, active=["C"], pol=pol).outcome is Outcome.INCOMPATIBLE


def test_self_compatibility_is_its_own_rule() -> None:
    r = ref("pool")
    observations = [reading(r, ObservationMeaning.TOTAL, 100), reading(r, ObservationMeaning.USED, 0)]
    resources = {"pool": stock(r)}
    refused = decide(claim_of(demand(r, 1), class_id="A"), resources, observations, active=["A"], pol=policy())
    allowed = decide(claim_of(demand(r, 1), class_id="A"), resources, observations, active=["A"],
                     pol=policy(self_compatible=["A"]))
    assert refused.outcome is Outcome.INCOMPATIBLE
    assert allowed.outcome is Outcome.FITS


def test_unknown_claim_class_fails_closed() -> None:
    r = ref("pool")
    observations = [reading(r, ObservationMeaning.TOTAL, 100), reading(r, ObservationMeaning.USED, 0)]
    assert decide(claim_of(demand(r, 1), class_id="Z"), {"pool": stock(r)}, observations).outcome is Outcome.UNKNOWN_CLASS


# --- unsupported kinds and capabilities fail explicitly -------------------------------------

@pytest.mark.parametrize("kind", [AccountingKind.WINDOW_BUDGET, AccountingKind.BALANCE, AccountingKind.OPAQUE_BUDGET])
def test_unsupported_accounting_kind_gives_unsupported(kind) -> None:
    r = ref("budget")
    observations = [reading(r, ObservationMeaning.TOTAL, 100), reading(r, ObservationMeaning.USED, 0)]
    envelope = decide(claim_of(demand(r, 1)), {"budget": stock(r, accounting=kind)}, observations)
    assert envelope.outcome is Outcome.UNSUPPORTED
    assert envelope.resources[0].outcome is Outcome.UNSUPPORTED


def test_required_capability_that_is_not_supported_fails_closed() -> None:
    r = ref("pool")
    observations = [reading(r, ObservationMeaning.TOTAL, 100), reading(r, ObservationMeaning.USED, 0)]
    envelope = decide(claim_of(demand(r, 1), required=frozenset({"accounting:window"})),
                      {"pool": stock(r)}, observations)
    assert envelope.outcome is Outcome.UNSUPPORTED
    assert "accounting:window" in envelope.reasons[0]


# --- validation -------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [-1, 1.0, True, 2**63, 10**30])
def test_quantity_rejects_negative_float_bool_and_overflow(value) -> None:
    with pytest.raises(ValueError):
        Quantity(UNIT, value)


def test_quantity_accepts_the_largest_value_below_the_bound() -> None:
    assert Quantity(UNIT, 2**63 - 1).value == 2**63 - 1


def test_mismatched_units_raise_rather_than_decide() -> None:
    r = ref("pool")
    other = Unit("other", 1)
    bad = Observation(r, ObservationMeaning.TOTAL, Known(Quantity(other, 100)), NotApplicable(), 0, evidence())
    with pytest.raises(ValueError):
        decide(claim_of(demand(r, 1)), {"pool": stock(r)}, [bad, reading(r, ObservationMeaning.USED, 0)])


def test_reserved_phase_fields_fail_closed_as_unsupported() -> None:
    r = ref("pool")
    observations = [reading(r, ObservationMeaning.TOTAL, 100), reading(r, ObservationMeaning.USED, 0)]
    resources = {"pool": stock(r)}
    with_peak = Claim(AttemptId("p"), (demand(r, 1),), "A", peak=(demand(r, 5),))
    with_transfer = Claim(AttemptId("t"), (demand(r, 1),), "A", transfers=("attempt-to-residency",))
    assert decide(with_peak, resources, observations).outcome is Outcome.UNSUPPORTED
    assert decide(with_transfer, resources, observations).outcome is Outcome.UNSUPPORTED
    assert decide(claim_of(demand(r, 1)), resources, observations).outcome is Outcome.FITS


def test_a_claim_names_each_resource_at_most_once() -> None:
    r = ref("pool")
    with pytest.raises(ValueError):
        claim_of(demand(r, 1), demand(r, 2))


def test_evaluation_is_pure_and_the_envelope_is_frozen() -> None:
    r, observations, liabilities = counterexample_inputs(included=20)
    snapshot = list(observations)
    first = decide(claim_of(demand(r, 20)), {"pool": stock(r)}, observations, liabilities=liabilities)
    second = decide(claim_of(demand(r, 20)), {"pool": stock(r)}, observations, liabilities=liabilities)
    assert first == second
    assert observations == snapshot
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.outcome = Outcome.FITS  # type: ignore[misc]


# --- metamorphic: names, order and unrelated resources do not change the decision --------------

def scenario(names: tuple[str, str, str], *, order=(0, 1), extra: bool = False):
    a_name, b_name, cls = names
    a, b = ref(a_name), ref(b_name)
    observations = [
        reading(a, ObservationMeaning.TOTAL, 100), reading(a, ObservationMeaning.USED, 40),
        reading(b, ObservationMeaning.TOTAL, 10), reading(b, ObservationMeaning.USED, 2),
    ]
    resources = {a_name: stock(a), b_name: stock(b)}
    demands = [demand(a, 50), demand(b, 7)]
    if extra:
        unrelated = ref("unrelated-" + a_name)
        observations += [reading(unrelated, ObservationMeaning.TOTAL, 0), reading(unrelated, ObservationMeaning.USED, None)]
        resources["unrelated-" + a_name] = stock(unrelated)
    claim = Claim(AttemptId("m"), tuple(demands[i] for i in order), cls)
    return evaluate_stock(now=NOW, policy=policy(classes=("A", "B", "C"), revision=3), claim=claim,
                          resources=resources, observations=observations, liabilities={}, active_classes=())


def decision_shape(envelope, names: dict[str, str]):
    """The decision with resource ids mapped back to canonical names, so renamed runs can be compared."""
    by_name = {names[r.ref.id]: (r.outcome, r.required, r.residual, r.guarantee) for r in envelope.resources}
    return envelope.outcome, envelope.guarantee, tuple(sorted(names[f] for f in envelope.failing)), by_name


def test_renaming_ids_and_classes_keeps_the_decision() -> None:
    base = scenario(("alpha", "beta", "A"))
    renamed = scenario(("zz-9", "qq-2", "C"))
    names = {"zz-9": "alpha", "qq-2": "beta"}
    assert decision_shape(base, {"alpha": "alpha", "beta": "beta"}) == decision_shape(renamed, names)


def test_permuting_demand_order_keeps_the_decision() -> None:
    assert decision_shape(scenario(("alpha", "beta", "A"), order=(0, 1)), {"alpha": "alpha", "beta": "beta"}) == \
        decision_shape(scenario(("alpha", "beta", "A"), order=(1, 0)), {"alpha": "alpha", "beta": "beta"})


def test_adding_an_unrelated_resource_keeps_the_decision() -> None:
    without = scenario(("alpha", "beta", "A"))
    with_extra = scenario(("alpha", "beta", "A"), extra=True)
    assert without.outcome is with_extra.outcome is Outcome.FITS
    assert without.failing == with_extra.failing == ()
    assert residual_of(without, "alpha") == residual_of(with_extra, "alpha")


# --- holdings: invariants of the transition model ------------------------------------------------

def holding(name: str = "h1", value: int = 40, owner: OwnerKind = OwnerKind.ATTEMPT) -> Holding:
    return Holding(HoldingId(name), owner, ref("pool"), Quantity(UNIT, value), OperationId("op-1"))


def test_timeout_transport_done_and_duplicate_never_release() -> None:
    state = grant((), holding())
    assert charged(state, ref("pool")) == 40

    after_timeout = timeout(state, HoldingId("h1"))
    assert after_timeout[0].lifecycle is Lifecycle.OPEN
    assert after_timeout[0].authority is Authority.LOST
    assert after_timeout[0].health is Health.STALE
    assert charged(after_timeout, ref("pool")) == 40

    assert charged(transport_done(state, HoldingId("h1")), ref("pool")) == 40
    assert charged(duplicate(after_timeout, HoldingId("h1")), ref("pool")) == 40


def settled(kind: SettlementKind = SettlementKind.STOCK_RELEASED, *, operation: str = "op-1",
            resource: ResourceRef | None = None, source: str = "authoritative_status") -> SettlementEvidence:
    return SettlementEvidence(HoldingId("h1"), OperationId(operation), kind, resource or ref("pool"), source)


def test_settle_needs_evidence_carrying_the_operation_key() -> None:
    state = grant((), holding())
    wrong_operation = settled(operation="op-other")
    assert charged(settle(state, wrong_operation), ref("pool")) == 40

    assert charged(settle(state, settled()), ref("pool")) == 0


def test_an_attempt_ending_is_not_stock_release() -> None:
    # The failing trace from review: a residency whose last member left still holds its stock until released.
    state = grant((), holding())
    state = transfer_to_residency(state, HoldingId("h1"), AttemptId("a1"))
    state = leave(state, HoldingId("h1"), AttemptId("a1"))
    state = settle(state, settled(SettlementKind.EXECUTION_ENDED, source="process_exit"))
    assert charged(state, ref("pool")) == 40


def test_stock_released_for_the_same_ref_releases_the_holding() -> None:
    state = grant((), holding())
    state = settle(state, settled(SettlementKind.STOCK_RELEASED))
    assert charged(state, ref("pool")) == 0
    assert state[0].lifecycle is Lifecycle.SETTLED


def test_stock_released_for_another_incarnation_does_not_release() -> None:
    state = grant((), holding())
    state = settle(state, settled(SettlementKind.STOCK_RELEASED, resource=ResourceRef("pool", "2")))
    assert charged(state, ref("pool")) == 40
    assert state[0].lifecycle is Lifecycle.OPEN


def test_settle_accepts_a_reconciling_holding_and_a_second_settle_is_a_no_op() -> None:
    reconciling = dataclasses.replace(holding(), lifecycle=Lifecycle.RECONCILING)
    state = settle((reconciling,), settled())
    assert state[0].lifecycle is Lifecycle.SETTLED
    assert charged(state, ref("pool")) == 0
    assert settle(state, settled()) == state


def test_the_other_transitions_still_require_an_open_holding() -> None:
    reconciling = (dataclasses.replace(holding(), lifecycle=Lifecycle.RECONCILING),)
    assert timeout(reconciling, HoldingId("h1")) == reconciling
    assert transfer_to_residency(reconciling, HoldingId("h1"), AttemptId("a1")) == reconciling


def test_charged_counts_each_incarnation_of_a_resource_separately() -> None:
    first = Holding(HoldingId("h1"), OwnerKind.ATTEMPT, ResourceRef("pool", "1"), Quantity(UNIT, 40), OperationId("op-1"))
    second = Holding(HoldingId("h2"), OwnerKind.ATTEMPT, ResourceRef("pool", "2"), Quantity(UNIT, 30), OperationId("op-2"))
    state = grant(grant((), first), second)
    assert charged(state, ResourceRef("pool", "1")) == 40
    assert charged(state, ResourceRef("pool", "2")) == 30


def test_a_resource_cannot_hold_two_units_at_once() -> None:
    state = grant((), holding())
    other_unit = Holding(HoldingId("h2"), OwnerKind.ATTEMPT, ref("pool"), Quantity(Unit("other", 1), 5), OperationId("op-2"))
    with pytest.raises(ValueError):
        grant(state, other_unit)


def test_a_settled_holding_no_longer_fixes_the_unit_of_its_resource() -> None:
    settled_state = settle(grant((), holding()), settled())
    other_unit = Holding(HoldingId("h2"), OwnerKind.ATTEMPT, ref("pool"), Quantity(Unit("other", 1), 5), OperationId("op-2"))
    assert charged(grant(settled_state, other_unit), ref("pool")) == 5


def test_residency_is_charged_once_whatever_its_member_count() -> None:
    state = grant((), holding())
    state = transfer_to_residency(state, HoldingId("h1"), AttemptId("a1"))
    assert charged(state, ref("pool")) == 40

    state = join(state, HoldingId("h1"), AttemptId("a2"))
    state = join(state, HoldingId("h1"), AttemptId("a3"))
    assert charged(state, ref("pool")) == 40

    state = leave(state, HoldingId("h1"), AttemptId("a1"))
    state = leave(state, HoldingId("h1"), AttemptId("a2"))
    assert charged(state, ref("pool")) == 40  # the residency stays until a release is verified

    settle_key = settled()
    assert charged(settle(state, settle_key), ref("pool")) == 40  # a member is still there

    state = leave(state, HoldingId("h1"), AttemptId("a3"))
    assert charged(settle(state, settle_key), ref("pool")) == 0


def test_an_attempt_end_does_not_settle_a_residency() -> None:
    state = transfer_to_residency(grant((), holding()), HoldingId("h1"), AttemptId("a1"))
    state = leave(state, HoldingId("h1"), AttemptId("a1"))
    assert state[0].owner_kind is OwnerKind.RESIDENCY
    assert charged(state, ref("pool")) == 40


# --- import boundary and domain vocabulary -------------------------------------------------------

CORE = ("kernel", "types", "holdings")
# The SQLite adapter and its read interface may import only the stdlib and this package (design §11 slice 1).
ADAPTERS = ("store", "report")
DOMAIN_WORDS = ("gpu", "vram", "cuda", "comfy", "model", "speech", "image", "interactive", "background")


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text())
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
    return names


def test_core_modules_import_only_the_stdlib_and_the_arbitration_package() -> None:
    import sys

    stdlib = sys.stdlib_module_names
    for module in CORE + ADAPTERS:
        for name in _imports(ARB / f"{module}.py"):
            top = name.split(".")[0]
            allowed = top in stdlib or name == "gateway.arbitration" or name.startswith("gateway.arbitration.")
            assert allowed, f"{module}.py imports {name}"


def test_no_domain_word_appears_in_the_arbitration_package_outside_legacy() -> None:
    pattern = re.compile(r"\b(" + "|".join(DOMAIN_WORDS) + r")s?\b", re.IGNORECASE)
    for path in sorted(ARB.glob("*.py")):
        if path.name == "legacy.py":
            continue
        hits = pattern.findall(path.read_text())
        assert hits == [], f"{path.name} contains domain words: {hits}"


# --- guarantee floor for empty claims and predicates ----------------------------------------

def test_a_claim_with_no_demands_bounds_nothing() -> None:
    envelope = decide(claim_of(), {}, [])
    assert envelope.outcome is Outcome.FITS
    assert envelope.guarantee is GuaranteeLevel.ADVISORY


def test_observation_evidence_is_reported_once_per_distinct_fact() -> None:
    r = ref("pool")
    observations = [reading(r, ObservationMeaning.TOTAL, 100), reading(r, ObservationMeaning.USED, 0)]
    envelope = decide(claim_of(demand(r, 1)), {"pool": stock(r)}, observations)
    assert len(envelope.evidence) == len(set(envelope.evidence))
