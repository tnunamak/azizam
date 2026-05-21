"""Standalone tests for the app profile loader."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.policy import (  # noqa: E402
    DEFAULT_PROFILE,
    DEFAULT_PROFILE_ID,
    AppProfile,
    CapabilitySelector,
    ModelAccessPolicy,
    TargetPolicy,
)
from gateway.profiles import (  # noqa: E402
    ProfileLoadError,
    load_profile,
    load_profile_from_entry,
    load_profiles_from_entries,
    profile_for_key_entry,
)


class TestLoadProfileDefaults(unittest.TestCase):
    def test_none_returns_default_profile(self) -> None:
        self.assertIs(load_profile(None), DEFAULT_PROFILE)

    def test_empty_mapping_returns_default_profile(self) -> None:
        self.assertIs(load_profile({}), DEFAULT_PROFILE)

    def test_default_id_substitution_when_no_block(self) -> None:
        profile = load_profile(None, default_id="simon")
        self.assertEqual(profile.id, "simon")
        # Other fields match DEFAULT_PROFILE.
        self.assertEqual(profile.protocols, DEFAULT_PROFILE.protocols)
        self.assertEqual(
            profile.fallback.cloud_allowed,
            DEFAULT_PROFILE.fallback.cloud_allowed,
        )
        self.assertEqual(
            profile.status_visibility.level,
            DEFAULT_PROFILE.status_visibility.level,
        )

    def test_default_id_unchanged_when_already_default(self) -> None:
        self.assertIs(
            load_profile(None, default_id=DEFAULT_PROFILE_ID), DEFAULT_PROFILE
        )


class TestLoadProfileFields(unittest.TestCase):
    def test_full_profile_block_round_trip(self) -> None:
        profile = load_profile(
            {
                "id": "image-agent",
                "protocols": ["openai.images"],
                "workflows": ["image.edit.controlnet.example"],
                "models": ["sdxl-base"],
                "backends": ["comfyui.local"],
                "allow": {
                    "intents": ["image.edit"],
                    "exposure": ["default", "opt_in"],
                    "trust_tiers": ["stable"],
                    "resource_tiers": ["batch"],
                    "tags": ["controlnet"],
                },
                "fallback": {"cloud_allowed": True},
                "native_passthrough": {
                    "enabled": True,
                    "allowed_backends": ["comfyui.local"],
                    "allowed_operations": ["queue_prompt", "history"],
                    "audit": False,
                },
                "status_visibility": {"level": "full"},
                "target_policy": {
                    "visibility": "available",
                    "activation": "existing_only",
                },
            }
        )

        self.assertIsInstance(profile, AppProfile)
        self.assertEqual(profile.id, "image-agent")
        self.assertEqual(profile.protocols, frozenset({"openai.images"}))
        self.assertEqual(
            profile.workflows, frozenset({"image.edit.controlnet.example"})
        )
        self.assertEqual(profile.models, frozenset({"sdxl-base"}))
        self.assertEqual(profile.backends, frozenset({"comfyui.local"}))
        self.assertEqual(
            profile.allow,
            CapabilitySelector(
                intents=frozenset({"image.edit"}),
                exposure=frozenset({"default", "opt_in"}),
                trust_tiers=frozenset({"stable"}),
                resource_tiers=frozenset({"batch"}),
                tags=frozenset({"controlnet"}),
            ),
        )
        self.assertTrue(profile.fallback.cloud_allowed)
        self.assertTrue(profile.native_passthrough.enabled)
        self.assertEqual(
            profile.native_passthrough.allowed_backends,
            frozenset({"comfyui.local"}),
        )
        self.assertEqual(
            profile.native_passthrough.allowed_operations,
            frozenset({"queue_prompt", "history"}),
        )
        self.assertFalse(profile.native_passthrough.audit)
        self.assertEqual(profile.status_visibility.level, "full")
        self.assertEqual(
            profile.target_policy,
            TargetPolicy(visibility="available", activation="existing_only"),
        )

    def test_empty_list_means_deny_all_for_dimension(self) -> None:
        profile = load_profile({"id": "locked", "protocols": []})
        self.assertEqual(profile.protocols, frozenset())

    def test_legacy_model_access_maps_to_target_policy(self) -> None:
        profile = load_profile(
            {"id": "legacy", "model_access": {"mode": "current_loaded"}}
        )
        self.assertEqual(
            profile.model_access,
            ModelAccessPolicy(mode="current_loaded"),
        )
        self.assertEqual(
            profile.target_policy,
            TargetPolicy(visibility="available", activation="existing_only"),
        )

    def test_missing_dimension_means_no_opinion(self) -> None:
        profile = load_profile({"id": "loose"})
        self.assertIsNone(profile.protocols)
        self.assertIsNone(profile.workflows)
        self.assertIsNone(profile.models)
        self.assertIsNone(profile.backends)
        self.assertIsNone(profile.allow.intents)
        self.assertIsNone(profile.allow.exposure)

    def test_partial_native_passthrough_defaults(self) -> None:
        profile = load_profile(
            {"id": "p", "native_passthrough": {"enabled": True}}
        )
        self.assertTrue(profile.native_passthrough.enabled)
        self.assertEqual(
            profile.native_passthrough.allowed_backends, frozenset()
        )
        self.assertEqual(
            profile.native_passthrough.allowed_operations, frozenset()
        )
        self.assertTrue(profile.native_passthrough.audit)

    def test_id_from_default_when_block_omits_it(self) -> None:
        profile = load_profile({"protocols": ["openai.chat"]}, default_id="bob")
        self.assertEqual(profile.id, "bob")
        self.assertEqual(profile.protocols, frozenset({"openai.chat"}))


class TestLoadProfileValidation(unittest.TestCase):
    def test_unknown_key_raises(self) -> None:
        with self.assertRaises(ProfileLoadError) as cm:
            load_profile({"id": "x", "mystery": True})
        self.assertIn("mystery", str(cm.exception))

    def test_non_mapping_profile_raises(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile(["not", "a", "mapping"])  # type: ignore[arg-type]

    def test_id_must_be_non_empty_string(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile({"id": ""})
        with self.assertRaises(ProfileLoadError):
            load_profile({"id": 7})

    def test_protocols_must_be_list_of_strings(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile({"id": "x", "protocols": "openai.chat"})
        with self.assertRaises(ProfileLoadError):
            load_profile({"id": "x", "protocols": {"openai.chat": True}})
        with self.assertRaises(ProfileLoadError):
            load_profile({"id": "x", "protocols": [1, 2]})

    def test_fallback_cloud_allowed_must_be_bool(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile({"id": "x", "fallback": {"cloud_allowed": "yes"}})

    def test_native_passthrough_enabled_must_be_bool(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile(
                {"id": "x", "native_passthrough": {"enabled": "yes"}}
            )

    def test_status_visibility_level_must_be_known(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile(
                {"id": "x", "status_visibility": {"level": "loud"}}
            )

    def test_model_access_mode_must_be_known(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile({"id": "x", "model_access": {"mode": "surprise"}})

    def test_target_policy_visibility_must_be_known(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile(
                {"id": "x", "target_policy": {"visibility": "surprise"}}
            )

    def test_target_policy_activation_must_be_known(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile(
                {"id": "x", "target_policy": {"activation": "surprise"}}
            )

    def test_fallback_must_be_mapping(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile({"id": "x", "fallback": True})

    def test_allow_must_be_mapping(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile({"id": "x", "allow": True})

    def test_allow_rejects_unknown_key(self) -> None:
        with self.assertRaises(ProfileLoadError) as cm:
            load_profile({"id": "x", "allow": {"mystery": ["value"]}})
        self.assertIn("mystery", str(cm.exception))

    def test_allow_fields_must_be_lists_of_strings(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile({"id": "x", "allow": {"resource_tiers": "batch"}})
        with self.assertRaises(ProfileLoadError):
            load_profile({"id": "x", "allow": {"tags": [1]}})


class TestLoadProfileFromEntry(unittest.TestCase):
    def test_entry_without_profile_returns_default_with_name_id(self) -> None:
        profile = load_profile_from_entry(
            {"name": "simon", "key_hash": "sha256:abc"}
        )
        self.assertEqual(profile.id, "simon")
        self.assertFalse(profile.fallback.cloud_allowed)

    def test_entry_without_profile_or_name_returns_default(self) -> None:
        profile = load_profile_from_entry({"key_hash": "sha256:abc"})
        self.assertIs(profile, DEFAULT_PROFILE)

    def test_entry_with_profile_block_overrides_name_id(self) -> None:
        profile = load_profile_from_entry(
            {
                "name": "simon",
                "key_hash": "sha256:abc",
                "profile": {
                    "id": "continue-local",
                    "protocols": ["openai.chat"],
                },
            }
        )
        self.assertEqual(profile.id, "continue-local")
        self.assertEqual(profile.protocols, frozenset({"openai.chat"}))

    def test_entry_with_profile_block_uses_name_when_id_absent(self) -> None:
        profile = load_profile_from_entry(
            {
                "name": "simon",
                "profile": {"protocols": ["openai.chat"]},
            }
        )
        self.assertEqual(profile.id, "simon")

    def test_entry_ignores_downstream_block(self) -> None:
        profile = load_profile_from_entry(
            {
                "name": "simon",
                "downstream": {
                    "tabby": {"api_key_env": "SIMON_TABBY_API_KEY"},
                    "tts": {
                        "headers": {
                            "Authorization": {
                                "env": "SIMON_TTS_API_KEY",
                                "prefix": "Bearer",
                            }
                        }
                    },
                },
            }
        )
        self.assertEqual(profile.id, "simon")
        self.assertIsNone(profile.protocols)

    def test_non_mapping_entry_raises(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile_from_entry("not a mapping")  # type: ignore[arg-type]

    def test_non_string_name_raises(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profile_from_entry({"name": 7})


class TestLoadProfilesFromEntries(unittest.TestCase):
    def test_maps_named_entries(self) -> None:
        profiles = load_profiles_from_entries(
            [
                {"name": "simon", "profile": {"protocols": ["openai.chat"]}},
                {"name": "image-agent", "key_hash": "sha256:abc"},
            ]
        )
        self.assertEqual(set(profiles), {"simon", "image-agent"})
        self.assertEqual(profiles["simon"].id, "simon")
        self.assertEqual(profiles["simon"].protocols, frozenset({"openai.chat"}))
        self.assertEqual(profiles["image-agent"].id, "image-agent")

    def test_entries_without_name_are_skipped(self) -> None:
        profiles = load_profiles_from_entries(
            [
                {"key_hash": "sha256:abc"},
                {"name": "", "key_hash": "sha256:def"},
                {"name": "ok", "key_hash": "sha256:ghi"},
            ]
        )
        self.assertEqual(set(profiles), {"ok"})

    def test_last_entry_wins_on_duplicate_name(self) -> None:
        profiles = load_profiles_from_entries(
            [
                {"name": "x", "profile": {"protocols": ["openai.chat"]}},
                {"name": "x", "profile": {"protocols": ["openai.images"]}},
            ]
        )
        self.assertEqual(
            profiles["x"].protocols, frozenset({"openai.images"})
        )

    def test_non_mapping_entry_raises(self) -> None:
        with self.assertRaises(ProfileLoadError):
            load_profiles_from_entries([{"name": "ok"}, "nope"])  # type: ignore[list-item]


class TestProfileForKeyEntryHelper(unittest.TestCase):
    def test_none_returns_default(self) -> None:
        self.assertIs(profile_for_key_entry(None), DEFAULT_PROFILE)

    def test_malformed_entry_falls_back_to_default(self) -> None:
        profile = profile_for_key_entry(
            {"name": "x", "profile": {"status_visibility": {"level": "bad"}}}
        )
        self.assertIs(profile, DEFAULT_PROFILE)

    def test_good_entry_returns_constructed_profile(self) -> None:
        profile = profile_for_key_entry(
            {"name": "simon", "profile": {"protocols": ["openai.chat"]}}
        )
        self.assertEqual(profile.id, "simon")
        self.assertEqual(profile.protocols, frozenset({"openai.chat"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
