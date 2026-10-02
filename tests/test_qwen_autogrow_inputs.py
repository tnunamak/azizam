"""Qwen-Image 2.1 reference images must use ComfyUI's dotted Autogrow keys.

ComfyUI's API format flattens the ``images`` Autogrow input into
``images.image_1``, ``images.image_2``... A nested ``"images": {"image_1": [...]}``
dict is not resolved as links: the node then runs with no reference images and
the edit silently becomes text-to-image (reproduced 2026-10-02: "make the outfit
red" returned an unrelated product photo).
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / "workflows"


class QwenAutogrowTests(unittest.TestCase):
    def test_reference_images_use_dotted_keys(self) -> None:
        checked = 0
        for path in WORKFLOWS.rglob("*.json"):
            if path.name.endswith(".ui.json"):
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            for node_id, node in data.items():
                if not isinstance(node, dict) or node.get("class_type") != "TextEncodeQwenImage21":
                    continue
                inputs = node["inputs"]
                self.assertNotIn("images", inputs, f"{path.name} node {node_id}: nested images dict")
                keys = [k for k in inputs if k.startswith("images.")]
                self.assertTrue(keys, f"{path.name} node {node_id}: no reference image inputs")
                for key in keys:
                    self.assertRegex(key, r"^images\.image_\d+$")
                    self.assertIsInstance(inputs[key], list)
                checked += 1
        self.assertGreaterEqual(checked, 3)


if __name__ == "__main__":
    unittest.main()
