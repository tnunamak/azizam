"""Differential golden tests: legacy admission decisions against the stock kernel, through gateway/arbitration/legacy.py.

Each converted case must agree on admit versus not-admit. A case that cannot be converted without loss must
report LegacyOnly, never a guessed answer. The legacy modules themselves are not changed.
"""

from __future__ import annotations

from gateway.arbitration.legacy import (
    LegacyOnly,
    RouteView,
    ShadowReport,
    legacy_advice,
    legacy_capacity_view,
    legacy_claim_view,
    legacy_route_view,
    shadow_compare,
    shadow_pool_advice,
)
from gateway.arbitration.types import (
    AuthorityMode,
    DecisionEnvelope,
    Outcome,
    ResourceRef,
)
from gateway.exclusive_pool import (
    MIB,
    ExclusivePoolSpec,
    PoolObservation,
    snapshot_and_claim,
)
from gateway.resource_admission import (
    AdmissionConstraints,
    CurrentLoad,
    ResourceEstimate,
    ServiceAvailability,
    advise_resource_admission,
)
from gateway.resource_claims import (
    AdmissionDecision,
    AdmissionRequest,
    AdmissionStatus,
    CapacitySnapshot,
    QueueClass,
    ResourceClaim,
    evaluate_admission,
)

NOW = 1000
VALIDITY = 60
INVENTORY = {
    "vram_mb": ResourceRef("gpu0-vram", "1"),
    "ram_mb": ResourceRef("ram", "1"),
    "disk_mb": ResourceRef("disk", "1"),
    "gpu0": ResourceRef("lock:gpu0", "1"),
}
POOL = ExclusivePoolSpec(
    pool_id="gpu0",
    max_claim_bytes=8 * 1024 * MIB,
    reserve_bytes=512 * MIB,
    retry_after_seconds=30,
    timeout_seconds=5,
    observer_service="observer",
    observer_path="/facts",
    free_pointer="/free",
    total_pointer="/total",
)


def old(claim: ResourceClaim, snapshot: CapacitySnapshot) -> AdmissionDecision:
    return evaluate_admission(AdmissionRequest("golden", claim), snapshot)


def new(claim: ResourceClaim, snapshot: CapacitySnapshot, *, validity_s: int | None = VALIDITY):
    return legacy_advice(claim, snapshot, INVENTORY, now=NOW, validity_s=validity_s,
                         authority=AuthorityMode.EXCLUSIVE)


def assert_agrees(old_decision: AdmissionDecision, new_advice) -> ShadowReport:
    report = shadow_compare(old_decision, new_advice)
    assert report.status == "agree", (old_decision.status, new_advice, report)
    return report


def test_insufficient_vram_is_delayed_in_both_and_the_kernel_names_no_fit() -> None:
    claim = ResourceClaim(gpu_required=True, vram_mb=18_000)
    snapshot = CapacitySnapshot(total_vram_mb=24_000, used_vram_mb=8_000, observed_at=NOW)

    decision, advice = old(claim, snapshot), new(claim, snapshot)

    assert decision.status is AdmissionStatus.DELAYED
    assert isinstance(advice, DecisionEnvelope)
    assert advice.outcome is Outcome.NO_FIT
    assert advice.resources[0].residual == 16_000
    assert_agrees(decision, advice)


def test_vram_with_a_fitting_claim_is_admitted_in_both() -> None:
    claim = ResourceClaim(gpu_required=True, vram_mb=8_000)
    snapshot = CapacitySnapshot(total_vram_mb=24_000, used_vram_mb=4_000, observed_at=NOW)

    decision, advice = old(claim, snapshot), new(claim, snapshot)

    assert decision.status is AdmissionStatus.ACCEPTED
    assert advice.outcome is Outcome.FITS
    assert_agrees(decision, advice)


def test_gpu_required_with_zero_vram_is_an_availability_predicate_not_a_zero_demand() -> None:
    claim = ResourceClaim(gpu_required=True, vram_mb=0)
    available = CapacitySnapshot(total_vram_mb=24_000, used_vram_mb=None, observed_at=NOW)
    no_gpu = CapacitySnapshot(total_vram_mb=0, used_vram_mb=0, observed_at=NOW)
    unknown = CapacitySnapshot(total_vram_mb=None, used_vram_mb=None, observed_at=NOW)

    assert old(claim, available).status is AdmissionStatus.ACCEPTED
    assert_agrees(old(claim, available), new(claim, available))
    assert old(claim, no_gpu).status is AdmissionStatus.DELAYED
    assert new(claim, no_gpu).outcome is Outcome.NO_FIT
    assert_agrees(old(claim, no_gpu), new(claim, no_gpu))
    assert old(claim, unknown).status is AdmissionStatus.DELAYED
    assert new(claim, unknown).outcome is Outcome.UNKNOWN
    assert_agrees(old(claim, unknown), new(claim, unknown))


def test_none_is_not_zero_for_an_unadvertised_total() -> None:
    claim = ResourceClaim(gpu_required=True, vram_mb=8_000)
    snapshot = CapacitySnapshot(total_vram_mb=None, used_vram_mb=0, observed_at=NOW)

    assert old(claim, snapshot).status is AdmissionStatus.DELAYED
    assert new(claim, snapshot).outcome is Outcome.UNKNOWN
    assert_agrees(old(claim, snapshot), new(claim, snapshot))


def test_zero_used_is_a_known_zero_not_an_unknown() -> None:
    claim = ResourceClaim(gpu_required=True, vram_mb=20_000)
    snapshot = CapacitySnapshot(total_vram_mb=24_000, used_vram_mb=0, observed_at=NOW)
    assert new(claim, snapshot).outcome is Outcome.FITS
    assert_agrees(old(claim, snapshot), new(claim, snapshot))


def test_ram_none_and_zero_are_both_no_demand_and_disk_is_checked_when_claimed() -> None:
    no_ram = ResourceClaim(ram_mb=None)
    zero_ram = ResourceClaim(ram_mb=0)
    snapshot = CapacitySnapshot(total_ram_mb=None, used_ram_mb=None, observed_at=NOW)

    for claim in (no_ram, zero_ram):
        assert old(claim, snapshot).status is AdmissionStatus.ACCEPTED
        assert new(claim, snapshot).outcome is Outcome.FITS
        assert_agrees(old(claim, snapshot), new(claim, snapshot))

    disk_claim = ResourceClaim(disk_mb=500)
    disk_snapshot = CapacitySnapshot(total_disk_mb=1_000, used_disk_mb=600, observed_at=NOW)
    assert old(disk_claim, disk_snapshot).status is AdmissionStatus.DELAYED
    assert new(disk_claim, disk_snapshot).outcome is Outcome.NO_FIT
    assert_agrees(old(disk_claim, disk_snapshot), new(disk_claim, disk_snapshot))


def test_exclusive_lock_conflict_is_no_fit_and_none_is_not_an_empty_set() -> None:
    claim = ResourceClaim(exclusive_locks=("gpu0",))
    held = CapacitySnapshot(active_exclusive_locks=frozenset({"gpu0"}), observed_at=NOW)
    free = CapacitySnapshot(active_exclusive_locks=frozenset(), observed_at=NOW)
    unknown = CapacitySnapshot(active_exclusive_locks=None, observed_at=NOW)

    assert old(claim, held).status is AdmissionStatus.DELAYED
    assert new(claim, held).outcome is Outcome.NO_FIT
    assert_agrees(old(claim, held), new(claim, held))

    assert old(claim, free).status is AdmissionStatus.ACCEPTED
    assert new(claim, free).outcome is Outcome.FITS
    assert_agrees(old(claim, free), new(claim, free))

    assert old(claim, unknown).status is AdmissionStatus.DELAYED
    assert new(claim, unknown).outcome is Outcome.UNKNOWN
    assert_agrees(old(claim, unknown), new(claim, unknown))


def test_a_lock_not_in_the_inventory_stays_legacy_only() -> None:
    claim = ResourceClaim(exclusive_locks=("unmapped",))
    snapshot = CapacitySnapshot(active_exclusive_locks=frozenset(), observed_at=NOW)
    assert isinstance(legacy_claim_view(claim, INVENTORY), LegacyOnly)
    assert isinstance(new(claim, snapshot), LegacyOnly)


def test_legacy_float_cpu_is_not_converted() -> None:
    for cpu in (2.0, 0.0):  # a zero float is also not converted, to keep the rule simple
        claim = ResourceClaim(cpu_cores=cpu)
        snapshot = CapacitySnapshot(total_cpu_cores=8, used_cpu_cores=1, observed_at=NOW)
        assert isinstance(legacy_claim_view(claim, INVENTORY), LegacyOnly)
        advice = new(claim, snapshot)
        assert isinstance(advice, LegacyOnly)
        assert shadow_compare(old(claim, snapshot), advice).status == "legacy-only"


def test_queue_and_profile_concurrency_stay_legacy_only() -> None:
    queued = CapacitySnapshot(
        active_by_queue={QueueClass.BATCH: 2},
        queue_limits={QueueClass.BATCH: 2},
        observed_at=NOW,
    )
    assert isinstance(new(ResourceClaim(queue_class=QueueClass.BATCH), queued), LegacyOnly)

    profiled = ResourceClaim(profile_id="profile-fast")
    assert isinstance(legacy_claim_view(profiled, INVENTORY), LegacyOnly)


def test_a_snapshot_without_observation_time_is_not_converted() -> None:
    claim = ResourceClaim(gpu_required=True, vram_mb=8_000)
    snapshot = CapacitySnapshot(total_vram_mb=24_000, used_vram_mb=4_000)  # observed_at None
    assert isinstance(legacy_capacity_view(snapshot, INVENTORY, validity_s=VALIDITY), LegacyOnly)
    assert isinstance(new(claim, snapshot), LegacyOnly)


def test_a_stale_legacy_snapshot_stays_legacy_only_because_the_legacy_path_never_expires_it() -> None:
    # The legacy evaluator ignores observed_at, so its ACCEPTED is not comparable with a kernel UNKNOWN.
    claim = ResourceClaim(gpu_required=True, vram_mb=8_000)
    snapshot = CapacitySnapshot(total_vram_mb=24_000, used_vram_mb=4_000, observed_at=NOW - 900)
    advice = new(claim, snapshot)
    assert isinstance(advice, LegacyOnly)
    assert advice.reason.startswith("freshness not preserved")
    assert shadow_compare(old(claim, snapshot), advice).status == "legacy-only"


def test_a_future_dated_legacy_snapshot_stays_legacy_only() -> None:
    claim = ResourceClaim(gpu_required=True, vram_mb=8_000)
    snapshot = CapacitySnapshot(total_vram_mb=24_000, used_vram_mb=4_000, observed_at=NOW + 5)
    advice = new(claim, snapshot)
    assert isinstance(advice, LegacyOnly)
    assert advice.reason.startswith("freshness not preserved")


def test_a_vram_value_beyond_the_quantity_bound_stays_legacy_only() -> None:
    claim = ResourceClaim(gpu_required=True, vram_mb=2**63)
    snapshot = CapacitySnapshot(total_vram_mb=2**63, used_vram_mb=0, observed_at=NOW)
    old(claim, snapshot)  # the legacy path still answers; the golden check below must not raise
    advice = new(claim, snapshot)
    assert isinstance(advice, LegacyOnly)
    assert shadow_compare(old(claim, snapshot), advice).status == "legacy-only"


def test_an_unclaimed_dimension_is_not_converted_so_its_large_value_is_irrelevant() -> None:
    claim = ResourceClaim()
    snapshot = CapacitySnapshot(total_ram_mb=2**63, observed_at=NOW)
    advice = new(claim, snapshot)
    assert isinstance(advice, DecisionEnvelope)
    assert advice.outcome is Outcome.FITS
    assert_agrees(old(claim, snapshot), advice)


def test_an_exclusive_lock_named_like_a_numeric_inventory_key_stays_legacy_only() -> None:
    claim = ResourceClaim(exclusive_locks=("vram_mb",))
    snapshot = CapacitySnapshot(active_exclusive_locks=frozenset(), observed_at=NOW)
    advice = new(claim, snapshot)
    assert isinstance(advice, LegacyOnly)
    assert shadow_compare(old(claim, snapshot), advice).status == "legacy-only"


def test_queue_limits_block_conversion_even_when_the_claim_has_no_queue_demand() -> None:
    snapshot = CapacitySnapshot(queue_limits={QueueClass.BATCH: 4}, observed_at=NOW)
    assert isinstance(legacy_capacity_view(snapshot, INVENTORY, validity_s=VALIDITY), LegacyOnly)


# --- the exclusive pool: shadow of snapshot_and_claim, reserve already embedded ----------------

def pool_pair(free_mb: int):
    total = 24 * 1024 * MIB
    return snapshot_and_claim(POOL, PoolObservation(free_mb * MIB, total), observed_at=float(NOW))


def test_pool_shadow_agrees_with_the_legacy_decision_across_every_free_mib() -> None:
    total_mb = 24 * 1024
    disagreements = []
    for free_mb in range(total_mb + 1):
        pair = pool_pair(free_mb)
        snapshot, claim = pair
        legacy = evaluate_admission(AdmissionRequest("pool", claim), snapshot)
        advice = shadow_pool_advice(pair, INVENTORY, now=NOW, validity_s=VALIDITY,
                                    authority=AuthorityMode.EXCLUSIVE)
        report = shadow_compare(legacy, advice)
        if report.status != "agree":
            disagreements.append((free_mb, legacy.status, report))
    assert disagreements == []


def test_pool_shadow_embeds_the_reserve_once() -> None:
    pair = pool_pair(15_872)  # used = 8704 + 512 reserve, available = 15360
    advice = shadow_pool_advice(pair, INVENTORY, now=NOW, validity_s=VALIDITY,
                                authority=AuthorityMode.EXCLUSIVE)
    assert isinstance(advice, DecisionEnvelope)
    assert advice.outcome is Outcome.FITS
    assert advice.resources[0].residual == 15_360  # the reserve was not subtracted a second time


def test_pool_exact_fit_is_accepted_and_one_mib_more_is_not() -> None:
    accepted = shadow_pool_advice(pool_pair(15_872), INVENTORY, now=NOW, validity_s=VALIDITY,
                                  authority=AuthorityMode.EXCLUSIVE)
    delayed = shadow_pool_advice(pool_pair(8_191), INVENTORY, now=NOW, validity_s=VALIDITY,
                                 authority=AuthorityMode.EXCLUSIVE)
    assert accepted.outcome is Outcome.FITS
    assert delayed.outcome is Outcome.NO_FIT


def test_pool_shadow_without_observation_time_stays_legacy_only() -> None:
    total = 24 * 1024 * MIB
    pair = snapshot_and_claim(POOL, PoolObservation(15_872 * MIB, total), observed_at=None)
    assert isinstance(shadow_pool_advice(pair, INVENTORY, now=NOW, validity_s=VALIDITY,
                                         authority=AuthorityMode.EXCLUSIVE), LegacyOnly)


def test_the_queue_and_cpu_case_is_legacy_only_for_the_reason_recorded() -> None:
    claim = ResourceClaim(gpu_required=True, vram_mb=8_000, cpu_cores=2, queue_class=QueueClass.INTERACTIVE,
                          profile_id="profile-fast")
    snapshot = CapacitySnapshot(total_vram_mb=24_000, used_vram_mb=4_000, observed_at=NOW,
                                queue_limits={QueueClass.INTERACTIVE: 4})
    advice = new(claim, snapshot)
    assert isinstance(advice, LegacyOnly)


# --- route admission: a neutral view of the unchanged legacy function --------------------------

def test_route_view_matches_the_unchanged_legacy_function() -> None:
    estimate = ResourceEstimate(intent="chat")
    allowed = legacy_route_view(ServiceAvailability(available=True), estimate)
    denied = legacy_route_view(ServiceAvailability(available=False), estimate)
    direct = advise_resource_admission(ServiceAvailability(available=True), estimate)

    assert allowed == RouteView(allowed=direct.allowed, degraded=direct.degraded, reason_codes=direct.reason_codes)
    assert allowed.allowed and not allowed.degraded
    assert not denied.allowed


def test_route_view_reports_degraded_as_allowed_with_a_reason() -> None:
    view = legacy_route_view(ServiceAvailability(available=True, degraded=True), ResourceEstimate(intent="chat"),
                             AdmissionConstraints(), CurrentLoad())
    assert view.allowed and view.degraded
    assert "service_degraded" in view.reason_codes


# --- shadow_compare: admission only, and it never raises ----------------------------------------

def test_shadow_compare_reports_disagreement_without_raising() -> None:
    claim = ResourceClaim(gpu_required=True, vram_mb=8_000)
    snapshot = CapacitySnapshot(total_vram_mb=24_000, used_vram_mb=0, observed_at=NOW)
    legacy_delay = AdmissionDecision(AdmissionStatus.DELAYED, "golden")
    advice = new(claim, snapshot)
    report = shadow_compare(legacy_delay, advice)
    assert report.status == "disagree"
    assert report.old_admits is False and report.new_outcome == "fits"


def test_shadow_compare_on_garbage_is_unreadable_not_an_exception() -> None:
    advice = new(ResourceClaim(), CapacitySnapshot(observed_at=NOW))

    assert shadow_compare(None, advice).status == "unreadable"
    assert shadow_compare(object(), advice).status == "unreadable"
    assert shadow_compare(AdmissionDecision(AdmissionStatus.ACCEPTED, "x"), None).status == "unreadable"

    class Exploding:
        @property
        def allowed(self) -> bool:
            raise RuntimeError("boom")

    assert shadow_compare(Exploding(), advice).status == "unreadable"


def test_shadow_compare_accepts_the_route_view_as_old_decision() -> None:
    view = RouteView(allowed=True, degraded=False, reason_codes=())
    advice = new(ResourceClaim(), CapacitySnapshot(observed_at=NOW))
    assert shadow_compare(view, advice).status == "agree"
