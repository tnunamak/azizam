from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.resource_admission import AdmissionOutcome  # noqa: E402
from gateway.resource_admission_http import (  # noqa: E402
    admission_http_response,
    advise_route_admission,
)


class ResourceAdmissionHTTPTests(unittest.TestCase):
    def test_denied_decision_translates_to_503_with_retry_after(self) -> None:
        decision = advise_route_admission(
            intent="image.generate",
            service_available=False,
            service_retry_after=30,
            service_reasons=("image backend unavailable",),
        )

        response = admission_http_response(decision)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers, {"Retry-After": "30"})
        self.assertEqual(
            {k: v for k, v in response.body["error"].items() if k != "decision"},
            {
                "code": "resource_admission_denied",
                "message": "This service is not available right now.",
            },
        )
        self.assertEqual(response.body["admission"]["outcome"], "denied")
        self.assertFalse(response.body["admission"]["allowed"])
        self.assertEqual(response.body["admission"]["reason_codes"], ("service_unavailable",))

    def test_degraded_decision_remains_successful_and_exposes_advice(self) -> None:
        decision = advise_route_admission(
            service_available=True,
            intent="chat.generate",
            service_degraded=True,
            service_retry_after=5,
            service_reasons=("warming model",),
        )

        response = admission_http_response(decision)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers, {"Retry-After": "5"})
        self.assertNotIn("error", response.body)
        self.assertEqual(response.body["admission"]["outcome"], "degraded")
        self.assertTrue(response.body["admission"]["allowed"])
        self.assertEqual(response.body["admission"]["reason_codes"], ("service_degraded",))

    def test_route_facts_feed_profile_and_queue_limits(self) -> None:
        decision = advise_route_admission(
            service_available=True,
            intent="image.generate",
            app_id="image-agent",
            resource_class="image",
            queue_key="gpu-media",
            max_app_concurrency={"image-agent": 1},
            max_queue_depth=2,
            active_by_app={"image-agent": 1},
            queue_depths={"gpu-media": 2},
            load_retry_after=12,
        )

        response = admission_http_response(decision)

        self.assertEqual(decision.outcome, AdmissionOutcome.DENIED)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers, {"Retry-After": "12"})
        self.assertEqual(
            decision.reasons,
            (
                "queue full: gpu-media",
                "app concurrency full: image-agent",
            ),
        )
        self.assertEqual(
            decision.reason_codes,
            (
                "queue_full",
                "app_concurrency_full",
            ),
        )

    def test_allowed_decision_uses_success_body_only(self) -> None:
        decision = advise_route_admission(
            service_available=True,
            intent="chat.generate",
            app_id="continue-local",
            estimate_reasons=("using primary llm",),
        )

        response = admission_http_response(decision)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers, {})
        self.assertEqual(response.body["admission"]["outcome"], "allowed")
        self.assertEqual(response.body["admission"]["reasons"], ("The request has resource requirements.",))
        self.assertEqual(response.body["admission"]["reason_codes"], ("estimate_reason",))


if __name__ == "__main__":
    unittest.main()


def test_refusal_message_prioritizes_rejection_over_simultaneous_delay():
    decision = advise_route_admission(intent="image.generate", service_available=True,
                                     service_degraded=True, disabled_intents=("image.generate",))
    response = admission_http_response(decision)
    assert response.body["error"]["message"] == "This kind of work is disabled."
    assert {r["effect"] for r in response.body["error"]["decision"]["reasons"]} == {"rejection", "delay"}
