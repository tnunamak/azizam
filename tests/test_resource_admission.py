"""Tests for pure resource admission advice."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.resource_admission import (  # noqa: E402
    AdmissionConstraints,
    AdmissionOutcome,
    CurrentLoad,
    ResourceEstimate,
    ServiceAvailability,
    advise_resource_admission,
)


class ResourceAdmissionTests(unittest.TestCase):
    def test_allows_when_service_and_resources_are_available(self) -> None:
        decision = advise_resource_admission(
            ServiceAvailability(),
            ResourceEstimate(intent="chat.generate", reasons=("using primary llm",)),
        )

        self.assertEqual(decision.outcome, AdmissionOutcome.ALLOWED)
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.retry_after, None)
        self.assertEqual(decision.reasons, ("using primary llm",))
        self.assertEqual(decision.reason_codes, ("estimate_reason",))

    def test_denies_when_backend_is_down(self) -> None:
        decision = advise_resource_admission(
            ServiceAvailability(available=False, retry_after=30, reasons=("backend down",)),
            ResourceEstimate(intent="image.generate"),
        )

        self.assertEqual(decision.outcome, AdmissionOutcome.DENIED)
        self.assertTrue(decision.denied)
        self.assertEqual(decision.retry_after, 30)
        self.assertEqual(decision.reasons, ("backend down",))
        self.assertEqual(decision.reason_codes, ("service_unavailable",))

    def test_degrades_when_service_is_degraded_but_available(self) -> None:
        decision = advise_resource_admission(
            ServiceAvailability(degraded=True, reasons=("slow warmup",), retry_after=10),
            ResourceEstimate(intent="chat.generate"),
        )

        self.assertEqual(decision.outcome, AdmissionOutcome.DEGRADED)
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.retry_after, 10)
        self.assertEqual(decision.reasons, ("slow warmup",))
        self.assertEqual(decision.reason_codes, ("service_degraded",))

    def test_denies_disabled_image_route_when_gpu_is_reserved_for_llm(self) -> None:
        decision = advise_resource_admission(
            ServiceAvailability(),
            ResourceEstimate(
                intent="image.generate",
                resource_class="image",
                requires_gpu=True,
                required_tokens=("gpu",),
            ),
            AdmissionConstraints(
                disabled_resource_classes=("image",),
                reserved_tokens=("gpu",),
                reasons=("gpu reserved for llm",),
            ),
        )

        self.assertEqual(decision.outcome, AdmissionOutcome.DENIED)
        self.assertEqual(
            decision.reasons,
            (
                "resource class disabled: image",
                "required resource reserved: gpu",
                "gpu reserved",
                "gpu reserved for llm",
            ),
        )
        self.assertEqual(
            decision.reason_codes,
            (
                "resource_class_disabled",
                "required_resource_reserved",
                "gpu_reserved",
                "constraint_reason",
            ),
        )

    def test_degrades_when_only_optional_resource_is_reserved(self) -> None:
        decision = advise_resource_admission(
            ServiceAvailability(),
            ResourceEstimate(
                intent="chat.generate",
                required_tokens=("cpu",),
                optional_tokens=("gpu",),
            ),
            AdmissionConstraints(reserved_tokens=("gpu",)),
        )

        self.assertEqual(decision.outcome, AdmissionOutcome.DEGRADED)
        self.assertEqual(decision.reasons, ("optional resource reserved: gpu",))
        self.assertEqual(decision.reason_codes, ("optional_resource_reserved",))

    def test_denies_when_queue_is_full(self) -> None:
        decision = advise_resource_admission(
            ServiceAvailability(),
            ResourceEstimate(intent="video.generate", queue_key="video"),
            AdmissionConstraints(max_queue_depth=2),
            CurrentLoad(queue_depths={"video": 2}, retry_after=120),
        )

        self.assertEqual(decision.outcome, AdmissionOutcome.DENIED)
        self.assertEqual(decision.retry_after, 120)
        self.assertEqual(decision.reasons, ("queue full: video",))
        self.assertEqual(decision.reason_codes, ("queue_full",))

    def test_denies_when_app_concurrency_is_full(self) -> None:
        decision = advise_resource_admission(
            ServiceAvailability(),
            ResourceEstimate(intent="chat.generate", app_id="desktop"),
            AdmissionConstraints(max_app_concurrency={"desktop": 1}),
            CurrentLoad(active_by_app={"desktop": 1}, retry_after=5),
        )

        self.assertEqual(decision.outcome, AdmissionOutcome.DENIED)
        self.assertEqual(decision.retry_after, 5)
        self.assertEqual(decision.reasons, ("app concurrency full: desktop",))
        self.assertEqual(decision.reason_codes, ("app_concurrency_full",))

    def test_denied_decision_preserves_codes_for_multiple_conflicts(self) -> None:
        decision = advise_resource_admission(
            ServiceAvailability(available=False, retry_after=60, reasons=("backend down",)),
            ResourceEstimate(intent="video.generate", queue_key="video"),
            AdmissionConstraints(max_queue_depth=2),
            CurrentLoad(queue_depths={"video": 2}, retry_after=30),
        )

        self.assertEqual(decision.outcome, AdmissionOutcome.DENIED)
        self.assertEqual(decision.retry_after, 30)
        self.assertEqual(
            decision.reasons,
            (
                "backend down",
                "queue full: video",
            ),
        )
        self.assertEqual(
            decision.reason_codes,
            (
                "service_unavailable",
                "queue_full",
            ),
        )

    def test_decision_as_dict_is_route_friendly(self) -> None:
        decision = advise_resource_admission(
            ServiceAvailability(available=False, reasons=("backend down",)),
            ResourceEstimate(intent="chat.generate"),
        )

        self.assertEqual(
            decision.as_dict(),
            {
                "outcome": "denied",
                "allowed": False,
                "retry_after": None,
                "reasons": ("backend down",),
                "reason_codes": ("service_unavailable",),
            },
        )


if __name__ == "__main__":
    unittest.main()
