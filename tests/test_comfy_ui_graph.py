"""Tests for gateway.engine_protocol.node_ui_graph (API graph -> ComfyUI UI workflow)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.engine_protocol.node_ui_graph import derive_ui_workflow

SAMPLERS = ["euler", "dpmpp_2m"]
OBJECT_INFO = {
    "CheckpointLoaderSimple": {
        "input": {"required": {"ckpt_name": [["a.safetensors", "b.safetensors"], {}]}},
        "input_order": {"required": ["ckpt_name"]},
        "output": ["MODEL", "CLIP", "VAE"],
        "output_name": ["MODEL", "CLIP", "VAE"],
    },
    "CLIPTextEncode": {
        "input": {"required": {"text": ["STRING", {"multiline": True}], "clip": ["CLIP", {}]}},
        "input_order": {"required": ["text", "clip"]},
        "output": ["CONDITIONING"],
        "output_name": ["CONDITIONING"],
    },
    "EmptyLatentImage": {
        "input": {"required": {
            "width": ["INT", {"default": 512}], "height": ["INT", {"default": 512}],
            "batch_size": ["INT", {"default": 1}]}},
        "input_order": {"required": ["width", "height", "batch_size"]},
        "output": ["LATENT"], "output_name": ["LATENT"],
    },
    "KSampler": {
        "input": {"required": {
            "model": ["MODEL", {}],
            "seed": ["INT", {"default": 0, "control_after_generate": True}],
            "steps": ["INT", {"default": 20}],
            "cfg": ["FLOAT", {"default": 8.0}],
            "sampler_name": [SAMPLERS, {}],
            "scheduler": [["simple", "karras"], {}],
            "positive": ["CONDITIONING", {}],
            "negative": ["CONDITIONING", {}],
            "latent_image": ["LATENT", {}],
            "denoise": ["FLOAT", {"default": 1.0}]}},
        "input_order": {"required": [
            "model", "seed", "steps", "cfg", "sampler_name", "scheduler",
            "positive", "negative", "latent_image", "denoise"]},
        "output": ["LATENT"], "output_name": ["LATENT"],
    },
    "VAEDecode": {
        "input": {"required": {"samples": ["LATENT", {}], "vae": ["VAE", {}]}},
        "input_order": {"required": ["samples", "vae"]},
        "output": ["IMAGE"], "output_name": ["IMAGE"],
    },
    "SaveImage": {
        "input": {"required": {"images": ["IMAGE", {}], "filename_prefix": ["STRING", {"default": "ComfyUI"}]},
                  "hidden": {"prompt": "PROMPT"}},
        "input_order": {"required": ["images", "filename_prefix"], "hidden": ["prompt"]},
        "output": [], "output_name": [],
    },
    "OptionalMask": {
        "input": {"required": {"image": ["IMAGE", {}]},
                  "optional": {"mask": ["MASK", {}], "force": ["STRING", {"forceInput": True}]}},
        "output": ["IMAGE"], "output_name": ["IMAGE"],
    },
}


def lookup(ct):
    return OBJECT_INFO.get(ct)


def txt2img():
    return {
        "_meta": {"name": "x"},
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "b.safetensors"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "cat", "clip": ["1", 1]},
              "_meta": {"title": "Positive"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["1", 1]}},
        "4": {"class_type": "EmptyLatentImage", "inputs": {"width": 640, "height": 480, "batch_size": 2}},
        "5": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "seed": 42, "steps": 8, "cfg": 1.5, "sampler_name": "dpmpp_2m",
            "scheduler": "karras", "positive": ["2", 0], "negative": ["3", 0],
            "latent_image": ["4", 0], "denoise": 0.9}},
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"images": ["6", 0], "filename_prefix": "out"}},
    }


def by_id(wf):
    return {n["id"]: n for n in wf["nodes"]}


def test_control_after_generate_alignment():
    node = by_id(derive_ui_workflow(txt2img(), lookup))[5]
    assert node["widgets_values"] == [42, "fixed", 8, 1.5, "dpmpp_2m", "karras", 0.9]


def test_links_wiring_and_types():
    wf = derive_ui_workflow(txt2img(), lookup)
    nodes = by_id(wf)
    assert [l[0] for l in wf["links"]] == list(range(1, len(wf["links"]) + 1))
    assert wf["last_link_id"] == len(wf["links"]) == 9
    for lid, f, fs, t, ts, typ in wf["links"]:
        assert nodes[f]["outputs"][fs]["type"] == typ
        assert lid in nodes[f]["outputs"][fs]["links"]
        assert nodes[t]["inputs"][ts]["link"] == lid
        assert nodes[t]["inputs"][ts]["type"] == typ
    assert {tuple(l[1:5]) for l in wf["links"]} >= {(1, 1, 2, 0), (5, 0, 6, 0), (1, 2, 6, 1)}
    # unlinked output slots serialise links as None
    assert nodes[7]["outputs"] == []
    # connection slots precede widget slots, as in the frontend
    assert [i["name"] for i in nodes[2]["inputs"]] == ["clip", "text"]
    assert nodes[2]["inputs"][1]["widget"] == {"name": "text"}
    assert nodes[2]["title"] == "Positive"
    assert nodes[1]["outputs"][1]["links"] == [1, 2]


def test_schema_envelope_and_layout():
    wf = derive_ui_workflow(txt2img(), lookup)
    assert wf["version"] == 0.4 and wf["revision"] == 0
    assert wf["last_node_id"] == 7
    assert wf["groups"] == [] and wf["config"] == {} and wf["extra"] == {}
    nodes = by_id(wf)
    assert sorted(n["order"] for n in nodes.values()) == list(range(7))
    for l in wf["links"]:  # topological: source ordered before target
        assert nodes[l[1]]["order"] < nodes[l[3]]["order"]
        assert nodes[l[1]]["pos"][0] < nodes[l[3]]["pos"][0]
    boxes = [(n["pos"], n["size"]) for n in nodes.values()]
    for i, (p, s) in enumerate(boxes):
        for q, t in boxes[i + 1:]:
            overlap = p[0] < q[0] + t[0] and q[0] < p[0] + s[0] and p[1] < q[1] + t[1] and q[1] < p[1] + s[1]
            assert not overlap


def test_widget_value_defaults():
    api = {"1": {"class_type": "EmptyLatentImage", "inputs": {}},
           "2": {"class_type": "CheckpointLoaderSimple", "inputs": {}}}
    nodes = by_id(derive_ui_workflow(api, lookup))
    assert nodes[1]["widgets_values"] == [512, 512, 1]
    assert nodes[2]["widgets_values"] == ["a.safetensors"]


def test_widget_converted_to_input():
    api = {"1": {"class_type": "EmptyLatentImage", "inputs": {"width": 64, "height": 64, "batch_size": 1}},
           "2": {"class_type": "KSampler", "inputs": {"model": ["3", 0], "seed": ["4", 0], "steps": 5,
                                                         "positive": ["5", 0], "negative": ["5", 0],
                                                         "latent_image": ["1", 0]}},
           "3": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
           "4": {"class_type": "EmptyLatentImage", "inputs": {}},  # stand-in INT source: LATENT
           "5": {"class_type": "CLIPTextEncode", "inputs": {"text": "t", "clip": ["3", 1]}}}
    wf = derive_ui_workflow(api, lookup)
    ks = by_id(wf)[2]
    seed_slot = next(i for i in ks["inputs"] if i["name"] == "seed")
    assert seed_slot["widget"] == {"name": "seed"} and seed_slot["link"] is not None
    # placeholder default keeps later widget indexes aligned
    assert ks["widgets_values"][:4] == [0, "fixed", 5, 8.0]
    link = next(l for l in wf["links"] if l[0] == seed_slot["link"])
    assert (link[1], link[3]) == (4, 2)


def test_unknown_class_returns_none():
    api = txt2img()
    api["8"] = {"class_type": "NoSuchNode", "inputs": {}}
    assert derive_ui_workflow(api, lookup) is None


def test_dangling_link_and_empty_graph_return_none():
    api = {"1": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["9", 0]}}}
    assert derive_ui_workflow(api, lookup) is None
    assert derive_ui_workflow({"_meta": {}}, lookup) is None


def test_cycle_returns_none():
    api = {"1": {"class_type": "VAEDecode", "inputs": {"samples": ["2", 0], "vae": ["3", 2]}},
           "2": {"class_type": "KSampler", "inputs": {"model": ["3", 0], "positive": ["4", 0],
                                                        "negative": ["4", 0], "latent_image": ["1", 0]}},
           "3": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
           "4": {"class_type": "CLIPTextEncode", "inputs": {"clip": ["3", 1]}}}
    # 1 -> 2 needs LATENT out of VAEDecode slot 0 which is IMAGE; structure only: cycle 1<-2<-1
    assert derive_ui_workflow(api, lookup) is None


def test_deterministic_and_json_serialisable():
    a = derive_ui_workflow(txt2img(), lookup)
    b = derive_ui_workflow(json.loads(json.dumps(txt2img())), lookup)
    assert a == b
    assert json.dumps(a) == json.dumps(b)
    changed = txt2img()
    changed["5"]["inputs"]["steps"] = 9
    assert derive_ui_workflow(changed, lookup)["id"] != a["id"]
    assert len(a["id"]) == 36


def test_input_not_mutated():
    api = txt2img()
    before = json.dumps(api, sort_keys=True)
    derive_ui_workflow(api, lookup)
    assert json.dumps(api, sort_keys=True) == before


def test_optional_unlinked_connection_omitted_and_force_input_is_connection():
    api = {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
           "2": {"class_type": "OptionalMask", "inputs": {"image": ["9", 0]}},
           "9": {"class_type": "VAEDecode", "inputs": {"samples": ["4", 0], "vae": ["1", 2]}},
           "4": {"class_type": "EmptyLatentImage", "inputs": {}}}
    n = by_id(derive_ui_workflow(api, lookup))[2]
    assert [i["name"] for i in n["inputs"]] == ["image"]
    assert n["widgets_values"] == []
    api["2"]["inputs"]["mask"] = ["9", 0]  # linked optional appears (type MASK, link set)
    api["2"]["inputs"]["force"] = ["1", 0]  # forceInput STRING is a connection, not a widget
    n = by_id(derive_ui_workflow(api, lookup))[2]
    assert [i["name"] for i in n["inputs"]] == ["image", "mask", "force"]
    assert all(i["link"] is not None for i in n["inputs"])
    assert n["widgets_values"] == []


# ---- Real-server oracle -------------------------------------------------

REPO = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "comfy_object_info_images.json"
IMAGES = REPO / "workflows" / "images"


def _pairs():
    return sorted(p for p in IMAGES.glob("*.json") if not p.name.endswith(".ui.json")
                  and p.with_suffix(".ui.json").exists())


def _frontend_authored(ui):
    """Hand/frontend-authored twins carry concrete slot types; the old derivation wrote '*'."""
    ins = [i for n in ui["nodes"] for i in n.get("inputs", [])]
    return bool(ins) and all(i.get("type") != "*" for i in ins)


def _mask_control(values):
    return ["<ctl>" if v in ("fixed", "randomize", "increment", "decrement") else v for v in values]


@pytest.mark.skipif(not FIXTURE.exists() or not _pairs(), reason="object_info fixture or workflows missing")
def test_oracle_against_workflow_twins():
    """Frontend-authored twins must match exactly; stale (old-derivation) twins only structurally."""
    info = json.loads(FIXTURE.read_text())
    exact = 0
    for api_path in _pairs():
        api = json.loads(api_path.read_text())
        ui = json.loads(api_path.with_suffix(".ui.json").read_text())
        derived = derive_ui_workflow(api, info.get)
        assert derived is not None, api_path.name
        dn, un = by_id(derived), by_id(ui)
        assert set(dn) == set(un), api_path.name
        assert {i: n["type"] for i, n in dn.items()} == {i: n["type"] for i, n in un.items()}
        if not _frontend_authored(ui):
            continue
        for i in dn:
            assert _mask_control(dn[i]["widgets_values"]) == _mask_control(un[i].get("widgets_values", [])), (
                api_path.name, i)
            assert [x["name"] for x in dn[i]["inputs"]] == [x["name"] for x in un[i]["inputs"]], (api_path.name, i)
        assert {tuple(l[1:5]) for l in derived["links"]} == {tuple(l[1:5]) for l in ui["links"]}, api_path.name
        exact += 1
    assert exact >= 1
