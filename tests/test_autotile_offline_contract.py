"""Pure autotile configuration contracts; no model, subprocess, or network."""

from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest


RUNTIME = Path(__file__).resolve().parents[1] / "skills" / "autotile-edge-stitch" / "scripts" / "runtime.py"


class AutotileOfflineContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = runpy.run_path(str(RUNTIME))

    def test_portable_ids_keep_display_spelling_but_share_canonical_key(self):
        validate_id = self.runtime["safe_spec_id"]
        key = self.runtime["spec_id_key"]
        composed, decomposed = "Café", "CAFE\u0301"
        self.assertEqual(validate_id(composed, "layer id"), composed)
        self.assertEqual(validate_id(decomposed, "layer id"), decomposed)
        self.assertEqual(key(composed), key(decomposed))
        self.assertNotEqual(key(composed), key("Cafe"))

    def test_required_value_preserves_the_original_path_and_names_missing_option(self):
        require = self.runtime["require"]
        supplied = Path("generated images")
        self.assertIs(require(supplied, "--output"), supplied)
        with self.assertRaisesRegex(ValueError, "--output is required"):
            require("  ", "--output")

    def test_workflow_output_resolves_nested_encoded_spaces_without_creating_files(self):
        output_path = self.runtime["comfy_output_path"]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(
                output_path(root, "tiles/grass%20edge.png"),
                (root / "tiles" / "grass edge.png").resolve(),
            )
            self.assertEqual(list(root.iterdir()), [])
