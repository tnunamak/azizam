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
    GridSpec, SizeSpec, VideoManifest, frame_count, parse_size, resolve_size, seconds_to_length, video_facts)
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


@pytest.mark.parametrize("path", MANIFESTS, ids=lambda p: p.stem)
def test_each_role_input_drives_the_node_videos_yml_names(path: Path) -> None:
    """A role input is bound to the very nodes the old code wrote the value into."""
    manifest = load(path)
    old = OLD[old_key(manifest)]
    roles = manifest["video"].get("roles", {})
    mapped = {name: {entry["node"] for entry in entries} for name, entries in manifest["bindings"]["comfyui"]["map"].items()}
    roles_to_old = {
        "length": set(map(str, old.get("length_nodes", ()))),
        "frame_rate": set(map(str, old.get("frame_rate_nodes", ()))),
    }
    sizes = [str(n) for n in old.get("size_node", ())]
    if sizes:
        roles_to_old["width"], roles_to_old["height"] = {sizes[0]}, {sizes[1]}
    for role, nodes in roles_to_old.items():
        if not nodes or old.get("builder"):
            continue
        assert role in roles, f"{role} role missing"
        assert mapped[roles[role]] == nodes, f"{role}: {mapped[roles[role]]} != {nodes}"
    if old.get("image_node"):
        assert mapped[roles["image"]] == {str(old["image_node"])}
    if old.get("image_strength_node"):
        assert mapped[roles["image_strength"]] == {str(old["image_strength_node"])}
    if old.get("source_video_input"):
        assert roles["source"] == old["source_video_input"]


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


def test_the_grid_sweep() -> None:
    for step, offset in ((8, 1), (17, 5), (4, 1)):
        for fps in (24, 25, 30, 12.5):
            for tenth in range(1, 420):
                seconds = tenth / 10
                frames = max(1, int(round(seconds * fps)))
                k = max(1, int(round((frames - offset) / float(step))))
                assert seconds_to_length(seconds, fps, step=step, offset=offset) == k * step + offset, (seconds, fps, step, offset)


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


def test_the_size_sweep() -> None:
    for spec in (LTX_SIZE, H3_SIZE, SizeSpec(default="832x448", max="832x448", multiple=32)):
        for requested in ("", "auto", "junk", "1x1", "63x63", "640x360", "1280x720", "4000x3000", "960x512", "833x449"):
            width, height = resolve_size(requested, spec)
            assert width % spec.multiple == 0 and height % spec.multiple == 0 and min(width, height) >= 64, (requested, spec)
            assert width <= parse_size(spec.max)[0] and height <= parse_size(spec.max)[1], (requested, spec)


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


# Deletion witnesses for schema clauses and video semantic comparisons.
# Assert the exact diagnostic so another rule cannot mask the missing one.

_ROLES = (
    "width", "height", "length", "frame_rate",
    "image", "image_strength", "source", "last_image",
)


def _video_set(video, path, value):
    if not path:
        return copy.deepcopy(value)
    target = video
    for key in path[:-1]:
        target = target.setdefault(key, {})
    target[path[-1]] = copy.deepcopy(value)
    return video


def _case_id(path):
    return ".".join(path) or "video"


_TYPE_CASES = [
    # Kills type=object at video and each nested object.
    *[(p, [], "type_mismatch") for p in
      [(), ("roles",), ("size",), ("length",), ("grid",), ("variants",)]],
    # Kills type=string for each roles.<role>.
    *[(("roles", role), 7, "type_mismatch") for role in _ROLES],
    # Kills type=string at size.default; its public diagnostic is default_invalid.
    (("size", "default"), 7, "default_invalid"),
    # Kills type=string at size.max.
    (("size", "max"), 7, "type_mismatch"),
    # Kills type=integer for size.multiple and each integer grid field.
    *[(p, 1.5, "type_mismatch") for p in
      [("size", "multiple"), ("grid", "step"), ("grid", "offset"),
       ("grid", "min_frames"), ("grid", "max_frames")]],
    # Kills type=number for every length field and grid.fps.
    *[(p, "5", "type_mismatch") for p in
      [("length", "min_seconds"), ("length", "max_seconds"),
       ("length", "default_seconds"), ("grid", "fps")]],
    # Kills type=string for each variant id.
    *[(("variants", key), 7, "type_mismatch") for key in ("image", "long")],
    # Kills type=array for requires.
    (("requires",), "image", "type_mismatch"),
    # Kills type=boolean for each flag.
    *[((key,), "true", "type_mismatch") for key in ("select_subject", "chain")],
]


@pytest.mark.parametrize(
    "path,value,code",
    [pytest.param(*case, id=_case_id(case[0])) for case in _TYPE_CASES],
)
def test_uncovered_video_schema_types(path, value, code):
    video = _video_set(section(), path, value)
    assert (("video", *path), code) in codes(doc_with(video))


@pytest.mark.parametrize("group", ["roles", "size", "length", "grid", "variants"])
def test_nested_video_objects_are_closed(group):
    # Kills additionalProperties=false for this nested object.
    video = _video_set(section(), (group, "unlisted"), "width")
    assert (("video", group, "unlisted"), "unknown_field") in codes(doc_with(video))


@pytest.mark.parametrize("missing", [
    # Kills each required member of the top-level video section.
    ("size",), ("length",), ("grid",),
    # Kills the remaining required grid member.
    ("grid", "offset"),
])
def test_remaining_video_required_fields(missing):
    video = section()
    target = video
    for key in missing[:-1]:
        target = target[key]
    del target[missing[-1]]
    assert (("video", *missing), "required_missing") in codes(doc_with(video))


@pytest.mark.parametrize("chain", [None, False], ids=["absent", "false"])
@pytest.mark.parametrize("missing", [None, "width", "height", "length"])
def test_non_chain_condition_requires_roles(chain, missing):
    # Kills non-chain roles/role requirements and the first chain=true guard.
    video = section()
    if chain is not None:
        video["chain"] = chain
    if missing is None:
        del video["roles"]
        path = ("video", "roles")
    else:
        del video["roles"][missing]
        path = ("video", "roles", missing)
    assert (path, "required_missing") in codes(doc_with(video))


@pytest.mark.parametrize("role", ["width", "height"])
def test_chain_roles_if_present_require_dimensions(role):
    # Kills the chain branch's required width or height.
    video = section()
    video["chain"] = True
    del video["roles"][role]
    assert (("video", "roles", role), "required_missing") in codes(doc_with(video))


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("bad", ["", "Upper", "a-b", "a" * 65],
                         ids=["empty", "initial-case", "alphabet", "length"])
def test_video_role_name_patterns(role, bad):
    # Kills this role's pattern, including its 1..64 length and naming bounds.
    video = _video_set(section(), ("roles", role), bad)
    assert (("video", "roles", role), "pattern_mismatch") in codes(doc_with(video))


@pytest.mark.parametrize("key", ["image", "long"])
@pytest.mark.parametrize("bad", ["", "Upper", "a/b", "a" * 129],
                         ids=["empty", "initial-case", "alphabet", "length"])
def test_video_variant_id_patterns(key, bad):
    # Kills this variant's pattern, including its 1..128 length and naming bounds.
    video = _video_set(section(), ("variants", key), bad)
    assert (("video", "variants", key), "pattern_mismatch") in codes(doc_with(video))


@pytest.mark.parametrize("key", ["default", "max"])
@pytest.mark.parametrize("bad", [
    "0x512", "960x0", "100000x1", "1x100000", "01x64", "64x01",
])
def test_size_pattern_numeric_boundaries(key, bad):
    # Kills the positive, non-zero-prefixed 1..5-digit bounds on both dimensions.
    video = _video_set(section(), ("size", key), bad)
    assert (("video", "size", key), "pattern_mismatch") in codes(doc_with(video))


_BOUND_CASES = [
    # Kills size.multiple.maximum=1024.
    (("size", "multiple"), 1025),
    # Kills exclusiveMinimum=0 and maximum=86400 for each seconds field.
    *[(("length", key), value)
      for key in ("min_seconds", "max_seconds", "default_seconds")
      for value in (0, 86401)],
    # Kills grid.fps.maximum=1000.
    (("grid", "fps"), 1001),
    # Kills grid.step.maximum=1024.
    (("grid", "step"), 1025),
    # Kills grid.offset.maximum=1024.
    (("grid", "offset"), 1025),
    # Kills each frame bound's minimum=1 and maximum=100000.
    *[(("grid", key), value)
      for key in ("min_frames", "max_frames")
      for value in (0, 100001)],
]


@pytest.mark.parametrize(
    "path,value",
    [pytest.param(*case, id=f"{_case_id(case[0])}={case[1]}")
     for case in _BOUND_CASES],
)
def test_remaining_video_numeric_bounds(path, value):
    video = _video_set(section(), path, value)
    # Exact code avoids a semantic bounds/default error masking deletion.
    assert (("video", *path), "limit_exceeded") in codes(doc_with(video))


@pytest.mark.parametrize("value", [["image", "image"], ["source", "source"],
                                  ["extensions", "extensions"]])
def test_requires_is_unique(value):
    # Kills requires.uniqueItems=true without breaking maxItems or the enum.
    video = _video_set(section(), ("requires",), value)
    assert (("video", "requires"), "limit_exceeded") in codes(doc_with(video))


@pytest.mark.parametrize("path,expected", [
    # Kills redundant maxItems=3: enum cardinality plus uniqueness implies it.
    (("properties", "requires", "maxItems"), 3),
    # Kills the second if's const: the non-chain branch otherwise masks deletion.
    (("allOf", 1, "if", "properties", "chain", "const"), True),
    # Kills the second if's required: the non-chain branch otherwise masks deletion.
    (("allOf", 1, "if", "required"), ["chain"]),
])
def test_redundant_video_schema_clauses_are_present(path, expected):
    # Structural assertions, not claims of observable refusal differences.
    value = json.loads(
        (ROOT / "registry" / "workflow_sections" / "video.schema.json")
        .read_text(encoding="utf-8")
    )
    for key in path:
        value = value[key]
    assert value == expected


@pytest.mark.parametrize("role", _ROLES)
@pytest.mark.parametrize("declared", [False, True], ids=["undeclared", "unbound"])
def test_every_video_role_is_declared_and_bound(role, declared):
    # Kills skipping this role in the declared-input/binding semantic checks.
    name = "new_role_input"
    video = _video_set(section(), ("roles", role), name)
    inputs = {name: {"type": "integer", "minimum": 1}} if declared else {}
    doc = doc_with(video, inputs=inputs)
    code = "role_input_unbound" if declared else "role_input_missing"
    assert (("video", "roles", role), code) in codes(doc)


@pytest.mark.parametrize("path,value,code", [
    # Kills only the lower-side default_seconds comparison.
    (("length", "default_seconds"), 0.5, "default_invalid"),
    # Kills only the width side of default_over_max.
    (("size", "default"), "1281x512", "default_over_max"),
    # Kills only the height side of default_over_max.
    (("size", "default"), "960x705", "default_over_max"),
])
def test_semantic_comparisons_have_independent_witnesses(path, value, code):
    video = _video_set(section(), path, value)
    assert (("video", *path), code) in codes(doc_with(video))


@pytest.mark.parametrize("path,value,code", [
    # Kills running video semantics despite a structural numeric-bound error.
    (("grid", "fps"), 0, "limit_exceeded"),
    # Kills running video semantics despite a structural type error.
    (("size", "multiple"), "64", "type_mismatch"),
])
def test_broken_video_structure_suppresses_semantic_cascades(path, value, code):
    video = _video_set(section(), path, value)
    video["roles"]["width"] = "not_an_input"
    found = codes(doc_with(video))
    assert (("video", *path), code) in found
    assert (("video", "roles", "width"), "role_input_missing") not in found
