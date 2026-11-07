"""The equality check of the schema 1 to 2 migration (CODE_TO_DATA_S3, S3.5a): what it compares, and that it can fail.

The check's positive control is here: swapping the ``steps`` and ``seam_steps`` targets of the tile composition changes no
projection and no template request, and the binding-content check must still fail. The other controls change one thing each
that another comparison must see (a template request, the multipart form, a policy fact in a service record).
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

from gateway import workflow_equality as eq

ROOT = Path(__file__).resolve().parents[1]
V1 = ROOT / "tests" / "fixtures" / "workflows_v1"
TILE = "image.generate.flux2-klein-4b-seamless-tile"
TILE_FILE = "flux2-klein-4b-seamless-tile.yml"


def make_tree(base: Path, *, workflows: Path | None = None) -> Path:
    """A tree that shares the work tree's code and data and holds its own copy of the workflow files."""
    base.mkdir(parents=True)
    for name in ("gateway", "workflows", "proxy.py"):
        (base / name).symlink_to(ROOT / name)
    (base / "registry").mkdir()
    for entry in (ROOT / "registry").iterdir():
        (base / "registry" / entry.name).symlink_to(entry)
    (base / "manifests").mkdir()
    shutil.copytree(workflows or ROOT / "manifests" / "workflows", base / "manifests" / "workflows")
    shutil.copytree(ROOT / "extensions", base / "extensions")
    return base


def edit(tree: Path, name: str, change) -> None:
    path = tree / "manifests" / "workflows" / name
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def swap_tile_targets(document: dict) -> None:
    table = document["bindings"]["gateway"]["map"]
    table["steps"], table["seam_steps"] = table["seam_steps"], table["steps"]


@pytest.fixture(scope="module")
def trees(tmp_path_factory):
    base = tmp_path_factory.mktemp("trees")
    return {"before_v1": make_tree(base / "v1", workflows=V1), "same": make_tree(base / "same")}


def run(before: Path, after: Path, tmp_path: Path, checks=eq.CHECKS):
    return eq.diff_effective(before, after, checks=checks, scratch=tmp_path)


def paths(result: eq.Result) -> list[tuple[str, str, str]]:
    return [(d.check, d.workflow, d.path) for d in result.unexplained]


# ---------------------------------------------------------------------------------------------------- binding content


def test_the_bundled_bindings_are_the_converters_edits_of_the_old_ones_and_the_named_edits(trees, tmp_path):
    result = run(trees["before_v1"], trees["same"], tmp_path, checks=("binding_content",))
    assert result.ok, [d.line() for d in result.unexplained]
    assert result.compared == {"binding_content": 39}  # 38 bundled workflows and the demo package's


def test_control_a_swap_of_two_composition_targets_fails_the_check(trees, tmp_path_factory):
    swapped = make_tree(tmp_path_factory.mktemp("swapped") / "tree")
    edit(swapped, TILE_FILE, swap_tile_targets)
    result = run(trees["before_v1"], swapped, tmp_path_factory.mktemp("scratch"), checks=("binding_content",))
    assert not result.ok
    assert paths(result) == [
        ("binding_content", TILE, "/gateway/map/seam_steps[0]/path"),
        ("binding_content", TILE, "/gateway/map/steps[0]/path"),
    ]


def test_control_the_swap_changes_no_compiled_request_because_a_composition_has_no_template(trees, tmp_path_factory):
    """Why the binding-content check exists: the request comparison cannot see this change."""
    swapped = make_tree(tmp_path_factory.mktemp("swapped") / "tree")
    edit(swapped, TILE_FILE, swap_tile_targets)
    result = run(trees["same"], swapped, tmp_path_factory.mktemp("scratch"), checks=("requests",))
    assert result.ok and not result.explained


def test_a_named_edit_removes_one_key_and_a_schema_two_before_side_has_had_it_already():
    edit_ = ({"workflow": "w", "path": ["a", "map", "y"], "operation": "remove"},)
    assert eq.apply_binding_edits("w", {"a": {"map": {"x": 1, "y": 2}}}, edit_) == {"a": {"map": {"x": 1}}}
    assert eq.apply_binding_edits("other", {"a": {"map": {"y": 2}}}, edit_) == {"a": {"map": {"y": 2}}}
    before = {"w": {"schema_version": 2, "bindings": {"a": {"map": {"x": 1}}}}}
    named = eq.Allowed(rules=(), binding_edits=edit_)
    assert eq.binding_content_differences(before, copy.deepcopy(before), named) == []
    after = {"w": {"schema_version": 2, "bindings": {"a": {"map": {"x": 1, "y": 2}}}}}
    assert [(d.path, d.after) for d in eq.binding_content_differences(before, after, named)] == [("/a/map/y", 2)]


def test_a_workflow_that_is_missing_on_one_side_is_a_difference():
    found = eq.binding_content_differences({"w": {"bindings": {}}}, {}, eq.Allowed(rules=(), binding_edits=()))
    assert [(d.workflow, d.after) for d in found] == [("w", "<absent>")]


# --------------------------------------------------------------------------------------- projections and requests


def test_a_tree_equals_itself_in_every_check(trees, tmp_path):
    result = run(trees["same"], trees["same"], tmp_path)
    assert result.ok and result.explained == []
    assert result.compared["projections"] > 300 and result.compared["requests"] > 300
    assert result.compared["services"] >= 1 and result.compared["protocols"] >= 1


def test_control_a_changed_target_in_a_template_workflow_changes_the_compiled_request(trees, tmp_path_factory):
    changed = make_tree(tmp_path_factory.mktemp("changed") / "tree")

    def retarget(document: dict) -> None:
        mapping = document["bindings"]["comfyui"]["map"]["steps"][0]
        mapping["input"] = "not_steps"

    edit(changed, "flux2-klein.yml", retarget)
    result = run(trees["same"], changed, tmp_path_factory.mktemp("scratch"), checks=("requests",))
    assert not result.ok
    assert {d.workflow for d in result.unexplained} == {"image.generate.flux2-klein"}
    assert all(d.path.startswith("/requests") for d in result.unexplained)


def test_control_a_changed_form_field_changes_the_multipart_request(trees, tmp_path_factory):
    changed = make_tree(tmp_path_factory.mktemp("changed") / "tree")
    edit(changed, "mesh.generate.trellis-2.yml", lambda d: d["bindings"]["http-multipart"]["map"].__setitem__("seed", "other"))
    result = run(trees["same"], changed, tmp_path_factory.mktemp("scratch"), checks=("requests",))
    assert not result.ok
    assert {(d.workflow, d.path.split("[")[0]) for d in result.unexplained} == {("mesh.generate.trellis-2", "/form")}


def test_control_a_changed_service_record_is_a_difference(trees, tmp_path_factory):
    changed = make_tree(tmp_path_factory.mktemp("changed") / "tree")
    (changed / "registry" / "services.yml").unlink()
    document = yaml.safe_load((ROOT / "registry" / "services.yml").read_text(encoding="utf-8"))
    document["records"][0]["probe"] = "changed"
    (changed / "registry" / "services.yml").write_text(yaml.safe_dump(document), encoding="utf-8")
    result = run(trees["same"], changed, tmp_path_factory.mktemp("scratch"), checks=("services",))
    assert paths(result) == [("services", document["records"][0]["id"], "/probe")]


def test_control_a_projection_change_is_seen(trees, tmp_path_factory):
    changed = make_tree(tmp_path_factory.mktemp("changed") / "tree")
    edit(changed, "flux2-klein.yml", lambda d: d["inputs"]["steps"].__setitem__("maximum", 99))
    result = run(trees["same"], changed, tmp_path_factory.mktemp("scratch"), checks=("projections",))
    assert not result.ok and {d.workflow for d in result.unexplained} == {"image.generate.flux2-klein"}


# ---------------------------------------------------------------------------------------------------------- pieces


def test_walk_names_each_leaf_that_differs():
    left = {"a": [1, {"b": 2}], "c": 3, "d": [1]}
    right = {"a": [1, {"b": 5}], "e": 3, "d": [1, 2]}
    assert eq.walk(left, right) == [("/a[1]/b", 2, 5), ("/c", 3, "<absent>"), ("/d", [1], [1, 2]), ("/e", "<absent>", 3)]
    assert eq.walk({"a": True}, {"a": 1}) == [("/a", True, 1)]


def test_a_glob_treats_brackets_as_text_and_a_star_as_any_run():
    assert eq.glob("/workflows[]/example_request/*", "/workflows[]/example_request/curl")
    assert not eq.glob("/workflows[]/x", "/workflowsx")
    assert not eq.glob("/a/*", "/b/a/c")


def test_a_rule_names_check_scope_workflow_path_and_before_value():
    rule = eq.Rule(checks=("projections",), scopes=("summary:*",), workflows=("w",), paths=("/a[]/b",), reason="r", before=None, has_before=True)
    base = dict(check="projections", workflow="w", path="/a[3]/b", before=None, after=1, scope="summary:client")
    assert rule.covers(eq.Difference(**base), "summary:client")
    assert not rule.covers(eq.Difference(**{**base, "workflow": "x"}), "summary:client")
    assert not rule.covers(eq.Difference(**{**base, "before": 0}), "summary:client")
    assert not rule.covers(eq.Difference(**base), "capabilities")
    assert not rule.covers(eq.Difference(**{**base, "check": "requests"}), "summary:client")
    assert not rule.covers(eq.Difference(**{**base, "path": "/a[3]/c"}), "summary:client")


def test_every_allowed_row_has_a_reason_and_the_list_is_short(tmp_path):
    allowed = eq.load_allowed()
    assert 0 < len(allowed.rules) <= 40 and all(rule.reason for rule in allowed.rules)
    bad = tmp_path / "bad.yml"
    bad.write_text(yaml.safe_dump({"allowed": [{"checks": ["projections"], "paths": ["/x"]}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="reason"):
        eq.load_allowed(bad)


def test_the_corpus_holds_the_callers_values_and_the_preparation_adds_defaults_and_generated_values():
    documents = eq.read_workflows(ROOT)
    document = documents["image.generate.flux2-klein"]
    corpus = eq.build_corpus({"image.generate.flux2-klein": document})["image.generate.flux2-klein"]
    names = [case["name"] for case in corpus]
    assert names[:2] == ["empty", "required"] and "minimum:steps" in names and "maximum:steps" in names
    assert all("seed" not in case["values"] for case in corpus)  # a generated value is not the caller's
    assert corpus[1]["values"]  # the required inputs are supplied with a valid value
    prepared = eq.prepare_corpus({"image.generate.flux2-klein": document}, {"image.generate.flux2-klein": corpus})["image.generate.flux2-klein"]
    assert all(isinstance(case["values"]["seed"], int) for case in prepared)
    assert eq.build_corpus(documents) == eq.build_corpus(copy.deepcopy(documents))
    assert eq.prepare_corpus(documents, eq.build_corpus(documents)) == eq.prepare_corpus(documents, eq.build_corpus(documents))


def test_a_schema_one_document_gets_the_same_corpus_as_its_conversion():
    v1 = yaml.safe_load((V1 / "flux2-klein-edit.yml").read_text(encoding="utf-8"))
    prepared = eq.prepare_corpus({"w": v1}, eq.build_corpus({"w": v1}))["w"]
    assert all("seed" in case["values"] for case in prepared)  # the converter declared the seed the file only mapped


# ----------------------------------------------------------------------------------- the review's six findings


def tree_without_workflows(base: Path) -> Path:
    empty = base / "empty-workflows"
    empty.mkdir(parents=True)
    tree = make_tree(base / "tree", workflows=empty)
    shutil.rmtree(tree / "extensions")
    (tree / "registry" / "services.yml").unlink()
    (tree / "registry" / "services.yml").write_text(yaml.safe_dump({"records": []}), encoding="utf-8")
    (tree / "registry" / "protocols").unlink()
    (tree / "registry" / "protocols").mkdir()
    return tree


@pytest.mark.parametrize("check", ["binding_content", "services", "protocols", "projections", "requests"])
def test_a_check_that_compared_nothing_fails_unless_it_is_asked_to_be_empty(check, tmp_path):
    """Finding 1: empty trees and empty records gave zero comparisons and a pass."""
    tree = tree_without_workflows(tmp_path / "t")
    result = run(tree, tree, tmp_path, checks=(check,))
    assert not result.ok and paths(result) == [(check, "<none>", "/")]
    assert result.compared[check] == 0
    allowed_empty = eq.diff_effective(tree, tree, checks=(check,), scratch=tmp_path, allow_empty=(check,))
    assert allowed_empty.ok


def test_every_workflow_file_is_read_the_way_discovery_reads_them_and_the_packages_are_included(tmp_path):
    """Finding 2: a package workflow and a .yaml file were outside the enumerated files."""
    documents = eq.read_workflows(ROOT)
    assert "image.generate.test-demo-package" in documents
    tree = make_tree(tmp_path / "t")
    renamed = tree / "manifests" / "workflows" / "flux2-klein.yml"
    renamed.rename(renamed.with_suffix(".yaml"))
    assert set(eq.read_workflows(tree)) == set(documents)
    assert eq.diff_effective(tree, tree, scratch=tmp_path).compared["requests"] > 300


def test_the_dump_must_hold_exactly_the_workflows_of_the_files():
    dump = {"errors": [], "workflows": {"a": {}}}
    eq._assert_dump_covers("after", dump, {"a": {}})
    with pytest.raises(RuntimeError, match=r"missing \['b'\]"):
        eq._assert_dump_covers("after", dump, {"a": {}, "b": {}})
    with pytest.raises(RuntimeError, match=r"extra \['a'\]"):
        eq._assert_dump_covers("after", dump, {})


def test_control_a_change_to_the_package_workflow_is_seen(trees, tmp_path_factory):
    changed = make_tree(tmp_path_factory.mktemp("changed") / "tree")
    path = changed / "extensions" / "bundled" / "demo-image-package" / "objects" / "workflow.yml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["bindings"]["comfyui"]["map"]["seed"][0]["input"] = "denoise"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    result = run(trees["same"], changed, tmp_path_factory.mktemp("scratch"), checks=("requests", "binding_content"))
    assert {d.workflow for d in result.unexplained} == {"image.generate.test-demo-package"}
    assert {d.check for d in result.unexplained} == {"requests", "binding_content"}


@pytest.mark.parametrize("check", ["requests", "projections", "binding_content"])
def test_a_workflow_on_one_side_only_fails_each_selected_check(check, trees, tmp_path_factory):
    """Finding 3: with checks=("requests",) removing a workflow passed."""
    removed = make_tree(tmp_path_factory.mktemp("removed") / "tree")
    (removed / "manifests" / "workflows" / "flux2-klein.yml").unlink()
    result = run(trees["same"], removed, tmp_path_factory.mktemp("scratch"), checks=(check,))
    assert not result.ok and ("image.generate.flux2-klein", "/") in {(d.workflow, d.path) for d in result.unexplained}
    assert {d.check for d in result.unexplained} == {check}


def test_the_allowed_list_names_exact_paths_and_a_changed_sample_is_not_covered():
    """Finding 4: whole /example_request/* and /examples/* subtrees were allowed, so a changed endpoint passed."""
    allowed = eq.load_allowed()
    assert not [path for rule in allowed.rules for path in rule.paths if "*" in path]
    mesh = "mesh.generate.trellis-2"
    before = "curl 'https://example.test/v1/meshes/generate' \\\n  -F 'seed=0' \\\n  -F 'image=@./image.bin'"
    after = before.replace("image.bin", "image.png")
    path = "/workflows[0]/example_request/curl"
    assert allowed.reason_for(eq.Difference("projections", mesh, path, before, after, "member_catalog"), "member_catalog")
    for changed in (after.replace("/v1/meshes/generate", "/v1/other"), after.replace("seed=0", "seed=5"), after + " --insecure"):
        assert allowed.reason_for(eq.Difference("projections", mesh, path, before, changed, "member_catalog"), "member_catalog") is None
    # a field the list does not name, under the same subtree, is not covered either
    assert allowed.reason_for(eq.Difference("projections", mesh, "/workflows[0]/examples/endpoint", "a", "b", "member_catalog"), "member_catalog") is None
    assert allowed.reason_for(eq.Difference("projections", mesh, "/workflows[0]/example_request/files/image/media_type",
                                            "<absent>", "image/png", "member_catalog"), "member_catalog") is None


def test_a_rule_can_pin_the_after_value_a_replacement_and_a_dropped_item():
    base = dict(checks=("projections",), scopes=(), workflows=None, paths=("/x",), reason="r")
    diff = lambda before, after: eq.Difference("projections", "w", "/x", before, after)  # noqa: E731
    assert eq.Rule(**base, after=2, has_after=True).covers(diff(1, 2), "") and not eq.Rule(**base, after=2, has_after=True).covers(diff(1, 3), "")
    replace = eq.Rule(**base, replace=(("a", "b"),))
    assert replace.covers(diff("xay", "xby"), "") and not replace.covers(diff("xay", "xbz"), "") and not replace.covers(diff("xy", "xy"), "")
    drops = eq.Rule(**base, drops={"key": "name", "value": "p"})
    assert drops.covers(diff([{"name": "p"}, {"name": "q"}], [{"name": "q"}]), "")
    assert not drops.covers(diff([{"name": "p"}, {"name": "q"}], []), "") and not drops.covers(diff([{"name": "q"}], [{"name": "q"}]), "")


def test_the_preparation_applies_defaults_generates_inside_the_bounds_and_runs_the_input_check():
    document = {"schema_version": 2, "id": "w", "inputs": {
        "d": {"type": "integer", "default": 7, "minimum": 0, "maximum": 10},
        "g": {"type": "integer", "minimum": 0, "generate": {"generator": "random_integer", "minimum": 0, "maximum": 3}},
        "z": {"type": "integer", "minimum": 0, "generate": {"generator": "random_integer", "minimum": 0, "maximum": 0}},
        "bad": {"type": "integer", "default": 99, "maximum": 10}}}
    values, rejected = eq.prepare_inputs("w", document, {})
    assert values["d"] == 7 and 0 <= values["g"] <= 3 and values["z"] == 0
    assert set(rejected) == {"bad"} and (values, rejected) == eq.prepare_inputs("w", document, {})
    assert eq.prepare_inputs("w", document, {"d": 4})[0]["d"] == 4
    assert eq.prepare_inputs("w", document, {"nope": 1})[1]["nope"] == "unknown input"
    with pytest.raises(ValueError, match="no reviewed generator"):
        eq.generated_value("w", "g", {"generate": {"generator": "other", "maximum": 1}})


def test_control_a_generator_range_of_zero_in_the_after_tree_changes_the_compiled_request(trees, tmp_path_factory):
    """Finding 5: generated values were handed in from the before documents, so this change was invisible."""
    changed = make_tree(tmp_path_factory.mktemp("changed") / "tree")
    edit(changed, "flux2-klein.yml", lambda d: d["inputs"]["seed"]["generate"].__setitem__("maximum", 0))
    result = run(trees["same"], changed, tmp_path_factory.mktemp("scratch"), checks=("requests",))
    assert not result.ok and {d.workflow for d in result.unexplained} == {"image.generate.flux2-klein"}


def test_control_a_changed_default_in_the_after_tree_changes_the_compiled_request(trees, tmp_path_factory):
    changed = make_tree(tmp_path_factory.mktemp("changed") / "tree")
    edit(changed, "flux2-klein.yml", lambda d: d["inputs"]["steps"].__setitem__("default", d["inputs"]["steps"]["maximum"] - 1))
    result = run(trees["same"], changed, tmp_path_factory.mktemp("scratch"), checks=("requests",))
    assert not result.ok and {d.workflow for d in result.unexplained} == {"image.generate.flux2-klein"}


def test_diff_effective_keeps_its_scratch_in_the_normal_temporary_directory(monkeypatch, tmp_path):
    """Finding 6: the scratch was made under the user's home folder, which fails on a fresh home."""
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import workflow_contract as contract
    finally:
        sys.path.remove(str(ROOT / "scripts"))
    made: list[tuple[tuple, dict]] = []
    real = tempfile.mkdtemp
    monkeypatch.setattr(contract.tempfile, "mkdtemp", lambda *a, **k: made.append((a, k)) or real(*a, **{**k, "dir": tmp_path}))
    monkeypatch.setattr(contract, "extract_ref", lambda ref, into: into)
    monkeypatch.setattr(contract, "diff_effective", lambda *a, **k: eq.Result([], [], {"x": 1}))
    assert contract.run_diff_effective("ref", ".", False) == 0
    assert made and all("dir" not in kwargs and str(Path.home()) not in repr(args) for args, kwargs in made)
