"""Phase 2 engine-knowledge migrations preserve the owner's observable behavior.

``tests/engine_p2_differential.py`` drives the real routes. The golden file holds
what the pre-migration code sent and answered for the owner's sanitized
configuration; the migrated code with the migrated configuration must match it
byte for byte, apart from the intended changes listed here.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import yaml

from tests.engine_p2_differential import FIXTURES, load_fixture, observe, running_gateway

_ROOT = Path(__file__).resolve().parent.parent
_GOLDEN = json.loads((FIXTURES / "golden.json").read_text(encoding="utf-8"))


def _mismatches(section: str, observed: dict, expected_changes: dict | None = None) -> list[str]:
    golden = _GOLDEN[section]
    changes = expected_changes or {}
    problems = [f"case set differs: {sorted(set(golden) ^ set(observed))}"] if set(golden) != set(observed) else []
    for case in sorted(set(golden) & set(observed)):
        expected = changes.get(case, golden[case])
        if observed[case] != expected:
            problems.append(f"{case}:\n  expected {json.dumps(expected)[:600]}\n  observed {json.dumps(observed[case])[:600]}")
    return problems


class MigratedConfigurationTests(unittest.TestCase):
    def test_the_proven_configuration_is_the_shipped_configuration(self) -> None:
        shipped = yaml.safe_load((_ROOT / "proxy_config.yml").read_text(encoding="utf-8"))
        migrated = load_fixture("owner_config.migrated.json")
        self.assertEqual(migrated["model_aliases"]["speech"], shipped["model_aliases"]["speech"])
        self.assertEqual(migrated["voices"], shipped["voices"])


class VoiceDesignDifferentialTests(unittest.TestCase):
    def test_owner_configuration_designs_byte_identical_requests(self) -> None:
        observed = observe(load_fixture("owner_config.migrated.json"), ("voice_design",))["voice_design"]
        problems = _mismatches("voice_design", observed)
        self.assertFalse(problems, "\n".join(problems[:20]))

    def test_a_replacement_design_engine_without_a_vocabulary_receives_the_free_text(self) -> None:
        fixture = copy.deepcopy(load_fixture("owner_config.migrated.json"))
        engines = fixture["model_aliases"]["speech"]["engines"]
        engines["designer"] = {"service": engines["omnivoice"]["service"], "model": "designer-1"}
        fixture["voices"]["design_engine"] = "designer"
        observed = observe(fixture, ("voice_design",))["voice_design"]
        case = observed["prompt[" + str(_prompt_index("A warm, calm young woman with a bright voice and an Australian accent")) + "] "
                        "'A warm, calm young woman with a bright voice and an Australian accent'"]
        self.assertEqual(case["response"]["json"]["design_instruct"], "A warm, calm young woman with a bright voice and an Australian accent")
        self.assertEqual(case["response"]["json"]["warnings"], ["quality_check_unavailable"])
        body = json.loads(case["upstream"][0]["body"])
        self.assertEqual(list(body), ["input", "instructions", "seed"])
        self.assertEqual(body["instructions"], "A warm, calm young woman with a bright voice and an Australian accent")
        nothing = observed["prompt[" + str(_prompt_index("warm and calm")) + "] 'warm and calm'"]
        self.assertEqual(nothing["response"]["status"], 200)

    def test_without_a_design_engine_voice_design_is_unconfigured_not_guessed(self) -> None:
        fixture = copy.deepcopy(load_fixture("owner_config.migrated.json"))
        del fixture["voices"]["design_engine"]
        observed = observe(fixture, ("voice_design",))["voice_design"]
        case = observed["explicit script"]
        self.assertEqual(case["upstream"], [])
        self.assertEqual(case["response"]["status"], 503)
        self.assertIn("not configured", case["response"]["json"]["error"]["message"])


class SpeechVoiceDifferentialTests(unittest.TestCase):
    """Every request and the voice listing match, except where a voice was silently replaced."""

    def test_owner_configuration_speaks_byte_identical_requests(self) -> None:
        observed = observe(load_fixture("owner_config.migrated.json"), ("speech",))["speech"]
        golden = _GOLDEN["speech"]
        self.assertEqual(set(observed), set(golden))
        identical = {case for case in golden if observed[case] == golden[case]}
        self.assertIn("GET /v1/audio/voices", identical)
        changed = sorted(set(golden) - identical)
        refused, now_listed = [], []
        for case in changed:
            old, new = golden[case], observed[case]
            old_body = json.loads(old["upstream"][0]["body"])
            # The old gateway sent the engine's default in place of a voice the engine did not know.
            self.assertEqual(old_body["voice"], _ENGINE_DEFAULTS[old_body["model"]], case)
            if new["response"]["status"] == 400:
                self.assertEqual(new["response"]["json"]["error"]["code"], "voice_not_available", case)
                self.assertEqual(new["upstream"], [], case)
                refused.append(case)
            else:
                new_body = json.loads(new["upstream"][0]["body"])
                self.assertEqual({**new_body, "voice": None}, {**old_body, "voice": None}, case)
                now_listed.append((case, new_body["voice"]))
        # Intended changes: an unknown voice is refused, and a listed Kokoro voice that was not an
        # OpenAI-name mapping reaches the engine instead of becoming af_heart.
        self.assertEqual(len(identical), 714)
        self.assertEqual(len(refused), 222)
        self.assertEqual(now_listed, [('kokoro voice="bm_george"', "bm_george")])

    def test_a_replacement_engine_speaks_its_own_voices(self) -> None:
        fixture = copy.deepcopy(load_fixture("owner_config.migrated.json"))
        fixture["model_aliases"]["speech"]["engines"]["kokoro"] = {
            "service": "tts-kokoro", "model": "narrator-tts",
            "voices": [{"id": "Narrator-7", "name": "Narrator"}, "Reader-2"], "default_voice": "Reader-2",
        }
        fixture["model_aliases"]["speech"]["engines"]["supertonic"] = {"service": "tts-supertonic", "model": "open-tts"}
        with running_gateway(fixture) as (_proxy, client, upstream, _root):
            def sent(model: str, **voice: object) -> object:
                resp = client.post("/v1/audio/speech", json={"model": model, "input": "hi", "response_format": "wav", **voice})
                taken = upstream.take()
                return json.loads(taken[0]["body"])["voice"] if resp.status_code == 200 else resp.json()["error"]["code"]

            self.assertEqual(sent("kokoro", voice="narrator-7"), "Narrator-7")
            self.assertEqual(sent("kokoro"), "Reader-2")
            self.assertEqual(sent("kokoro", voice="alloy"), "voice_not_available")
            # An engine without a voice list takes the caller's voice id; nothing is invented.
            self.assertEqual(sent("supertonic", voice="Any-Voice"), "any-voice")
            self.assertEqual(sent("supertonic"), "")
            listing = client.get("/v1/audio/voices").json()
        self.assertNotIn("kokoro", listing["engines"])


_ENGINE_DEFAULTS = {"supertonic": "F1", "kokoro": "af_heart", "k2-fsa/OmniVoice": "alloy"}


def _prompt_index(prompt: str) -> int:
    from tests.engine_p2_differential import _DESIGN_PROMPTS

    return _DESIGN_PROMPTS.index(prompt)


if __name__ == "__main__":
    unittest.main()
