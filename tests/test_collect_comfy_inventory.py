"""Tests for the safe ComfyUI inventory collector."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.workflow_requirements import validate_workflow_requirements  # noqa: E402
from scripts.local.collect_comfy_inventory import (  # noqa: E402
    collect_inventory,
    collect_model_artifacts,
    collect_node_classes,
)


class TestCollectComfyInventory(unittest.TestCase):
    def test_collects_safe_node_classes_from_object_info(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            object_info = Path(tmp) / "object_info.json"
            object_info.write_text(
                json.dumps(
                    {
                        "KSampler": {"input": {"required": {}}},
                        "ControlNetApplyAdvanced": {},
                        "../private": {},
                        "Bad Class": {},
                    }
                ),
                encoding="utf-8",
            )

            classes = collect_node_classes(object_info)

        self.assertEqual(classes, ["ControlNetApplyAdvanced", "KSampler"])

    def test_collects_model_artifacts_by_role_without_hashing_or_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "models" / "checkpoints" / "flux").mkdir(parents=True)
            (root / "models" / "checkpoints" / "flux" / "klein.safetensors").write_text(
                "not read",
                encoding="utf-8",
            )
            (root / "models" / "vae").mkdir(parents=True)
            (root / "models" / "vae" / "ae.safetensors").write_text("not read", encoding="utf-8")
            (root / "models" / "loras").mkdir(parents=True)
            (root / "models" / "loras" / "style.safetensors").write_text(
                "not read",
                encoding="utf-8",
            )
            (root / "models" / "controlnet").mkdir(parents=True)
            (root / "models" / "controlnet" / "canny.safetensors").write_text(
                "not read",
                encoding="utf-8",
            )
            (root / "models" / "upscale_models").mkdir(parents=True)
            (root / "models" / "upscale_models" / "4x.pth").write_text(
                "not read",
                encoding="utf-8",
            )
            (root / "models" / "unknown").mkdir(parents=True)
            (root / "models" / "unknown" / "secret.safetensors").write_text(
                "ignored",
                encoding="utf-8",
            )

            artifacts = collect_model_artifacts(root)

        by_path = {artifact["path"]: artifact for artifact in artifacts}
        self.assertEqual(
            {
                (artifact["role"], artifact["path"], artifact["name"])
                for artifact in artifacts
            },
            {
                ("checkpoint", "models/checkpoints/flux/klein.safetensors", "klein.safetensors"),
                ("vae", "models/vae/ae.safetensors", "ae.safetensors"),
                ("lora", "models/loras/style.safetensors", "style.safetensors"),
                ("controlnet", "models/controlnet/canny.safetensors", "canny.safetensors"),
                ("upscaler", "models/upscale_models/4x.pth", "4x.pth"),
            },
        )
        self.assertNotIn("sha256", by_path["models/checkpoints/flux/klein.safetensors"])
        self.assertNotIn(tmp, json.dumps(artifacts))

    def test_includes_sha256_only_from_supplied_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_path = root / "models" / "controlnet" / "canny.safetensors"
            model_path.parent.mkdir(parents=True)
            model_path.write_text("not read", encoding="utf-8")
            hashes = Path(tmp) / "hashes.json"
            hashes.write_text(
                json.dumps(
                    {
                        "models/controlnet/canny.safetensors": "A" * 64,
                        f"{tmp}/models/controlnet/private.safetensors": "B" * 64,
                    }
                ),
                encoding="utf-8",
            )

            inventory = collect_inventory(root, hash_inventory_path=hashes)

        self.assertEqual(
            inventory["model_artifacts"],
            [
                {
                    "role": "controlnet",
                    "path": "models/controlnet/canny.safetensors",
                    "name": "canny.safetensors",
                    "sha256": "a" * 64,
                }
            ],
        )
        self.assertNotIn(tmp, json.dumps(inventory))

    def test_inventory_feeds_workflow_requirement_validator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            object_info = root / "object_info.json"
            object_info.write_text(
                json.dumps({"ControlNetApplyAdvanced": {}, "KSampler": {}}),
                encoding="utf-8",
            )
            model_path = root / "models" / "controlnet" / "canny.safetensors"
            model_path.parent.mkdir(parents=True)
            model_path.write_text("not read", encoding="utf-8")

            inventory = collect_inventory(root, object_info_path=object_info)

        result = validate_workflow_requirements(
            {
                "requires": {
                    "custom_nodes": ["ControlNetApplyAdvanced"],
                    "models": [
                        {
                            "role": "controlnet",
                            "path": "models/controlnet/canny.safetensors",
                        }
                    ],
                },
            },
            inventory,
        )
        self.assertTrue(result.ok)

    def test_cli_outputs_redacted_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            model_path = root / "models" / "upscale_models" / "4x.pth"
            model_path.parent.mkdir(parents=True)
            model_path.write_text("not read", encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "local" / "collect_comfy_inventory.py"),
                    str(root),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

        output = json.loads(completed.stdout)
        self.assertEqual(output["source"]["root"], "<redacted>")
        self.assertEqual(output["model_artifacts"][0]["path"], "models/upscale_models/4x.pth")
        self.assertNotIn(tmp, completed.stdout)


if __name__ == "__main__":
    unittest.main()
