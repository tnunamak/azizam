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


# ------------------------------------------------------------------------------------- the second review's five findings

DEMO = "image.generate.test-demo-package"


def test_json_equality_tells_booleans_numbers_and_their_types_apart_at_every_depth():
    """Finding 1: Python equality made true equal 1 and false equal 0, so an exact pin accepted a changed type."""
    assert eq.json_equal({"a": [1, {"b": "x"}]}, {"a": [1, {"b": "x"}]})
    for left, right in ((True, 1), (1, True), (False, 0), (0, False), (1, 1.0), ([0], [False]), ({"a": 1}, {"a": True}), ({"a": 1}, {"b": 1})):
        assert not eq.json_equal(left, right), (left, right)


def test_a_pin_a_drop_and_a_retained_item_are_checked_by_json_type():
    base = dict(checks=("projections",), scopes=(), workflows=None, paths=("/x",), reason="r")
    diff = lambda before, after: eq.Difference("projections", "w", "/x", before, after)  # noqa: E731
    after_true = eq.Rule(**base, after=True, has_after=True)
    assert after_true.covers(diff(eq.ABSENT, True), "") and not after_true.covers(diff(eq.ABSENT, 1), "")
    before_zero = eq.Rule(**base, before=0, has_before=True)
    assert before_zero.covers(diff(0, 5), "") and not before_zero.covers(diff(False, 5), "")
    drops = eq.Rule(**base, drops={"value": "p"})
    assert drops.covers(diff(["p", 0], [0]), "") and not drops.covers(diff(["p", 0], [False]), "")
    assert not drops.covers(diff(["p", True], [1]), "")


def test_a_generator_range_change_that_draws_the_same_value_is_still_seen(tmp_path_factory):
    """Finding 2: 0..3 and 0..4 both draw 1 for this input, so one draw per input could not tell them apart."""
    spec = lambda high: {"type": "integer", "minimum": 0, "generate": {"generator": "random_integer", "minimum": 0, "maximum": high}}  # noqa: E731
    assert eq.generated_value("image.generate.flux2-klein", "seed", spec(3)) == eq.generated_value("image.generate.flux2-klein", "seed", spec(4))

    def with_range(base: Path, high: int) -> Path:
        tree = make_tree(base / "tree")
        edit(tree, "flux2-klein.yml", lambda d: d["inputs"]["seed"]["generate"].__setitem__("maximum", high))
        return tree

    narrow, wide = with_range(tmp_path_factory.mktemp("narrow"), 3), with_range(tmp_path_factory.mktemp("wide"), 4)
    result = run(narrow, wide, tmp_path_factory.mktemp("scratch"), checks=("requests",))
    assert not result.ok and {d.workflow for d in result.unexplained} == {"image.generate.flux2-klein"}
    assert "/generate/seed/generate/maximum" in {d.path for d in result.unexplained}  # the rule itself
    assert any(d.path.startswith("/requests") for d in result.unexplained)  # and the boundary draw it moves
    assert run(narrow, with_range(tmp_path_factory.mktemp("same"), 3), tmp_path_factory.mktemp("scratch"), checks=("requests",)).ok


def test_the_corpus_of_a_tree_holds_the_least_and_the_greatest_generated_value():
    document = {"schema_version": 2, "id": "w", "inputs": {
        "g": {"type": "integer", "minimum": 2, "generate": {"generator": "random_integer", "minimum": 0, "maximum": 9}}}}
    prepared = eq.prepare_corpus({"w": document}, eq.build_corpus({"w": document}))["w"]
    assert {case["name"]: case["values"]["g"] for case in prepared if case["name"].startswith("generated-")} == {
        "generated-minimum:g": 2, "generated-maximum:g": 9}


def write_package(tree: Path, name: str, workflow_path: str, graph_path: str, workflow_id: str) -> None:
    """A package whose workflow object sits at ``workflow_path`` and names its graph as ``package://<graph_path>``."""
    package = tree / "extensions" / "bundled" / name
    demo = tree / "extensions" / "bundled" / "demo-image-package"
    document = yaml.safe_load((demo / "objects" / "workflow.yml").read_text(encoding="utf-8"))
    document["id"] = workflow_id
    document["bindings"]["comfyui"]["workflow"] = f"package://{graph_path}"
    for path, text in ((workflow_path, yaml.safe_dump(document, sort_keys=False)),
                       (graph_path, (demo / "workflows" / "test-demo-image.json").read_text(encoding="utf-8")),
                       ("extension.yml", yaml.safe_dump({"schema_version": 1, "kind": "extension_package", "id": name, "name": name,
                                                         "version": "0.1.0", "objects": {"workflows": [workflow_path]}}))):
        (package / path).parent.mkdir(parents=True, exist_ok=True)
        (package / path).write_text(text, encoding="utf-8")


def test_a_package_root_is_the_folder_of_the_declaring_manifest_wherever_the_workflow_file_is(tmp_path):
    """Finding 3: the dump took the workflow file's grandparent as the package folder."""
    tree = make_tree(tmp_path / "t")
    write_package(tree, "top-package", "workflow.yaml", "graphs/g.json", "image.generate.top-level")
    write_package(tree, "deep-package", "objects/deeper/still/workflow.yml", "graphs/g.json", "image.generate.deep-object")
    documents = eq._sources().read_documents(tree)
    package_roots = {workflow_id: documents[workflow_id][2] for workflow_id in documents}
    bundled = tree / "extensions" / "bundled"
    assert package_roots["image.generate.top-level"] == bundled / "top-package"
    assert package_roots["image.generate.deep-object"] == bundled / "deep-package"
    assert package_roots[DEMO] == bundled / "demo-image-package"
    assert package_roots["image.generate.flux2-klein"] is None
    result = eq.diff_effective(tree, tree, scratch=tmp_path, checks=("requests",))  # each graph resolves in its own package
    assert result.ok and result.compared["requests"] > 300


def test_a_package_reference_to_a_missing_file_is_an_error_and_not_a_workflow_left_out(tmp_path):
    """Finding 3, second part: a missing file was filtered out of the list and out of the expected ids alike."""
    tree = make_tree(tmp_path / "t")
    manifest = tree / "extensions" / "bundled" / "demo-image-package" / "extension.yml"
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    document["objects"]["workflows"].append("objects/gone.yml")
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match=r"objects/gone\.yml.*does not exist"):
        eq.read_workflows(tree)


def allowed_reason(workflow: str, path: str, scope: str, before, after):
    return eq.load_allowed().reason_for(eq.Difference("projections", workflow, path, before, after, scope), scope)


def test_a_hash_may_change_but_may_not_disappear_or_change_type():
    """Finding 4: any hash change was allowed, a vanished hash included."""
    for scope in ("summary:admin", "summary:internal"):
        assert allowed_reason("image.generate.flux2-klein", "/manifest_hash", scope, "a" * 64, "b" * 64)
        assert allowed_reason("image.generate.flux2-klein", "/manifest_hash", scope, "a" * 64, eq.ABSENT) is None
        assert allowed_reason("image.generate.flux2-klein", "/manifest_hash", scope, eq.ABSENT, "b" * 64) is None
        assert allowed_reason("image.generate.flux2-klein", "/manifest_hash", scope, "a" * 64, 7) is None
        assert allowed_reason("image.generate.flux2-klein", "/manifest_hash", scope, None, "b" * 64) is None


def test_a_capability_may_become_the_inferred_value_for_the_listed_workflows_only():
    """Finding 4: any null-to-value change for any workflow was allowed."""
    for scope in ("capabilities", "projected"):
        assert allowed_reason("image.generate.flux2-klein", "[0]/capability", scope, None, "image")
        assert allowed_reason("video.generate.ltx-2.5-t2v", "[0]/capability", scope, None, "video")
        assert allowed_reason("image.generate.flux2-klein", "[0]/capability", scope, None, "video") is None  # not its value
        assert allowed_reason("image.generate.flux2-klein", "[0]/capability", scope, None, eq.ABSENT) is None  # disappearance
        assert allowed_reason("image.generate.flux2-klein", "[0]/capability", scope, "image", None) is None
        assert allowed_reason("audio.generate.tangoflux", "[0]/capability", scope, None, "audio") is None  # not listed
        assert allowed_reason("unrelated.workflow", "[0]/capability", scope, None, "image") is None


def test_every_pinned_capability_is_the_prefix_of_the_intent_of_the_workflow_it_names():
    """The cross-check: the converter infers the capability from the intent prefix, so the pin must say the same."""
    documents = eq.read_workflows(ROOT)
    pinned = {}
    for rule in eq.load_allowed().rules:
        if rule.paths == ("[]/capability",):
            assert rule.workflows and rule.has_before and rule.before is None and rule.has_after
            pinned.update({workflow: rule.after for workflow in rule.workflows})
    assert pinned and set(pinned) <= set(documents)
    assert {workflow: documents[workflow]["intent"].split(".")[0] for workflow in pinned} == pinned


def test_the_demo_package_workflow_has_policy_facts_and_needs_no_configuration(tmp_path):
    """Finding 5: the rewrite dropped the demo's facts, so the shipped demo showed as needing configuration."""
    tree = make_tree(tmp_path / "t")
    corpus = tmp_path / "corpus.json"
    corpus.write_text("{}", encoding="utf-8")
    summary = eq.dump_tree(tree, corpus)["workflows"][DEMO]["projections"]["summary:client"]
    assert "needs_configuration" not in summary
    assert (summary["exposure"], summary["trust_tier"], summary["resource_tier"], summary["tags"]) == ("opt_in", "experimental", "batch", ["test", "demo"])
    assert not [rule for rule in eq.load_allowed().rules if rule.workflows and DEMO in rule.workflows and "/needs_configuration" in rule.paths]


# ---------------------------------------------------------------------------------------- round 3: grid, hash shape, drop types


def generated(**constraints) -> dict:
    return {"type": "integer", **constraints, "generate": {"generator": "random_integer", "minimum": 0, "maximum": 8}}


def test_the_least_and_the_greatest_generated_value_lie_on_the_inputs_grid():
    """Range 0..8 with step 2 from offset 1 holds 1, 3, 5, 7: the extrema are 1 and 7, not the raw 0 and 8 the check refuses."""
    spec = generated(minimum=0, maximum=8, step=2, offset=1)
    assert eq.generator_range("w", "g", spec) == (1, 7)
    assert all(eq.validate_input_value("g", value, spec) is None for value in eq.generator_range("w", "g", spec))
    assert eq.validate_input_value("g", 0, spec) is not None   # the raw bound is refused
    drawn = {eq.generated_value("w", f"g{n}", spec) for n in range(40)}
    assert drawn <= {1, 3, 5, 7} and len(drawn) > 1
    document = {"schema_version": 2, "id": "w", "inputs": {"g": spec}}
    prepared = eq.prepare_corpus({"w": document}, eq.build_corpus({"w": document}))["w"]
    boundary = {case["name"]: case for case in prepared if case["name"].startswith("generated-")}
    assert {name: case["values"]["g"] for name, case in boundary.items()} == {"generated-minimum:g": 1, "generated-maximum:g": 7}
    assert not any(case["rejected"] for case in boundary.values())
    assert eq.generator_range("w", "g", generated(minimum=0, maximum=4)) == (0, 4)
    assert eq.generator_range("w", "g", generated(minimum=3, maximum=3, step=2)) is None   # no grid value left


@pytest.mark.parametrize(("constraints", "expected"), [
    pytest.param({"step": 0.5}, list(range(0, 9)), id="fractional-step-has-every-integer"),
    pytest.param({"step": 1.5, "offset": 0.5}, [2, 5, 8], id="fractional-step-and-offset"),
    pytest.param({"step": 0.5, "offset": 0.25}, None, id="fractional-lattice-with-no-integer"),
    pytest.param({"minimum": 1.5}, list(range(2, 9)), id="fractional-minimum-rounds-up"),
    pytest.param({"maximum": 6.5}, list(range(0, 7)), id="fractional-maximum-rounds-down"),
    pytest.param({"minimum": 1.5, "maximum": 1.9}, None, id="fractional-bounds-with-no-integer"),
    pytest.param({"enum": [1, 7]}, [1, 7], id="enum"),
    pytest.param({"enum": [1, 6, 9, 2.5, True, "x"], "step": 3}, [6], id="enum-cut-by-grid-range-and-type"),
    pytest.param({"enum": [5, 6], "minimum": 6}, [6], id="enum-cut-by-bound"),
    pytest.param({"enum": [20]}, None, id="enum-outside-the-rule-range"),
    pytest.param({"enum": [1.0, 7.0], "type": "number"}, [1, 7], id="integral-float-enum-members"),
    pytest.param({"enum": [True, 3]}, [1, 3], id="a-true-member-equals-one-as-the-check-reads-it"),
    pytest.param({"enum": [float("nan"), float("inf"), 4]}, [4], id="non-finite-enum-members-admit-nothing"),
    pytest.param({"step": 2, "offset": 1, "minimum": 2, "maximum": 6}, [3, 5], id="integer-step-unchanged"),
])
def test_the_generated_values_are_exactly_the_integers_the_input_check_accepts(constraints, expected):
    spec = generated(**constraints)
    values = eq.generator_values("w", "g", spec)
    accepted = [n for n in range(-3, 12) if eq.validate_input_value("g", n, spec) is None and 0 <= n <= 8]
    assert (None if values is None else list(values)) == expected
    assert (expected or []) == accepted   # the checker agrees with the real input check on every integer of the rule's range
    if expected:
        assert eq.generator_range("w", "g", spec) == (expected[0], expected[-1])
        drawn = {eq.generated_value("w", f"g{n}", spec) for n in range(60)}
        assert drawn <= set(expected) and all(eq.validate_input_value("g", value, spec) is None for value in drawn)
    else:
        assert eq.generator_range("w", "g", spec) is None and eq.generated_value("w", "g", spec) is None


@pytest.mark.parametrize("spec", [
    pytest.param({"type": "string", "generate": {"generator": "random_integer", "maximum": 8}}, id="type"),
    pytest.param(generated(step=0), id="zero-step"),
    pytest.param(generated(step=-1), id="negative-step"),
    pytest.param(generated(step="2"), id="text-step"),
    pytest.param(generated(offset=True, step=2), id="boolean-offset"),
    pytest.param(generated(minimum="1"), id="text-minimum"),
    pytest.param(generated(maximum=float("inf")), id="infinite-maximum"),
])
def test_a_generated_input_the_helper_cannot_judge_fails_loudly_naming_the_workflow_and_input(spec):
    with pytest.raises(ValueError, match="w input g"):
        eq.generator_values("w", "g", spec)


def test_a_big_range_is_not_walked_value_by_value():
    spec = {"type": "integer", "step": 7, "offset": 3, "generate": {"generator": "random_integer", "minimum": 0, "maximum": 2**62}}
    low, high = eq.generator_range("w", "g", spec)
    assert (low, high) == (3, 3 + 7 * ((2**62 - 3) // 7)) and eq.validate_input_value("g", high, spec) is None
    assert eq.validate_input_value("g", eq.generated_value("w", "g", spec), spec) is None


@pytest.mark.parametrize("maximum", [2**63 - 1, 2**64, 2**80])
def test_a_range_of_more_values_than_a_machine_word_counts_still_draws(maximum):
    spec = {"type": "integer", "generate": {"generator": "random_integer", "minimum": 0, "maximum": maximum}}
    drawn = eq.generated_value("w", "g", spec)
    assert 0 <= drawn <= maximum and eq.validate_input_value("g", drawn, spec) is None


def test_a_small_range_draws_the_same_value_as_before():
    """Counting without len() leaves every draw that already fit unchanged (the frozen corpus depends on them)."""
    import random
    spec = {"type": "integer", "step": 3, "offset": 1, "generate": {"generator": "random_integer", "minimum": 0, "maximum": 50}}
    values = range(1, 50, 3)
    assert eq.generated_value("w", "g", spec) == values[random.Random("w:g").randrange(len(values))]


def with_input_change(tmp_path_factory, change) -> Path:
    tree = make_tree(tmp_path_factory.mktemp("tree") / "tree")
    edit(tree, "flux2-klein.yml", lambda d: change(d["inputs"]["seed"]))
    return tree


@pytest.mark.parametrize("change", [
    pytest.param(lambda spec: spec.__setitem__("step", 2), id="step"),
    pytest.param(lambda spec: spec.update(step=2, offset=1), id="offset"),
])
def test_a_step_or_an_offset_change_on_a_generated_input_is_seen_even_when_the_range_and_the_draw_stay_the_same(change, tmp_path_factory):
    def narrow(spec):
        spec.update(minimum=0, maximum=4)
        spec["generate"].update(minimum=0, maximum=4)

    plain = with_input_change(tmp_path_factory, narrow)
    gridded = with_input_change(tmp_path_factory, lambda spec: (narrow(spec), change(spec)))
    result = run(plain, gridded, tmp_path_factory.mktemp("scratch"), checks=("requests",))
    assert not result.ok
    assert {d.path for d in result.unexplained if d.path.startswith("/generate")} & {"/generate/seed/step", "/generate/seed/offset"}
    assert run(plain, with_input_change(tmp_path_factory, narrow), tmp_path_factory.mktemp("scratch"), checks=("requests",)).ok


def test_a_hash_must_keep_the_shape_of_the_digest_the_gateway_writes():
    """The gateway writes the SHA-256 of the document as 64 lowercase hexadecimal digits; nothing else is a hash."""
    good, other = "a" * 64, "0123456789abcdef" * 4
    for scope in ("summary:admin", "summary:internal"):
        assert allowed_reason("image.generate.flux2-klein", "/manifest_hash", scope, good, other)
        for broken in ("", "broken", "A" * 64, "a" * 63, "a" * 65, "g" * 64, " " + "a" * 63, "a" * 64 + "\n"):
            assert allowed_reason("image.generate.flux2-klein", "/manifest_hash", scope, good, broken) is None, broken
            assert allowed_reason("image.generate.flux2-klein", "/manifest_hash", scope, broken, good) is None, broken


def test_the_hash_pattern_is_the_shape_of_a_real_manifest_hash():
    from gateway.manifests import manifest_identity

    document = yaml.safe_load((ROOT / "manifests" / "workflows" / "flux2-klein.yml").read_text(encoding="utf-8"))
    digest = manifest_identity(document).manifest_hash
    assert allowed_reason("image.generate.flux2-klein", "/manifest_hash", "summary:admin", digest, "b" * 64)
    # Paired with the refusals that the old "any string" rule let through: a real digest that lost its shape is not accepted.
    for broken in (digest.upper(), digest[:-1], digest + "0", digest + "\n", digest[:-1] + "g", "sha256:" + digest):
        assert allowed_reason("image.generate.flux2-klein", "/manifest_hash", "summary:admin", digest, broken) is None, broken
        assert allowed_reason("image.generate.flux2-klein", "/manifest_hash", "summary:admin", broken, digest) is None, broken


def test_a_drop_matches_numbers_and_booleans_by_type():
    base = dict(checks=("projections",), scopes=(), workflows=None, paths=("/x",), reason="r")
    diff = lambda before, after: eq.Difference("projections", "w", "/x", before, after)  # noqa: E731
    number = eq.Rule(**base, drops={"value": 1})
    assert number.covers(diff([1, 2], [2]), "") and number.covers(diff([2, 1, 3], [2, 3]), "")
    assert not number.covers(diff([True, 2], [2]), "")   # true is not 1
    assert not number.covers(diff([1.0, 2], [2]), "")    # 1.0 is not 1
    assert not number.covers(diff([1, 2], [1]), "")      # the wrong item was dropped
    boolean = eq.Rule(**base, drops={"value": False})
    assert boolean.covers(diff([False, 1], [1]), "") and boolean.covers(diff([True, False], [True]), "")
    assert not boolean.covers(diff([0, 1], [1]), "")     # 0 is not false
    assert not boolean.covers(diff([True, 1], [1]), "")
    keyed = eq.Rule(**base, drops={"key": "n", "value": 0})
    assert keyed.covers(diff([{"n": 0}, {"n": 1}], [{"n": 1}]), "")
    assert not keyed.covers(diff([{"n": False}, {"n": 1}], [{"n": 1}]), "")


def test_the_dump_gives_a_package_workflow_the_facts_of_a_row_only_when_the_row_names_its_package(tmp_path):
    """The dump follows the gateway's rule: the demo's workflow id declared by a package of another id gets no facts."""
    tree = make_tree(tmp_path / "t")
    manifest = tree / "extensions" / "bundled" / "demo-image-package" / "extension.yml"
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    document["id"] = "some-other-package"
    manifest.write_text(yaml.safe_dump(document), encoding="utf-8")
    corpus = tmp_path / "corpus.json"
    corpus.write_text("{}", encoding="utf-8")
    summary = eq.dump_tree(tree, corpus)["workflows"][DEMO]["projections"]["summary:client"]
    assert summary["needs_configuration"] is True and summary["exposure"] is None
