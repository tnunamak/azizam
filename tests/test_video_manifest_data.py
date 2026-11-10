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
    GridSpec, SizeSpec, VideoManifest, check_video_variants, frame_count, parse_size, resolve_size, seconds_to_length, video_facts)
from gateway.workflow_contract import check_contract

ROOT = Path(__file__).resolve().parent.parent
# Only the generate manifests: an edit manifest (video-mux puts a sound on a video) has no frame grid, no size and no
# videos.yml entry, so it holds no ``video`` section.
MANIFESTS = sorted(path for path in (ROOT / "manifests" / "workflows").glob("video-*.yml")
                   if yaml.safe_load(path.read_text(encoding="utf-8"))["intent"] == "video.generate")
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



# ------------------------------------------------------------------------------------------- cross-registry variants


@pytest.fixture
def reference_variant_records(monkeypatch) -> tuple[SimpleNamespace, ...]:
    from gateway import workflow_request_maps

    document = {"schema_version": 1, "maps": [
        {"select": {"intent": "fixture.generate", "mode": "async"},
         "inputs": {"guide_frame": {"in": "body", "field": "input_reference"}},
         "default": {"in": "extension"}},
        {"select": {"any": True}, "default": {"in": "extension"}},
    ]}
    # The execution table is required; the bundled one serves, since no fixture intent has a row.
    bundled = (ROOT / "registry" / "workflow_request_maps.yml").read_text(encoding="utf-8")
    document["execution"] = yaml.safe_load(bundled)["execution"]
    with monkeypatch.context() as context:
        context.setattr(Path, "read_text", lambda *_args, **_kwargs: json.dumps(document))
        maps = workflow_request_maps.load(Path("variant-reference-fixture.yml"))
    monkeypatch.setattr(workflow_request_maps, "_MAPS", maps)

    def entry(name: str, *, role: str | None = None) -> SimpleNamespace:
        manifest = section()
        inputs = {}
        if role is not None:
            manifest["roles"][role] = "guide_frame"
            inputs["guide_frame"] = {"type": "asset", "accepts": ["image/png"]}
        # Runtime entries need not have an intent attribute. Read the effective contract, not a stale manifest.
        return SimpleNamespace(id=f"fixture.generate.{name}", contract={
            "intent": "fixture.generate", "capability": "clip", "clip": manifest,
            "inputs": inputs, "bindings": {},
        })

    owner, first, second = entry("base"), entry("first", role="guide"), entry("second", role="anchor")
    owner.contract["clip"]["variants"] = {"guide": first.id, "anchor": second.id}
    return owner, first, second


def test_registry_check_rejects_ambiguous_reference_variants(reference_variant_records) -> None:
    owner, first, second = reference_variant_records
    edges = owner.contract["clip"]["variants"]
    expected = f"{owner.id}: multiple variants accept the reference: {sorted([first.id, second.id])!r}"
    for ordered in (list(edges.items()), list(reversed(edges.items()))):
        owner.contract["clip"]["variants"] = dict(ordered)
        with pytest.raises(ValueError) as caught:
            check_video_variants(iter(reference_variant_records))
        assert str(caught.value) == expected


def test_registry_check_uses_the_variant_role_not_just_reference_acceptance(reference_variant_records) -> None:
    owner, first, second = reference_variant_records
    second.contract["clip"]["roles"]["anchor"] = "length"
    check_video_variants(reference_variant_records)
    # The selector also accepts a role whose name is the contract input, with no explicit role mapping.
    owner.contract["clip"]["variants"] = {"guide_frame": first.id, "anchor": second.id}
    first.contract["clip"]["roles"].pop("guide")
    check_video_variants(reference_variant_records)



@pytest.mark.parametrize("replace", [False, True])
def test_add_manifest_rejects_ambiguous_variants_without_changing_contents(reference_variant_records, replace) -> None:
    from gateway.discovery import RegistryLoadError, WorkflowRegistry

    owner, first, second = reference_variant_records

    def document(record):
        # The runtime fixture records carry only what variant selection reads; a manifest also needs outputs.
        return {"id": record.id, "version": "1", "schema_version": 2,
                "outputs": {"video": {"media_type": "video/mp4"}}, **copy.deepcopy(record.contract)}

    registry = WorkflowRegistry()
    registry.add_manifest(document(owner))
    registry.add_manifest(document(first))
    if replace:
        previous = document(second)
        previous["clip"]["roles"]["anchor"] = "length"
        registry.add_manifest(previous)
    records, errors = registry.contents()
    errors.append(RegistryLoadError(Path("existing.yml"), "existing load error"))
    before_records, before_errors = dict(records), list(errors)

    with pytest.raises(ValueError) as caught:
        registry.add_manifest(document(second), replace=replace)

    assert str(caught.value) == (
        f"{owner.id}: multiple variants accept the reference: {sorted([first.id, second.id])!r}")
    after_records, after_errors = registry.contents()
    assert after_records is records and after_records == before_records
    assert after_errors is errors and after_errors == before_errors
    assert all(after_records[key] is value for key, value in before_records.items())


def test_adopt_rejects_ambiguous_variants_without_changing_contents(reference_variant_records) -> None:
    from gateway.discovery import RegistryLoadError, WorkflowRegistry

    owner, first, second = reference_variant_records
    registry = WorkflowRegistry()
    # Deliberately bypass add_manifest to exercise adoption of an unchecked incoming registry.
    old_records = {first.id: first}
    old_errors = [RegistryLoadError(Path("old.yml"), "old load error")]
    registry.restore((old_records, old_errors))
    incoming = WorkflowRegistry()
    incoming_records = {record.id: record for record in reference_variant_records}
    incoming_errors = [RegistryLoadError(Path("incoming.yml"), "incoming load error")]
    incoming.restore((incoming_records, incoming_errors))

    with pytest.raises(ValueError) as caught:
        registry.adopt(incoming)

    assert str(caught.value) == (
        f"{owner.id}: multiple variants accept the reference: {sorted([first.id, second.id])!r}")
    records, errors = registry.contents()
    assert records is old_records and records == {first.id: first}
    assert errors is old_errors and errors == [RegistryLoadError(Path("old.yml"), "old load error")]
    assert incoming.contents()[0] is incoming_records and incoming.contents()[1] is incoming_errors


def test_shipped_registry_has_unambiguous_reference_variants() -> None:
    from gateway.discovery import WorkflowRegistry

    registry = WorkflowRegistry.from_directory(ROOT / "manifests" / "workflows")
    assert registry.errors == []
    records = tuple(registry.records())
    assert any(facts.variants for record in records if (facts := video_facts(record)) is not None)
    check_video_variants(records)


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


def test_two_roles_naming_one_workflow_are_not_ambiguous(reference_variant_records) -> None:
    owner, first, second = reference_variant_records
    first.contract["clip"]["roles"]["anchor"] = "guide_frame"
    owner.contract["clip"]["variants"] = {"guide": first.id, "anchor": first.id}
    check_video_variants(reference_variant_records)


def test_registry_load_reports_ambiguous_variants_as_a_load_error(reference_variant_records, monkeypatch) -> None:
    from gateway import discovery

    registry = discovery.WorkflowRegistry()
    monkeypatch.setattr(registry, "records", lambda: list(reference_variant_records))
    monkeypatch.setattr(discovery, "_iter_manifest_files", lambda _root: [])
    registry.load_directory(ROOT / "manifests" / "workflows", policy={})
    assert [error.source_path for error in registry.errors] == [ROOT / "manifests" / "workflows"]
    assert [error.message for error in registry.errors] == [
        f"{reference_variant_records[0].id}: multiple variants accept the reference: "
        f"{sorted([reference_variant_records[1].id, reference_variant_records[2].id])!r}"
    ]


def test_reference_variants_skip_missing_workflows_and_video_facts():
    from gateway.video_grid import reference_variants

    eligible = SimpleNamespace(id="eligible", contract={
        "capability": "clip", "clip": {"roles": {"guide": "guide_frame"}},
    })
    without_facts = SimpleNamespace(id="without-facts", contract={"capability": "clip"})
    candidates = {"eligible": eligible, "without-facts": without_facts}
    manifest = VideoManifest(variants={
        "missing": "missing-workflow", "bare": "without-facts", "guide": "eligible",
    })
    looked_up, referenced = [], []

    def get_workflow(workflow_id):
        looked_up.append(workflow_id)
        return candidates.get(workflow_id)

    def reference_input(candidate):
        referenced.append(candidate)
        assert candidate is eligible
        return "guide_frame"

    result = reference_variants(manifest, get_workflow, reference_input)
    assert looked_up == ["missing-workflow", "without-facts", "eligible"]
    assert referenced == [eligible]
    assert len(result) == 1
    assert result[0][0] is eligible
    assert result[0][1] == VideoManifest(roles={"guide": "guide_frame"})



def test_a_directory_file_that_brings_a_variant_conflict_is_refused_and_absent(monkeypatch, tmp_path) -> None:
    """One check per directory is the fast path; on a conflict the load refuses the file that brings it, as one add does."""
    import copy

    from gateway import discovery
    from gateway.manifests import load_workflow_manifest

    shipped = load_workflow_manifest(ROOT / "manifests" / "workflows" / "ace-step-1.5-xl-turbo.yml")
    files = {tmp_path / f"{name}.yml": {**copy.deepcopy(shipped), "id": f"{shipped['id']}-{name}"} for name in ("a", "b", "c")}
    late = f"{shipped['id']}-c"

    def check(records) -> None:
        ids = {record.id for record in records}
        if {f"{shipped['id']}-b", late} <= ids:
            raise ValueError(f"{late}: claims the reference of {shipped['id']}-b")

    monkeypatch.setattr(discovery, "_iter_manifest_files", lambda _root: list(files))
    monkeypatch.setattr(discovery, "load_workflow_manifest", lambda path: copy.deepcopy(files[path]))
    monkeypatch.setattr(discovery.video_grid, "check_video_variants", check)
    registry = discovery.WorkflowRegistry()
    registry.load_directory(tmp_path, policy={})
    assert registry.ids() == [f"{shipped['id']}-a", f"{shipped['id']}-b"]
    assert [(error.source_path, error.message) for error in registry.errors] == [
        (tmp_path / "c.yml", f"{late}: claims the reference of {shipped['id']}-b")]


def test_a_later_file_with_the_same_id_takes_the_place_of_a_refused_one(monkeypatch, tmp_path) -> None:
    """A file refused for a variant conflict does not keep a later file with its id out, as adding each file in order."""
    import copy

    from gateway import discovery
    from gateway.manifests import load_workflow_manifest

    shipped = load_workflow_manifest(ROOT / "manifests" / "workflows" / "ace-step-1.5-xl-turbo.yml")
    usable = {**copy.deepcopy(shipped), "id": f"{shipped['id']}-x"}
    files = {tmp_path / "a.yml": {**copy.deepcopy(usable), "version": "conflicting"}, tmp_path / "b.yml": usable}

    def check(records) -> None:
        if any(record.manifest.get("version") == "conflicting" for record in records):
            raise ValueError(f"{usable['id']}: claims a taken reference")

    monkeypatch.setattr(discovery, "_iter_manifest_files", lambda _root: list(files))
    monkeypatch.setattr(discovery, "load_workflow_manifest", lambda path: copy.deepcopy(files[path]))
    monkeypatch.setattr(discovery.video_grid, "check_video_variants", check)
    registry = discovery.WorkflowRegistry()
    registry.load_directory(tmp_path, policy={})
    assert [(record.id, record.source_path) for record in registry.records()] == [(usable["id"], tmp_path / "b.yml")]
    assert [(error.source_path, error.message) for error in registry.errors] == [
        (tmp_path / "a.yml", f"{usable['id']}: claims a taken reference")]
