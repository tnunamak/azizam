"""V5a: the facts a video request needs are manifest data (docs/design/CODE_TO_DATA_S7_VIDEO_MOVE.md, section 2).

Four groups: the schema accepts the optional ``video`` section and refuses what is not in it; the contract check refuses
a section that cannot work; every shipped video manifest carries the numbers ``registry/defaults/videos.yml`` holds today,
value for value; and ``gateway/video_grid.py`` computes the frame grid and the size exactly as the old create code does.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from gateway.video_grid import (
    GridSpec, SizeSpec, VideoManifest, frame_count, resolve_size, seconds_to_length, video_facts)
from gateway.workflow_contract import check_contract

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = sorted((ROOT / "manifests" / "workflows").glob("video-*.yml"))
OLD = yaml.safe_load((ROOT / "registry" / "defaults" / "videos.yml").read_text(encoding="utf-8"))["workflows"]
T2V = ROOT / "manifests" / "workflows" / "video-ltx-2.5-t2v.yml"


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def old_key(manifest: dict) -> str:
    return "video-" + manifest["id"].split(".", 2)[2]


def section() -> dict:
    return {
        "roles": {"width": "width", "height": "height", "length": "length"},
        "size": {"default": "960x512", "max": "1280x704", "multiple": 64},
        "length": {"min_seconds": 1, "max_seconds": 10},
        "grid": {"fps": 24, "step": 8, "offset": 1},
    }


def doc_with(video: dict, *, inputs: dict | None = None, mapped: tuple[str, ...] = ("width", "height", "length")) -> dict:
    """A small valid video document that carries ``video``. The roles the section names are inputs; ``mapped`` of them are bound."""
    doc = load(T2V)
    doc["video"] = video
    for name in ("width", "height", "length"):
        doc["inputs"][name] = {"type": "integer", "minimum": 1, "maximum": 5000}
    doc["inputs"].update(inputs or {})
    binding = doc["bindings"]["comfyui"]["map"]
    for name in ("width", "height", "length"):
        binding.pop(name, None)
    for name in mapped:
        binding[name] = [{"node": "20", "input": "value"}]
    return doc


def codes(doc: dict) -> set[tuple[tuple, str]]:
    return {(error.path, error.code) for error in check_contract(doc).errors}


# ------------------------------------------------------------------------------------------------ the schema


def test_a_video_manifest_may_carry_the_video_section() -> None:
    assert check_contract(doc_with(section())).ok


def test_the_video_section_is_optional() -> None:
    bare = load(T2V)
    bare.pop("video", None)
    assert check_contract(bare).ok


def test_the_video_section_is_closed() -> None:
    video = section()
    video["flavor"] = "x"
    assert (("video", "flavor"), "unknown_field") in codes(doc_with(video))


def test_a_manifest_of_another_capability_may_not_carry_the_section() -> None:
    doc = doc_with(section())
    doc["capability"] = "audio"
    assert any(path[:1] == ("video",) for path, _ in codes(doc))


@pytest.mark.parametrize("path,value", [
    (("size", "multiple"), 0),
    (("grid", "step"), 0),
    (("grid", "offset"), -1),
    (("grid", "fps"), 0),
    (("size", "default"), "big"),
    (("size", "max"), "1280"),
    (("length", "min_seconds"), "1"),
    (("requires",), ["telepathy"]),
])
def test_the_schema_refuses_a_value_of_the_wrong_kind(path: tuple, value: object) -> None:
    video = section()
    target = video
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert any(p[:1] == ("video",) for p, _ in codes(doc_with(video)))


@pytest.mark.parametrize("missing", [("size", "default"), ("size", "max"), ("grid", "fps"), ("grid", "step"),
                                       ("length", "min_seconds"), ("length", "max_seconds")])
def test_the_schema_requires_the_limits_a_request_is_checked_against(missing: tuple) -> None:
    video = section()
    del video[missing[0]][missing[1]]
    assert (("video", *missing), "required_missing") in codes(doc_with(video))


@pytest.mark.parametrize("role", ["width", "height", "length"])
def test_a_size_and_a_length_need_their_roles(role: str) -> None:
    video = section()
    del video["roles"][role]
    assert any(p[:2] == ("video", "roles") for p, _ in codes(doc_with(video)))


def test_a_chain_manifest_needs_no_length_role() -> None:
    video = section()
    del video["roles"]["length"]
    video["chain"] = True
    assert check_contract(doc_with(video)).ok


# -------------------------------------------------------------------------------------------- the contract check


def test_a_role_must_name_a_declared_input() -> None:
    video = section()
    video["roles"]["width"] = "not_an_input"
    assert (("video", "roles", "width"), "role_input_missing") in codes(doc_with(video))


def test_a_role_input_must_be_bound() -> None:
    doc = doc_with(section(), mapped=("height", "length"))
    assert (("video", "roles", "width"), "role_input_unbound") in codes(doc)


def test_the_seconds_range_may_not_be_empty() -> None:
    video = section()
    video["length"] = {"min_seconds": 5, "max_seconds": 2}
    assert (("video", "length"), "bounds_empty") in codes(doc_with(video))


def test_the_default_seconds_must_lie_in_the_range() -> None:
    video = section()
    video["length"]["default_seconds"] = 11
    assert (("video", "length", "default_seconds"), "default_invalid") in codes(doc_with(video))


def test_the_default_size_may_not_exceed_the_largest_size() -> None:
    video = section()
    video["size"]["default"] = "1920x1080"
    assert (("video", "size", "default"), "default_over_max") in codes(doc_with(video))


def test_the_frame_bounds_may_not_be_empty() -> None:
    video = section()
    video["grid"].update({"min_frames": 50, "max_frames": 20})
    assert (("video", "grid"), "bounds_empty") in codes(doc_with(video))


def test_a_valid_section_raises_no_error() -> None:
    assert codes(doc_with(section())) == set()


# ---------------------------------------------------------------- the shipped manifests equal videos.yml, value for value


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.stem)
def test_every_shipped_video_manifest_holds_a_valid_video_section(path: Path) -> None:
    manifest = load(path)
    assert "video" in manifest, "the manifest has no video section"
    assert check_contract(manifest).ok, [e.as_dict() for e in check_contract(manifest).errors]


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.stem)
def test_the_section_holds_the_values_videos_yml_holds(path: Path) -> None:
    manifest = load(path)
    video = manifest["video"]
    old = OLD[old_key(manifest)]
    assert video["size"] == {"default": old["default_size"], "max": old["max_size"], "multiple": old.get("size_multiple", 32)}
    assert video["length"]["min_seconds"] == old["min_seconds"]
    assert video["length"]["max_seconds"] == old["max_seconds"]
    assert video["length"].get("default_seconds") == old.get("default_seconds")
    assert video["grid"]["fps"] == old["frame_rate"]
    assert video["grid"]["step"] == old.get("length_step", 8)
    assert video["grid"]["offset"] == old.get("length_offset", 1)
    assert video["grid"].get("max_frames") == old.get("max_length")
    assert video["grid"].get("min_frames") is None, "the old code has no floor beyond one step"
    assert bool(video.get("select_subject")) == bool(old.get("subject_select"))
    assert bool(video.get("chain")) == bool(old.get("builder"))
    requires = set(video.get("requires", ()))
    assert ("image" in requires) == bool(old.get("requires_input_reference"))
    assert ("extensions" in requires) == bool(old.get("requires_extensions"))
    assert ("source" in requires) == bool(old.get("source_video_input"))


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.stem)
def test_the_variants_name_the_workflows_videos_yml_names(path: Path) -> None:
    manifest = load(path)
    old = OLD[old_key(manifest)]
    variants = manifest["video"].get("variants", {})
    expected = {}
    if old.get("i2v_workflow"):
        expected["image"] = "video.generate." + old["i2v_workflow"].removeprefix("video-")
    if old.get("chain_workflow"):
        expected["long"] = "video.generate." + old["chain_workflow"].removeprefix("video-")
    assert variants == expected
    shipped = {load(p)["id"] for p in MANIFESTS}
    assert set(variants.values()) <= shipped


def graph_nodes(manifest: dict) -> dict:
    return json.loads((ROOT / manifest["bindings"]["comfyui"]["workflow"]).read_text(encoding="utf-8"))


# Targets a manifest binds beyond the nodes registry/defaults/videos.yml names for the role (workflow key, role -> nodes).
INTENDED_EXTRA_NODES = {
    (f"video-ltx-2.5-{variant}", "frame_rate"): {"24"}  # the float twin of the integer frame-rate node 23
    for variant in ("t2v", "i2v", "keyframes", "keyframes-mid")
}


def check_role_nodes(manifest: dict) -> None:
    """A role input is bound to the very nodes the old code wrote the value into (its ``node_roles``), plus the pinned extras."""
    key = old_key(manifest)
    old = OLD[key]
    if old.get("builder"):
        return  # a chain builder writes its own graph
    old_roles = old.get("node_roles", {})
    roles = manifest["video"]["roles"]
    mapped = {name: {entry["node"] for entry in entries} for name, entries in manifest["bindings"]["comfyui"]["map"].items()}
    expected: dict[str, set[str]] = {}
    if old_roles.get("size_node"):
        width_node, height_node = (str(n) for n in old_roles["size_node"])
        expected["width"], expected["height"] = {width_node}, {height_node}
    for role, field in (("length", "length_nodes"), ("frame_rate", "frame_rate_nodes")):
        if old_roles.get(field):
            expected[role] = {str(n) for n in old_roles[field]}
    for role, field in (("image", "image_node"), ("image_strength", "image_strength_node")):
        if old_roles.get(field):
            expected[role] = {str(old_roles[field])}
    assert expected, f"{key}: nothing to compare"
    for role, nodes in expected.items():
        assert role in roles, f"{key}: {role} role missing"
        want = nodes | INTENDED_EXTRA_NODES.get((key, role), set())
        assert mapped[roles[role]] == want, f"{key} {role}: {mapped[roles[role]]} != {want}"
    if old.get("source_video_input"):
        assert roles["source"] == old["source_video_input"]


@pytest.mark.parametrize("path", [p for p in MANIFESTS if not OLD[old_key(load(p))].get("builder")], ids=lambda p: p.stem)
def test_each_role_input_drives_the_node_videos_yml_names(path: Path) -> None:
    check_role_nodes(load(path))


def test_the_node_check_sees_a_width_sent_to_the_height_node() -> None:
    manifest = load(T2V)
    manifest["bindings"]["comfyui"]["map"]["width"] = copy.deepcopy(manifest["bindings"]["comfyui"]["map"]["height"])
    with pytest.raises(AssertionError, match="width"):
        check_role_nodes(manifest)


def test_the_node_check_sees_a_dropped_legacy_target() -> None:
    manifest = load(T2V)
    manifest["bindings"]["comfyui"]["map"]["frame_rate"] = [{"node": "24", "input": "value"}]
    with pytest.raises(AssertionError, match="frame_rate"):
        check_role_nodes(manifest)


def test_the_node_check_sees_an_unpinned_extra_target() -> None:
    manifest = load(ROOT / "manifests" / "workflows" / "video-minimax-h3-t2v.yml")
    manifest["bindings"]["comfyui"]["map"]["frame_rate"].append({"node": "99", "input": "fps"})
    with pytest.raises(AssertionError, match="frame_rate"):
        check_role_nodes(manifest)


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.stem)
def test_every_bound_node_exists_in_the_graph(path: Path) -> None:
    manifest = load(path)
    graph = graph_nodes(manifest)
    for name, entries in manifest["bindings"]["comfyui"]["map"].items():
        for entry in entries:
            assert entry["node"] in graph, f"{name}: node {entry['node']} is not in the graph"
            assert entry["input"] in graph[entry["node"]]["inputs"], f"{name}: no input {entry['input']} on node {entry['node']}"


def test_the_stored_limits_of_the_size_inputs_agree_with_the_section() -> None:
    """A width or height input never allows more than the section's largest size."""
    for path in MANIFESTS:
        manifest = load(path)
        video = manifest["video"]
        max_w, max_h = (int(v) for v in video["size"]["max"].split("x"))
        for role, limit in (("width", max_w), ("height", max_h)):
            name = video.get("roles", {}).get(role)
            if name:
                assert manifest["inputs"][name]["maximum"] >= limit, f"{path.stem} {role}"


# ------------------------------------------------------------------------------------------------ the frame grid


LTX = GridSpec(fps=24, step=8, offset=1)
H3 = GridSpec(fps=24, step=17, offset=5)
SCAIL = GridSpec(fps=24, step=4, offset=1, max_frames=81)


def test_ltx_three_seconds_is_73_frames() -> None:
    assert frame_count(3, None, LTX) == 73
    assert 73 / 24 != 3  # the wire shows the adjusted duration


@pytest.mark.parametrize("seconds,expected", [(1, 25), (2, 49), (4, 97), (5, 121), (10, 241), (0.04, 9)])
def test_ltx_examples(seconds: float, expected: int) -> None:
    assert frame_count(seconds, None, LTX) == expected


@pytest.mark.parametrize("seconds,expected", [(2, 56), (5, 124), (10, 243)])
def test_h3_examples(seconds: float, expected: int) -> None:
    assert frame_count(seconds, None, H3) == expected


@pytest.mark.parametrize("seconds,expected", [(1, 25), (3, 73), (4, 81), (30, 81)])
def test_scail_is_capped_at_81(seconds: float, expected: int) -> None:
    assert frame_count(seconds, None, SCAIL) == expected


def test_the_frame_rate_of_the_request_replaces_the_default() -> None:
    assert frame_count(2, 30, LTX) == seconds_to_length(2, 30, step=8, offset=1) == 57


def test_min_frames_floors_the_length() -> None:
    assert frame_count(0.04, None, GridSpec(fps=24, step=8, offset=1, min_frames=33)) == 33


def test_banker_rounding_on_ties_is_the_old_rule() -> None:
    # 13 frames sit halfway between k=1 and k=2 (round(1.5) == 2); 21 frames sit between k=2 and k=3 (round(2.5) == 2).
    assert seconds_to_length(13 / 24, 24, step=8, offset=1) == 17
    assert seconds_to_length(21 / 24, 24, step=8, offset=1) == 17


def test_the_grid_equals_the_old_function_over_a_sweep() -> None:
    import proxy

    for step, offset in ((8, 1), (17, 5), (4, 1)):
        for fps in (24, 25, 30, 12.5):
            for tenth in range(1, 420):
                seconds = tenth / 10
                assert seconds_to_length(seconds, fps, step=step, offset=offset) == proxy._seconds_to_length(
                    seconds, fps, step=step, offset=offset), (seconds, fps, step, offset)


# ----------------------------------------------------------------------------------------------------- the size


LTX_SIZE = SizeSpec(default="960x512", max="1280x704", multiple=64)
H3_SIZE = SizeSpec(default="960x544", max="1344x768", multiple=32)


@pytest.mark.parametrize("requested,expected", [
    (None, (960, 512)), ("", (960, 512)),
    ("1280x720", (1280, 704)),        # clamped to the largest, then floored
    ("2000x2000", (1280, 704)),
    ("100x100", (64, 64)),            # floor of 64
    ("1000x600", (960, 576)),
    ("auto", (1024, 704)),            # the old reader takes anything it cannot parse as 1024x1024, then clamps
])
def test_ltx_size(requested: str | None, expected: tuple[int, int]) -> None:
    assert resolve_size(requested, LTX_SIZE) == expected


def test_h3_size() -> None:
    assert resolve_size("1920x1080", H3_SIZE) == (1344, 768)
    assert resolve_size(None, H3_SIZE) == (960, 544)


def test_the_size_equals_the_old_function_over_a_sweep() -> None:
    import proxy

    for spec in (LTX_SIZE, H3_SIZE, SizeSpec(default="832x448", max="832x448", multiple=32)):
        for requested in ("", "auto", "junk", "1x1", "63x63", "640x360", "1280x720", "4000x3000", "960x512", "833x449"):
            old = proxy._parse_size_clamped(requested or spec.default, spec.max, spec.multiple)
            assert resolve_size(requested, spec) == old, (requested, spec)


def test_the_used_size_string() -> None:
    assert f"{'x'.join(map(str, resolve_size('1280x720', LTX_SIZE)))}" == "1280x704"


# --------------------------------------------------------------------------------------------- the typed accessor


def entry_of(contract: dict) -> SimpleNamespace:
    return SimpleNamespace(contract=contract)


def test_video_facts_reads_the_typed_section() -> None:
    manifest = video_facts(entry_of(load(T2V)))
    assert isinstance(manifest, VideoManifest)
    assert manifest.size == SizeSpec(default="960x512", max="1280x704", multiple=64)
    assert manifest.grid == GridSpec(fps=24, step=8, offset=1)
    assert manifest.length.min_seconds == 1 and manifest.length.max_seconds == 10
    assert manifest.roles["width"] and manifest.variants["image"] == "video.generate.ltx-2.5-i2v"
    assert manifest.chain is False and manifest.select_subject is False


def test_video_facts_reads_a_plain_mapping_too() -> None:
    assert video_facts(load(T2V)) == video_facts(entry_of(load(T2V)))


def test_video_facts_is_none_for_a_manifest_without_the_section() -> None:
    bare = load(T2V)
    bare.pop("video")
    assert video_facts(entry_of(bare)) is None


def test_video_facts_reads_the_requires_and_flags() -> None:
    scail = video_facts(entry_of(load(ROOT / "manifests" / "workflows" / "video-scail-2-replace.yml")))
    assert scail.select_subject is True
    assert {"extensions", "source"} <= set(scail.requires)
    assert scail.grid.max_frames == 81 and scail.length.default_seconds == 4
    long = video_facts(entry_of(load(ROOT / "manifests" / "workflows" / "video-ltx-2.5-long.yml")))
    assert long.chain is True


def test_video_facts_does_not_alias_the_contract() -> None:
    contract = load(T2V)
    manifest = video_facts(entry_of(contract))
    original = copy.deepcopy(contract["video"])
    with pytest.raises((TypeError, AttributeError)):
        manifest.roles["width"] = "changed"  # type: ignore[index]
    assert contract["video"] == original


# ------------------------------------------------------- a length input covers every length the grid can compute


def spec_range(manifest: dict, role: str) -> tuple[float, float]:
    spec = manifest["inputs"][manifest["video"]["roles"][role]]
    return spec["minimum"], spec["maximum"]


@pytest.mark.parametrize("path", [p for p in MANIFESTS if not load(p)["video"].get("chain")], ids=lambda p: p.stem)
def test_the_length_input_fits_every_length_the_declared_ranges_can_compute(path: Path) -> None:
    manifest = load(path)
    video = manifest["video"]
    grid = GridSpec(**video["grid"])
    low, high = spec_range(manifest, "length")
    rate_role = video["roles"].get("frame_rate")
    rate_spec = manifest["inputs"][rate_role] if rate_role else None
    rates = range(rate_spec["minimum"], rate_spec["maximum"] + 1) if rate_spec else [None]
    step = manifest["inputs"][video["roles"]["length"]].get("step", 1)
    offset = manifest["inputs"][video["roles"]["length"]].get("offset", 0)
    for rate in rates:
        for seconds in (video["length"]["min_seconds"], video["length"]["max_seconds"]):
            length = frame_count(seconds, rate, grid)
            assert low <= length <= high, f"{seconds} s at {rate} fps computes {length}, outside {low}..{high}"
            assert (length - offset) % step == 0


@pytest.mark.parametrize("name,rate,expected", [
    ("video-ltx-2.5-t2v", 30, 297), ("video-ltx-2.5-t2v", 50, 497), ("video-ltx-2.5-i2v", 50, 497),
    ("video-ltx-2.5-keyframes", 50, 497), ("video-ltx-2.5-keyframes-mid", 50, 497),
    ("video-minimax-h3-t2v", 50, 498), ("video-minimax-h3-i2v", 50, 498),
    ("video-minimax-h3-keyframes", 50, 498), ("video-minimax-h3-keyframes-mid", 50, 498),
])
def test_ten_seconds_at_a_high_frame_rate_fits_the_length_input(name: str, rate: int, expected: int) -> None:
    manifest = load(ROOT / "manifests" / "workflows" / f"{name}.yml")
    length = frame_count(10, rate, GridSpec(**manifest["video"]["grid"]))
    assert length == expected
    assert spec_range(manifest, "length")[1] >= length


# --------------------------------------------------------------- a role needs an input of the right kind, used once


@pytest.mark.parametrize("role,target", [
    ("width", "prompt"), ("height", "prompt"), ("length", "negative_prompt"), ("frame_rate", "prompt"),
    ("image", "width"), ("image_strength", "prompt"),
])
def test_a_role_mapped_to_an_input_of_the_wrong_kind_is_refused(role: str, target: str) -> None:
    doc = load(ROOT / "manifests" / "workflows" / "video-ltx-2.5-i2v.yml")
    doc["video"]["roles"][role] = target
    assert (("video", "roles", role), "role_input_type_mismatch") in codes(doc)


@pytest.mark.parametrize("first,second", [("width", "height"), ("height", "length"), ("image", "image_strength")])
def test_two_roles_sharing_one_input_are_refused(first: str, second: str) -> None:
    doc = load(ROOT / "manifests" / "workflows" / "video-ltx-2.5-i2v.yml")
    doc["video"]["roles"][first] = doc["video"]["roles"][second]
    found = codes(doc)
    assert any(path[:2] == ("video", "roles") and code == "role_input_shared" for path, code in found), found


def test_the_shipped_roles_have_the_right_kinds_and_are_distinct() -> None:
    for path in MANIFESTS:
        found = check_contract(load(path)).errors
        assert not [e for e in found if e.code in ("role_input_type_mismatch", "role_input_shared")], path.stem


def test_the_role_kinds_are_schema_metadata_not_code() -> None:
    import gateway.workflow_contract as contract

    text = Path(contract.__file__).read_text(encoding="utf-8")
    assert '"width"' not in text and '"prompt"' not in text


# -------------------------------------------------------------------------- numbers in a section must be finite


def with_text_replaced(old: str, new: str) -> dict:
    text = T2V.read_text(encoding="utf-8")
    assert old in text
    return yaml.safe_load(text.replace(old, new, 1))


@pytest.mark.parametrize("old,new,path", [
    ("grid: {fps: 24,", "grid: {fps: .nan,", ("video", "grid", "fps")),
    ("length: {min_seconds: 1,", "length: {min_seconds: .nan,", ("video", "length", "min_seconds")),
    ("max_seconds: 10}", "max_seconds: .nan}", ("video", "length", "max_seconds")),
    ("grid: {fps: 24,", "grid: {fps: .inf,", ("video", "grid", "fps")),
])
def test_a_non_finite_number_in_the_section_is_refused(old: str, new: str, path: tuple) -> None:
    assert (path, "number_not_finite") in codes(with_text_replaced(old, new))


def test_a_finite_section_raises_no_finiteness_error() -> None:
    assert not [c for _, c in codes(load(T2V)) if c == "number_not_finite"]
