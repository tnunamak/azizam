"""Tests for pure ComfyUI API workflow graph validation."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from gateway.comfyui_workflow_validation import (  # noqa: E402
    validate_comfyui_workflow_graph,
)


class TestComfyUIWorkflowValidation(unittest.TestCase):
    def test_valid_workflow_accepts_meta(self) -> None:
        workflow = {
            "3": {
                "class_type": "KSampler",
                "inputs": {"seed": 123, "model": ["4", 0]},
            },
            "_meta": {"title": "Example workflow"},
            "__ui_workflow__": {"nodes": []},
        }

        self.assertEqual(validate_comfyui_workflow_graph(workflow), [])

    def test_workflow_must_be_mapping(self) -> None:
        self.assertEqual(
            validate_comfyui_workflow_graph([]),
            ["workflow must be a mapping of node ids to node definitions"],
        )

    def test_workflow_must_have_non_meta_node(self) -> None:
        self.assertEqual(
            validate_comfyui_workflow_graph({"_meta": {"title": "No nodes"}}),
            ["workflow must contain at least one node"],
        )

    def test_node_must_be_mapping(self) -> None:
        self.assertEqual(
            validate_comfyui_workflow_graph({"3": "KSampler"}),
            ["node '3' must be a mapping"],
        )

    def test_node_requires_class_type_and_inputs_mapping(self) -> None:
        self.assertEqual(
            validate_comfyui_workflow_graph({"3": {"class_type": "", "inputs": []}}),
            [
                "node '3'.class_type must be a non-empty string",
                "node '3'.inputs must be a mapping",
            ],
        )

    def test_proxy_loader_rejects_malformed_workflow_before_ui_attachment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflows_dir = Path(tmp)
            (workflows_dir / "bad.json").write_text(
                json.dumps({"3": {"class_type": "KSampler", "inputs": []}}),
                encoding="utf-8",
            )
            (workflows_dir / "bad.ui.json").write_text(
                json.dumps({"nodes": []}),
                encoding="utf-8",
            )

            with patch.object(proxy, "COMFY_WORKFLOWS_DIR", workflows_dir):
                self.assertIsNone(proxy.load_comfy_workflow("bad"))

    def test_audio_and_video_loaders_reject_malformed_workflows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workflow_path = Path(tmp) / "bad.json"
            workflow_path.write_text(
                json.dumps({"3": {"class_type": "KSampler", "inputs": []}}),
                encoding="utf-8",
            )
            workflow_cfg = {"file": str(workflow_path)}

            self.assertIsNone(proxy._load_audio_workflow_file(workflow_cfg))
            self.assertIsNone(proxy._load_video_workflow_file(workflow_cfg))


if __name__ == "__main__":
    unittest.main()
