"""Tests for ``scripts/local/inventory_workflow_controls``.

These exercise the pure functions only — no subprocess, no disk reads
beyond the synthetic fixture defined in this file.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.local.inventory_workflow_controls import (  # noqa: E402
    CANDIDATE_LOGICAL_NAMES,
    ControlEntry,
    inventory_workflow,
    redact_value,
    summarize,
)


SECRET_PROMPT = "totally private prompt — do not log me, ever, under any circumstance"
SECRET_FILENAME = "private-reference-image-2026-05-14.png"
SECRET_BASE64 = "QUJDRA" * 20  # well over the 40-char base64 threshold


# Synthetic API-format workflow exercising the shapes the redactor must handle.
FIXTURE_WORKFLOW = {
    "_meta": {"name": "fixture", "description": "do not surface"},
    "__ui_workflow__": {"junk": True},
    # Text-encode node — input "text" carries the user's prompt.
    "6": {
        "class_type": "CLIPTextEncode",
        "inputs": {
            "text": SECRET_PROMPT,
            "clip": ["30", 1],  # graph edge — must be skipped
        },
    },
    # Latent size node — width/height/batch_size are obvious bindables.
    "27": {
        "class_type": "EmptySD3LatentImage",
        "inputs": {
            "width": 1024,
            "height": 768,
            "batch_size": 1,
        },
    },
    # KSampler — steps/seed/cfg/denoise/guidance-ish controls.
    "31": {
        "class_type": "KSampler",
        "inputs": {
            "model": ["30", 0],
            "seed": 42,
            "steps": 25,
            "cfg": 1.0,
            "denoise": 0.85,
            "sampler_name": "euler",
            "scheduler": "simple",
        },
    },
    # Flux guidance node — exposes `guidance`.
    "35": {
        "class_type": "FluxGuidance",
        "inputs": {
            "conditioning": ["6", 0],
            "guidance": 3.5,
        },
    },
    # LoadImage — exposes `image` as a filename string we must NOT print.
    "10": {
        "class_type": "LoadImage",
        "inputs": {
            "image": SECRET_FILENAME,
            "upload": True,
        },
    },
    # Unknown / exotic node — input names not in the allow-list.
    "99": {
        "class_type": "MysteryCustomNode",
        "inputs": {
            "wobble": 0.5,
            "secret_blob": SECRET_BASE64,
            "stash_url": "https://example.invalid/keep-this-out",
            "path_hint": "/var/lib/comfy/private/x.safetensors",
            "tags": ["a", "b", "c"],
            "config": {"k": "v"},
            "nothing": None,
        },
    },
}


class TestRedactValue(unittest.TestCase):
    def test_scalars_emit_type_only(self) -> None:
        self.assertEqual(redact_value(None), {"type": "null"})
        self.assertEqual(redact_value(True), {"type": "boolean"})
        self.assertEqual(redact_value(False), {"type": "boolean"})
        self.assertEqual(redact_value(7), {"type": "integer"})
        self.assertEqual(redact_value(7.5), {"type": "number"})

    def test_string_redaction_drops_raw_value(self) -> None:
        r = redact_value(SECRET_PROMPT)
        self.assertEqual(r["type"], "string")
        self.assertIn(r["category"], ("text", "long"))
        # The raw string never appears anywhere in the redacted dict.
        self.assertNotIn(SECRET_PROMPT, json.dumps(r))

    def test_url_filename_path_base64_categories(self) -> None:
        self.assertEqual(redact_value("https://x.invalid")["category"], "url")
        self.assertEqual(redact_value("foo.png")["category"], "filename")
        self.assertEqual(redact_value("/etc/passwd")["category"], "path")
        self.assertEqual(redact_value(SECRET_BASE64)["category"], "base64")

    def test_short_enum_strings_are_not_misclassified_as_base64(self) -> None:
        # ``euler`` / ``dpmpp_2m`` would be a false positive if base64
        # detection were not length-gated.
        self.assertEqual(redact_value("euler")["category"], "text")
        self.assertEqual(redact_value("dpmpp_2m")["category"], "text")


class TestInventoryWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = inventory_workflow(FIXTURE_WORKFLOW)
        self.by_key = {(e.node_id, e.input_name): e for e in self.entries}

    def test_meta_and_double_underscore_keys_are_skipped(self) -> None:
        for e in self.entries:
            self.assertFalse(e.node_id == "_meta")
            self.assertFalse(e.node_id.startswith("__"))

    def test_graph_edges_are_not_reported_as_controls(self) -> None:
        # ``clip`` on node 6 and ``model`` on node 31 are edges, not controls.
        self.assertNotIn(("6", "clip"), self.by_key)
        self.assertNotIn(("31", "model"), self.by_key)
        self.assertNotIn(("35", "conditioning"), self.by_key)

    def test_candidate_logical_names_match_expected(self) -> None:
        expected = {
            ("6", "text"): "text",
            ("27", "width"): "width",
            ("27", "height"): "height",
            ("31", "seed"): "seed",
            ("31", "steps"): "steps",
            ("31", "cfg"): "cfg",
            ("31", "denoise"): "denoise",
            ("35", "guidance"): "guidance",
            ("10", "image"): "image",
        }
        for key, name in expected.items():
            self.assertIn(key, self.by_key, key)
            self.assertEqual(
                self.by_key[key].candidate_logical_name,
                name,
                f"{key} should map to {name}",
            )

    def test_unknown_inputs_have_no_candidate(self) -> None:
        for key in [
            ("99", "wobble"),
            ("99", "secret_blob"),
            ("99", "stash_url"),
            ("99", "path_hint"),
            ("99", "tags"),
            ("99", "config"),
            ("99", "nothing"),
            ("31", "sampler_name"),
            ("31", "scheduler"),
            ("27", "batch_size"),
            ("10", "upload"),
        ]:
            self.assertIn(key, self.by_key, key)
            self.assertIsNone(
                self.by_key[key].candidate_logical_name,
                f"{key} must not be auto-mapped",
            )

    def test_no_raw_string_values_appear_in_serialized_output(self) -> None:
        # Serialize the inventory as a manifest author / agent would and
        # verify no sensitive raw content leaks.
        payload = json.dumps([e.to_dict() for e in self.entries])
        self.assertNotIn(SECRET_PROMPT, payload)
        self.assertNotIn(SECRET_FILENAME, payload)
        self.assertNotIn(SECRET_BASE64, payload)
        self.assertNotIn("example.invalid", payload)
        self.assertNotIn("/var/lib/comfy/private", payload)

    def test_numeric_values_are_not_leaked(self) -> None:
        # The actual values 42 / 1024 / 768 / 3.5 / 0.85 are private to the
        # workflow; the inventory must report types only.
        payload = json.dumps([e.to_dict() for e in self.entries])
        # The number 27 is a node id and appears legitimately. We only
        # check inputs that are unique to the redacted-value side.
        for sentinel in ("0.85", "3.5"):
            # These floats appear nowhere except as input values.
            self.assertNotIn(sentinel, payload, sentinel)

    def test_sensitive_string_shapes_carry_review_notes(self) -> None:
        for key in [("10", "image"), ("99", "stash_url"), ("99", "secret_blob")]:
            entry = self.by_key[key]
            self.assertTrue(
                entry.notes,
                f"{key} should have at least one review note",
            )

    def test_inventory_is_pure_no_disk_access(self) -> None:
        # Calling twice produces stable output (same ordering and content).
        again = inventory_workflow(FIXTURE_WORKFLOW)
        self.assertEqual(
            [e.to_dict() for e in self.entries],
            [e.to_dict() for e in again],
        )


class TestSummarize(unittest.TestCase):
    def test_summary_counts_and_candidate_grouping(self) -> None:
        entries = inventory_workflow(FIXTURE_WORKFLOW)
        s = summarize(entries)
        self.assertEqual(s["total_controls"], len(entries))
        self.assertGreater(s["controls_with_candidate"], 0)
        self.assertEqual(list(s["allowed_logical_names"]), list(CANDIDATE_LOGICAL_NAMES))
        # All candidate keys are members of the conservative allow-list.
        for name in s["candidates"]:
            self.assertIn(name, CANDIDATE_LOGICAL_NAMES)
        # width/height/steps/guidance/seed/denoise/cfg/text/image should all
        # be present in the candidate roll-up.
        for expected in (
            "width",
            "height",
            "steps",
            "guidance",
            "seed",
            "denoise",
            "cfg",
            "text",
            "image",
        ):
            self.assertIn(expected, s["candidates"], expected)


class TestInputValidation(unittest.TestCase):
    def test_non_dict_workflow_raises(self) -> None:
        with self.assertRaises(TypeError):
            inventory_workflow(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_unexpected_node_shapes_are_tolerated(self) -> None:
        # A node value that is not a dict (e.g. a stray string) should be
        # skipped silently rather than crashing the inventory.
        entries = inventory_workflow({"1": "garbage", "2": None, "3": 7})
        self.assertEqual(entries, [])

    def test_control_entry_is_jsonable(self) -> None:
        entry = ControlEntry(
            node_id="1",
            class_type="X",
            input_name="y",
            value={"type": "integer"},
            candidate_logical_name=None,
            notes=("hi",),
        )
        # Round-trips through json without exception.
        json.dumps(entry.to_dict())


if __name__ == "__main__":
    unittest.main(verbosity=2)
