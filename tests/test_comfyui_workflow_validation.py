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
from gateway.engine_protocol.node_validation import (  # noqa: E402
    validate_workflow_graph,
)
from gateway.discovery import WorkflowRegistry  # noqa: E402


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

        self.assertEqual(validate_workflow_graph(workflow), [])

    def test_workflow_must_be_mapping(self) -> None:
        self.assertEqual(
            validate_workflow_graph([]),
            ["workflow must be a mapping of node ids to node definitions"],
        )

    def test_workflow_must_have_non_meta_node(self) -> None:
        self.assertEqual(
            validate_workflow_graph({"_meta": {"title": "No nodes"}}),
            ["workflow must contain at least one node"],
        )

    def test_node_must_be_mapping(self) -> None:
        self.assertEqual(
            validate_workflow_graph({"3": "KSampler"}),
            ["node '3' must be a mapping"],
        )

    def test_node_requires_class_type_and_inputs_mapping(self) -> None:
        self.assertEqual(
            validate_workflow_graph({"3": {"class_type": "", "inputs": []}}),
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
                self.assertIsNone(proxy.load_graph_workflow("bad"))

    def test_proxy_loader_resolves_imported_package_workflow_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            package_dir = Path(tmp)
            workflows_dir = package_dir / "workflows"
            workflows_dir.mkdir()
            (workflows_dir / "demo.json").write_text(
                json.dumps(
                    {
                        "3": {
                            "class_type": "KSampler",
                            "inputs": {"seed": 123},
                        }
                    }
                ),
                encoding="utf-8",
            )
            (workflows_dir / "demo.ui.json").write_text(
                json.dumps({"nodes": [{"id": 3, "type": "KSampler"}], "links": []}),
                encoding="utf-8",
            )
            registry = WorkflowRegistry()
            registry.add_manifest(
                {
                    "schema_version": 1,
                    "id": "image.generate.demo",
                    "intent": "image.generate",
                    "inputs": {"prompt": {"type": "string"}},
                    "outputs": {"image": {"media_type": "image/png"}},
                    "bindings": {
                        "comfyui": {
                            "workflow": "package://workflows/demo.json",
                            "map": {
                                "prompt": [{"node": "3", "input": "text"}],
                            },
                        }
                    },
                },
                source_path=package_dir,
            )

            with patch.object(proxy, "GATEWAY_WORKFLOW_REGISTRY", registry), patch.object(
                proxy,
                "COMFY_WORKFLOWS_DIR",
                package_dir / "empty",
            ):
                workflow = proxy.load_graph_workflow("demo")
                manifest = proxy._find_image_manifest("image.generate", "demo")

            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertIn("3", workflow)
            self.assertEqual(manifest["id"], "image.generate.demo")

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
