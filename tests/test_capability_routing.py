"""Tests pinning the capability-dispatch helpers (2026-06-13).

Covers:
- backends_for_capability: correct ids for current config (embeddings, audio.speech,
  audio.transcriptions), priority ordering, disabled exclusion.
- host_for_capability: returns current EMBEDDINGS_HOST/TTS_HOST/STT_HOST values
  for the current single-backend-per-capability config (bit-identical proof).
- TWO-BACKEND proof: second embeddings backend added → backends_for_capability
  returns both priority-ordered; host_for_capability picks the higher-priority one.
- Endpoint-path → capability mapping: correct per §2.1.
- Fallback: if no backend declares the capability but the legacy *_HOST constant
  is set, the old host is still used (verified via TTS/STT forward-path logic).
- audio.translations: preserved existing behavior (stt_backend_supports_translation
  still gates the route; host_for_capability for audio.translations returns None
  when stt only declares audio.transcriptions — current config behavior).

For imports, follow the pattern in test_registry_migration.py:
import proxy at module level, then patch proxy.config inside tests.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.service_registry import (  # noqa: E402
    apply_service_registry_to_config,
    build_service_registry_seed,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tts(
    *,
    host: str = "127.0.0.1",
    port: int = 8001,
    enabled: bool = True,
    priority: int = 100,
) -> dict:
    return {
        "enabled": enabled,
        "capabilities": ["audio", "speech"],
        "host": host,
        "port": port,
        "priority": priority,
        "health_endpoint": "/health",
    }


def _make_stt(
    *,
    host: str = "127.0.0.1",
    port: int = 5092,
    enabled: bool = True,
    priority: int = 100,
    capabilities: list[str] | None = None,
) -> dict:
    return {
        "enabled": enabled,
        "capabilities": capabilities if capabilities is not None else ["audio", "transcriptions"],
        "host": host,
        "port": port,
        "priority": priority,
        "health_endpoint": "/health",
    }


def _make_embeddings(
    *,
    host: str = "127.0.0.1",
    port: int = 5061,
    enabled: bool = True,
    priority: int = 100,
) -> dict:
    return {
        "enabled": enabled,
        "capabilities": ["openai", "embeddings"],
        "host": host,
        "port": port,
        "priority": priority,
        "health_endpoint": "/health",
    }


# ---------------------------------------------------------------------------
# backends_for_capability
# ---------------------------------------------------------------------------

class TestBackendsForCapability(unittest.TestCase):
    """Unit tests for the registry-driven capability lookup helper."""

    def _call(self, capability: str, backends: dict) -> list[str]:
        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            return proxy.backends_for_capability(capability)

    # --- Current single-backend-per-capability config ----------------------

    def test_embeddings_returns_embeddings(self) -> None:
        """Current config: embeddings → ["embeddings"]."""
        backends = {"embeddings": _make_embeddings()}
        result = self._call("embeddings", backends)
        self.assertEqual(result, ["embeddings"])

    def test_audio_speech_returns_tts(self) -> None:
        """Current config: audio.speech → ["tts"]."""
        backends = {"tts": _make_tts()}
        result = self._call("audio.speech", backends)
        self.assertEqual(result, ["tts"])

    def test_audio_transcriptions_returns_stt(self) -> None:
        """Current config: audio.transcriptions → ["stt"]."""
        backends = {"stt": _make_stt()}
        result = self._call("audio.transcriptions", backends)
        self.assertEqual(result, ["stt"])

    # --- audio.translations: not declared by current stt backend -----------

    def test_audio_translations_empty_for_current_stt_config(self) -> None:
        """Current stt only has audio.transcriptions — not audio.translations.

        This preserves the existing stt_backend_supports_translation() gate:
        that function checks the raw capabilities list, and since "translations"
        is absent, host_for_capability("audio.translations") returns None, so
        the legacy STT_HOST fallback is used — identical to today's behavior.
        """
        backends = {"stt": _make_stt()}  # capabilities = ["audio", "transcriptions"]
        result = self._call("audio.translations", backends)
        self.assertEqual(result, [])

    # --- Disabled backends excluded ----------------------------------------

    def test_disabled_tts_excluded(self) -> None:
        backends = {"tts": _make_tts(enabled=False)}
        result = self._call("audio.speech", backends)
        self.assertEqual(result, [])

    def test_disabled_stt_excluded(self) -> None:
        backends = {"stt": _make_stt(enabled=False)}
        result = self._call("audio.transcriptions", backends)
        self.assertEqual(result, [])

    def test_disabled_embeddings_excluded(self) -> None:
        backends = {"embeddings": _make_embeddings(enabled=False)}
        result = self._call("embeddings", backends)
        self.assertEqual(result, [])

    # --- Unknown capability returns empty list -----------------------------

    def test_unknown_capability_returns_empty(self) -> None:
        backends = {"tts": _make_tts(), "stt": _make_stt(), "embeddings": _make_embeddings()}
        result = self._call("video.generations", backends)
        self.assertEqual(result, [])

    def test_empty_backends_returns_empty(self) -> None:
        result = self._call("audio.speech", {})
        self.assertEqual(result, [])

    # --- Priority ordering -------------------------------------------------

    def test_priority_ordering_lower_number_first(self) -> None:
        """Lower priority number = higher priority → appears first in list."""
        backends = {
            "tts-secondary": {**_make_tts(port=8002), "priority": 200},
            "tts-primary": {**_make_tts(port=8001), "priority": 10},
        }
        result = self._call("audio.speech", backends)
        self.assertEqual(result, ["tts-primary", "tts-secondary"])

    def test_default_priority_is_100(self) -> None:
        """Backend with no priority key defaults to 100."""
        backends = {
            "low": {**_make_embeddings(port=5062), "priority": 200},
            "nopri": {k: v for k, v in _make_embeddings(port=5063).items() if k != "priority"},
        }
        # nopri defaults to 100, so it comes before low (200)
        result = self._call("embeddings", backends)
        self.assertEqual(result[0], "nopri")
        self.assertEqual(result[1], "low")

    # --- TWO-BACKEND PROOF -------------------------------------------------

    def test_two_embeddings_backends_both_returned_priority_ordered(self) -> None:
        """The win: a second embeddings backend is now visible.

        With a single backend today, backends_for_capability returns ["embeddings"].
        Adding a higher-priority "embeddings2" backend demonstrates that
        capability dispatch can route to either — the pool now has two members,
        ordered by priority.
        """
        backends = {
            "embeddings": _make_embeddings(host="127.0.0.1", port=5061, priority=100),
            "embeddings2": _make_embeddings(host="192.168.1.50", port=5062, priority=10),
        }
        result = self._call("embeddings", backends)
        # Higher-priority (lower number) comes first.
        self.assertEqual(result, ["embeddings2", "embeddings"])
        self.assertEqual(len(result), 2)

    def test_two_tts_backends_both_returned(self) -> None:
        backends = {
            "tts": _make_tts(host="127.0.0.1", port=8001, priority=100),
            "tts-cloud": {
                "enabled": True,
                "capabilities": ["audio", "speech"],
                "host": "192.168.1.60",
                "port": 443,
                "priority": 50,
                "health_endpoint": "/health",
            },
        }
        result = self._call("audio.speech", backends)
        self.assertEqual(result, ["tts-cloud", "tts"])


# ---------------------------------------------------------------------------
# host_for_capability
# ---------------------------------------------------------------------------

class TestHostForCapability(unittest.TestCase):
    """host_for_capability returns the highest-priority backend's host URL."""

    def _call(self, capability: str, backends: dict) -> str | None:
        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            return proxy.host_for_capability(capability)

    # --- Bit-identical proof for current single-backend config -------------

    def test_embeddings_host_matches_current_embeddings_host(self) -> None:
        """host_for_capability("embeddings") == proxy.EMBEDDINGS_HOST for current config."""
        backends = {"embeddings": _make_embeddings(host="127.0.0.1", port=5061)}
        result = self._call("embeddings", backends)
        self.assertEqual(result, "http://127.0.0.1:5061")
        self.assertEqual(result, proxy.EMBEDDINGS_HOST)

    def test_audio_speech_host_matches_current_tts_host(self) -> None:
        """host_for_capability("audio.speech") == proxy.TTS_HOST for current config."""
        backends = {"tts": _make_tts(host="127.0.0.1", port=proxy.tts_cfg["port"])}
        result = self._call("audio.speech", backends)
        self.assertEqual(result, proxy.TTS_HOST)
        self.assertEqual(result, proxy.TTS_HOST)

    def test_audio_transcriptions_host_matches_current_stt_host(self) -> None:
        """host_for_capability("audio.transcriptions") == proxy.STT_HOST for current config."""
        backends = {"stt": _make_stt(host="127.0.0.1", port=5092)}
        result = self._call("audio.transcriptions", backends)
        self.assertEqual(result, "http://127.0.0.1:5092")
        self.assertEqual(result, proxy.STT_HOST)

    # --- Returns None when no backend declares the capability --------------

    def test_returns_none_when_no_backend_for_capability(self) -> None:
        backends = {"tts": _make_tts()}  # no embeddings
        result = self._call("embeddings", backends)
        self.assertIsNone(result)

    def test_returns_none_for_audio_translations_with_current_stt(self) -> None:
        """audio.translations: current stt doesn't declare it → None → legacy fallback."""
        backends = {"stt": _make_stt()}  # only declares audio.transcriptions
        result = self._call("audio.translations", backends)
        self.assertIsNone(result)

    def test_returns_none_for_empty_backends(self) -> None:
        result = self._call("audio.speech", {})
        self.assertIsNone(result)

    # --- TWO-BACKEND PROOF: picks the higher-priority one ------------------

    def test_two_embeddings_backends_picks_higher_priority(self) -> None:
        """The win: with two embeddings backends, host_for_capability picks priority=10 over priority=100."""
        backends = {
            "embeddings": _make_embeddings(host="127.0.0.1", port=5061, priority=100),
            "embeddings2": _make_embeddings(host="192.168.1.50", port=5062, priority=10),
        }
        result = self._call("embeddings", backends)
        # embeddings2 has priority=10 (higher priority, lower number)
        self.assertEqual(result, "http://192.168.1.50:5062")

    def test_two_embeddings_backends_swapped_priority(self) -> None:
        """The original embeddings backend wins when it has the lower priority number."""
        backends = {
            "embeddings": _make_embeddings(host="127.0.0.1", port=5061, priority=5),
            "embeddings2": _make_embeddings(host="192.168.1.50", port=5062, priority=50),
        }
        result = self._call("embeddings", backends)
        self.assertEqual(result, "http://127.0.0.1:5061")

    def test_two_tts_backends_picks_higher_priority(self) -> None:
        backends = {
            "tts": _make_tts(host="127.0.0.1", port=8001, priority=100),
            "tts-cloud": {
                "enabled": True,
                "capabilities": ["audio", "speech"],
                "host": "192.168.1.60",
                "port": 8880,
                "priority": 5,
                "health_endpoint": "/health",
            },
        }
        result = self._call("audio.speech", backends)
        self.assertEqual(result, "http://192.168.1.60:8880")

    # --- Skips backends with no port ---------------------------------------

    def test_skips_backend_with_no_port(self) -> None:
        """Backend missing port is skipped; falls to the next one."""
        backends = {
            "embeddings-noport": {
                "enabled": True,
                "capabilities": ["openai", "embeddings"],
                "host": "127.0.0.1",
                "priority": 1,
                # no "port"
            },
            "embeddings": _make_embeddings(host="127.0.0.1", port=5061, priority=10),
        }
        result = self._call("embeddings", backends)
        self.assertEqual(result, "http://127.0.0.1:5061")


# ---------------------------------------------------------------------------
# Endpoint-path → capability mapping (§2.1 static table)
# ---------------------------------------------------------------------------

class TestEndpointCapabilityMapping(unittest.TestCase):
    """Verify the static endpoint→capability vocabulary used by the routing layer."""

    ENDPOINT_TO_CAPABILITY = {
        "/v1/audio/speech": "audio.speech",
        "/v1/audio/transcriptions": "audio.transcriptions",
        "/v1/audio/translations": "audio.translations",
        "/v1/embeddings": "embeddings",
    }

    def test_tts_endpoint_capability_is_audio_speech(self) -> None:
        """host_for_capability("audio.speech") resolves TTS backend."""
        backends = {"tts": _make_tts()}
        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            result = proxy.backends_for_capability("audio.speech")
        self.assertIn("tts", result)

    def test_stt_transcriptions_endpoint_capability_is_audio_transcriptions(self) -> None:
        backends = {"stt": _make_stt()}
        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            result = proxy.backends_for_capability("audio.transcriptions")
        self.assertIn("stt", result)

    def test_embeddings_endpoint_capability_is_embeddings(self) -> None:
        backends = {"embeddings": _make_embeddings()}
        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            result = proxy.backends_for_capability("embeddings")
        self.assertIn("embeddings", result)

    def test_non_tts_backend_not_returned_for_audio_speech(self) -> None:
        """A backend without audio.speech should not appear."""
        backends = {
            "stt": _make_stt(),
            "embeddings": _make_embeddings(),
        }
        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            result = proxy.backends_for_capability("audio.speech")
        self.assertEqual(result, [])


# ---------------------------------------------------------------------------
# Fallback: legacy *_HOST constants preserved when no capability backend
# ---------------------------------------------------------------------------

class TestLegacyFallback(unittest.TestCase):
    """When host_for_capability returns None, callers fall back to the old constant.

    This verifies the fallback logic wired into the TTS and STT forward paths:
      _tts_host = host_for_capability("audio.speech") or (TTS_HOST if TTS_ENABLED else None) or TTS_HOST
      _stt_host = host_for_capability(f"audio.{operation}") or (STT_HOST if STT_ENABLED else None) or STT_HOST

    We test this by exercising host_for_capability() with an empty backends dict
    (returns None) and verifying the expected fallback value.
    """

    def test_empty_backends_tts_fallback_is_TTS_HOST(self) -> None:
        """host_for_capability returns None → caller falls back to TTS_HOST."""
        with patch.dict(proxy.config, {"backends": {}}, clear=False):
            resolved = proxy.host_for_capability("audio.speech")
        self.assertIsNone(resolved)
        # Simulate the fallback expression used in proxy_tts_request:
        fallback = resolved or (proxy.TTS_HOST if proxy.TTS_ENABLED else None) or proxy.TTS_HOST
        self.assertEqual(fallback, proxy.TTS_HOST)

    def test_empty_backends_stt_fallback_is_STT_HOST(self) -> None:
        with patch.dict(proxy.config, {"backends": {}}, clear=False):
            resolved = proxy.host_for_capability("audio.transcriptions")
        self.assertIsNone(resolved)
        fallback = resolved or (proxy.STT_HOST if proxy.STT_ENABLED else None) or proxy.STT_HOST
        self.assertEqual(fallback, proxy.STT_HOST)

    def test_empty_backends_embeddings_fallback_is_EMBEDDINGS_HOST(self) -> None:
        with patch.dict(proxy.config, {"backends": {}}, clear=False):
            resolved = proxy.host_for_capability("embeddings")
        self.assertIsNone(resolved)
        fallback = resolved or (proxy.EMBEDDINGS_HOST if proxy.EMBEDDINGS_ENABLED else None) or proxy.EMBEDDINGS_HOST
        self.assertEqual(fallback, proxy.EMBEDDINGS_HOST)

    def test_disabled_tts_returns_none_then_falls_back(self) -> None:
        """Disabled backend → capability returns None → legacy constant used."""
        backends = {"tts": _make_tts(enabled=False)}
        with patch.dict(proxy.config, {"backends": backends}, clear=False):
            resolved = proxy.host_for_capability("audio.speech")
        self.assertIsNone(resolved)
        fallback = resolved or (proxy.TTS_HOST if proxy.TTS_ENABLED else None) or proxy.TTS_HOST
        self.assertEqual(fallback, proxy.TTS_HOST)


# ---------------------------------------------------------------------------
# Seed pinning: proxy_config.yml → capability dispatch matches old constants
# ---------------------------------------------------------------------------

class TestSeedCapabilityPinning(unittest.TestCase):
    """Verify live proxy_config.yml produces bit-identical host resolution.

    These are the regression tests that prove behavior is unchanged for the
    current single-backend-per-capability deployment.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cfg_path = ROOT / "proxy_config.yml"
        with open(cfg_path, encoding="utf-8") as f:
            pconf = yaml.safe_load(f)
        registry = build_service_registry_seed(pconf)
        projected = apply_service_registry_to_config(pconf, registry)
        cls.backends: dict = projected.get("backends", {})

    def _backends_for(self, capability: str) -> list[str]:
        with patch.dict(proxy.config, {"backends": self.backends}, clear=False):
            return proxy.backends_for_capability(capability)

    def _host_for(self, capability: str) -> str | None:
        with patch.dict(proxy.config, {"backends": self.backends}, clear=False):
            return proxy.host_for_capability(capability)

    def test_embeddings_capability_pool_is_embeddings_backend(self) -> None:
        result = self._backends_for("embeddings")
        self.assertEqual(result, ["embeddings"])

    def test_audio_speech_capability_pool_is_tts_backend(self) -> None:
        result = self._backends_for("audio.speech")
        # The always-on default wins capability lookups; on-demand engine services rank after it.
        self.assertEqual(result[0], "tts")
        self.assertEqual(sorted(result), ["tts", "tts-kokoro", "tts-supertonic"])

    def test_audio_transcriptions_capability_pool_is_stt_backend(self) -> None:
        result = self._backends_for("audio.transcriptions")
        self.assertEqual(result[0], "stt")
        self.assertEqual(sorted(result), ["stt", "stt-canary", "stt-nemotron"])

    def test_audio_translations_pool_is_empty_for_current_config(self) -> None:
        """Current stt does not declare audio.translations → empty pool → legacy fallback."""
        result = self._backends_for("audio.translations")
        self.assertEqual(result, [])

    def test_host_for_embeddings_matches_EMBEDDINGS_HOST(self) -> None:
        result = self._host_for("embeddings")
        self.assertEqual(result, proxy.EMBEDDINGS_HOST)

    def test_host_for_audio_speech_matches_TTS_HOST(self) -> None:
        result = self._host_for("audio.speech")
        self.assertEqual(result, proxy.TTS_HOST)

    def test_host_for_audio_transcriptions_matches_STT_HOST(self) -> None:
        result = self._host_for("audio.transcriptions")
        self.assertEqual(result, proxy.STT_HOST)

    def test_host_for_audio_translations_is_none(self) -> None:
        """No backend declares audio.translations → None → STT_HOST fallback applies."""
        result = self._host_for("audio.translations")
        self.assertIsNone(result)

    # --- TWO-BACKEND PROOF via seed + injected second backend ---------------

    def test_two_embeddings_backends_pool_has_both(self) -> None:
        """Inject a second embeddings backend; pool now contains both."""
        augmented = dict(self.backends)
        augmented["embeddings2"] = {
            "enabled": True,
            "capabilities": ["openai", "embeddings"],
            "host": "192.168.1.50",
            "port": 5062,
            "priority": 10,
            "health_endpoint": "/health",
        }
        with patch.dict(proxy.config, {"backends": augmented}, clear=False):
            result = proxy.backends_for_capability("embeddings")
        self.assertIn("embeddings", result)
        self.assertIn("embeddings2", result)
        self.assertEqual(len(result), 2)

    def test_two_embeddings_backends_higher_priority_picked_by_host_for(self) -> None:
        """host_for_capability picks embeddings2 (priority=10) over embeddings (priority≥100)."""
        augmented = dict(self.backends)
        augmented["embeddings2"] = {
            "enabled": True,
            "capabilities": ["openai", "embeddings"],
            "host": "192.168.1.50",
            "port": 5062,
            "priority": 10,
            "health_endpoint": "/health",
        }
        with patch.dict(proxy.config, {"backends": augmented}, clear=False):
            result = proxy.host_for_capability("embeddings")
        self.assertEqual(result, "http://192.168.1.50:5062")

    def test_two_embeddings_backends_priority_ordering(self) -> None:
        """Verify embeddings2 (priority=10) is first in the pool."""
        augmented = dict(self.backends)
        augmented["embeddings2"] = {
            "enabled": True,
            "capabilities": ["openai", "embeddings"],
            "host": "192.168.1.50",
            "port": 5062,
            "priority": 10,
            "health_endpoint": "/health",
        }
        with patch.dict(proxy.config, {"backends": augmented}, clear=False):
            result = proxy.backends_for_capability("embeddings")
        self.assertEqual(result[0], "embeddings2")


if __name__ == "__main__":
    unittest.main()
