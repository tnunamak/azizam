"""Image graph roles have one validated, immutable home, separate from input maps."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from gateway.discovery import WorkflowRegistry
from gateway import image_graph_roles

BINDING = "comfyui"  # every shipped image graph's binding key


def load(section, registry):
    """The loader over the shipped image graphs' binding."""
    return image_graph_roles.load(section, registry, BINDING)

ROOT = Path(__file__).resolve().parents[1]
NAMES = [
    "anima-turbo",
    "fibo-2-edit-turbo",
    "flux2-klein",
    "flux2-klein-4b",
    "flux2-klein-4b-edit",
    "flux2-klein-4b-inpaint",
    "flux2-klein-edit",
    "flux2-klein-snofs",
    "krea2-turbo",
    "krea2-turbo-snofs",
    "moss-image",
    "moss-image-fast",
    "moss-image-quality",
    "qwen-image-2.1-edit",
    "qwen-image-2.1-edit-multiref",
    "qwen-image-2.1-inpaint",
    "sdxl-seamless-tile",
    "seedvr2-upscale"
]
KREA = "workflows/images/krea2-turbo.json"
EDIT = "workflows/images/flux2-klein-edit.json"
MULTI = "workflows/images/qwen-image-2.1-edit-multiref.json"


@pytest.fixture
def corpus(tmp_path):
    # All mutations, including graph/manifest mutations, stay in private copies.
    section_file = tmp_path / "registry/defaults/images.yml"
    section_file.parent.mkdir(parents=True)
    section_file.write_text((ROOT / "registry/defaults/images.yml").read_text())
    manifests = tmp_path / "manifests/workflows"
    manifests.mkdir(parents=True)
    for name in NAMES:
        for relative in (f"workflows/images/{name}.json", f"manifests/workflows/{name}.yml"):
            target = tmp_path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text((ROOT / relative).read_text())
    section = yaml.safe_load(section_file.read_text())
    registry = WorkflowRegistry.from_directory(manifests, policy={})
    assert not registry.errors
    return section, registry, tmp_path


def _plain(value):
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _plain(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_plain(child) for child in value]
    return value


def _refuses(section, registry, graph, key):
    with pytest.raises(ValueError) as exc:
        load(section, registry)
    assert graph in str(exc.value)
    assert key in str(exc.value)


def test_all_eighteen_shipped_entries_load(corpus):
    section, registry, _ = corpus
    result = load(section, registry)
    assert set(result) == {f"workflows/images/{name}.json" for name in NAMES}
    assert len(result) == 18
    assert {path for path, roles in result.items() if "batch_size" in roles} == {
        EDIT, "workflows/images/flux2-klein-4b-edit.json",
    }
    assert {path for path, roles in result.items() if "reference_order" in roles} == {MULTI}


def test_temporary_parity_with_each_graph_meta(corpus):
    section, registry, root = corpus
    records = {record.contract["bindings"]["comfyui"]["workflow"]: record for record in registry.records()}
    for path, actual in load(section, registry).items():
        meta = json.loads((root / path).read_text())["_meta"]
        binding = records[path].contract["bindings"]["comfyui"]
        background = meta["background_removal"]
        output, = [name for name, selector in binding["selectors"].items()
                   if selector["node"] == background["save_node"]]
        expected = {"background": {"output": output, "processor": background["rmbg_node"],
                                   "slot": background["transparent_image"][1]}}
        assert background["transparent_image"][0] == background["rmbg_node"]
        if "batch_size_node" in meta and "batch_size" not in binding["map"]:
            expected["batch_size"] = [{"node": meta["batch_size_node"], "input": "batch_size"}]
        if "image_nodes" in meta:
            expected["reference_order"] = []
            for node in meta["image_nodes"]:
                name, = [name for name, targets in binding["map"].items()
                         if any(target.get("node") == node and target.get("input") == "image"
                                for target in targets)]
                expected["reference_order"].append(name)
        assert _plain(actual) == expected, path


def test_unknown_graph_path_is_refused(corpus):
    section, registry, _ = corpus
    unknown = "workflows/images/not-registered.json"
    section["graph_roles"][unknown] = section["graph_roles"].pop(KREA)
    _refuses(section, registry, unknown, "graph_roles")


def test_missing_processor_node_is_refused(corpus):
    section, registry, _ = corpus
    section["graph_roles"][KREA]["background"]["processor"] = "missing"
    _refuses(section, registry, KREA, "background.processor")


def test_a_processor_that_does_not_cut_out_the_selected_picture_is_refused(corpus):
    section, registry, root = corpus
    graph = json.loads((root / KREA).read_text())
    other = next(node for node, spec in graph.items()
                 if node != "10" and not node.startswith("_") and spec.get("inputs", {}).get("image") != graph["10"]["inputs"]["image"])
    section["graph_roles"][KREA]["background"]["processor"] = other
    _refuses(section, registry, KREA, "background.processor")


@pytest.mark.parametrize("slot", [-1, 999, True])
def test_invalid_background_slot_is_refused(corpus, slot):
    section, registry, _ = corpus
    section["graph_roles"][KREA]["background"]["slot"] = slot
    _refuses(section, registry, KREA, "background.slot")


@pytest.mark.parametrize("missing", ["output", "selector"])
def test_undeclared_background_output_is_refused(corpus, missing):
    section, registry, _ = corpus
    record = next(record for record in registry.records()
                  if record.contract["bindings"]["comfyui"]["workflow"] == KREA)
    if missing == "output":
        record.contract["outputs"].pop("image")
    else:
        record.contract["bindings"]["comfyui"]["selectors"].pop("image")
    _refuses(section, registry, KREA, "background.output")


def test_missing_reference_order_input_is_refused(corpus):
    section, registry, _ = corpus
    section["graph_roles"][MULTI]["reference_order"][1] = "missing"
    _refuses(section, registry, MULTI, "reference_order")


def test_repeated_reference_order_input_is_refused(corpus):
    section, registry, _ = corpus
    section["graph_roles"][MULTI]["reference_order"][1] = "source_image"
    _refuses(section, registry, MULTI, "reference_order")


def test_repeating_a_mapped_role_is_refused(corpus):
    section, registry, _ = corpus
    section["graph_roles"][KREA]["batch_size"] = [{"node": "6", "input": "batch_size"}]
    _refuses(section, registry, KREA, "batch_size")


def test_repeating_a_mapped_target_under_another_role_is_refused(corpus):
    section, registry, _ = corpus
    section["graph_roles"][EDIT]["batch_size"] = [{"node": "47", "input": "width"}]
    _refuses(section, registry, EDIT, "batch_size")


def test_missing_batch_node_is_refused(corpus):
    section, registry, _ = corpus
    section["graph_roles"][EDIT]["batch_size"][0]["node"] = "missing"
    _refuses(section, registry, EDIT, "batch_size")


def test_result_is_recursively_read_only_and_detached(corpus):
    section, registry, _ = corpus
    result = load(section, registry)
    with pytest.raises(TypeError):
        result[KREA] = {}
    with pytest.raises(TypeError):
        result[KREA]["background"]["slot"] = 999
    with pytest.raises(TypeError):
        result[EDIT]["batch_size"][0]["node"] = "missing"
    with pytest.raises(TypeError):
        result[MULTI]["reference_order"][0] = "missing"
    section["graph_roles"][KREA]["background"]["processor"] = "changed"
    assert result[KREA]["background"]["processor"] == "10"


def test_loading_does_not_need_or_mutate_graph_meta(corpus):
    section, registry, root = corpus
    before = copy.deepcopy(section)
    for name in NAMES:
        path = root / f"workflows/images/{name}.json"
        graph = json.loads(path.read_text())
        graph.pop("_meta")
        path.write_text(json.dumps(graph))
    assert len(load(section, registry)) == 18
    assert section == before


@pytest.mark.parametrize("level", ["table", "entry", "background", "target"])
def test_schema_closes_every_graph_roles_object(corpus, level):
    section, _, _ = corpus
    schema = json.loads((ROOT / "registry/defaults/images.schema.json").read_text())
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(section))
    places = {
        "table": section["graph_roles"],
        "entry": section["graph_roles"][KREA],
        "background": section["graph_roles"][KREA]["background"],
        "target": section["graph_roles"][EDIT]["batch_size"][0],
    }
    places[level]["unexpected"] = {}
    assert list(validator.iter_errors(section))
