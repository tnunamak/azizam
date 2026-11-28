"""Tests for the local ControlNet readiness audit helper."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.local.audit_controlnet_readiness import (  # noqa: E402
    CONTROLNET_MANIFEST_CONTROLS,
    audit_controlnet_readiness,
    controlnet_classes_from_object_info,
    inventory_controlnet_custom_nodes,
    controlnet_nodes_from_workflow,
)


class TestControlNetReadiness(unittest.TestCase):
    def test_missing_models_do_not_enable_manifest_controls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_dir = root / "models" / "controlnet"
            model_dir.mkdir(parents=True)
            (model_dir / "put_controlnets_and_t2i_here").write_text("")

            report = audit_controlnet_readiness(
                root,
                object_info={
                    "ControlNetLoader": {},
                    "ControlNetApplyAdvanced": {},
                },
                workflow={
                    "44": {
                        "class_type": "ControlNetApplyAdvanced",
                        "inputs": {"strength": 1.0, "control_net": ["43", 0]},
                    }
                },
            )

        self.assertFalse(report.can_enable)
        self.assertEqual(report.valid_manifest_controls, ())
        self.assertIn("controlnet_model_files", report.missing_prerequisites)
        self.assertEqual(report.model_files, ())

    def test_all_prerequisites_make_controls_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_dir = root / "models" / "controlnet"
            model_dir.mkdir(parents=True)
            (model_dir / "flux-canny.safetensors").write_text("")

            report = audit_controlnet_readiness(
                root,
                object_info={
                    "ControlNetLoader": {},
                    "ControlNetApplyAdvanced": {},
                },
                workflow={
                    "43": {
                        "class_type": "ControlNetLoader",
                        "inputs": {"control_net_name": "flux-canny.safetensors"},
                    },
                    "44": {
                        "class_type": "ControlNetApplyAdvanced",
                        "inputs": {"strength": 1.0, "control_net": ["43", 0]},
                    },
                },
            )

        self.assertTrue(report.can_enable)
        self.assertEqual(report.valid_manifest_controls, CONTROLNET_MANIFEST_CONTROLS)
        self.assertEqual(report.missing_prerequisites, ())
        self.assertEqual(report.model_files, ("models/controlnet/flux-canny.safetensors",))

    def test_object_info_class_detection_is_case_and_separator_tolerant(self) -> None:
        classes = controlnet_classes_from_object_info(
            {
                "Load_ControlNet_Model": {},
                "ApplyControlNet": {},
                "KSampler": {},
            }
        )

        self.assertEqual(classes, ("ApplyControlNet", "Load_ControlNet_Model"))

    def test_custom_node_inventory_uses_safe_names_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            custom_nodes = root / "custom_nodes"
            custom_nodes.mkdir()
            (custom_nodes / "ComfyUI-ControlNet-Example").mkdir()
            (custom_nodes / "OtherPack").mkdir()

            found = inventory_controlnet_custom_nodes(root)

        self.assertEqual(found, ("ComfyUI-ControlNet-Example/",))

    def test_workflow_detection_uses_class_type_and_input_names_only(self) -> None:
        nodes = controlnet_nodes_from_workflow(
            {
                "1": {
                    "class_type": "PlainNode",
                    "inputs": {"controlnet_strength": 0.8},
                },
                "2": {"class_type": "ControlNetLoader", "inputs": {}},
                "3": {"class_type": "KSampler", "inputs": {"prompt": "private"}},
            }
        )

        self.assertEqual(nodes, ("1:PlainNode", "2:ControlNetLoader"))


if __name__ == "__main__":
    unittest.main()
