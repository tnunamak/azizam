"""Focused tests for image background-removal decision and workflow wiring."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402


class TestBackgroundRemoval(unittest.TestCase):
    def test_parse_openai_background_option(self) -> None:
        for raw, expected in [
            (None, "auto"),
            ("", "auto"),
            ("auto", "auto"),
            ("transparent", "transparent"),
            ("opaque", "opaque"),
            (" Transparent ", "transparent"),
        ]:
            with self.subTest(raw=raw):
                parsed, err = proxy.parse_image_background(raw)
                self.assertIsNone(err)
                self.assertEqual(parsed, expected)

        parsed, err = proxy.parse_image_background("remove")
        self.assertIsNone(parsed)
        self.assertIn("background", err or "")

    def test_parse_valid_decision(self) -> None:
        decision = proxy.parse_background_removal_decision(
            '{"remove_background": true, "confidence": 0.87, "reason": "asset"}'
        )
        self.assertTrue(decision.remove_background)
        self.assertEqual(decision.confidence, 0.87)
        self.assertEqual(decision.source, "llm")

    def test_parse_invalid_decision_falls_back_false(self) -> None:
        decision = proxy.parse_background_removal_decision('{"remove_background": "yes"}')
        self.assertFalse(decision.remove_background)
        self.assertEqual(decision.confidence, 0.0)
        self.assertIn("boolean", decision.error)

    def test_workflow_toggle_uses_committed_rmbg_branch(self) -> None:
        workflow = proxy.load_graph_workflow("flux2-klein-4b")
        self.assertIsNotNone(workflow)
        workflow = copy.deepcopy(workflow)
        meta = workflow.pop("_meta", {})
        workflow.pop("__ui_workflow__", None)

        original_save_input = meta["background_removal"]["plain_image"]
        ok = proxy.set_workflow_background_removal(workflow, meta, True)

        self.assertTrue(ok)
        rewired = workflow["9"]["inputs"]["images"]
        self.assertEqual(rewired, meta["background_removal"]["transparent_image"])
        rmbg_node = workflow[rewired[0]]
        self.assertEqual(rmbg_node["class_type"], "RMBG")
        self.assertEqual(rmbg_node["inputs"]["image"], original_save_input)
        self.assertEqual(rmbg_node["inputs"]["model"], "RMBG-2.0")
        self.assertEqual(rmbg_node["inputs"]["background"], "Alpha")

        self.assertTrue(proxy.set_workflow_background_removal(workflow, meta, False))
        self.assertEqual(workflow["9"]["inputs"]["images"], original_save_input)

    def test_all_image_workflows_have_committed_background_removal_branch(self) -> None:
        for path in sorted((ROOT / "workflows" / "images").glob("*.json")):
            if path.name.endswith(".ui.json"):
                continue
            with self.subTest(path=path.name):
                workflow = json.loads(path.read_text(encoding="utf-8"))
                meta = workflow.pop("_meta", {})
                config = meta.get("background_removal")
                self.assertIsInstance(config, dict)
                self.assertTrue(proxy.set_workflow_background_removal(workflow, meta, True))
                save = workflow[config["save_node"]]
                selected = save["inputs"]["images"]
                self.assertEqual(selected, config["transparent_image"])
                self.assertEqual(workflow[selected[0]]["class_type"], "RMBG")
                self.assertEqual(
                    workflow[selected[0]]["inputs"]["image"],
                    config["plain_image"],
                )
                self.assertTrue(proxy.set_workflow_background_removal(workflow, meta, False))
                self.assertEqual(save["inputs"]["images"], config["plain_image"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
