"""Standalone tests for app profile policy decisions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.policy import (  # noqa: E402
    ALLOWED,
    DENIED,
    DEFAULT_PROFILE,
    AppProfile,
    CapabilitySelector,
    FallbackPolicy,
    NativePassthroughPolicy,
    PolicyDecision,
    PolicyRequest,
    RemoteProviderPolicy,
    StatusVisibilityPolicy,
    apply_to_receipt,
    evaluate,
    visibility_for,
)
from gateway.receipts import RouteReceipt  # noqa: E402


class TestEvaluateAllowed(unittest.TestCase):
    def test_protocol_in_allow_list_is_allowed(self) -> None:
        profile = AppProfile(
            id="continue-local",
            protocols=frozenset({"openai.chat", "openai.embeddings"}),
        )
        decision = evaluate(profile, PolicyRequest(protocol="openai.chat"))

        self.assertEqual(decision.decision, ALLOWED)
        self.assertTrue(decision.allowed)
        self.assertIn("profile:continue-local", decision.sources)
        self.assertTrue(
            any("openai.chat" in reason for reason in decision.reasons),
            decision.reasons,
        )

    def test_protocol_family_scope_is_allowed(self) -> None:
        profile = AppProfile(id="local-suite", protocols=frozenset({"openai"}))
        decision = evaluate(profile, PolicyRequest(protocol="openai.images"))

        self.assertEqual(decision.decision, ALLOWED)
        self.assertIn("profile:local-suite", decision.sources)

    def test_workflow_model_backend_all_in_allow_lists(self) -> None:
        profile = AppProfile(
            id="image-agent",
            protocols=frozenset({"openai.images"}),
            workflows=frozenset({"image.edit.controlnet.example"}),
            models=frozenset({"sdxl-base"}),
            backends=frozenset({"comfyui.local"}),
        )
        request = PolicyRequest(
            protocol="openai.images",
            workflow="image.edit.controlnet.example",
            model="sdxl-base",
            backend="comfyui.local",
        )

        self.assertEqual(evaluate(profile, request).decision, ALLOWED)

    def test_unrestricted_profile_allows_request(self) -> None:
        decision = evaluate(DEFAULT_PROFILE, PolicyRequest(protocol="openai.chat"))

        self.assertEqual(decision.decision, ALLOWED)
        self.assertIn("no restrictions configured", decision.reasons)

    def test_capability_selectors_allow_matching_metadata(self) -> None:
        profile = AppProfile(
            id="family-chat",
            allow=CapabilitySelector(
                intents=frozenset({"chat.completions"}),
                exposure=frozenset({"default"}),
                trust_tiers=frozenset({"stable"}),
                resource_tiers=frozenset({"interactive"}),
                tags=frozenset({"coder"}),
            ),
        )
        request = PolicyRequest(
            intent="chat.completions",
            capability_exposure="default",
            capability_trust_tier="stable",
            capability_resource_tier="interactive",
            capability_tags=frozenset({"coder", "local"}),
        )

        decision = evaluate(profile, request)
        self.assertEqual(decision.decision, ALLOWED)
        self.assertTrue(
            any("resource tier" in reason for reason in decision.reasons),
            decision.reasons,
        )

    def test_capability_selectors_skip_missing_metadata_for_compatibility(self) -> None:
        profile = AppProfile(
            id="legacy-caller",
            allow=CapabilitySelector(
                exposure=frozenset({"default"}),
                trust_tiers=frozenset({"stable"}),
                resource_tiers=frozenset({"interactive"}),
            ),
        )

        decision = evaluate(profile, PolicyRequest(protocol="openai.chat"))

        self.assertEqual(decision.decision, ALLOWED)
        self.assertIn("no restrictions configured", decision.reasons)


class TestEvaluateDenied(unittest.TestCase):
    def test_protocol_not_in_allow_list_is_denied(self) -> None:
        profile = AppProfile(
            id="continue-local",
            protocols=frozenset({"openai.chat"}),
        )
        decision = evaluate(profile, PolicyRequest(protocol="openai.images"))

        self.assertEqual(decision.decision, DENIED)
        self.assertTrue(decision.denied)
        self.assertEqual(decision.sources, ("profile:continue-local",))
        self.assertTrue(
            any("openai.images" in reason for reason in decision.reasons),
            decision.reasons,
        )

    def test_empty_allow_list_denies_everything(self) -> None:
        profile = AppProfile(id="locked", protocols=frozenset())
        decision = evaluate(profile, PolicyRequest(protocol="openai.chat"))

        self.assertEqual(decision.decision, DENIED)

    def test_denied_workflow_short_circuits_before_other_checks(self) -> None:
        profile = AppProfile(
            workflows=frozenset({"only.this"}),
            models=frozenset({"only.that"}),
        )
        request = PolicyRequest(workflow="something.else", model="only.that")

        decision = evaluate(profile, request)
        self.assertEqual(decision.decision, DENIED)
        self.assertTrue(
            any("workflow 'something.else'" in r for r in decision.reasons),
            decision.reasons,
        )

    def test_model_and_backend_denials(self) -> None:
        profile = AppProfile(
            models=frozenset({"sdxl-base"}),
            backends=frozenset({"comfyui.local"}),
        )
        self.assertEqual(
            evaluate(profile, PolicyRequest(model="other")).decision, DENIED
        )
        self.assertEqual(
            evaluate(profile, PolicyRequest(backend="cloud.openai")).decision,
            DENIED,
        )

    def test_capability_selector_denials(self) -> None:
        profile = AppProfile(
            id="family-chat",
            allow=CapabilitySelector(
                intents=frozenset({"chat.completions"}),
                exposure=frozenset({"default"}),
                trust_tiers=frozenset({"stable"}),
                resource_tiers=frozenset({"interactive"}),
                tags=frozenset({"coder"}),
            ),
        )

        cases = [
            (
                PolicyRequest(intent="image.generate"),
                "intent 'image.generate'",
            ),
            (
                PolicyRequest(capability_exposure="opt_in"),
                "exposure 'opt_in'",
            ),
            (
                PolicyRequest(capability_trust_tier="experimental"),
                "trust tier 'experimental'",
            ),
            (
                PolicyRequest(capability_resource_tier="batch"),
                "resource tier 'batch'",
            ),
            (
                PolicyRequest(capability_tags=frozenset({"image"})),
                "capability tags",
            ),
        ]
        for request, reason_fragment in cases:
            with self.subTest(reason=reason_fragment):
                decision = evaluate(profile, request)
                self.assertEqual(decision.decision, DENIED)
                self.assertIn("policy:capability-selectors", decision.sources)
                self.assertTrue(
                    any(reason_fragment in reason for reason in decision.reasons),
                    decision.reasons,
                )

    def test_empty_capability_selector_denies_present_dimension(self) -> None:
        profile = AppProfile(
            id="locked",
            allow=CapabilitySelector(resource_tiers=frozenset()),
        )
        decision = evaluate(
            profile,
            PolicyRequest(capability_resource_tier="interactive"),
        )

        self.assertEqual(decision.decision, DENIED)


class TestDefaultLocalProfile(unittest.TestCase):
    def test_default_profile_is_local_first(self) -> None:
        self.assertFalse(DEFAULT_PROFILE.fallback.cloud_allowed)
        self.assertFalse(DEFAULT_PROFILE.native_passthrough.enabled)
        self.assertEqual(DEFAULT_PROFILE.status_visibility.level, "basic")
        self.assertEqual(DEFAULT_PROFILE.id, "default-local")

    def test_default_profile_denies_cloud_fallback(self) -> None:
        request = PolicyRequest(
            protocol="openai.chat", requires_cloud_fallback=True
        )
        decision = evaluate(DEFAULT_PROFILE, request)

        self.assertEqual(decision.decision, DENIED)
        self.assertIn("policy:local-only", decision.sources)
        self.assertIn("cloud fallback disabled by profile", decision.reasons)

    def test_cloud_fallback_allowed_when_profile_enables_it(self) -> None:
        profile = DEFAULT_PROFILE.with_overrides(
            fallback=FallbackPolicy(cloud_allowed=True)
        )
        decision = evaluate(
            profile, PolicyRequest(requires_cloud_fallback=True)
        )

        self.assertEqual(decision.decision, ALLOWED)


class TestRemoteProviders(unittest.TestCase):
    def test_remote_provider_denied_by_default(self) -> None:
        decision = evaluate(
            DEFAULT_PROFILE,
            PolicyRequest(remote_provider="typesafe"),
        )

        self.assertEqual(decision.decision, DENIED)
        self.assertIn("policy:remote-provider", decision.sources)

    def test_remote_provider_allowed_when_profile_names_it(self) -> None:
        profile = DEFAULT_PROFILE.with_overrides(
            remote=RemoteProviderPolicy(providers=frozenset({"typesafe"}))
        )
        decision = evaluate(profile, PolicyRequest(remote_provider="typesafe"))

        self.assertEqual(decision.decision, ALLOWED)

    def test_paid_fallback_requires_separate_permission(self) -> None:
        profile = DEFAULT_PROFILE.with_overrides(
            remote=RemoteProviderPolicy(providers=frozenset({"openrouter"}))
        )
        denied = evaluate(
            profile,
            PolicyRequest(remote_provider="openrouter", remote_paid_fallback=True),
        )
        allowed = evaluate(
            profile.with_overrides(
                remote=RemoteProviderPolicy(
                    providers=frozenset({"openrouter"}),
                    allow_paid_fallback=True,
                )
            ),
            PolicyRequest(remote_provider="openrouter", remote_paid_fallback=True),
        )

        self.assertEqual(denied.decision, DENIED)
        self.assertEqual(allowed.decision, ALLOWED)


class TestNativePassthrough(unittest.TestCase):
    def test_native_passthrough_denied_by_default(self) -> None:
        request = PolicyRequest(
            protocol="openai.chat",
            native_passthrough=True,
            native_backend="comfyui.local",
            native_operation="queue_prompt",
        )
        decision = evaluate(DEFAULT_PROFILE, request)

        self.assertEqual(decision.decision, DENIED)
        self.assertIn("policy:native-passthrough", decision.sources)
        self.assertIn("native passthrough disabled by profile", decision.reasons)

    def test_native_passthrough_allowed_when_enabled(self) -> None:
        profile = AppProfile(
            id="power-user",
            native_passthrough=NativePassthroughPolicy(
                enabled=True,
                allowed_backends=frozenset({"comfyui.local"}),
                allowed_operations=frozenset({"queue_prompt", "history"}),
            ),
        )
        request = PolicyRequest(
            native_passthrough=True,
            native_backend="comfyui.local",
            native_operation="queue_prompt",
        )
        self.assertEqual(evaluate(profile, request).decision, ALLOWED)

    def test_native_passthrough_denies_disallowed_backend(self) -> None:
        profile = AppProfile(
            native_passthrough=NativePassthroughPolicy(
                enabled=True,
                allowed_backends=frozenset({"comfyui.local"}),
            ),
        )
        request = PolicyRequest(
            native_passthrough=True, native_backend="comfyui.remote"
        )
        decision = evaluate(profile, request)
        self.assertEqual(decision.decision, DENIED)
        self.assertTrue(
            any("comfyui.remote" in r for r in decision.reasons), decision.reasons
        )

    def test_native_passthrough_denies_disallowed_operation(self) -> None:
        profile = AppProfile(
            native_passthrough=NativePassthroughPolicy(
                enabled=True,
                allowed_operations=frozenset({"history"}),
            ),
        )
        request = PolicyRequest(
            native_passthrough=True, native_operation="delete_all"
        )
        decision = evaluate(profile, request)
        self.assertEqual(decision.decision, DENIED)
        self.assertTrue(
            any("delete_all" in r for r in decision.reasons), decision.reasons
        )

    def test_native_passthrough_allows_method_qualified_operation(self) -> None:
        profile = AppProfile(
            native_passthrough=NativePassthroughPolicy(
                enabled=True,
                allowed_operations=frozenset({"GET /v1/models", "POST /prompt"}),
            ),
        )
        decision = evaluate(
            profile,
            PolicyRequest(
                native_passthrough=True,
                native_operation="POST /prompt",
            ),
        )
        self.assertEqual(decision.decision, ALLOWED)

    def test_native_passthrough_allows_path_legacy_scope_for_method_operation(self) -> None:
        profile = AppProfile(
            native_passthrough=NativePassthroughPolicy(
                enabled=True,
                allowed_operations=frozenset({"v1/models"}),
            ),
        )
        decision = evaluate(
            profile,
            PolicyRequest(
                native_passthrough=True,
                native_operation="GET /v1/models",
            ),
        )
        self.assertEqual(decision.decision, ALLOWED)

    def test_native_passthrough_allows_prefix_scope(self) -> None:
        profile = AppProfile(
            native_passthrough=NativePassthroughPolicy(
                enabled=True,
                allowed_operations=frozenset({"GET /history/*"}),
            ),
        )
        decision = evaluate(
            profile,
            PolicyRequest(
                native_passthrough=True,
                native_operation="GET /history/abc",
            ),
        )
        self.assertEqual(decision.decision, ALLOWED)


class TestVisibility(unittest.TestCase):
    def test_visibility_level_carried_in_decision(self) -> None:
        profile = AppProfile(
            id="internal-tool",
            status_visibility=StatusVisibilityPolicy(level="full"),
        )
        decision = evaluate(profile, PolicyRequest(protocol="openai.chat"))

        self.assertEqual(decision.visibility_level, "full")
        self.assertEqual(visibility_for(profile), "full")

    def test_invalid_visibility_level_fails_fast(self) -> None:
        with self.assertRaises(ValueError):
            StatusVisibilityPolicy(level="loud")

    def test_denied_decision_still_reports_visibility(self) -> None:
        profile = AppProfile(
            protocols=frozenset({"openai.chat"}),
            status_visibility=StatusVisibilityPolicy(level="none"),
        )
        decision = evaluate(profile, PolicyRequest(protocol="openai.images"))

        self.assertEqual(decision.decision, DENIED)
        self.assertEqual(decision.visibility_level, "none")


class TestReceiptCompatibility(unittest.TestCase):
    def test_receipt_fields_shape(self) -> None:
        decision = PolicyDecision(
            decision=ALLOWED,
            sources=("profile:x", "policy:local-only"),
            reasons=("ok",),
            visibility_level="basic",
        )
        fields = decision.receipt_fields()

        self.assertEqual(fields["decision"], ALLOWED)
        self.assertEqual(
            fields["sources"], ["profile:x", "policy:local-only"]
        )
        self.assertEqual(fields["reasons"], ["ok"])
        self.assertEqual(fields["visibility_level"], "basic")

    def test_apply_to_receipt_records_decision_sources_and_reasons(self) -> None:
        receipt = RouteReceipt(protocol="openai.chat", intent="chat.generate")
        profile = AppProfile(
            id="continue-local",
            protocols=frozenset({"openai.chat"}),
            status_visibility=StatusVisibilityPolicy(level="standard"),
        )
        decision = evaluate(profile, PolicyRequest(protocol="openai.chat"))

        apply_to_receipt(decision, receipt)

        self.assertEqual(receipt.policy_decision, ALLOWED)
        self.assertIn("profile:continue-local", receipt.policy_sources)
        self.assertTrue(receipt.policy_reasons)
        self.assertEqual(receipt.visibility, "standard")

    def test_apply_to_receipt_rejects_non_receipt(self) -> None:
        decision = PolicyDecision(decision=ALLOWED)
        with self.assertRaises(TypeError):
            apply_to_receipt(decision, object())


if __name__ == "__main__":
    unittest.main(verbosity=2)
