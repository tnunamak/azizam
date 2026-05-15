"""Tests for the pure runtime-auth/profile bridge."""

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
    PolicyRequest,
)
from gateway.profiles import ProfileLoadError  # noqa: E402
from gateway.receipts import RouteReceipt  # noqa: E402
from gateway.runtime_auth import (  # noqa: E402
    RuntimeIdentity,
    build_identity_registry,
    discovery_audience_for_profile,
    evaluate_and_apply,
    hash_api_key,
    key_hashes_for_entry,
    make_policy_request,
    normalize_key_hash,
    runtime_identity_from_entry,
)


# A precomputed hash for the literal "opk_test_key" value, matching how
# proxy.hash_api_key would produce it.
LITERAL_KEY = "opk_test_key"
LITERAL_HASH = hash_api_key(LITERAL_KEY)


class TestHashing(unittest.TestCase):
    def test_normalize_passes_prefixed_through(self) -> None:
        self.assertEqual(
            normalize_key_hash("sha256:abc"), "sha256:abc"
        )

    def test_normalize_adds_prefix(self) -> None:
        self.assertEqual(normalize_key_hash("abc"), "sha256:abc")

    def test_hash_matches_proxy_format(self) -> None:
        self.assertTrue(LITERAL_HASH.startswith("sha256:"))
        self.assertEqual(len(LITERAL_HASH), len("sha256:") + 64)


class TestKeyHashesForEntry(unittest.TestCase):
    def test_key_hash_normalized(self) -> None:
        hashes = key_hashes_for_entry({"key_hash": "abc"})
        self.assertEqual(hashes, ["sha256:abc"])

    def test_literal_key_is_hashed(self) -> None:
        hashes = key_hashes_for_entry({"key": LITERAL_KEY})
        self.assertEqual(hashes, [LITERAL_HASH])

    def test_key_env_uses_injected_mapping_not_os_environ(self) -> None:
        env = {"SOME_KEY": LITERAL_KEY}
        hashes = key_hashes_for_entry(
            {"key_env": "SOME_KEY"}, env=env
        )
        self.assertEqual(hashes, [LITERAL_HASH])

    def test_key_env_missing_falls_back_to_literal_key(self) -> None:
        env: dict[str, str] = {}
        hashes = key_hashes_for_entry(
            {"key": LITERAL_KEY, "key_env": "MISSING"}, env=env
        )
        self.assertEqual(hashes, [LITERAL_HASH])

    def test_key_env_missing_with_no_literal_yields_nothing(self) -> None:
        hashes = key_hashes_for_entry({"key_env": "MISSING"}, env={})
        self.assertEqual(hashes, [])

    def test_both_key_hash_and_literal_register_separately(self) -> None:
        hashes = key_hashes_for_entry(
            {"key_hash": "sha256:precomputed", "key": LITERAL_KEY}
        )
        self.assertEqual(hashes, ["sha256:precomputed", LITERAL_HASH])

    def test_no_env_argument_does_not_read_os_environ(self) -> None:
        # We pass key_env without an env mapping — the env var name must NOT
        # be looked up in os.environ.
        import os

        os.environ["RUNTIME_AUTH_TEST_LEAK"] = LITERAL_KEY
        try:
            hashes = key_hashes_for_entry(
                {"key_env": "RUNTIME_AUTH_TEST_LEAK"}
            )
        finally:
            del os.environ["RUNTIME_AUTH_TEST_LEAK"]
        self.assertEqual(hashes, [])


class TestRuntimeIdentityFromEntry(unittest.TestCase):
    def test_preserves_downstream_dict_unread(self) -> None:
        downstream = {
            "tabby": {"api_key_env": "SIMON_TABBY_API_KEY"},
            "tts": {
                "headers": {
                    "Authorization": {
                        "env": "SIMON_TTS_API_KEY",
                        "prefix": "Bearer",
                    }
                }
            },
        }
        identity = runtime_identity_from_entry(
            {
                "name": "simon",
                "key_hash": "sha256:abc",
                "downstream": downstream,
            },
            key_hash="sha256:abc",
        )
        self.assertEqual(identity.name, "simon")
        self.assertEqual(identity.key_hash, "sha256:abc")
        # The downstream dict is preserved verbatim — no env var was looked up.
        self.assertEqual(identity.downstream, downstream)
        # Without a profile block the loader returns DEFAULT_PROFILE with
        # the entry name substituted as the profile id.
        self.assertEqual(identity.profile.id, "simon")
        self.assertEqual(
            identity.profile.protocols, DEFAULT_PROFILE.protocols
        )
        self.assertEqual(
            identity.profile.fallback.cloud_allowed,
            DEFAULT_PROFILE.fallback.cloud_allowed,
        )

    def test_loads_profile_block(self) -> None:
        identity = runtime_identity_from_entry(
            {
                "name": "image-agent",
                "key_hash": "sha256:abc",
                "profile": {
                    "id": "image-agent",
                    "protocols": ["openai.images"],
                },
            },
            key_hash="sha256:abc",
        )
        self.assertEqual(identity.profile.id, "image-agent")
        self.assertEqual(
            identity.profile.protocols, frozenset({"openai.images"})
        )

    def test_normalizes_key_hash_argument(self) -> None:
        identity = runtime_identity_from_entry(
            {"name": "x", "key_hash": "abc"}, key_hash="abc"
        )
        self.assertEqual(identity.key_hash, "sha256:abc")

    def test_strict_mode_raises_on_bad_profile(self) -> None:
        with self.assertRaises(ProfileLoadError):
            runtime_identity_from_entry(
                {
                    "name": "x",
                    "key_hash": "sha256:abc",
                    "profile": {"status_visibility": {"level": "loud"}},
                },
                key_hash="sha256:abc",
                strict=True,
            )

    def test_lenient_mode_falls_back_to_default(self) -> None:
        identity = runtime_identity_from_entry(
            {
                "name": "x",
                "key_hash": "sha256:abc",
                "profile": {"status_visibility": {"level": "loud"}},
            },
            key_hash="sha256:abc",
            strict=False,
        )
        self.assertIs(identity.profile, DEFAULT_PROFILE)


class TestBuildIdentityRegistry(unittest.TestCase):
    def test_skips_non_mapping_entries(self) -> None:
        registry = build_identity_registry(
            [
                {"name": "ok", "key_hash": "sha256:abc"},
                "garbage",  # type: ignore[list-item]
                None,  # type: ignore[list-item]
            ]
        )
        self.assertEqual(set(registry), {"sha256:abc"})

    def test_last_duplicate_hash_wins(self) -> None:
        registry = build_identity_registry(
            [
                {"name": "first", "key_hash": "sha256:abc"},
                {"name": "second", "key_hash": "sha256:abc"},
            ]
        )
        self.assertEqual(registry["sha256:abc"].name, "second")

    def test_entry_registered_under_multiple_hashes(self) -> None:
        registry = build_identity_registry(
            [
                {
                    "name": "simon",
                    "key_hash": "sha256:precomputed",
                    "key": LITERAL_KEY,
                },
            ]
        )
        self.assertIn("sha256:precomputed", registry)
        self.assertIn(LITERAL_HASH, registry)
        self.assertEqual(registry["sha256:precomputed"].name, "simon")
        self.assertEqual(registry[LITERAL_HASH].name, "simon")
        # Same identity object semantics — both registrations carry the same
        # downstream and profile.
        self.assertEqual(
            registry["sha256:precomputed"].profile,
            registry[LITERAL_HASH].profile,
        )

    def test_env_resolution_uses_injected_mapping(self) -> None:
        registry = build_identity_registry(
            [{"name": "envguy", "key_env": "ENV_KEY"}],
            env={"ENV_KEY": LITERAL_KEY},
        )
        self.assertEqual(set(registry), {LITERAL_HASH})
        self.assertEqual(registry[LITERAL_HASH].name, "envguy")

    def test_strict_mode_surfaces_profile_errors(self) -> None:
        with self.assertRaises(ProfileLoadError):
            build_identity_registry(
                [
                    {
                        "name": "x",
                        "key_hash": "sha256:abc",
                        "profile": {"status_visibility": {"level": "loud"}},
                    }
                ],
                strict=True,
            )

    def test_lenient_mode_keeps_default_profile_for_bad_entry(self) -> None:
        registry = build_identity_registry(
            [
                {
                    "name": "x",
                    "key_hash": "sha256:abc",
                    "profile": {"status_visibility": {"level": "loud"}},
                }
            ],
        )
        self.assertIs(registry["sha256:abc"].profile, DEFAULT_PROFILE)

    def test_profiles_loaded_from_named_entries(self) -> None:
        registry = build_identity_registry(
            [
                {
                    "name": "simon",
                    "key_hash": "sha256:abc",
                    "profile": {"protocols": ["openai.chat"]},
                },
                {
                    "name": "image-agent",
                    "key_hash": "sha256:def",
                    "profile": {"protocols": ["openai.images"]},
                },
            ]
        )
        self.assertEqual(
            registry["sha256:abc"].profile.protocols,
            frozenset({"openai.chat"}),
        )
        self.assertEqual(
            registry["sha256:def"].profile.protocols,
            frozenset({"openai.images"}),
        )


class TestPolicyBridge(unittest.TestCase):
    def test_make_policy_request_passthrough(self) -> None:
        request = make_policy_request(
            protocol="openai.chat",
            model="gpt-5",
            requires_cloud_fallback=True,
        )
        self.assertIsInstance(request, PolicyRequest)
        self.assertEqual(request.protocol, "openai.chat")
        self.assertEqual(request.model, "gpt-5")
        self.assertTrue(request.requires_cloud_fallback)

    def test_evaluate_and_apply_writes_receipt_compatible_decision(
        self,
    ) -> None:
        identity = RuntimeIdentity(
            name="simon",
            key_hash="sha256:abc",
            profile=AppProfile(
                id="continue-local",
                protocols=frozenset({"openai.chat"}),
            ),
        )
        receipt = RouteReceipt(protocol="openai.chat", intent="chat")
        decision = evaluate_and_apply(
            identity, make_policy_request(protocol="openai.chat"), receipt
        )

        self.assertEqual(decision.decision, ALLOWED)
        self.assertEqual(receipt.policy_decision, ALLOWED)
        self.assertIn("profile:continue-local", receipt.policy_sources)
        self.assertTrue(
            any("openai.chat" in r for r in receipt.policy_reasons),
            receipt.policy_reasons,
        )
        # Visibility from profile defaults to "basic".
        self.assertEqual(receipt.visibility, "basic")

    def test_evaluate_and_apply_records_denial(self) -> None:
        identity = RuntimeIdentity(
            name="simon",
            key_hash="sha256:abc",
            profile=AppProfile(
                id="continue-local",
                protocols=frozenset({"openai.chat"}),
            ),
        )
        receipt = RouteReceipt(protocol="openai.images", intent="image.gen")
        decision = evaluate_and_apply(
            identity,
            make_policy_request(protocol="openai.images"),
            receipt,
        )

        self.assertEqual(decision.decision, DENIED)
        self.assertEqual(receipt.policy_decision, DENIED)

    def test_evaluate_and_apply_uses_default_profile_when_no_identity(
        self,
    ) -> None:
        receipt = RouteReceipt(protocol="openai.chat", intent="chat")
        decision = evaluate_and_apply(
            None, make_policy_request(protocol="openai.chat"), receipt
        )

        self.assertEqual(decision.decision, ALLOWED)
        self.assertEqual(receipt.policy_decision, ALLOWED)
        self.assertIn(f"profile:{DEFAULT_PROFILE.id}", receipt.policy_sources)

    def test_evaluate_and_apply_without_receipt_returns_decision(self) -> None:
        decision = evaluate_and_apply(
            None, make_policy_request(protocol="openai.chat")
        )
        self.assertEqual(decision.decision, ALLOWED)


class TestDiscoveryAudienceForProfile(unittest.TestCase):
    def _profile_with_level(self, level: str) -> AppProfile:
        return AppProfile().with_overrides(
            status_visibility=AppProfile().status_visibility.__class__(
                level=level
            )
        )

    def test_none_profile_defaults_to_client(self) -> None:
        self.assertEqual(discovery_audience_for_profile(None), "client")

    def test_none_visibility_maps_to_client(self) -> None:
        self.assertEqual(
            discovery_audience_for_profile(self._profile_with_level("none")),
            "client",
        )

    def test_basic_visibility_maps_to_client(self) -> None:
        self.assertEqual(
            discovery_audience_for_profile(self._profile_with_level("basic")),
            "client",
        )

    def test_standard_visibility_maps_to_admin(self) -> None:
        self.assertEqual(
            discovery_audience_for_profile(
                self._profile_with_level("standard")
            ),
            "admin",
        )

    def test_full_visibility_maps_to_internal(self) -> None:
        self.assertEqual(
            discovery_audience_for_profile(self._profile_with_level("full")),
            "internal",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
