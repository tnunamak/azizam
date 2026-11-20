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
from tests.support.config_snapshot import derived_patch
from gateway.engine_protocol.node_validation import (  # noqa: E402
    validate_workflow_graph,
)
from gateway.discovery import WorkflowRegistry  # noqa: E402
from tests.support.workflow_docs import STANDARD_FACTS  # noqa: E402


class TestComfyUIWorkflowValidation(unittest.TestCase):
    def test_valid_workflow_accepts_meta(self) -> None:
        workflow = {
            "3": {
                "class_type": "KSampler",
                "inputs": {"seed": 123, "model": ["4", 0]},
            },
            "4": {"class_type": "Source", "inputs": {}},
            "_meta": {"title": "Example workflow"},
            "__ui_workflow__": {"nodes": []},
        }

        self.assertEqual(validate_workflow_graph(workflow), [])

    @staticmethod
    def node(**inputs):
        return {"class_type": "Synthetic", "inputs": inputs}

    def test_link_to_missing_node_is_rejected(self) -> None:
        self.assertEqual(validate_workflow_graph({"A": self.node(source=["B", 0])}),
                         ["node 'A'.inputs.source links to missing node 'B'"])

    def test_metadata_is_not_a_link_target(self) -> None:
        for target in ("_meta", "__ui_workflow__"):
            with self.subTest(target=target):
                self.assertEqual(validate_workflow_graph({
                    "A": self.node(source=[target, 0]), target: self.node(),
                }), [f"node 'A'.inputs.source links to missing node {target!r}"])

    def test_two_node_cycle_is_rejected(self) -> None:
        self.assertEqual(validate_workflow_graph({
            "A": self.node(source=["B", 0]), "B": self.node(source=["A", 0]),
        }), ["workflow has a cycle: 'A' -> 'B' -> 'A'"])

    def test_self_link_is_rejected(self) -> None:
        self.assertEqual(validate_workflow_graph({"A": self.node(source=["A", 0])}),
                         ["workflow has a cycle: 'A' -> 'A'"])

    def test_diamond_shared_source_is_not_a_cycle(self) -> None:
        self.assertEqual(validate_workflow_graph({
            "sink": self.node(left=["left", 0], right=["right", 999999]),
            "left": self.node(source=["source", 0]),
            "right": self.node(source=["source", 0]), "source": self.node(),
        }), [])

    def test_string_and_zero_constant_shape_is_a_link_to_missing_node(self) -> None:
        self.assertEqual(validate_workflow_graph({"A": self.node(choice=["euler", 0])}),
                         ["node 'A'.inputs.choice links to missing node 'euler'"])

    def test_non_link_constants_are_unchecked(self) -> None:
        for value in (["absent", True], ["absent", False], ["absent", -1],
                      ["absent", 1.0], ["absent", "0"], ["absent", None],
                      ["absent"], ["absent", 0, 1], [1, 0], ("absent", 0)):
            with self.subTest(value=value):
                self.assertEqual(validate_workflow_graph({"A": self.node(value=value)}), [])

    def test_shape_errors_precede_link_errors_and_bad_nodes_are_not_walked(self) -> None:
        self.assertEqual(validate_workflow_graph({
            "A": self.node(source=["absent", 0]), "bad": {"inputs": {"x": ["missing", 0]}},
            "other": "not a node",
        }), ["node 'bad'.class_type must be a non-empty string",
             "node 'other' must be a mapping",
             "node 'A'.inputs.source links to missing node 'absent'"])

    def test_disconnected_cycles_are_reported_once_each(self) -> None:
        self.assertEqual(validate_workflow_graph({
            "A": self.node(first=["A", 0], duplicate=["A", 1]),
            "B": self.node(source=["C", 0]), "C": self.node(source=["B", 0]),
        }), ["workflow has a cycle: 'A' -> 'A'", "workflow has a cycle: 'B' -> 'C' -> 'B'"])

    def test_deep_graph_uses_no_recursive_walk(self) -> None:
        graph = {str(i): self.node(source=[str(i + 1), 0]) for i in range(2000)}
        graph["2000"] = self.node()
        self.assertEqual(validate_workflow_graph(graph), [])

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

            with derived_patch("GRAPH_WORKFLOWS_DIR", workflows_dir):
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
                    "schema_version": 2,
                    "id": "image.generate.demo",
                    "intent": "image.generate",
                    "capability": "image",
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
                facts=STANDARD_FACTS,
            )

            with patch.object(proxy, "GATEWAY_WORKFLOW_REGISTRY", registry), derived_patch("GRAPH_WORKFLOWS_DIR", package_dir / "empty",
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


    def _video_cfg(self, tmp: str, graph: dict, **roles) -> dict:
        path = Path(tmp) / "video.json"
        path.write_text(json.dumps(graph), encoding="utf-8")
        return {"file": str(path), "node_roles": roles}

    def test_the_video_loader_refuses_a_prompt_role_whose_node_lacks_the_prompt_field(self) -> None:
        # A graph edited under a running gateway kept node 9 but moved the prompt to a string node: writing
        # `prompt` into node 9 would be ignored by the engine and the video made from the placeholder text.
        graph = {"9": self.node(value="placeholder"), "8": self.node(width=64)}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(proxy._load_video_workflow_file(
                self._video_cfg(tmp, graph, prompt_node="9", prompt_field="prompt", size_node="8")))
            self.assertIsNone(proxy._load_video_workflow_file(
                self._video_cfg(tmp, graph, prompt_node="9", size_node="8")))  # the default field is `text`
            loaded = proxy._load_video_workflow_file(
                self._video_cfg(tmp, graph, prompt_node="9", prompt_field="value", size_node="8"))
            self.assertIsNotNone(loaded)

    def test_the_video_loader_refuses_a_negative_prompt_role_whose_node_has_no_text(self) -> None:
        graph = {"9": self.node(text="a"), "7": self.node(prompt="b")}
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(proxy._load_video_workflow_file(
                self._video_cfg(tmp, graph, prompt_node="9", negative_prompt_node="7")))
            self.assertIsNotNone(proxy._load_video_workflow_file(
                self._video_cfg(tmp, {"9": self.node(text="a"), "7": self.node(text="b")},
                                prompt_node="9", negative_prompt_node="7")))

    def test_a_manifest_mapping_to_an_input_the_node_lacks_is_refused_not_added(self) -> None:
        from gateway.manifests import ManifestError, bind_backend_request
        manifest = {"id": "video.generate.demo", "inputs": {"prompt": {"type": "string"}},
                    "bindings": {"comfyui": {"map": {"prompt": [{"node": "9", "input": "prompt"}]}}}}
        with self.assertRaises(ManifestError) as raised:
            bind_backend_request(manifest, "comfyui", {"9": self.node(value="placeholder")}, {"prompt": "a lighthouse"})
        self.assertEqual(raised.exception.code, "target_missing")
        bound = bind_backend_request(manifest, "comfyui", {"9": self.node(prompt="placeholder")}, {"prompt": "a lighthouse"})
        self.assertEqual(bound.request["9"]["inputs"]["prompt"], "a lighthouse")


if __name__ == "__main__":
    unittest.main()
