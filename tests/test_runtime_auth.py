"""Tests for the runtime-auth bridge between an App's identity and its access plans."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.policy import ALLOWED, DENIED, NO_ACCESS_PLAN, PolicyRequest  # noqa: E402
from gateway.receipts import RouteReceipt  # noqa: E402
from gateway.runtime_auth import (  # noqa: E402
    RuntimeIdentity,
    discovery_audience_for_access,
    evaluate_and_apply,
    hash_api_key,
    make_policy_request,
    normalize_key_hash,
    plan_ref,
)
from tests.support.access import access, identity, plan_for  # noqa: E402

LITERAL_HASH = hash_api_key("opk_test_key")


class TestHashing(unittest.TestCase):
    def test_normalize_passes_prefixed_through(self) -> None:
        self.assertEqual(normalize_key_hash("sha256:abc"), "sha256:abc")

    def test_normalize_adds_prefix(self) -> None:
        self.assertEqual(normalize_key_hash("abc"), "sha256:abc")

    def test_hash_matches_proxy_format(self) -> None:
        self.assertTrue(LITERAL_HASH.startswith("sha256:"))
        self.assertEqual(len(LITERAL_HASH), len("sha256:") + 64)


class TestPolicyBridge(unittest.TestCase):
    def test_make_policy_request_passthrough(self) -> None:
        request = make_policy_request(protocol="openai.chat", intent="chat.completions", model="gpt-5",
                                      capability_exposure="default", capability_tags=["coder"],
                                      requires_cloud_fallback=True)
        self.assertIsInstance(request, PolicyRequest)
        self.assertEqual((request.protocol, request.intent, request.model), ("openai.chat", "chat.completions", "gpt-5"))
        self.assertEqual(request.capability_tags, frozenset({"coder"}))
        self.assertTrue(request.requires_cloud_fallback)

    def test_evaluate_and_apply_writes_receipt_compatible_decision(self) -> None:
        chat = identity(plan_for("plan_chat", caps=("chat",)))
        receipt = RouteReceipt(protocol="openai.chat", intent="chat.completions")
        decision = evaluate_and_apply(chat, make_policy_request(intent="chat.completions", model="m"), receipt)
        self.assertEqual(decision.decision, ALLOWED)
        self.assertEqual(receipt.policy_decision, ALLOWED)
        self.assertIn("access_plan:plan_chat", receipt.policy_sources)
        self.assertEqual(plan_ref(chat), "plan_chat")

    def test_evaluate_and_apply_records_denial(self) -> None:
        chat = identity(plan_for("plan_chat", caps=("chat",)))
        receipt = RouteReceipt(protocol="openai.images", intent="image.generate")
        decision = evaluate_and_apply(chat, make_policy_request(intent="image.generate", model="flux"), receipt)
        self.assertEqual(decision.decision, DENIED)
        self.assertEqual(receipt.policy_decision, DENIED)

    def test_no_identity_is_denied_for_want_of_a_plan(self) -> None:
        receipt = RouteReceipt(protocol="openai.chat", intent="chat.completions")
        decision = evaluate_and_apply(None, make_policy_request(intent="chat.completions"), receipt)
        self.assertEqual((decision.decision, decision.code), (DENIED, NO_ACCESS_PLAN))
        self.assertEqual(receipt.policy_decision, DENIED)

    def test_identity_without_plans_is_denied(self) -> None:
        bare = RuntimeIdentity(name="bare", key_hash="sha256:abc")
        self.assertEqual(evaluate_and_apply(bare, make_policy_request(intent="chat.completions")).code, NO_ACCESS_PLAN)
        self.assertIsNone(plan_ref(bare))


class TestDiscoveryAudienceForAccess(unittest.TestCase):
    def test_no_access_bounds_to_client(self) -> None:
        self.assertEqual(discovery_audience_for_access(None), "client")

    def test_receipt_detail_maps_to_audience(self) -> None:
        for level, audience in (("none", "client"), ("basic", "client"), ("standard", "admin"), ("full", "internal")):
            with self.subTest(level=level):
                self.assertEqual(discovery_audience_for_access(access(plan_for(receipts=level))), audience)

    def test_the_lower_receipt_detail_of_two_plans_wins(self) -> None:
        both = access(plan_for("plan_full", receipts="full"), plan_for("plan_basic", receipts="basic"))
        self.assertEqual(discovery_audience_for_access(both), "client")


if __name__ == "__main__":
    unittest.main(verbosity=2)
