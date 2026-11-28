"""Contract tests for inert examples shipped with the extension packages."""

import json
import runpy
from pathlib import Path

import yaml


BUNDLED = Path(__file__).resolve().parents[1] / "extensions" / "bundled"


def test_demo_code_adapter_answers_its_readiness_probe():
    adapter_path = BUNDLED / "demo-code-package/code/demo_adapter.py"
    adapter = runpy.run_path(str(adapter_path))["DemoAdapter"]()

    assert adapter.health() == {"status": "ok"}


def test_demo_code_translator_preserves_supplied_messages_and_supplies_defaults():
    translator_path = BUNDLED / "demo-code-package/code/demo_translator.py"
    translator = runpy.run_path(str(translator_path))
    to_canonical = translator["request_to_canonical"]
    from_canonical = translator["response_from_canonical"]

    assert to_canonical({}) == {"model": "default", "messages": []}
    request = {"model": "fixture-model", "messages": [{"role": "user", "content": "fixture"}]}
    assert to_canonical({**request, "ignored": True}) == request
    response = {"message": {"role": "assistant", "content": "fixture reply"}}
    assert from_canonical(response) == {"response": response}


def test_demo_image_binding_targets_real_inputs_and_agrees_with_the_seed_default():
    package = BUNDLED / "demo-image-package"
    workflow = yaml.safe_load((package / "objects/workflow.yml").read_text(encoding="utf-8"))
    binding = workflow["bindings"]["comfyui"]
    assert binding["workflow"] == "package://workflows/test-demo-image.json"
    template = json.loads((package / "workflows/test-demo-image.json").read_text(encoding="utf-8"))

    prompt_targets = binding["map"]["prompt"]
    seed_targets = binding["map"]["seed"]
    assert len(prompt_targets) == len(seed_targets) == 1
    prompt_target = prompt_targets[0]
    seed_target = seed_targets[0]
    prompt_node = template[prompt_target["node"]]
    seed_node = template[seed_target["node"]]
    assert prompt_node["class_type"] == "CLIPTextEncode"
    assert prompt_target["input"] == "text"
    assert isinstance(prompt_node["inputs"][prompt_target["input"]], str)
    assert seed_node["class_type"] == "KSampler"
    assert seed_target["input"] == "seed"
    template_seed = seed_node["inputs"][seed_target["input"]]
    assert type(template_seed) is int
    assert template_seed == workflow["inputs"]["seed"]["default"]
