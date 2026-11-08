"""The segment-chain composer (S7 parity, M9): a template of named segment variants, a rule that picks one per position.

These tests use a small made-up template, so they show that the composer owes nothing to a shipped chain. The byte-level
proof for the shipped chains is ``tests/test_s7_video_chain_graphs.py``.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from gateway import segment_chain as sc
from gateway import video_extend


def _node(cls: str, **inputs):
    return {"class_type": cls, "inputs": inputs}


def _document() -> dict:
    return {
        "id": "toy",
        "binding": {
            "model_family": "toy", "stride_reason": "toy_grid", "frame_step": 4, "frame_offset": 1,
            "default_context_frames": 9, "context_windows": [], "min_shot_seconds": 1.0, "options": ["state_ledger"],
            "negative_default": "bad", "node_prefix": "p{index}.",
            "rule": [{"variant": "start", "index": 0, "source": False}, {"variant": "more"}],
        },
        "template": {
            "shared": {"load": _node("Load", name="m")},
            "source": {
                "nodes": {"clip": _node("Clip", file="$source_file"),
                          "voice": {**_node("Voice", clip=["clip", 0], seconds="$source_seconds"), "if": "sound"}},
                "exports": {"frames": ["clip", 0], "sound": {"$if": "sound", "then": ["voice", 0]}},
            },
            "variants": {
                "start": {
                    "nodes": {"gen": _node("Gen", model=["load", 0], text="$prompt", length="$frames", seed="$seed"),
                              "pic": {**_node("Pic", file="$first_frame_file"), "if": "first_frame"},
                              "dec": _node("Dec", samples=["@gen", 0], pic={"$if": "first_frame", "then": ["@pic", 0]}),
                              "snd": _node("Snd", samples=["@gen", 0])},
                    "unit": {"sample": "gen", "decode": "dec"},
                    "exports": {"frames": ["@dec", 0], "sound": ["@snd", 0], "latent": ["@gen", 0]},
                },
                "more": {
                    "nodes": {"gen": _node("Gen", model=["load", 0], text="$prompt", length="$sampled_frames",
                                           seed="$seed", pin="$frames_in", latent="$latent_in"),
                              "dec": _node("Dec", samples=["@gen", 0]),
                              "cat": _node("Cat", a="$frames_in", b=["@dec", 0]),
                              "snd": {**_node("Snd", samples=["@gen", 0], start="$context_seconds"), "if": "sound"}},
                    "unit": {"sample": "gen", "decode": "dec"},
                    "exports": {"frames": ["@cat", 0], "sound": {"$if": "sound", "then": ["@snd", 0]},
                                "latent": ["@gen", 0]},
                },
            },
            "finish": {
                "sound_join": {"id": "join{k}", "class_type": "Join", "first": "a", "next": "b", "inputs": {"mode": "after"}},
                "nodes": {"out": _node("Out", frames="$frames_out", sound={"$if": "sound_out", "then": "$sound_out"},
                                       rate="$fps")},
                "save_node": "out",
            },
        },
    }


def _spec(*frames: int, source=None, first_frame_image=None) -> SimpleNamespace:
    segments = [video_extend.Segment(f"shot {i}", n, i) for i, n in enumerate(frames)]
    return SimpleNamespace(segments=segments, width=64, height=48, fps=8.0, seed=100, source=source,
                           first_frame_image=first_frame_image, first_frame_strength=1.0, context_strength=0.5,
                           filename_prefix="out/toy")


def _compose(document: dict, spec: SimpleNamespace, context: int = 9) -> sc.Composed:
    return sc.compose(sc.load(document), spec, context=context, negative="bad")


def test_each_position_gets_the_variant_the_rule_names():
    composed = _compose(_document(), _spec(13, 8, 8))
    graph = composed.graph
    assert [u["sample"] for u in composed.units] == ["p0.gen", "p1.gen", "p2.gen"]
    assert graph["p0.gen"]["inputs"]["length"] == 13            # the start variant: its own frames
    assert graph["p1.gen"]["inputs"]["length"] == 9 + 8          # the more variant: context plus new frames
    assert graph["p1.gen"]["inputs"]["pin"] == ["p0.dec", 0]
    assert graph["p2.gen"]["inputs"]["pin"] == ["p1.cat", 0]
    assert graph["p2.gen"]["inputs"]["latent"] == ["p1.gen", 0]  # the previous segment's latent export
    assert [graph[f"p{i}.gen"]["inputs"]["seed"] for i in range(3)] == [100, 101, 102]
    assert composed.total_frames == 13 + 8 + 8


def test_a_request_with_a_source_starts_on_the_default_variant_and_keeps_the_source_sound():
    source = SimpleNamespace(filename="in.mp4", frames=20, width=64, height=48, has_audio=True)
    composed = _compose(_document(), _spec(8, source=source))
    graph = composed.graph
    assert graph["clip"]["inputs"]["file"] == "in.mp4" and graph["voice"]["inputs"]["seconds"] == 20 / 8.0
    assert graph["p0.gen"]["inputs"]["pin"] == ["clip", 0]
    assert graph["join0"] == {"class_type": "Join", "inputs": {"mode": "after", "a": ["voice", 0], "b": ["p0.snd", 0]}}
    assert graph["out"]["inputs"]["sound"] == ["join0", 0]
    assert composed.total_frames == 28


def test_a_silent_source_drops_the_nodes_and_inputs_that_need_sound():
    source = SimpleNamespace(filename="in.mp4", frames=20, width=64, height=48, has_audio=False)
    graph = _compose(_document(), _spec(8, 8, source=source)).graph
    assert "voice" not in graph and "p0.snd" not in graph and not any(k.startswith("join") for k in graph)
    assert "sound" not in graph["out"]["inputs"]


def test_a_condition_drops_an_input_and_a_node_when_its_fact_is_false():
    plain = _compose(_document(), _spec(13)).graph
    assert "p0.pic" not in plain and "pic" not in plain["p0.dec"]["inputs"]
    pinned = _compose(_document(), _spec(13, first_frame_image="a.png")).graph
    assert pinned["p0.pic"]["inputs"]["file"] == "a.png" and pinned["p0.dec"]["inputs"]["pic"] == ["p0.pic", 0]


def test_the_composed_graph_does_not_share_state_with_the_template():
    template = sc.load(_document())
    before = copy.deepcopy(template.variants)
    first = sc.compose(template, _spec(13, 8), context=9, negative="x").graph
    first["p1.gen"]["inputs"]["pin"].append("changed")
    assert template.variants == before
    assert sc.compose(template, _spec(13, 8), context=9, negative="x").graph["p1.gen"]["inputs"]["pin"] == ["p0.dec", 0]


@pytest.mark.parametrize("change, message", [
    (lambda d: d["template"]["variants"]["more"]["nodes"]["gen"]["inputs"].update(text="$nope"), "unknown parameter"),
    (lambda d: d["template"]["variants"]["more"]["nodes"]["snd"].update({"if": "nope"}), "unknown fact"),
    (lambda d: d["template"]["variants"]["more"]["nodes"]["dec"]["inputs"].update(samples=["@missing", 0]), "not a node"),
    (lambda d: d["binding"]["rule"].insert(0, {"variant": "ghost"}), "does not declare"),
    (lambda d: d["binding"]["rule"][-1].update(index=1), "last entry is the default"),
    (lambda d: d["binding"].update(node_prefix="p."), "must hold"),
    (lambda d: d["template"]["variants"]["more"].update(unit={"sample": "gen"}), "sample and decode"),
    (lambda d: d["template"]["variants"]["more"]["exports"].update(extra=["@gen", 0]), "exports are some of"),
    (lambda d: d["template"]["finish"].update(save_node="nowhere"), "must name a finish node"),
    # a link to a node no segment can reach, a node a fact can leave out, and a slot no node has
    (lambda d: d["template"]["variants"]["start"]["nodes"]["gen"]["inputs"].update(model=["missing", 0]), "dangling link"),
    (lambda d: d["template"]["variants"]["start"]["nodes"]["dec"]["inputs"].update(pic=["@pic", 0]), "dangling link"),
    (lambda d: d["template"]["variants"]["start"]["nodes"]["dec"]["inputs"].update(samples=["@gen", -1]), "output slot"),
    (lambda d: d["template"]["variants"]["start"]["nodes"]["dec"]["inputs"].update(samples=["@gen", True]), "output slot"),
    (lambda d: d["template"]["variants"]["start"]["exports"].update(frames=["@dec", -2]), "output slot"),
    # a parameter that is None until the finish step
    (lambda d: d["template"]["variants"]["more"]["nodes"]["cat"]["inputs"].update(a="$frames_out"), "only in the finish"),
    (lambda d: d["template"]["variants"]["more"]["nodes"]["cat"]["inputs"].update(a="$sound_out"), "only in the finish"),
    # a rule fact of the wrong type never matches
    (lambda d: d["binding"]["rule"][0].update(source="false"), "source must be true or false"),
    (lambda d: d["binding"]["rule"][0].update(index="0"), "index must be a whole number"),
])
def test_a_template_that_asks_for_what_the_composer_does_not_give_is_refused_at_load(change, message):
    document = _document()
    change(document)
    with pytest.raises(sc.ChainTemplateError, match=message):
        sc.load(document)


def test_the_shipped_chains_load_and_their_rules_pick_a_variant_for_every_position():
    engines = video_extend.ENGINES
    assert sorted(engines) == ["h3_chain", "ltx25_chain"]
    for engine in engines.values():
        binding = engine.binding
        for index, source in ((0, False), (0, True), (1, False), (1, True), (5, True)):
            assert sc.pick_variant(binding, index=index, source=source) in engine.template.variants


def _links(value):
    if isinstance(value, list):
        if len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int):
            yield value[0]
        else:
            for item in value:
                yield from _links(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _links(item)


def _sources():
    return (None,
            SimpleNamespace(filename="in.mp4", frames=20, width=64, height=48, has_audio=True),
            SimpleNamespace(filename="in.mp4", frames=20, width=32, height=24, has_audio=False))


@pytest.mark.parametrize("name", ["toy", "ltx25_chain", "h3_chain"])
def test_no_reachable_variant_or_fact_combination_links_to_a_missing_node(name):
    template = sc.load(_document()) if name == "toy" else video_extend.ENGINES[name].template
    step, context = template.binding.frame_step, template.binding.default_context_frames
    for source in _sources():
        for first in (None, "a.png"):
            for count in (1, 2, 4):
                spec = _spec(*([step * 2 + template.binding.frame_offset] + [step] * (count - 1)), source=source,
                             first_frame_image=first)
                graph = sc.compose(template, spec, context=context, negative="n").graph
                missing = {t for node in graph.values() for t in _links(node["inputs"]) if t not in graph}
                assert not missing, (name, source, first, count, missing)


def test_a_failure_in_any_node_of_a_segment_names_the_segment_under_a_custom_prefix():
    units = _compose(_document(), _spec(13, 8, 8)).units
    prefix = "p{index}."                                        # the toy binding's node prefix
    assert video_extend.segment_of_node(units, "p1.cat", node_prefix=prefix) == 1
    assert video_extend.segment_of_node(units, "p2.snd", node_prefix=prefix) == 2
    assert video_extend.segment_of_node(units, "p1.cat", node_prefix=prefix, progress_only=True) is None
    assert video_extend.segment_of_node(units, "p1.gen", node_prefix=prefix, progress_only=True) == 1
    assert video_extend.segment_of_node(units, "p2.dec", progress_only=True) == 2
    assert video_extend.segment_of_node(units, "load", node_prefix=prefix) is None


def test_a_prefix_that_starts_another_does_not_claim_its_nodes():
    units = _compose(_document(), _spec(*([13] + [8] * 11))).units  # p1. starts the text of p10. and p11.
    assert video_extend.segment_of_node(units, "p10.cat", node_prefix="p{index}.") == 10
    assert video_extend.segment_of_node(units, "p1.cat", node_prefix="p{index}.") == 1
