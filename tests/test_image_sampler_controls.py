"""Every image workflow with a sampler lets a caller set steps, sampler and scheduler.

The inputs bind to the sampler inputs of each graph and default to the graph's own values, so a caller
who omits them gets the graph unchanged.
"""
import copy
import json
from pathlib import Path

import pytest
import yaml

from gateway.manifests import bind_backend_request, validate_inputs

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ROOT / "manifests" / "workflows"

# Where each control lives: (node class, input). A stock sampler holds all three on one node. A custom
# sampler spreads them over a sampler-select node and a scheduler node.
STOCK = {"steps": "steps", "sampler": "sampler_name", "scheduler": "scheduler"}
CUSTOM = {"steps": ("BasicScheduler", "steps"), "sampler": ("KSamplerSelect", "sampler_name"),
          "scheduler": ("BasicScheduler", "scheduler")}
STOCK_NODES = ("KSampler", "LanPaint_KSampler")

# Image workflows that have no caller-controllable sampler, and why.
LEFT_OUT = {
    "image.generate.moss-image": "the few-step adapter node supplies sampler and sigmas",
    "image.edit.seedvr2-upscale": "a one-step upscaler, not a generation sampler",
    "image.edit.image-identity": "a composed gateway route with no graph map; steps is set by its own route code",
}


def load(path):
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    binding = manifest.get("bindings", {}).get("comfyui", {})
    graph = json.loads((ROOT / binding["workflow"]).read_text(encoding="utf-8")) if binding.get("workflow") else None
    return manifest, binding, graph


def image_cases():
    cases = []
    for path in sorted(MANIFESTS.glob("*.yml")):
        manifest, binding, graph = load(path)
        if manifest["id"].split(".")[:2] not in (["image", "generate"], ["image", "edit"]) or graph is None:
            continue
        cases.append(pytest.param(manifest, binding, graph, id=path.stem))
    return cases


def controls(graph, manifest_id=None):
    """Map each control to (node id, node class, input) in this graph, or None when it has no sampler."""
    if manifest_id in LEFT_OUT:
        return None
    classes = {node_id: node.get("class_type") for node_id, node in graph.items() if isinstance(node, dict)}
    stock = [i for i, c in classes.items() if c in STOCK_NODES]
    if stock:
        return {name: (stock[0], classes[stock[0]], field) for name, field in STOCK.items()}
    found = {}
    for name, (klass, field) in CUSTOM.items():
        nodes = [i for i, c in classes.items() if c == klass]
        if not nodes:
            return None
        found[name] = (nodes[0], klass, field)
    return found


def test_the_cases_cover_the_image_workflows():
    assert len(image_cases()) >= 12


@pytest.mark.parametrize("manifest,binding,graph", image_cases())
def test_every_image_manifest_with_a_sampler_exposes_the_three_controls(manifest, binding, graph):
    found = controls(graph, manifest["id"])
    if manifest["id"] in LEFT_OUT:
        assert "sampler" not in manifest["inputs"] and "scheduler" not in manifest["inputs"]
        return
    assert found is not None, manifest["id"]
    for name, (node_id, klass, field) in found.items():
        spec = manifest["inputs"][name]
        mapped = binding["map"][name]
        assert {"node": str(node_id), "input": field} in [{"node": str(m["node"]), "input": m["input"]} for m in mapped]
        value = graph[node_id]["inputs"][field]
        assert spec["default"] == value, f"{name} default must equal the graph value"
        if name == "steps":
            assert spec["type"] == "integer" and spec["minimum"] <= value <= spec["maximum"]
            assert "work" in spec["help"] or "trained" in spec["help"], "help names the model's good range"
        else:
            assert spec["type"] == "string" and value in spec["enum"]
            assert spec["choices_from"] == {"node_type": klass, "input": field}
            assert len(spec["enum"]) == len(set(spec["enum"]))


@pytest.mark.parametrize("manifest,binding,graph", image_cases())
def test_defaults_leave_the_graph_unchanged(manifest, binding, graph):
    found = controls(graph, manifest["id"])
    if found is None:
        pytest.skip("no sampler")
    defaults = {name: manifest["inputs"][name]["default"] for name in found}
    result = bind_backend_request(manifest, "comfyui", graph, defaults)
    assert json.dumps(result.request, sort_keys=True) == json.dumps(graph, sort_keys=True)


@pytest.mark.parametrize("manifest,binding,graph", image_cases())
def test_bad_sampler_is_refused_and_lists_the_choices(manifest, binding, graph):
    if controls(graph, manifest["id"]) is None:
        pytest.skip("no sampler")
    result = validate_inputs(manifest, {"sampler": "no-such-sampler"})
    assert "sampler" in result.rejected
    assert result.rejected["sampler"].startswith("sampler must be one of: ")
    for choice in manifest["inputs"]["sampler"]["enum"]:
        assert choice in result.rejected["sampler"]
    result = validate_inputs(manifest, {"scheduler": "no-such-scheduler"})
    assert result.rejected["scheduler"].startswith("scheduler must be one of: ")


@pytest.mark.parametrize("manifest,binding,graph", image_cases())
def test_steps_out_of_range_is_refused(manifest, binding, graph):
    if controls(graph, manifest["id"]) is None:
        pytest.skip("no sampler")
    spec = manifest["inputs"]["steps"]
    assert "steps" in validate_inputs(manifest, {"steps": spec["minimum"] - 1}).rejected
    assert "steps" in validate_inputs(manifest, {"steps": spec["maximum"] + 1}).rejected
    assert not validate_inputs(manifest, {"steps": spec["maximum"]}).rejected


@pytest.mark.parametrize("manifest,binding,graph", image_cases())
def test_only_steps_moves_when_steps_change(manifest, binding, graph):
    found = controls(graph, manifest["id"])
    if found is None:
        pytest.skip("no sampler")
    spec = manifest["inputs"]["steps"]
    low, high = spec["minimum"], spec["maximum"]
    seed = {"seed": 7, "prompt": "same prompt"} if "prompt" in manifest["inputs"] else {"seed": 7}
    a = bind_backend_request(manifest, "comfyui", graph, {**seed, "steps": low}).request
    b = bind_backend_request(manifest, "comfyui", graph, {**seed, "steps": high}).request
    node_id, _, field = found["steps"]
    assert a[node_id]["inputs"][field] == low and b[node_id]["inputs"][field] == high
    b_aligned = copy.deepcopy(b)
    b_aligned[node_id]["inputs"][field] = low
    assert a == b_aligned, "a different step count must change nothing else"


@pytest.mark.parametrize("manifest,binding,graph", image_cases())
def test_chosen_sampler_and_scheduler_land_on_their_inputs(manifest, binding, graph):
    found = controls(graph, manifest["id"])
    if found is None:
        pytest.skip("no sampler")
    sampler = next(v for v in manifest["inputs"]["sampler"]["enum"] if v != manifest["inputs"]["sampler"]["default"])
    scheduler = next(v for v in manifest["inputs"]["scheduler"]["enum"] if v != manifest["inputs"]["scheduler"]["default"])
    request = bind_backend_request(manifest, "comfyui", graph, {"sampler": sampler, "scheduler": scheduler}).request
    assert request[found["sampler"][0]]["inputs"][found["sampler"][2]] == sampler
    assert request[found["scheduler"][0]]["inputs"][found["scheduler"][2]] == scheduler


def test_engine_snapshot_replaces_the_sampler_choices():
    manifest, _, _ = load(MANIFESTS / "krea2-turbo.yml")
    assert "newly-added" not in manifest["inputs"]["sampler"]["enum"]
    ok = validate_inputs(manifest, {"sampler": "newly-added"}, choices={"sampler": ["euler", "newly-added"]})
    assert not ok.rejected
