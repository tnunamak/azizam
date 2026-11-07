"""The equality check of the schema 1 to 2 migration (CODE_TO_DATA_S3, S3.5a): what it compares, and that it can fail.

The check's positive control is here: swapping the ``steps`` and ``seam_steps`` targets of the tile composition changes no
projection and no template request, and the binding-content check must still fail. The other controls change one thing each
that another comparison must see (a template request, the multipart form, a policy fact in a service record).
"""

from __future__ import annotations

import copy
import json
import shutil
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
    assert result.compared == {"binding_content": 38}


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
    assert 0 < len(allowed.rules) <= 12 and all(rule.reason for rule in allowed.rules)
    bad = tmp_path / "bad.yml"
    bad.write_text(yaml.safe_dump({"allowed": [{"checks": ["projections"], "paths": ["/x"]}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="reason"):
        eq.load_allowed(bad)


def test_the_corpus_fixes_generated_values_and_covers_defaults_bounds_enums_and_the_empty_request():
    documents = eq.read_workflows(ROOT)
    corpus = eq.build_corpus({"image.generate.flux2-klein": documents["image.generate.flux2-klein"]})
    names = [case["name"] for case in corpus["image.generate.flux2-klein"]]
    assert names[:2] == ["empty", "defaults"] and "minimum:steps" in names and "maximum:steps" in names
    assert all(case["values"]["seed"] == 1 for case in corpus["image.generate.flux2-klein"])  # the injected source: minimum + 1
    assert corpus == json.loads(json.dumps(corpus)) and eq.build_corpus(documents) == eq.build_corpus(copy.deepcopy(documents))


def test_a_schema_one_document_gets_the_same_corpus_as_its_conversion():
    v1 = yaml.safe_load((V1 / "flux2-klein-edit.yml").read_text(encoding="utf-8"))
    corpus = eq.build_corpus({"w": v1})["w"]
    assert all("seed" in case["values"] for case in corpus)  # the converter declared the seed the file only mapped
