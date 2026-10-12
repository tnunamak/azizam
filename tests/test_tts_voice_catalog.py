"""Speech voices are engine data: OpenAI voice names are the identities.

Each engine record in proxy_config.yml lists its voices; ``voice_aliases`` holds
older spellings. Backends only ever see an engine voice id, so the same name
means the same speaker on every engine that shares the reference clips.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway import media_jobs, receipt_store  # noqa: E402
from gateway.speech_voices import resolve_voice, voice_records  # noqa: E402
from tests.test_openai_compat_validation import _FakeTTSAsyncClient  # noqa: E402

OPENAI_VOICES = {
    "alloy", "ash", "ballad", "cedar", "coral", "echo", "fable",
    "marin", "nova", "onyx", "sage", "shimmer", "verse",
}
SPEECH = yaml.safe_load((ROOT / "proxy_config.yml").read_text(encoding="utf-8"))["model_aliases"]["speech"]
OMNIVOICE = SPEECH["engines"]["omnivoice"]
ALIASES = SPEECH["voice_aliases"]


def map_voice(name: str) -> str | None:
    return resolve_voice(OMNIVOICE, ALIASES, name.lower())


class SeededVoiceDataTests(unittest.TestCase):
    def test_the_cloned_voices_are_openais_voice_list_then_the_design_presets(self) -> None:
        ids = [record["id"] for record in voice_records(OMNIVOICE)]
        self.assertEqual(set(ids[:13]), OPENAI_VOICES)
        self.assertTrue(all(i.endswith(("_female", "_male")) for i in ids[13:]))
        # Chatterbox clones the same 13 clips; the design presets are OmniVoice's alone.
        self.assertEqual([r["id"] for r in voice_records(SPEECH["engines"]["chatterbox"])], ids[:13])

    def test_every_old_spelling_names_a_cloned_voice(self) -> None:
        for alias, target in ALIASES.items():
            with self.subTest(alias=alias):
                self.assertIn(target, OPENAI_VOICES)
                self.assertNotIn(alias, OPENAI_VOICES)
                self.assertEqual(map_voice(alias), target)
                self.assertEqual(map_voice(target), target)  # aliases never chain

    def test_every_engine_maps_onto_its_own_listed_voices(self) -> None:
        for engine_id, spec in SPEECH["engines"].items():
            ids = {record["id"] for record in voice_records(spec)}
            with self.subTest(engine=engine_id):
                self.assertIn(spec["default_voice"], ids)
                self.assertLessEqual(set((spec.get("voice_map") or {}).values()), ids)

    def test_names_known_and_unknown(self) -> None:
        self.assertEqual(map_voice("cheerful_female"), "shimmer")
        self.assertEqual(map_voice("Marin"), "marin")
        self.assertEqual(map_voice("de_female"), "de_female")
        self.assertEqual(map_voice(""), "alloy")  # no voice: the engine default
        for name in ("no_such_voice", "Jenny", " alloy"):
            with self.subTest(voice=name):
                self.assertIsNone(map_voice(name))


class VoiceRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.receipts_db = root / "receipts.db"
        self.media_jobs_db = root / "media_jobs.db"
        receipt_store.init_db(self.receipts_db)
        media_jobs.init_db(self.media_jobs_db)
        self.client = TestClient(proxy.app)
        _FakeTTSAsyncClient.requests = []
        self.patches = [
            patch.object(proxy, "AUTH_ENABLED", False),
            patch.object(proxy, "_enforce_runtime_route_policy", return_value=None),
            patch.object(proxy, "_current_loaded_model_policy_decision", return_value=None),
            patch.object(proxy.httpx, "AsyncClient", _FakeTTSAsyncClient),
            patch.object(proxy, "_ensure_speech_service_ready", AsyncMock(return_value=None)),
            patch.object(receipt_store, "DB_PATH", self.receipts_db),
            patch.object(media_jobs, "DB_PATH", self.media_jobs_db),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def speak(self, voice: str, model: str = "omnivoice"):
        return self.client.post(
            "/v1/audio/speech",
            json={"model": model, "voice": voice, "input": "hello", "response_format": "wav"},
        )

    def sent_voice(self, voice: str, model: str = "omnivoice") -> str:
        resp = self.speak(voice, model)
        self.assertEqual(resp.status_code, 200, resp.text)
        return json.loads(_FakeTTSAsyncClient.requests[-1]["content"])["voice"]

    def test_backend_only_sees_engine_voice_ids(self) -> None:
        # An engine that clones the OpenAI names (OmniVoice here) receives the canonical name.
        self.assertEqual(self.sent_voice("cheerful_female"), "shimmer")
        self.assertEqual(self.sent_voice("Marin"), "marin")

    def test_an_unknown_voice_is_refused_not_replaced(self) -> None:
        sent = len(_FakeTTSAsyncClient.requests)
        for model in ("omnivoice", "tts-1"):
            with self.subTest(model=model):
                resp = self.speak("nope", model=model)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual((resp.json()["error"]["code"], resp.json()["error"]["param"]), ("voice_not_available", "voice"))
        self.assertEqual(len(_FakeTTSAsyncClient.requests), sent)

    def test_a_preset_voice_engine_resolves_legacy_names_through_the_canonical_voice(self) -> None:
        # tts-1 is Supertonic while Chatterbox is disabled: a legacy name must land on the same preset
        # voice as its canonical name, not on the engine's default voice.
        shimmer = self.sent_voice("shimmer", model="tts-1")
        self.assertEqual(shimmer, "F4")
        self.assertEqual(self.sent_voice("cheerful_female", model="tts-1"), shimmer)
        self.assertEqual(self.sent_voice("casual_male", model="tts-1"), self.sent_voice("echo", model="tts-1"))
        self.assertEqual(self.sent_voice("Marin", model="tts-1"), "F1")
        self.assertEqual(self.sent_voice("", model="tts-1"), "F1")  # no voice: the engine default
        self.assertEqual(self.sent_voice("M3", model="tts-1"), "M3")  # an engine-native name passes through

    def test_every_listed_kokoro_voice_reaches_the_engine(self) -> None:
        self.assertEqual(self.sent_voice("bm_george", model="kokoro"), "bm_george")

    def test_voices_endpoint_lists_every_voice_tts_1_speaks(self) -> None:
        body = self.client.get("/v1/voices").json()
        entries = {v["voice_id"]: v for v in body["voices"]}
        # The default is what tts-1 sends when no voice is named.
        self.assertEqual(body["default_voice"], self.sent_voice("", model="tts-1"))
        for name in OPENAI_VOICES:
            self.assertFalse(entries[name]["alias"], name)
            self.assertEqual(entries[name]["mapped_to"], name)
            self.assertIn(entries[name]["gender"], {"female", "male"})
        for alias, target in ALIASES.items():
            self.assertTrue(entries[alias]["alias"], alias)
            self.assertEqual(entries[alias]["mapped_to"], target)
            self.assertEqual(entries[alias]["gender"], entries[target]["gender"])
        # Every listed voice is spoken by tts-1; a preset only a later engine lists reaches that engine.
        for voice_id in entries:
            with self.subTest(voice=voice_id):
                self.sent_voice(voice_id, model="tts-1")
        self.assertEqual(self.sent_voice("de_female", model="tts-1"), "de_female")
        self.assertEqual(body["voices"], self.client.get("/v1/voices", params={"model": "tts-1"}).json()["voices"])

if __name__ == "__main__":
    unittest.main()
