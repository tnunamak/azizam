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


def _read_manifest(workflow):
    path = ROOT / "manifests" / "workflows" / f"{workflow}.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("workflow", KLEIN_WORKFLOWS)
def test_klein_declares_graph_requirements(workflow):
    binding = _read_manifest(workflow)["bindings"]["comfyui"]
    graph = json.loads((ROOT / binding["workflow"]).read_text(encoding="utf-8"))
    requires = binding.get("requires", {})
    graph_models = {
        Path(node["inputs"][LOADER_INPUTS[node["class_type"]]]).name
        for node in graph.values()
        if isinstance(node, dict) and node.get("class_type") in LOADER_INPUTS
    }
    declared_models = {Path(model["path"]).name for model in requires.get("models", [])}
    assert graph_models <= declared_models, graph_models - declared_models
    if any(isinstance(node, dict) and node.get("class_type") == "RMBG" for node in graph.values()):
        assert "RMBG" in requires.get("custom_nodes", [])


@pytest.mark.parametrize("workflow", KLEIN_WORKFLOWS)
def test_klein_help_says_what_options_change(workflow):
    options = _read_manifest(workflow)["inputs"]
    option_names = ["sampler", "text_encoder"]
    if "lora_strength" in options:
        option_names.append("lora_strength")
    for option in option_names:
        assert re.search(r"\b(?:changes|affects)\b", options[option]["help"]), option
