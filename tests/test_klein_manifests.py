"""Keep the Klein workflow manifests aligned with their checked-in graphs."""

import json
from pathlib import Path
import re

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
KLEIN_WORKFLOWS = ("flux2-klein", "flux2-klein-snofs")
LOADER_INPUTS = {
    "UnetLoaderGGUF": "unet_name",
    "CLIPLoader": "clip_name",
    "VAELoader": "vae_name",
    "LoraLoaderModelOnly": "lora_name",
}
LOADER_REQUIREMENTS = {
    "UnetLoaderGGUF": ("unet", "diffusion_model"),
    "CLIPLoader": ("text_encoders", "text_encoder"),
    "VAELoader": ("vae", "vae"),
    "LoraLoaderModelOnly": ("loras", "lora"),
}
KLEIN_CUSTOM_NODES = {"RMBG"}


def _read_manifest(workflow):
    path = ROOT / "manifests" / "workflows" / f"{workflow}.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("workflow", KLEIN_WORKFLOWS)
def test_klein_declares_graph_requirements(workflow):
    binding = _read_manifest(workflow)["bindings"]["comfyui"]
    graph = json.loads((ROOT / binding["workflow"]).read_text(encoding="utf-8"))
    requires = binding.get("requires", {})
    graph_models = {}
    for node in graph.values():
        if not isinstance(node, dict) or node.get("class_type") not in LOADER_INPUTS:
            continue
        loader = node["class_type"]
        folder, role = LOADER_REQUIREMENTS[loader]
        filename = Path(node["inputs"][LOADER_INPUTS[loader]]).name
        graph_models[(folder, filename)] = role

    models = requires.get("models", [])
    declared_models = {
        (Path(model["path"]).parent.as_posix(), Path(model["path"]).name): model["role"]
        for model in models
    }
    assert len(declared_models) == len(models), "duplicate declared model path"
    assert declared_models.keys() == graph_models.keys()
    assert declared_models == graph_models

    graph_classes = {node.get("class_type") for node in graph.values() if isinstance(node, dict)}
    assert requires.get("custom_nodes", []) == sorted(graph_classes & KLEIN_CUSTOM_NODES)


@pytest.mark.parametrize("workflow", KLEIN_WORKFLOWS)
def test_klein_help_says_what_options_change(workflow):
    options = _read_manifest(workflow)["inputs"]
    option_names = ["sampler", "text_encoder"]
    if "lora_strength" in options:
        option_names.append("lora_strength")
    for option in option_names:
        assert re.search(r"\b(?:changes|affects)\b", options[option]["help"]), option
