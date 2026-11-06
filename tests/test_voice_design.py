"""Reading a voice description against an engine's design vocabulary (gateway/voice_design.py)."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from gateway.voice_design import DesignPromptError, design_instruct, design_request

_CONFIG = yaml.safe_load((Path(__file__).resolve().parent.parent / "proxy_config.yml").read_text(encoding="utf-8"))
# The seed data of the engine the owner designs voices on.
OMNIVOICE = _CONFIG["model_aliases"]["speech"]["engines"][_CONFIG["voices"]["design_engine"]]["voice_design"]


def instruct(prompt: str) -> tuple[str, list[str]]:
    return design_instruct(prompt, OMNIVOICE)


class SeededVocabularyTests(unittest.TestCase):
    def test_an_instruct_string_in_the_engine_vocabulary_passes_through(self) -> None:
        self.assertEqual(instruct("female, middle-aged, low pitch, british accent"), ("female, middle-aged, low pitch, british accent", []))

    def test_free_text_is_read_for_attributes_and_the_rest_is_reported(self) -> None:
        text, ignored = instruct("A warm, calm young woman with a bright voice and an Australian accent")
        self.assertEqual(text, "female, young adult, high pitch, australian accent")
        self.assertEqual(ignored, ["warm", "calm"])

    def test_whisper_and_age_words(self) -> None:
        self.assertEqual(instruct("An old man, deep voice, whispering")[0], "male, elderly, low pitch, whisper")
        self.assertEqual(instruct("a little girl")[0], "female")
        self.assertEqual(instruct("a child")[0], "child")

    def test_the_more_specific_pitch_wins(self) -> None:
        self.assertEqual(instruct("male, very low voice")[0], "male, very low pitch")
        self.assertEqual(instruct("female, very high")[0], "female, very high pitch")

    def test_a_description_with_no_known_attribute_is_refused_with_the_vocabulary(self) -> None:
        with self.assertRaises(DesignPromptError) as ctx:
            instruct("warm, calm, friendly narrator")
        self.assertIn("female, male, child, teenager, young adult", str(ctx.exception))
        self.assertIn("russian accent.", str(ctx.exception))

    def test_words_inside_other_words_do_not_match(self) -> None:
        self.assertEqual(instruct("male, a manly tone")[0], "male")      # not "man" via "manly"
        self.assertNotIn("accent", instruct("male, businesslike")[0])
        self.assertEqual(instruct("female, light-hearted")[0], "female")  # not "high pitch" via "light"

    def test_the_seeded_request_template_keeps_the_engine_wire_fields(self) -> None:
        self.assertEqual(
            list(design_request(OMNIVOICE, script="Hi.", instructions="male", seed=7).items()),
            [("input", "Hi."), ("voice", "alloy"), ("instructions", "male"), ("seed", 7), ("response_format", "wav")],
        )


class ReplacementEngineTests(unittest.TestCase):
    def test_no_vocabulary_passes_the_exact_description(self) -> None:
        for settings in (None, {}, {"vocabulary": {}}, {"request": {"text": "{instructions}"}}):
            with self.subTest(settings=settings):
                self.assertEqual(design_instruct("  A warm, calm narrator!  ", settings), ("  A warm, calm narrator!  ", []))

    def test_another_vocabulary_follows_its_own_data(self) -> None:
        settings = {
            "vocabulary": {"mood": {"Cheerful": ["happy", "cheerful"], "Sombre": ["sad"]}, "gender": {"F": ["woman"]}},
            "ignore": ["a"],
            "separator": " | ",
        }
        self.assertEqual(design_instruct("A happy, SAD woman narrator", settings), ("Cheerful | F", ["sad", "narrator"]))
        with self.assertRaises(DesignPromptError) as ctx:
            design_instruct("female", settings)
        self.assertIn("Use words from: Cheerful, Sombre, F.", str(ctx.exception))

    def test_without_a_template_the_request_carries_only_the_three_values(self) -> None:
        self.assertEqual(design_request(None, script="Hi.", instructions="calm", seed=3), {"input": "Hi.", "instructions": "calm", "seed": 3})
        self.assertEqual(
            design_request({"request": {"prompt": "{instructions}", "text": "{input}", "format": "mp3", "n": 1}}, script="Hi.", instructions="calm", seed=3),
            {"prompt": "calm", "text": "Hi.", "format": "mp3", "n": 1},
        )


if __name__ == "__main__":
    unittest.main()
