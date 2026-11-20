"""MiniMax H3 two-pass image-to-video (video.generate.minimax-h3-i2v-hires): manifest, graph and binding.

Pass 1 renders at half the requested size, a learned x2 latent upscale follows, and pass 2 refines at the requested
size. The half size is derived inside the graph (core math nodes), so one width and one height input drive every node.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.manifests import (  # noqa: E402
    BUNDLED_PROVENANCE,
    apply_backend_binding,
    load_workflow_manifest,
    manifest_identity,
    validate_inputs,
)

MANIFEST = ROOT / "manifests/workflows/video-minimax-h3-i2v-hires.yml"
GRAPH = ROOT / "workflows/videos/video-minimax-h3-i2v-hires.json"
NOTES = ROOT / "docs/workflows/video.generate.minimax-h3-i2v-hires.md"
POLICY = ROOT / "registry/workflow_policy.yml"
WORKFLOW_ID = "video.generate.minimax-h3-i2v-hires"


def _value(graph: dict[str, Any], ref: Any) -> Any:
    """The value a node input holds once ComfyUI has run the constant and math nodes it links to."""
    if not isinstance(ref, list):
        return ref
    node = graph[ref[0]]
    kind, inputs = node["class_type"], node["inputs"]
    if kind in ("INTConstant", "PrimitiveInt", "PrimitiveStringMultiline"):
        return inputs["value"]
    if kind == "ComfyMathExpression":
        assert inputs["expression"] == "a // 2", inputs["expression"]
        assert ref[1] == 1, "the integer output is slot 1"
        return _value(graph, inputs["values.a"]) // 2
    raise AssertionError(f"unexpected {kind}")


def _bound(**request: Any) -> dict[str, Any]:
    manifest = load_workflow_manifest(MANIFEST)
    result = validate_inputs(manifest, request)
    assert result.ok, result.rejected
    return apply_backend_binding(manifest, "comfyui", json.loads(GRAPH.read_text(encoding="utf-8")), result.accepted)


def test_identity_and_catalog_facts() -> None:
    manifest = load_workflow_manifest(MANIFEST)
    identity = manifest_identity(manifest, BUNDLED_PROVENANCE)
    assert identity.id == WORKFLOW_ID
    assert identity.trusted
    assert manifest["intent"] == "video.generate"
    assert "US, EU, UK" in manifest["license"]
    assert manifest["video"]["size"] == {"default": "1216x704", "max": manifest["video"]["size"]["max"], "multiple": 64}
    assert manifest["video"]["grid"] == {"fps": 24, "step": 17, "offset": 5}
    assert manifest["video"]["requires"] == ["image"]
    # Users never type a model id, and the sigma ladders are fixed in the graph.
    assert set(manifest["inputs"]) == {"prompt", "seed", "image", "width", "height", "length", "frame_rate"}


def test_policy_row_matches_the_single_pass_workflow() -> None:
    rows = yaml.safe_load(POLICY.read_text(encoding="utf-8"))["workflows"]
    assert rows[WORKFLOW_ID] == rows["video.generate.minimax-h3-i2v"]


def test_purpose_is_unique_among_workflows() -> None:
    purposes = [yaml.safe_load(path.read_text(encoding="utf-8")).get("purpose")
                for path in (ROOT / "manifests/workflows").glob("*.yml")]
    mine = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["purpose"]
    assert purposes.count(mine) == 1


def test_manifest_requires_every_model_the_graph_loads() -> None:
    manifest = load_workflow_manifest(MANIFEST)
    graph = json.loads(GRAPH.read_text(encoding="utf-8"))
    required = {m["path"].split("/", 1)[1] for m in manifest["bindings"]["comfyui"]["requires"]["models"]}
    loaded = {node["inputs"][key] for node in graph.values()
              for key in ("unet_name", "clip_name", "vae_name", "model_name") if key in node["inputs"]}
    assert loaded == required


def test_graph_has_two_passes_with_their_own_conditioning_and_no_lora() -> None:
    graph = json.loads(GRAPH.read_text(encoding="utf-8"))
    kinds = [node["class_type"] for node in graph.values()]
    assert kinds.count("MiniMaxH3ImageToVideo") == 2
    assert kinds.count("SamplerCustomAdvanced") == 2
    assert "LoraLoaderModelOnly" not in kinds
    assert graph["unet"]["class_type"] == "UnetLoaderGGUF"
    assert graph["shift"]["inputs"]["shift_video"] == 12.0 and graph["shift"]["inputs"]["shift_audio"] == 3.0
    assert graph["sig1"]["inputs"]["sigmas"] == "0.9035, 0.8000, 0.6316, 0.3158, 0.0000"
    assert graph["sig2"]["inputs"]["sigmas"] == "0.9035, 0.6316, 0.3158, 0.0000"
    # Pass 2 samples the upscaled latent under conditioning built at the target size, never pass 1's.
    assert graph["g1"]["inputs"]["conditioning"] == ["cond", 0]
    assert graph["g2"]["inputs"]["conditioning"] == ["cond2", 0]
    assert graph["p1"]["inputs"]["latent_image"] == ["cond", 1]
    assert graph["up"]["inputs"]["samples"] == ["p1", 0]
    assert graph["p2"]["inputs"]["latent_image"] == ["up", 0]


def test_a_1216x704_request_runs_pass_1_at_half_size_and_pass_2_at_full_size() -> None:
    graph = _bound(prompt="a kite rises", image="uploaded.png", width=1216, height=704, length=39, seed=7, frame_rate=24)
    pass1, pass2, up = graph["cond"]["inputs"], graph["cond2"]["inputs"], graph["up"]["inputs"]
    assert (_value(graph, pass1["width"]), _value(graph, pass1["height"])) == (608, 352)
    assert (_value(graph, pass2["width"]), _value(graph, pass2["height"])) == (1216, 704)
    assert (_value(graph, up["width"]), _value(graph, up["height"])) == (1216, 704)
    fit = graph["fit"]["inputs"]
    assert (_value(graph, fit["width"]), _value(graph, fit["height"])) == (1216, 704)
    assert _value(graph, pass1["length"]) == _value(graph, pass2["length"]) == 39
    assert graph["noise"]["inputs"]["noise_seed"] == 7
    assert graph["ref"]["inputs"]["image"] == "uploaded.png"
    assert graph["vid"]["inputs"]["fps"] == 24


def test_one_prompt_reaches_both_conditioning_nodes() -> None:
    graph = _bound(prompt="a kite rises\n\nAudio: wind", image="uploaded.png", width=1216, height=704, length=39)
    assert _value(graph, graph["cond"]["inputs"]["prompt"]) == "a kite rises\n\nAudio: wind"
    assert _value(graph, graph["cond2"]["inputs"]["prompt"]) == "a kite rises\n\nAudio: wind"


def test_half_size_follows_the_request_and_stays_on_the_canvas_step() -> None:
    graph = _bound(prompt="x", image="uploaded.png", width=640, height=384, length=22)
    assert (_value(graph, graph["cond"]["inputs"]["width"]), _value(graph, graph["cond"]["inputs"]["height"])) == (320, 192)
    assert _value(graph, graph["cond"]["inputs"]["width"]) % 32 == 0


def test_a_size_that_is_not_a_multiple_of_64_is_refused() -> None:
    manifest = load_workflow_manifest(MANIFEST)
    result = validate_inputs(manifest, {"prompt": "x", "image": "uploaded.png", "width": 1152, "height": 704})
    assert "width" not in result.rejected
    result = validate_inputs(manifest, {"prompt": "x", "image": "uploaded.png", "width": 1200, "height": 704})
    assert "width" in result.rejected


def test_binding_does_not_mutate_the_template() -> None:
    original = json.loads(GRAPH.read_text(encoding="utf-8"))
    template = copy.deepcopy(original)
    manifest = load_workflow_manifest(MANIFEST)
    result = validate_inputs(manifest, {"prompt": "x", "image": "uploaded.png", "width": 1216, "height": 704, "length": 39})
    apply_backend_binding(manifest, "comfyui", template, result.accepted)
    assert template == original


def test_notes_page_exists_and_explains_the_second_conditioning_node() -> None:
    text = NOTES.read_text(encoding="utf-8")
    assert "second conditioning node" in text
