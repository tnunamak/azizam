"""Tests for the uncensored image workflows (Krea 2 Turbo + SNOFS, Anima Turbo)."""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.manifests import (  # noqa: E402
    apply_backend_binding,
    load_workflow_manifest,
    manifest_identity,
    validate_inputs,
)

SNOFS_MANIFEST = ROOT / "manifests/workflows/krea2-turbo-snofs.yml"
SNOFS_WORKFLOW = ROOT / "workflows/images/krea2-turbo-snofs.json"
ANIMA_MANIFEST = ROOT / "manifests/workflows/anima-turbo.yml"
ANIMA_WORKFLOW = ROOT / "workflows/images/anima-turbo.json"
STOCK_WORKFLOW = ROOT / "workflows/images/krea2-turbo.json"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class TestKrea2SnofsWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(SNOFS_MANIFEST)
        self.workflow = _load(SNOFS_WORKFLOW)

    def test_identity(self) -> None:
        identity = manifest_identity(self.manifest)
        self.assertEqual(identity.id, "image.generate.krea2-turbo-snofs")
        self.assertEqual(self.manifest["intent"], "image.generate")
        self.assertTrue(identity.trusted)

    def test_lora_sits_between_unet_and_sampler(self) -> None:
        lora = self.workflow["11"]
        self.assertEqual(lora["class_type"], "LoraLoaderModelOnly")
        self.assertEqual(lora["inputs"]["lora_name"], "snofs_krea_v1_4.safetensors")
        self.assertEqual(lora["inputs"]["strength_model"], 1.0)
        self.assertEqual(lora["inputs"]["model"], ["1", 0])
        self.assertEqual(self.workflow["7"]["inputs"]["model"], ["11", 0])

    def test_matches_stock_krea_apart_from_lora(self) -> None:
        stock = _load(STOCK_WORKFLOW)
        stock.pop("_meta")
        snofs = copy.deepcopy(self.workflow)
        snofs.pop("_meta")
        snofs.pop("11")
        snofs["7"]["inputs"]["model"] = ["1", 0]
        snofs["9"]["inputs"]["filename_prefix"] = stock["9"]["inputs"]["filename_prefix"]
        self.assertEqual(snofs, stock)

    def test_manifest_requires_all_models(self) -> None:
        paths = {m["path"] for m in self.manifest["requires"]["models"]}
        self.assertIn("loras/snofs_krea_v1_4.safetensors", paths)
        self.assertIn("diffusion_models/krea2_turbo_int8_convrot.safetensors", paths)

    def test_inputs_bind_to_nodes(self) -> None:
        original = copy.deepcopy(self.workflow)
        result = validate_inputs(
            self.manifest,
            {"prompt": "A photograph of an adult", "width": 768, "height": 1024,
             "steps": 9, "seed": 7, "lora_strength": 0.8, "batch_size": 2},
        )
        self.assertTrue(result.ok, result.rejected)
        out = apply_backend_binding(self.manifest, "comfyui", self.workflow, result.accepted)
        self.assertEqual(self.workflow, original)
        self.assertEqual(out["4"]["inputs"]["text"], "A photograph of an adult")
        self.assertEqual((out["6"]["inputs"]["width"], out["6"]["inputs"]["height"]), (768, 1024))
        self.assertEqual(out["6"]["inputs"]["batch_size"], 2)
        self.assertEqual((out["7"]["inputs"]["steps"], out["7"]["inputs"]["seed"]), (9, 7))
        self.assertEqual(out["11"]["inputs"]["strength_model"], 0.8)

    def test_lora_strength_range(self) -> None:
        result = validate_inputs(self.manifest, {"prompt": "x", "lora_strength": 5})
        self.assertIn("lora_strength", result.rejected)

    def test_every_binding_target_exists(self) -> None:
        for entries in self.manifest["bindings"]["comfyui"]["map"].values():
            for e in entries:
                self.assertIn(e["input"], self.workflow[e["node"]]["inputs"])


class TestAnimaTurboWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_workflow_manifest(ANIMA_MANIFEST)
        self.workflow = _load(ANIMA_WORKFLOW)

    def test_identity_and_models(self) -> None:
        self.assertEqual(manifest_identity(self.manifest).id, "image.generate.anima-turbo")
        paths = {m["path"] for m in self.manifest["requires"]["models"]}
        self.assertEqual(
            paths,
            {"diffusion_models/anima-turbo-v1.1.safetensors",
             "text_encoders/qwen_3_06b_base.safetensors", "vae/qwen_image_vae.safetensors"},
        )
        self.assertEqual(self.manifest["requires"]["custom_nodes"], ["RMBG"])

    def test_workflow_loads_models_named_in_manifest(self) -> None:
        self.assertEqual(self.workflow["1"]["inputs"]["unet_name"], "anima-turbo-v1.1.safetensors")
        self.assertEqual(self.workflow["2"]["inputs"]["clip_name"], "qwen_3_06b_base.safetensors")
        self.assertEqual(self.workflow["2"]["inputs"]["type"], "stable_diffusion")
        self.assertEqual(self.workflow["7"]["inputs"]["cfg"], 1.0)

    def test_inputs_bind_to_nodes(self) -> None:
        result = validate_inputs(self.manifest, {"prompt": "1girl, adult", "seed": 3, "steps": 8})
        self.assertTrue(result.ok, result.rejected)
        out = apply_backend_binding(self.manifest, "comfyui", self.workflow, result.accepted)
        self.assertEqual(out["4"]["inputs"]["text"], "1girl, adult")
        self.assertEqual((out["7"]["inputs"]["seed"], out["7"]["inputs"]["steps"]), (3, 8))

    def test_every_binding_target_exists(self) -> None:
        for entries in self.manifest["bindings"]["comfyui"]["map"].values():
            for e in entries:
                self.assertIn(e["input"], self.workflow[e["node"]]["inputs"])


if __name__ == "__main__":
    unittest.main()
