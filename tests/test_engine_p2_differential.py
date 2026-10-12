"""Phase 2 engine-knowledge migrations preserve the owner's observable behavior.

``tests/engine_p2_differential.py`` drives the real routes. The golden file holds
what the pre-migration code sent and answered for the owner's sanitized
configuration; the migrated code with the migrated configuration must match it
byte for byte, apart from the intended changes listed here.
"""
from __future__ import annotations

import asyncio
import copy
import json
import unittest
from pathlib import Path

import yaml

from tests.engine_p2_differential import (
    _PRESET_VOICES, _SPEECH_MODELS, FIXTURES, load_fixture, observe, running_gateway,
)

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


class SttChunkMigrationTests(unittest.TestCase):
    def test_the_migrated_backends_are_the_schema_6_migration_of_the_owner_registry(self) -> None:
        import sqlite3

        from gateway import stt_chunk_transform_migration as migration

        owner, migrated = load_fixture("owner_config.json"), load_fixture("owner_config.migrated.json")
        self.assertEqual(migrated["backends"], migration.migrated_backends(owner["backends"]))
        # The registry migration attaches the same rows to registry-shaped records, once.
        registry = {"services": {sid: {"enabled": cfg["enabled"], "capabilities": cfg["capabilities"],
                                       "config": {k: v for k, v in cfg.items() if k != "capabilities"}}
                                 for sid, cfg in owner["backends"].items()}}
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE operator_settings (key TEXT, value_json TEXT)")
        conn.execute("CREATE TABLE operator_settings_generations (setting_key TEXT, operator_settings_generation INT)")
        conn.execute("INSERT INTO operator_settings VALUES ('service_registry', ?)", (json.dumps(registry),))
        conn.execute("INSERT INTO operator_settings_generations VALUES ('service_registry', 1)")
        changed = migration.migrate_registry(conn)
        self.assertEqual(sorted(changed), sorted(sid for sid in owner["backends"] if sid.startswith("stt")))
        self.assertEqual(migration.migrate_registry(conn), [])
        stored = json.loads(conn.execute("SELECT value_json FROM operator_settings").fetchone()[0])
        for sid, service in stored["services"].items():
            self.assertEqual(service["config"].get("transforms"), migrated["backends"][sid].get("transforms"), sid)

    def test_every_speech_to_text_spelling_gets_the_row_and_chat_services_do_not(self) -> None:
        from gateway.stt_chunk_transform_migration import migrated_backends

        backends = {
            "compound": {"capabilities": ["audio.transcriptions"]},
            "stt-word": {"capabilities": ["stt"]},
            "translate": {"capabilities": ["audio", "translations"]},
            "chunked-only": {"chunking": {"enabled": True}},
            "dual": {"capabilities": ["chat", "transcriptions"]},
            "speech": {"capabilities": ["audio", "speech"]},
            "kept": {"capabilities": ["transcriptions"], "transforms": ["llamacpp-cache-prompt"]},
        }
        rows = {sid: cfg.get("transforms") for sid, cfg in migrated_backends(backends).items()}
        row = {"kind": "drop-params", "phase": "response", "drop": ["timing"]}
        self.assertEqual(rows, {
            "compound": [row], "stt-word": [row], "translate": [row], "chunked-only": [row],
            "dual": None, "speech": None, "kept": ["llamacpp-cache-prompt", row],
        })

    def test_a_schema_5_store_gains_the_rows_once_and_keeps_a_later_removal(self) -> None:
        import sqlite3
        import tempfile

        from gateway import admin_store

        owner = load_fixture("owner_config.json")
        registry = {"services": {"stt-canary": {"enabled": True, "capabilities": ["audio", "transcriptions"],
                                                "config": {"host": "127.0.0.1", "port": 8013}},
                                 "tts": {"enabled": True, "capabilities": ["audio", "speech"],
                                         "config": {"host": "127.0.0.1", "port": 8002}}}}
        self.assertIn("stt-canary", owner["backends"])
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "admin.db"
            admin_store.init_db(db)
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE schema_version SET version=5")
                conn.execute("INSERT INTO operator_settings VALUES ('service_registry', ?, 123, 'fixture')", (json.dumps(registry),))
                conn.execute("INSERT INTO operator_settings_generations VALUES ('service_registry', 7)")

            def stored() -> tuple[dict, int]:
                with sqlite3.connect(db) as conn:
                    raw = conn.execute("SELECT value_json FROM operator_settings WHERE key='service_registry'").fetchone()[0]
                    generation = conn.execute("SELECT operator_settings_generation FROM operator_settings_generations").fetchone()[0]
                return json.loads(raw), generation

            admin_store.init_db(db)
            upgraded, generation = stored()
            self.assertEqual(upgraded["services"]["stt-canary"]["config"]["transforms"],
                             [{"kind": "drop-params", "phase": "response", "drop": ["timing"]}])
            self.assertNotIn("transforms", upgraded["services"]["tts"]["config"])
            self.assertEqual((generation, admin_store.schema_version(db)), (8, admin_store.SCHEMA_VERSION))
            # The operator removes the row; a restart does not restore it.
            upgraded["services"]["stt-canary"]["config"]["transforms"] = []
            with sqlite3.connect(db) as conn:
                conn.execute("UPDATE operator_settings SET value_json=?", (json.dumps(upgraded),))
            admin_store.init_db(db)
            self.assertEqual(stored()[0]["services"]["stt-canary"]["config"]["transforms"], [])

    def test_owner_configuration_merges_byte_identical_transcripts(self) -> None:
        observed = observe(load_fixture("owner_config.migrated.json"), ("stt",))["stt"]
        problems = _mismatches("stt", observed)
        self.assertFalse(problems, "\n".join(problems[:20]))
        # Direct (single-request) answers keep the diagnostic; only chunk merging drops it.
        self.assertIn('"timing"', observed["stt-canary timing x1"]["response"]["body"])

    def test_without_the_row_a_timing_chunk_cannot_be_merged(self) -> None:
        fixture = copy.deepcopy(load_fixture("owner_config.migrated.json"))
        fixture["backends"]["stt-canary"]["transforms"] = []
        observed = observe(fixture, ("stt",))["stt"]
        self.assertEqual(observed["stt-canary timing x2"]["response"]["status"], 502)
        self.assertEqual(observed["stt-canary plain x2"], _GOLDEN["stt"]["stt-canary plain x2"])
        self.assertEqual(observed["stt timing x2"], _GOLDEN["stt"]["stt timing x2"])


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
    """Every request matches, except where a voice was silently replaced; the listing is what is accepted."""

    def test_owner_configuration_speaks_byte_identical_requests(self) -> None:
        observed = observe(load_fixture("owner_config.migrated.json"), ("speech",))["speech"]
        golden = _GOLDEN["speech"]
        self.assertEqual(set(observed), set(golden))
        identical = {case for case in golden if observed[case] == golden[case]}
        changed = sorted(set(golden) - identical - {"GET /v1/audio/voices"})
        refused, now_listed, rerouted = [], [], {}
        for case in changed:
            old, new = golden[case], observed[case]
            old_body = json.loads(old["upstream"][0]["body"])
            # The old gateway sent the engine's default in place of a voice the engine did not know.
            self.assertEqual(old_body["voice"], _ENGINE_DEFAULTS[old_body["model"]], case)
            model, voice = case.split(" voice=")
            if new["response"]["status"] == 400:
                self.assertEqual(new["response"]["json"]["error"]["code"], "voice_not_available", case)
                self.assertEqual(new["upstream"], [], case)
                refused.append(case)
            elif new == observed.get(f"{_REROUTED.get(model)} voice={voice}"):
                # A tier sends the voice to its first engine that lists it, exactly as naming that engine does.
                rerouted.setdefault(_REROUTED[model], []).append(case)
            else:
                new_body = json.loads(new["upstream"][0]["body"])
                self.assertEqual({**new_body, "voice": None}, {**old_body, "voice": None}, case)
                now_listed.append((case, new_body["voice"]))
        # Intended changes: an unknown voice is refused; a voice that only a later engine of a tier
        # lists reaches that engine; a listed Kokoro voice that was not an OpenAI-name mapping reaches
        # the engine instead of becoming af_heart.
        self.assertEqual(len(identical), 713)
        self.assertEqual(len(refused), 146)
        presets = [f'"{v}"' for v in (*_PRESET_VOICES, "DE_Female")]
        self.assertEqual(sorted(rerouted["omnivoice"]), sorted(
            f"{model} voice={voice}" for model in ("tts-1", "gpt-4o-mini-tts", "tts", "tts-fast") for voice in presets))
        self.assertEqual(sorted(rerouted["kokoro"]), sorted(
            f"{model} voice={voice}" for model in ("tts-1-hd", "tts-quality")
            for voice in ('"af_heart"', '"AF_Bella"', '"bm_george"', '"am_onyx"')))
        self.assertEqual(now_listed, [('kokoro voice="bm_george"', "bm_george")])
        # An unknown voice is still refused on the default model.
        self.assertEqual(observed['tts-1 voice="nope"']["response"]["json"]["error"]["code"], "voice_not_available")

    def test_the_voice_listing_is_exactly_the_voices_each_model_accepts(self) -> None:
        from tests.engine_p2_differential import _observe_speech

        with running_gateway(load_fixture("owner_config.migrated.json")) as (proxy, client, upstream, _root):
            observed = asyncio.run(_observe_speech(proxy, client, upstream))
            overall = client.get("/v1/audio/voices").json()
            for model in _SPEECH_MODELS:
                resp = client.get("/v1/audio/voices", params={"model": model})
                refused = observed[f'{model} voice="alloy"']["response"]
                if refused["status"] != 200:  # no such model, or not deployed in the owner's registry
                    self.assertEqual((resp.status_code, resp.json()), (refused["status"], refused["json"]), model)
                    continue
                listing = resp.json()
                listed = {entry["id"].lower() for entry in listing["voices"]}
                # Every listed voice is spoken by the model ...
                for voice in sorted(listed):
                    spoken = client.post("/v1/audio/speech", json={"model": model, "input": "hi", "response_format": "wav", "voice": voice})
                    upstream.take()
                    self.assertEqual(spoken.status_code, 200, f"{model} {voice}")
                # ... and every voice the harness got spoken is listed.
                for case, result in observed.items():
                    named, _, voice = case.partition(" voice=")
                    voice = json.loads(voice) if voice else None
                    if named != model or not isinstance(voice, str) or voice in ("", "<omitted>") or voice.startswith("voice_"):
                        continue
                    self.assertEqual(result["response"]["status"] == 200, voice.lower() in listed, f"{model} {voice!r}")
                # Try offers the same voices, without the accepted spellings.
                choices = proxy._speech_voice_choices(model)
                if choices is not None:
                    self.assertEqual([c["id"] for c in choices["voices"]],
                                     [e["id"] for e in listing["voices"] if not e["alias"]], model)
                    self.assertEqual(choices["default_voice"], listing["default_voice"], model)
            self.assertEqual(overall["voices"], client.get("/v1/audio/voices", params={"model": "tts-1"}).json()["voices"])
        # The owner's tts-1 tier is Supertonic then OmniVoice: its codes lead, then the clips and presets.
        self.assertEqual(overall["default_voice"], "F1")
        ids = [entry["id"] for entry in overall["voices"] if not entry["alias"]]
        self.assertEqual(ids[:11], ["F1", "F2", "F3", "F4", "F5", "M1", "M2", "M3", "M4", "M5", "alloy"])
        self.assertIn("de_female", ids)

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
# The engine a tier's later-listed voices reach in the owner's configuration.
_REROUTED = {"tts-1": "omnivoice", "gpt-4o-mini-tts": "omnivoice", "tts": "omnivoice", "tts-fast": "omnivoice",
             "tts-1-hd": "kokoro", "tts-quality": "kokoro"}


def _prompt_index(prompt: str) -> int:
    from tests.engine_p2_differential import _DESIGN_PROMPTS

    return _DESIGN_PROMPTS.index(prompt)


if __name__ == "__main__":
    unittest.main()
