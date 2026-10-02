"""Reading a voice description for OmniVoice's design vocabulary (gateway/voice_design.py)."""

from __future__ import annotations

import unittest

from gateway.voice_design import VOCABULARY, DesignPromptError, design_instruct


class DesignInstructTests(unittest.TestCase):
    def test_an_instruct_string_in_the_engine_vocabulary_passes_through(self) -> None:
        self.assertEqual(design_instruct("female, middle-aged, low pitch, british accent"), ("female, middle-aged, low pitch, british accent", []))

    def test_free_text_is_read_for_attributes_and_the_rest_is_reported(self) -> None:
        instruct, ignored = design_instruct("A warm, calm young woman with a bright voice and an Australian accent")
        self.assertEqual(instruct, "female, young adult, high pitch, australian accent")
        self.assertEqual(ignored, ["warm", "calm"])

    def test_whisper_and_age_words(self) -> None:
        self.assertEqual(design_instruct("An old man, deep voice, whispering")[0], "male, elderly, low pitch, whisper")
        self.assertEqual(design_instruct("a little girl")[0], "female")
        self.assertEqual(design_instruct("a child")[0], "child")

    def test_the_more_specific_pitch_wins(self) -> None:
        self.assertEqual(design_instruct("male, very low voice")[0], "male, very low pitch")
        self.assertEqual(design_instruct("female, very high")[0], "female, very high pitch")

    def test_a_description_with_no_known_attribute_is_refused_with_the_vocabulary(self) -> None:
        with self.assertRaises(DesignPromptError) as ctx:
            design_instruct("warm, calm, friendly narrator")
        self.assertIn(VOCABULARY, str(ctx.exception))

    def test_words_inside_other_words_do_not_match(self) -> None:
        self.assertEqual(design_instruct("male, a manly tone")[0], "male")      # not "man" via "manly"
        self.assertNotIn("accent", design_instruct("male, businesslike")[0])
        self.assertEqual(design_instruct("female, light-hearted")[0], "female")  # not "high pitch" via "light"


if __name__ == "__main__":
    unittest.main()
