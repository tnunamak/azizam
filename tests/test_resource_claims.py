from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.resource_claims import (  # noqa: E402
    AdmissionRequest,
    AdmissionStatus,
    CapacitySnapshot,
    QueueClass,
    ResourceClaim,
    evaluate_admission,
    summarize_claim,
    summarize_decision,
)


class ResourceClaimAdmissionTests(unittest.TestCase):
    def test_accepts_when_capacity_and_limits_allow_claim(self) -> None:
        claim = ResourceClaim(
            gpu_required=True,
            vram_mb=8_000,
            cpu_cores=2,
            ram_mb=4_096,
            disk_mb=20_000,
            queue_class=QueueClass.INTERACTIVE,
            profile_id="profile-fast",
            reasons=("chat completion needs local gpu",),
        )
        request = AdmissionRequest("req-accepted", claim)
        capacity = CapacitySnapshot(
            total_vram_mb=24_000,
            used_vram_mb=4_000,
            total_cpu_cores=8,
            used_cpu_cores=1,
            total_ram_mb=32_000,
            used_ram_mb=8_000,
            total_disk_mb=200_000,
            used_disk_mb=50_000,
            active_by_queue={QueueClass.INTERACTIVE: 1},
            queue_limits={QueueClass.INTERACTIVE: 4},
            active_by_profile={"profile-fast": 0},
            profile_limits={"profile-fast": 1},
        )

        decision = evaluate_admission(request, capacity)

        self.assertIs(decision.status, AdmissionStatus.ACCEPTED)
        self.assertTrue(decision.accepted)
        self.assertEqual(decision.reasons, ("chat completion needs local gpu",))

    def test_delays_when_vram_is_not_available(self) -> None:
        claim = ResourceClaim(gpu_required=True, vram_mb=18_000)
        request = AdmissionRequest("req-vram", claim)
        capacity = CapacitySnapshot(total_vram_mb=24_000, used_vram_mb=8_000)

        decision = evaluate_admission(request, capacity)

        self.assertIs(decision.status, AdmissionStatus.DELAYED)
        self.assertFalse(decision.accepted)
        self.assertEqual(
            decision.reasons,
            ("not enough vram_mb: requested 18000, available 16000",),
        )

    def test_rejects_when_queue_concurrency_limit_is_reached(self) -> None:
        claim = ResourceClaim(queue_class=QueueClass.BATCH)
        request = AdmissionRequest("req-queue", claim)
        capacity = CapacitySnapshot(
            active_by_queue={QueueClass.BATCH: 2},
            queue_limits={QueueClass.BATCH: 2},
        )

        decision = evaluate_admission(request, capacity)

        self.assertIs(decision.status, AdmissionStatus.REJECTED)
        self.assertEqual(decision.reasons, ("queue concurrency limit reached for batch",))

    def test_rejects_when_profile_concurrency_limit_is_reached(self) -> None:
        claim = ResourceClaim(profile_id="private-profile")
        request = AdmissionRequest("req-profile", claim)
        capacity = CapacitySnapshot(
            active_by_profile={"private-profile": 1},
            profile_limits={"private-profile": 1},
        )

        decision = evaluate_admission(request, capacity)

        self.assertIs(decision.status, AdmissionStatus.REJECTED)
        self.assertEqual(decision.reasons, ("profile concurrency limit reached",))

    def test_safe_summaries_redact_identifiers_and_keep_operational_fields(self) -> None:
        claim = ResourceClaim(
            gpu_required=True,
            vram_mb=12_000,
            exclusive_locks=("tenant-secret/model-a", "tenant-secret/model-b"),
            profile_id="tenant-secret-profile",
            caveats=("vram estimate is conservative",),
        )
        decision = evaluate_admission(
            AdmissionRequest("request-secret-id", claim),
            CapacitySnapshot(total_vram_mb=24_000, used_vram_mb=0, active_exclusive_locks=frozenset()),
        )

        claim_summary = summarize_claim(claim)
        decision_summary = summarize_decision(decision)

        self.assertTrue(claim_summary["gpu_required"])
        self.assertEqual(claim_summary["vram_mb"], 12_000)
        self.assertEqual(claim_summary["exclusive_lock_count"], 2)
        self.assertEqual(claim_summary["queue_class"], "interactive")
        self.assertEqual(claim_summary["caveats"], ("vram estimate is conservative",))
        self.assertNotIn("tenant-secret", repr(claim_summary))
        self.assertNotIn("request-secret-id", repr(decision_summary))
        self.assertEqual(decision_summary["status"], "accepted")
        self.assertTrue(decision_summary["accepted"])


if __name__ == "__main__":
    unittest.main()
