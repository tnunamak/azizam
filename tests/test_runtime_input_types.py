"""Runtime types (CODE_TO_DATA_S3, S3.7): inputs are checked through their whole tree, with a path and a code for each
refusal; one function, ``media_bearing``, says which inputs carry media; the OpenAPI schema follows the type grammar."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from gateway import extensions, model_metadata
from gateway import output_selectors as sel
from gateway.manifests import (
    ContractError, ManifestError, asset_leaf_specs, bind_backend_request, media_bearing, validate_inputs)
from gateway.workflow_openapi import _input_schema, advertised_inputs_schema, request_mapping, workflow_inputs_schema
from tests.test_output_selectors import TEMPLATE, descriptor, manifest as selector_manifest, refused, save

ASSET = {"type": "asset", "accepts": ["image/png"]}
BOX = {"type": "object", "required": True, "properties": {"width": {"type": "integer", "required": True}}}
PEOPLE = {"type": "array", "max_items": 3, "items": {"type": "object", "properties": {"box": BOX}}}


def contract(inputs: dict[str, Any], **binding: Any) -> dict[str, Any]:
    return {"schema_version": 2, "id": "x.y", "intent": "x.y", "inputs": inputs,
            "outputs": {"first": {"media_type": "a/b"}}, "bindings": {"eng": {"workflow": "w.json", "map": {}, **binding}}}


def problems(inputs: dict[str, Any], supplied: dict[str, Any]) -> list[tuple[tuple[Any, ...], str]]:
    return [(error.path, error.code) for error in validate_inputs(contract(inputs), supplied).errors]


# ------------------------------------------------------------------------------------------ media_bearing (T01)


@pytest.mark.parametrize("spec", [
    ASSET,
    {"type": "array", "max_items": 3, "items": ASSET},
    {"type": "object", "properties": {"photo": ASSET}},
    {"type": "array", "max_items": 3, "items": {"type": "object", "properties": {"photo": ASSET}}},
])
def test_media_bearing_finds_an_asset_at_any_depth(spec):
    assert media_bearing(spec) is True
    assert len(list(asset_leaf_specs(spec))) == 1


@pytest.mark.parametrize("spec", [
    {"type": "string"},
    {"type": "array", "max_items": 3, "items": {"type": "string"}},
    {"type": "object", "properties": {"n": {"type": "integer"}}},
    {"type": "object", "opaque": True, "photo": ASSET},   # a valid opaque object lists no properties, so nothing is searched
    {"type": ["asset"]}, {"type": {"type": "asset"}}, {"type": "array", "items": "asset"},
    {"type": "object", "properties": {"a": "asset", "b": None, "c": ["asset"]}}, {"type": "array"}, {"type": "object", "properties": []},
    None, "asset", 7, [],
])
def test_media_bearing_answers_false_and_never_raises_for_the_rest(spec):
    assert media_bearing(spec) is False


# ------------------------------------------------------------------- nested refusal paths at run time (T02 to T07)


def test_a_wrong_nested_value_is_refused_at_its_own_place():
    supplied = {"people": [{"box": {"width": 1}}, {"box": {"width": 2}}, {"box": 7}]}
    result = validate_inputs(contract({"people": PEOPLE}), supplied)
    assert [(e.path, e.code) for e in result.errors] == [(("inputs", "people", 2, "box"), "value_invalid")]
    assert result.errors[0].pointer == "/inputs/people/2/box"
    assert not result.accepted
    ok = validate_inputs(contract({"people": PEOPLE}), {"people": [{"box": {"width": 3}}]})
    assert ok.errors == [] and ok.accepted == {"people": [{"box": {"width": 3}}]}


def test_a_closed_object_lists_unknown_missing_and_wrong_siblings_together():
    person = {"type": "object", "properties": {"age": {"type": "integer", "required": True}, "name": {"type": "string", "required": True}}}
    result = validate_inputs(contract({"person": person}), {"person": {"age": "private-value", "extra": 1}})
    assert [(e.path, e.code) for e in result.contract_errors().errors] == [
        (("inputs", "person", "age"), "value_invalid"),
        (("inputs", "person", "extra"), "unknown_field"),
        (("inputs", "person", "name"), "required_missing"),
    ]
    assert "private-value" not in repr([e.as_dict() for e in result.errors]) + repr(result.rejected)
    assert problems({"person": person}, {"person": {"age": 1, "name": "A"}}) == []


def test_an_array_of_assets_checks_every_element():
    spec = {"type": "array", "max_items": 3, "items": ASSET}
    assert problems({"photos": spec}, {"photos": ["asset:one", 17, False]}) == [
        (("inputs", "photos", 1), "value_invalid"), (("inputs", "photos", 2), "value_invalid")]
    assert problems({"photos": spec}, {"photos": ["asset:one", {"asset_id": "two"}, "https://x.test/a.png"]}) == []


def test_an_asset_inside_an_object_inside_an_array_is_refused_at_the_leaf():
    spec = {"type": "array", "max_items": 3, "items": {"type": "object", "properties": {"photo": {**ASSET, "required": True}, "caption": {"type": "string"}}}}
    supplied = {"people": [{"photo": "a", "caption": "x"}, {"photo": "b"}, {"photo": 17}]}
    assert problems({"people": spec}, supplied) == [(("inputs", "people", 2, "photo"), "value_invalid")]
    assert problems({"people": spec}, {"people": [{"caption": "x"}]}) == [(("inputs", "people", 0, "photo"), "required_missing")]


def test_errors_across_inputs_are_collected_and_the_first_reason_stays_the_old_one():
    inputs = {"zeta": {"type": "integer"}, "people": PEOPLE}
    result = validate_inputs(contract(inputs), {"zeta": "SECRET", "people": [{"box": 7}, {"box": 8}]})
    errors = result.contract_errors()
    assert [(e.path, e.code) for e in errors.errors] == [
        (("inputs", "people", 0, "box"), "value_invalid"), (("inputs", "people", 1, "box"), "value_invalid"),
        (("inputs", "zeta"), "value_invalid")]
    assert errors.primary.path == ("inputs", "zeta") and str(errors) == "zeta must be an integer"
    assert next(iter(result.rejected.items())) == ("zeta", "zeta must be an integer")  # what a frontend reports
    assert "SECRET" not in repr(errors.as_list())


def test_unknown_and_missing_inputs_have_codes_and_the_old_reasons():
    result = validate_inputs(contract({"a": {"type": "integer", "required": True}}), {"b": 1})
    assert [(e.path, e.code) for e in result.errors] == [(("inputs", "b"), "input_unknown"), (("inputs", "a"), "required_missing")]
    assert result.rejected == {"b": "unknown input", "a": "required input missing"}


def test_the_error_list_is_capped_with_numeric_order_and_one_marker():
    spec = {"type": "array", "max_items": 256, "items": {"type": "integer"}}
    errors = validate_inputs(contract({"n": spec}), {"n": ["x"] * 120}).contract_errors()
    assert [e.path[2] for e in errors.errors[:-1]] == list(range(100))   # index 2 sorts before 10
    assert (errors.errors[-1].path, errors.errors[-1].code) == ((), "too_many_errors") and len(errors.errors) == 101
    assert errors.primary.path == ("inputs", "n", 0)
    assert len(validate_inputs(contract({"n": spec}), {"n": ["x"] * 100}).contract_errors().errors) == 100


def test_an_array_out_of_size_is_refused_and_its_children_are_still_checked():
    spec = {"type": "array", "min_items": 1, "max_items": 256, "items": {"type": "integer"}}
    assert problems({"n": spec}, {"n": []}) == [(("inputs", "n"), "value_invalid")]
    assert problems({"n": spec}, {"n": [1] * 257}) == [(("inputs", "n"), "value_invalid")]
    assert problems({"n": spec}, {"n": [1] * 256}) == [] and problems({"n": spec}, {"n": [1]}) == []
    assert problems({"n": spec}, {"n": ["x"] + [1] * 256}) == [(("inputs", "n"), "value_invalid"), (("inputs", "n", 0), "value_invalid")]


def test_a_nested_scalar_is_held_to_the_rules_of_a_top_level_one():
    inputs = {"s": {"type": "string", "min_length": 2, "max_length": 3}, "i": {"type": "integer"}, "n": {"type": "number"},
              "g": {"type": "integer", "step": 5, "offset": 1}, "box": {"type": "object", "properties": {
                  "s": {"type": "string", "min_length": 2, "max_length": 3}, "i": {"type": "integer"}, "n": {"type": "number"},
                  "g": {"type": "integer", "step": 5, "offset": 1}}}}
    cases = {"s": ["a", "abcd", "ab", "abc"], "i": [True, 3.0, 3], "n": [True, float("nan"), float("inf"), 1.5],
             "g": [7, 6, 11]}
    for name, values in cases.items():
        for value in values:
            top = problems(inputs, {name: value}) != []
            nested = problems(inputs, {"box": {name: value}}) != []
            assert top == nested, (name, value)
    assert problems(inputs, {"s": "a"}) == [(("inputs", "s"), "value_invalid")]
    assert problems(inputs, {"box": {"s": "a"}}) == [(("inputs", "box", "s"), "value_invalid")]


def test_a_path_with_a_slash_or_tilde_survives_the_pointer_round_trip():
    error = ContractError(("inputs", "a/b", "c~d", 3), "value_invalid", "x")
    assert error.pointer == "/inputs/a~1b/c~0d/3"


# ---------------------------------------------------- the source-echo check follows the shared predicate (T19, control B)

LINKED = {**TEMPLATE, "5": {"class_type": "C", "inputs": {"in": ["1", 0], "file": "placeholder"}}}


@pytest.mark.parametrize("spec, target", [
    ({"type": "array", "max_items": 3, "items": ASSET}, {"node": "5", "input": "file"}),
    ({"type": "array", "max_items": 3, "items": ASSET}, {"path": "5.inputs.file"}),
    ({"type": "array", "max_items": 3, "items": {"type": "object", "properties": {"photo": ASSET}}}, {"node": "5", "input": "file"}),
    ({"type": "object", "properties": {"photo": ASSET}}, {"node": "5", "input": "file"}),
])
def test_a_selector_on_a_node_that_a_container_of_assets_writes_is_an_echo(spec, target):
    m = selector_manifest(selectors={"first": {"node": "5", "key": "files", "from": "out_dir"}}, inputs={"photos": spec}, map={"photos": [target]})
    with pytest.raises(sel.SelectorErrors) as caught:
        save(m, template=LINKED)
    assert [(e.path, e.code) for e in caught.value.errors] == [(("bindings", "eng", "selectors", "first", "node"), "selector_echoes_source")]
    assert caught.value.errors[0].pointer == "/bindings/eng/selectors/first/node"


def test_a_container_without_assets_and_an_unrelated_producer_are_not_echoes():
    strings = {"type": "array", "max_items": 3, "items": {"type": "string"}}
    plain = selector_manifest(selectors={"first": {"node": "5", "key": "files", "from": "out_dir"}}, inputs={"photos": strings},
                              map={"photos": [{"node": "5", "input": "file"}]})
    assert save(plain, template=LINKED)
    assets = {"type": "array", "max_items": 3, "items": ASSET}
    elsewhere = selector_manifest(selectors={"first": {"node": "5", "key": "files", "from": "out_dir"}}, inputs={"photos": assets},
                                  map={"photos": [{"node": "1", "input": "text"}]})
    assert save(elsewhere, template=LINKED)


# ------------------------------------- no route resolves the references in a container, so none reaches an engine (T21)

PHOTOS = {"type": "array", "max_items": 3, "items": ASSET}


def test_a_container_of_assets_cannot_be_bound_into_an_engine_request():
    m = contract({"photos": PHOTOS}, map={"photos": [{"path": "photos"}]})
    with pytest.raises(ManifestError) as caught:
        bind_backend_request(m, "eng", {}, {"photos": ["asset:one"]})
    assert caught.value.errors[0].path == ("inputs", "photos") and caught.value.errors[0].code == "media_not_resolved"
    assert bind_backend_request(contract({"s": ASSET}, map={"s": [{"path": "s"}]}), "eng", {}, {"s": "uploaded.png"}).request == {"s": "uploaded.png"}


def test_the_extension_intake_refuses_a_container_of_assets_and_keeps_a_scalar_asset():
    result = extensions.validate_extension_inputs(
        {"photos": PHOTOS, "one": ASSET}, {"photos": ["asset:a"], "one": "asset:x"}, namespace="ns")
    assert "photos" in result.rejected and "one" in result.accepted
    assert "cannot hold media" in result.rejected["photos"] and "asset:a" not in result.rejected["photos"]


# ------------------------------------------------------------------------ OpenAPI follows the grammar (T20, T21)

PEOPLE_MEDIA = {"type": "array", "min_items": 1, "max_items": 3, "items": {"type": "object", "properties": {
    "photo": {**ASSET, "required": True}, "caption": {"type": "string", "min_length": 2, "max_length": 8}}}}


def test_an_array_of_objects_with_assets_projects_with_the_asset_at_the_leaf():
    schema = _input_schema(PEOPLE_MEDIA, form=False)
    assert (schema["type"], schema["minItems"], schema["maxItems"]) == ("array", 1, 3)
    item = schema["items"]
    assert (item["type"], item["additionalProperties"], item["required"]) == ("object", False, ["photo"])
    assert item["properties"]["photo"]["x-azizam-asset"] is True and item["properties"]["photo"]["type"] == "string"
    assert "format" not in item["properties"]["photo"]
    assert (item["properties"]["caption"]["minLength"], item["properties"]["caption"]["maxLength"]) == (2, 8)
    form = _input_schema(PEOPLE_MEDIA, form=True)
    assert form["type"] == "array" and "format" not in form and form["items"]["properties"]["photo"]["format"] == "binary"
    assert "x-azizam-asset" not in form


def test_an_opaque_object_and_a_scalar_project_as_they_did():
    assert _input_schema({"type": "object", "opaque": True, "help": "Reserved."}, form=False) == {"type": "object", "description": "Reserved."}
    assert _input_schema({"type": "integer", "minimum": 1, "step": 2, "offset": 1, "default": 3}, form=False) == {
        "type": "integer", "minimum": 1, "default": 3, "x-azizam-step": 2, "x-azizam-offset": 1}


def record_of(inputs: dict[str, Any], intent: str = "image.generate") -> SimpleNamespace:
    doc = {"inputs": inputs, "bindings": {"eng": {"map": {name: [{"path": name}] for name in inputs}}}}
    return SimpleNamespace(id=f"{intent}.t", intent=intent, contract=doc, manifest=doc)


@pytest.mark.parametrize("intent", ["image.generate", "image.edit", "audio.generate", "video.generate"])
def test_no_route_advertises_a_container_of_assets_but_a_scalar_asset_stays_a_file(intent):
    record = record_of({"prompt": {"type": "string"}, "photos": PHOTOS, "one": ASSET}, intent)
    for mode in ("sync", "async"):
        mapping = request_mapping(record, mode)
        assert mapping["photos"] == {"in": "none"}
        assert mapping["one"]["in"] != "none"
    assert "photos" not in advertised_inputs_schema(record)["properties"]
    assert "photos" in workflow_inputs_schema(record)["properties"]


# ----------------------------------------------------------- model metadata reads the asset leaves, wherever they sit


def test_the_asset_inputs_of_a_workflow_include_the_leaves_inside_containers():
    inputs = {"a": ASSET, "b": PHOTOS, "c": {"type": "object", "properties": {"v": {"type": "asset", "accepts": ["video/*"]}}}, "d": {"type": "string"}}
    leaves = model_metadata._asset_inputs({"inputs": inputs})
    assert [leaf["accepts"] for leaf in leaves] == [["image/png"], ["image/png"], ["video/*"]]


# ------------------------------------------------------------------ refusal order: enum before the length bounds

COMBINED = {"type": "string", "enum": ["ok"], "max_length": 2}


def test_a_value_that_breaks_the_enum_and_a_length_bound_gets_the_enum_reason_at_run():
    assert validate_inputs(contract({"caption": COMBINED}), {"caption": "toolong"}).rejected == {"caption": "caption must be one of: ok"}
    short = {"type": "string", "enum": ["okay"], "min_length": 3}
    assert validate_inputs(contract({"caption": short}), {"caption": "x"}).rejected == {"caption": "caption must be one of: okay"}
    # A value inside the enum still meets the bound after it.
    bounded = {"type": "string", "enum": ["toolong"], "max_length": 2}
    assert validate_inputs(contract({"caption": bounded}), {"caption": "toolong"}).rejected == {"caption": "caption must have at most 2 characters"}


def test_a_default_that_breaks_the_enum_and_a_length_bound_gets_the_enum_reason_at_save():
    from tests.test_workflow_contract import with_input
    from gateway.workflow_contract import check_contract
    messages = [e.message for e in check_contract(with_input("x", {**COMBINED, "default": "toolong"})).errors if e.path == ("inputs", "x", "default")]
    assert messages == ["the value must be one of: ok"]
