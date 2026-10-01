"""Canonical TTS voice catalog: OpenAI voice names are the identities.

The gateway owns alias -> canonical mapping (one place).  Backends only ever
see a canonical voice name, so the same name means the same speaker on every
TTS tier/backend that shares the voice catalog.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.transforms import (  # noqa: E402
    CANONICAL_TTS_VOICES,
    DEFAULT_TTS_VOICE,
    LEGACY_TTS_VOICE_ALIASES,
    run_request_pipeline,
)
from tests.test_openai_compat_validation import _FakeTTSAsyncClient  # noqa: E402

OPENAI_VOICES = {
    "alloy", "ash", "ballad", "cedar", "coral", "echo", "fable",
    "marin", "nova", "onyx", "sage", "shimmer", "verse",
}


def map_voice(name: str) -> str:
    return run_request_pipeline(["voxtral-voices"], {"voice": name})["voice"]


class CanonicalVoiceMappingTests(unittest.TestCase):
    def test_canonical_set_is_openais_current_voice_list(self) -> None:
        self.assertEqual(set(CANONICAL_TTS_VOICES), OPENAI_VOICES)

    def test_canonical_names_map_to_themselves(self) -> None:
        for name in OPENAI_VOICES:
            with self.subTest(voice=name):
                self.assertEqual(map_voice(name), name)

    def test_every_legacy_alias_resolves_to_a_canonical_voice(self) -> None:
        self.assertTrue(LEGACY_TTS_VOICE_ALIASES)
        for alias, target in LEGACY_TTS_VOICE_ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIn(target, OPENAI_VOICES)
                self.assertNotIn(alias, OPENAI_VOICES)
                self.assertEqual(map_voice(alias), target)

    def test_known_legacy_names_keep_their_character(self) -> None:
        self.assertEqual(map_voice("cheerful_female"), "shimmer")
        self.assertEqual(map_voice("neutral_female"), "alloy")
        self.assertEqual(map_voice("casual_male"), "echo")

    def test_unknown_and_empty_voice_use_documented_default(self) -> None:
        self.assertEqual(DEFAULT_TTS_VOICE, "alloy")
        for name in ("", "no_such_voice", "Jenny"):
            with self.subTest(voice=name):
                self.assertEqual(map_voice(name), DEFAULT_TTS_VOICE)

    def test_language_presets_pass_through(self) -> None:
        for name in ("de_female", "pt_male", "hi_male"):
            self.assertEqual(map_voice(name), name)

    def test_aliases_never_chain(self) -> None:
        for target in LEGACY_TTS_VOICE_ALIASES.values():
            self.assertEqual(map_voice(target), target)


class VoiceRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(proxy.app)
        _FakeTTSAsyncClient.requests = []
        self.patches = [
            patch.object(proxy, "AUTH_ENABLED", False),
            patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
            patch.object(proxy, "_current_loaded_model_policy_decision", return_value=None),
            patch.object(proxy.httpx, "AsyncClient", _FakeTTSAsyncClient),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def speak(self, voice: str) -> str:
        resp = self.client.post(
            "/v1/audio/speech",
            json={"model": "tts-1", "voice": voice, "input": "hello", "response_format": "wav"},
        )
        self.assertEqual(resp.status_code, 200, resp.text)
        return json.loads(_FakeTTSAsyncClient.requests[-1]["content"])["voice"]

    def test_backend_only_sees_canonical_voice_names(self) -> None:
        self.assertEqual(self.speak("cheerful_female"), "shimmer")
        self.assertEqual(self.speak("Marin"), "marin")
        self.assertEqual(self.speak("nope"), DEFAULT_TTS_VOICE)

    def test_voices_endpoint_lists_canonical_then_aliases(self) -> None:
        body = self.client.get("/v1/voices").json()
        entries = {v["voice_id"]: v for v in body["voices"]}
        self.assertEqual(body["default_voice"], DEFAULT_TTS_VOICE)
        for name in OPENAI_VOICES:
            self.assertFalse(entries[name]["alias"], name)
            self.assertEqual(entries[name]["mapped_to"], name)
            self.assertIn(entries[name]["gender"], {"female", "male"})
        for alias, target in LEGACY_TTS_VOICE_ALIASES.items():
            self.assertTrue(entries[alias]["alias"], alias)
            self.assertEqual(entries[alias]["mapped_to"], target)
            self.assertEqual(entries[alias]["gender"], entries[target]["gender"])
        # Every listed voice is accepted by the speech route without falling to the default.
        for voice_id, entry in entries.items():
            self.assertEqual(map_voice(voice_id), entry["mapped_to"])
        self.assertEqual(body["voices"][0]["voice_id"] in OPENAI_VOICES, True)


if __name__ == "__main__":
    unittest.main()
