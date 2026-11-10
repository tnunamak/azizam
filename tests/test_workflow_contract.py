"""The schema 2 workflow contract (CODE_TO_DATA_S3, S3.2): the two check layers, the 1 to 2 converter, the command, and the
report-only load. Every rule has a refusal test with the exact path and code; the positive controls put back the defects
the design names (a typo key, a string default on a seed, authority keys in the document)."""

from __future__ import annotations

import copy
import json
from fractions import Fraction
import re
import sys
from pathlib import Path

import pytest
import yaml

from gateway import workflow_call
from gateway.manifests import ContractErrors, validate_input_value
from gateway.workflow_contract import (
    GENERATORS,
    SCHEMA_PATH,
    ContractContext,
    check_contract,
    value_problems,
)
from gateway.workflow_convert import convert_1_to_2, convert_and_check
from tests.support.section_edits import without_section_additions
from tests.test_domain_literal_ratchet import vocabulary

ROOT = Path(__file__).resolve().parent.parent
BUNDLED = sorted((ROOT / "manifests" / "workflows").glob("*.yml"))
# The 39 bundled files as they were in schema 1, frozen for the converter tests.
V1_FIXTURES = sorted((ROOT / "tests" / "fixtures" / "workflows_v1").glob("*.yml"))
sys.path.insert(0, str(ROOT / "scripts"))
import workflow_contract as command  # noqa: E402


def base() -> dict:
    """A small valid schema 2 document that uses every input type."""
    return {
        "schema_version": 2,
        "id": "demo.flow",
        "version": "1.0.0",
        "intent": "demo.run",
        "capability": "demo",
        "purpose": "Make a demo",
        "inputs": {
            "prompt": {"type": "string", "required": True, "min_length": 1, "max_length": 100},
            "steps": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            "scale": {"type": "number", "minimum": 0, "maximum": 2, "default": 1.5},
            "flag": {"type": "boolean", "default": False},
            "mode": {"type": "string", "enum": ["a", "b"], "default": "a"},
            "source": {"type": "asset", "accepts": ["image/png", "video/*"], "required": True},
            "seed": {"type": "integer", "minimum": 0, "generate": {"generator": "random_integer", "minimum": 0, "maximum": 99}},
            "extra": {"type": "object", "opaque": True},
            "people": {
                "type": "array",
                "max_items": 4,
                "items": {"type": "object", "properties": {"name": {"type": "string", "required": True}, "photo": {"type": "asset", "accepts": ["image/*"]}}},
            },
        },
        "outputs": {
            "picture": {"media_type": "image/png", "count": {"min": 1, "max": 4}},
            "facts": {"media_type": "application/json", "schema": {"type": "object", "properties": {"n": {"type": "integer"}}}},
        },
        "bindings": {"adapter": {"map": {"prompt": "p"}}},
    }


def pairs(doc: dict, context: ContractContext | None = None) -> list[tuple[tuple, str]]:
    return [(error.path, error.code) for error in check_contract(doc, context).errors]


def with_input(name: str, spec: dict) -> dict:
    doc = base()
    doc["inputs"][name] = spec
    return doc


def test_the_small_document_is_valid():
    assert pairs(base()) == []


def test_a_document_that_is_not_a_mapping_is_refused():
    assert pairs([]) == [((), "type_mismatch")]  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------------------- versions


@pytest.mark.parametrize("version", [1, 3, "2", True, None])
def test_only_schema_version_two_is_supported_and_nothing_else_is_reported(version):
    doc = base()
    doc["schema_version"] = version
    doc["bogus"] = 1
    assert pairs(doc) == [(("schema_version",), "schema_version_unsupported")]


def test_a_missing_schema_version_is_unsupported_too():
    doc = base()
    del doc["schema_version"]
    assert pairs(doc) == [(("schema_version",), "schema_version_unsupported")]


# ------------------------------------------------------------------------------------------------- positive controls


def test_control_a_typo_key_is_refused_at_its_path():
    doc = with_input("size", {"type": "integer", "minimum": 1, "maxium": 9})
    assert pairs(doc) == [(("inputs", "size", "maxium"), "unknown_field")]


def test_control_a_random_string_default_on_a_seed_is_refused_at_its_path():
    doc = with_input("seed2", {"type": "integer", "minimum": 0, "default": "random"})
    assert pairs(doc) == [(("inputs", "seed2", "default"), "default_invalid")]


@pytest.mark.parametrize("key", ["exposure", "trusted", "source", "trust_tier", "resource_tier", "tags"])
def test_control_an_authority_key_at_the_root_is_refused_at_its_path(key):
    doc = base()
    doc[key] = "default" if key != "trusted" else True
    errors = check_contract(doc).errors
    assert [(e.path, e.code) for e in errors] == [((key,), "unknown_field")]
    assert "registry record" in errors[0].message


def test_control_the_controls_are_valid_when_the_defect_is_not_there():
    doc = with_input("size", {"type": "integer", "minimum": 1, "maximum": 9})
    assert pairs(doc) == []


# ------------------------------------------------------------------------------------------- D2 closure, annotations


def test_unknown_keys_are_refused_at_every_level():
    doc = base()
    doc["extra_root"] = 1
    doc["inputs"]["prompt"]["extra"] = 1
    doc["inputs"]["people"]["items"]["properties"]["name"]["extra"] = 1
    doc["outputs"]["picture"]["extra"] = 1
    doc["outputs"]["picture"]["count"]["extra"] = 1
    doc["inputs"]["seed"]["generate"]["extra"] = 1
    assert sorted(pairs(doc), key=str) == sorted([
        (("extra_root",), "unknown_field"),
        (("inputs", "prompt", "extra"), "unknown_field"),
        (("inputs", "people", "items", "properties", "name", "extra"), "unknown_field"),
        (("outputs", "picture", "extra"), "unknown_field"),
        (("outputs", "picture", "count", "extra"), "unknown_field"),
        (("inputs", "seed", "generate", "extra"), "unknown_field"),
    ], key=str)


def test_a_key_that_belongs_to_another_type_is_refused():
    assert pairs(with_input("x", {"type": "string", "minimum": 1})) == [(("inputs", "x", "minimum"), "unknown_field")]
    assert pairs(with_input("x", {"type": "boolean", "enum": [True]})) == [(("inputs", "x", "enum"), "unknown_field")]


def test_annotations_are_namespaced_bounded_and_allowed_at_three_places():
    doc = base()
    doc["annotations"] = {"example.org/note": {"any": ["json"]}}
    doc["inputs"]["prompt"]["annotations"] = {"example.org/hint": 1}
    doc["outputs"]["picture"]["annotations"] = {"example.org/hint": "x"}
    assert pairs(doc) == []


def test_an_annotation_key_without_a_domain_is_refused():
    doc = base()
    doc["annotations"] = {"note": 1}
    assert pairs(doc) == [(("annotations", "note"), "key_invalid")]


def test_more_than_32_annotations_are_refused():
    doc = base()
    doc["annotations"] = {f"example.org/n{i}": i for i in range(33)}
    assert pairs(doc) == [(("annotations",), "limit_exceeded")]


def test_annotations_together_are_bounded():
    doc = base()
    doc["annotations"] = {"example.org/a": "x" * 60}
    doc["inputs"]["prompt"]["annotations"] = {"example.org/b": "y" * 60}
    assert pairs(doc, ContractContext(annotation_bytes=100)) == [(("annotations",), "annotations_too_large")]
    assert pairs(doc, ContractContext(annotation_bytes=1000)) == []


# ------------------------------------------------------------------------------------------------ D4: types, bounds


def test_the_required_root_fields_are_named():
    doc = base()
    for key in ("capability", "version", "outputs"):
        del doc[key]
    assert sorted(pairs(doc)) == [(("capability",), "required_missing"), (("outputs",), "required_missing"), (("version",), "required_missing")]


def test_an_input_needs_a_type_and_a_known_one_and_nothing_else_follows():
    assert pairs(with_input("x", {"label": "x"})) == [(("inputs", "x", "type"), "required_missing")]
    assert pairs(with_input("x", {"type": "picture"})) == [(("inputs", "x", "type"), "value_invalid")]


def test_the_old_picture_type_is_gone():
    assert pairs(with_input("x", {"type": "image", "modality": "image"})) == [(("inputs", "x", "type"), "value_invalid")]


def test_input_names_follow_the_naming_rule():
    assert pairs(with_input("Bad-Name", {"type": "string"})) == [(("inputs", "Bad-Name"), "key_invalid")]


def test_at_most_64_inputs():
    doc = base()
    doc["inputs"] = {f"i{n}": {"type": "boolean"} for n in range(65)}
    assert pairs(doc) == [(("inputs",), "limit_exceeded")]


@pytest.mark.parametrize(
    ("spec", "path", "code"),
    [
        ({"type": "integer", "minimum": 5, "maximum": 1}, ("inputs", "x"), "bounds_empty"),
        ({"type": "number", "minimum": 5, "maximum": 1}, ("inputs", "x"), "bounds_empty"),
        ({"type": "string", "min_length": 5, "max_length": 1}, ("inputs", "x"), "bounds_empty"),
        ({"type": "integer", "minimum": 1.5}, ("inputs", "x", "minimum"), "integer_expected"),
        ({"type": "integer", "maximum": 2.5}, ("inputs", "x", "maximum"), "integer_expected"),
        ({"type": "number", "maximum": float("inf")}, ("inputs", "x", "maximum"), "number_not_finite"),
        ({"type": "integer", "step": 0}, ("inputs", "x", "step"), "limit_exceeded"),
        ({"type": "array", "max_items": 1, "min_items": 3, "items": {"type": "boolean"}}, ("inputs", "x"), "bounds_empty"),
        ({"type": "array", "items": {"type": "boolean"}}, ("inputs", "x", "max_items"), "required_missing"),
        ({"type": "array", "max_items": 257, "items": {"type": "boolean"}}, ("inputs", "x", "max_items"), "limit_exceeded"),
        ({"type": "array", "max_items": 2}, ("inputs", "x", "items"), "required_missing"),
        ({"type": "string", "label": ""}, ("inputs", "x", "label"), "limit_exceeded"),
    ],
)
def test_bound_rules(spec, path, code):
    assert pairs(with_input("x", spec)) == [(path, code)]


def test_an_object_has_properties_or_is_opaque_but_not_both_or_neither():
    assert pairs(with_input("x", {"type": "object"})) == [(("inputs", "x"), "shape_invalid")]
    both = {"type": "object", "opaque": True, "properties": {"a": {"type": "string"}}}
    assert pairs(with_input("x", both)) == [(("inputs", "x"), "shape_invalid")]
    assert pairs(with_input("x", {"type": "object", "opaque": False})) == [(("inputs", "x", "opaque"), "value_invalid")]


def test_nesting_is_at_most_four_levels_and_the_error_names_the_fifth():
    leaf = {"type": "string"}
    nest = leaf
    for name in ("d", "c", "b", "a"):
        nest = {"type": "object", "properties": {name: nest}}
    doc = with_input("x", nest)  # x is level 1, then a, b, c, d
    assert pairs(doc) == [(("inputs", "x", "properties", "a", "properties", "b", "properties", "c", "properties", "d"), "nesting_too_deep")]
    shallow = with_input("x", {"type": "object", "properties": {"a": {"type": "object", "properties": {"b": {"type": "object", "properties": {"c": leaf}}}}}})
    assert pairs(shallow) == []


def test_an_array_of_arrays_counts_its_levels_too():
    inner = {"type": "boolean"}
    for _ in range(4):
        inner = {"type": "array", "max_items": 2, "items": inner}
    assert pairs(with_input("x", inner)) == [(("inputs", "x", "items", "items", "items", "items"), "nesting_too_deep")]


# ------------------------------------------------------------------------------------------------ D4: defaults, enums


@pytest.mark.parametrize(
    ("spec", "path", "code"),
    [
        ({"type": "integer", "default": 1.5}, ("inputs", "x", "default"), "default_invalid"),
        ({"type": "integer", "default": True}, ("inputs", "x", "default"), "default_invalid"),
        ({"type": "integer", "minimum": 5, "default": 1}, ("inputs", "x", "default"), "default_invalid"),
        ({"type": "integer", "maximum": 5, "default": 9}, ("inputs", "x", "default"), "default_invalid"),
        ({"type": "integer", "step": 4, "offset": 1, "default": 6}, ("inputs", "x", "default"), "default_invalid"),
        ({"type": "number", "default": float("nan")}, ("inputs", "x", "default"), "default_invalid"),
        ({"type": "string", "default": 5}, ("inputs", "x", "default"), "default_invalid"),
        ({"type": "string", "max_length": 2, "default": "abc"}, ("inputs", "x", "default"), "default_invalid"),
        ({"type": "string", "min_length": 2, "default": "a"}, ("inputs", "x", "default"), "default_invalid"),
        ({"type": "string", "enum": ["a"], "default": "b"}, ("inputs", "x", "default"), "default_invalid"),
        ({"type": "boolean", "default": "yes"}, ("inputs", "x", "default"), "default_invalid"),
        ({"type": "object", "opaque": True, "default": []}, ("inputs", "x", "default"), "default_invalid"),
        ({"type": "array", "max_items": 1, "items": {"type": "integer"}, "default": [1, 2]}, ("inputs", "x", "default"), "default_invalid"),
        ({"type": "array", "max_items": 3, "items": {"type": "integer"}, "default": [1, "a"]}, ("inputs", "x", "default", 1), "default_invalid"),
    ],
)
def test_a_default_must_fit_its_spec(spec, path, code):
    assert pairs(with_input("x", spec)) == [(path, code)]


def test_defaults_of_objects_are_checked_member_by_member_with_their_own_paths():
    spec = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}, "s": {"type": "string", "required": True}}}
    assert pairs(with_input("x", {**spec, "default": {"n": 2, "s": "ok"}})) == []
    assert pairs(with_input("x", {**spec, "default": {"n": 0, "s": "ok"}})) == [(("inputs", "x", "default", "n"), "default_invalid")]
    assert pairs(with_input("x", {**spec, "default": {"n": 2}})) == [(("inputs", "x", "default", "s"), "required_missing")]
    assert pairs(with_input("x", {**spec, "default": {"n": 2, "s": "ok", "z": 1}})) == [(("inputs", "x", "default", "z"), "unknown_field")]


def test_a_default_message_never_holds_the_default():
    errors = check_contract(with_input("x", {"type": "string", "enum": ["a"], "default": "zzzsecret"})).errors
    assert "zzzsecret" not in " ".join(error.message for error in errors)


def test_enum_values_have_the_type_of_the_input():
    assert pairs(with_input("x", {"type": "integer", "enum": [1, 2.5]})) == [(("inputs", "x", "enum", 1), "enum_value_invalid")]
    assert pairs(with_input("x", {"type": "string", "enum": ["a", 2]})) == [(("inputs", "x", "enum", 1), "enum_value_invalid")]
    assert pairs(with_input("x", {"type": "integer", "enum": [1, 2]})) == []


def test_an_enum_is_not_empty_and_has_no_repeats():
    assert pairs(with_input("x", {"type": "string", "enum": []})) == [(("inputs", "x", "enum"), "limit_exceeded")]
    assert pairs(with_input("x", {"type": "string", "enum": ["a", "a"]})) == [(("inputs", "x", "enum"), "limit_exceeded")]


def test_a_broken_part_is_reported_once_and_its_checks_are_skipped():
    # The enum is empty, so the default is not read against it: one cause, one error.
    assert pairs(with_input("x", {"type": "string", "enum": [], "default": "a"})) == [(("inputs", "x", "enum"), "limit_exceeded")]


# ------------------------------------------------------------------------------------------------------ D4: assets


def test_an_asset_lists_what_it_accepts():
    assert pairs(with_input("x", {"type": "asset"})) == [(("inputs", "x", "accepts"), "required_missing")]
    assert pairs(with_input("x", {"type": "asset", "accepts": []})) == [(("inputs", "x", "accepts"), "limit_exceeded")]
    assert pairs(with_input("x", {"type": "asset", "accepts": ["IMAGE/PNG"]})) == [(("inputs", "x", "accepts", 0), "pattern_mismatch")]


def test_an_accepted_type_must_be_one_the_sniffer_measures_or_a_family_it_knows():
    assert pairs(with_input("x", {"type": "asset", "accepts": ["image/png", "video/*", "audio/*"]})) == []
    assert pairs(with_input("x", {"type": "asset", "accepts": ["image/png", "font/otf"]})) == [(("inputs", "x", "accepts", 1), "accepts_unknown_type")]
    assert pairs(with_input("x", {"type": "asset", "accepts": ["font/*"]})) == [(("inputs", "x", "accepts", 0), "accepts_unknown_type")]
    assert pairs(with_input("x", {"type": "asset", "accepts": ["image/avif"]})) == [(("inputs", "x", "accepts", 0), "accepts_unknown_type")]


def test_the_known_types_come_from_the_context_so_a_new_one_is_data():
    context = ContractContext(media_types=frozenset({"font/otf"}))
    assert pairs(with_input("x", {"type": "asset", "accepts": ["font/otf", "font/*"]}), context)[:0] == []
    assert [error for error in pairs(with_input("x", {"type": "asset", "accepts": ["font/otf", "font/*"]}), context)
            if error[0][:2] == ("inputs", "x")] == []


def test_an_asset_has_no_default():
    spec = {"type": "asset", "accepts": ["image/*"], "default": "x"}
    assert pairs(with_input("x", spec)) == [(("inputs", "x", "default"), "unknown_field")]


def test_the_sniffed_type_table_is_what_the_sniffer_returns():
    samples = [
        b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF89a", b"RIFF\x00\x00\x00\x00WEBP", b"\x00\x00\x00\x00ftyp",
        b"RIFF\x00\x00\x00\x00WAVE", b"ID3", b"fLaC", b"OggS",
    ]
    assert {workflow_call.sniff(sample)[0] for sample in samples} == set(workflow_call.SNIFFED_MEDIA_TYPES)


# ------------------------------------------------------------------------------------------------- D6: generators


def test_a_generator_must_be_registered():
    spec = {"type": "integer", "generate": {"generator": "no_such_thing", "minimum": 0, "maximum": 9}}
    assert pairs(with_input("x", spec)) == [(("inputs", "x", "generate", "generator"), "generator_unknown")]


def test_the_registry_is_the_context_so_a_new_generator_is_reviewed_code_not_a_document():
    spec = {"type": "integer", "generate": {"generator": "fancy", "minimum": 0, "maximum": 9}}
    context = ContractContext(generators={**GENERATORS, "fancy": GENERATORS["random_integer"]})
    assert pairs(with_input("x", spec), context) == []


def test_a_generator_fills_the_types_it_declares():
    spec = {"type": "number", "generate": {"generator": "random_integer", "minimum": 0, "maximum": 9}}
    assert pairs(with_input("x", spec)) == [(("inputs", "x", "generate"), "generator_type_mismatch")]


def test_a_bounded_generator_needs_both_bounds():
    assert pairs(with_input("x", {"type": "integer", "generate": {"generator": "random_integer", "minimum": 0}})) == [
        (("inputs", "x", "generate", "maximum"), "required_missing")
    ]
    assert pairs(with_input("x", {"type": "integer", "generate": {"generator": "random_integer"}})) == [
        (("inputs", "x", "generate", "maximum"), "required_missing"),
        (("inputs", "x", "generate", "minimum"), "required_missing"),
    ]


@pytest.mark.parametrize(
    ("spec_bounds", "rule"),
    [({"minimum": 100}, {"minimum": 0, "maximum": 9}), ({"maximum": -1}, {"minimum": 0, "maximum": 9}), ({}, {"minimum": 9, "maximum": 0})],
)
def test_an_empty_range_is_refused(spec_bounds, rule):
    spec = {"type": "integer", **spec_bounds, "generate": {"generator": "random_integer", **rule}}
    assert pairs(with_input("x", spec)) == [(("inputs", "x", "generate"), "generator_range_empty")]


def test_a_default_and_a_generate_rule_are_exclusive():
    spec = {"type": "integer", "default": 1, "generate": {"generator": "random_integer", "minimum": 0, "maximum": 9}}
    assert pairs(with_input("x", spec)) == [(("inputs", "x", "generate"), "default_and_generate")]


def test_generate_is_never_a_value_of_default():
    spec = {"type": "integer", "default": {"generator": "random_integer"}}
    assert pairs(with_input("x", spec)) == [(("inputs", "x", "default"), "default_invalid")]


def test_a_gateway_only_input_needs_a_generator():
    assert pairs(with_input("x", {"type": "integer", "settable": False})) == [(("inputs", "x", "settable"), "settable_without_generate")]
    spec = {"type": "integer", "settable": False, "generate": {"generator": "random_integer", "minimum": 0, "maximum": 9}}
    assert pairs(with_input("x", spec)) == []


# ------------------------------------------------------------------------------------------------------ D5: outputs


def test_an_output_needs_a_media_type_and_a_valid_name():
    doc = base()
    doc["outputs"] = {"a": {}}
    assert pairs(doc) == [(("outputs", "a", "media_type"), "required_missing")]
    doc["outputs"] = {"bad/name": {"media_type": "image/png"}}
    assert pairs(doc) == [(("outputs", "bad/name"), "key_invalid")]
    doc["outputs"] = {}
    assert pairs(doc) == [(("outputs",), "limit_exceeded")]


def test_a_media_type_is_a_media_type():
    doc = base()
    doc["outputs"]["picture"]["media_type"] = "Image"
    assert pairs(doc) == [(("outputs", "picture", "media_type"), "pattern_mismatch")]


def test_an_output_count_is_ordered_and_under_the_deployment_limit():
    doc = base()
    doc["outputs"]["picture"]["count"] = {"min": 3, "max": 2}
    assert pairs(doc) == [(("outputs", "picture", "count"), "count_invalid")]
    doc["outputs"]["picture"]["count"] = {"min": 1, "max": 5}
    assert pairs(doc, ContractContext(max_assets=4)) == [(("outputs", "picture", "count", "max"), "count_over_limit")]
    assert pairs(doc, ContractContext(max_assets=5)) == []
    doc["outputs"]["picture"]["count"] = {"min": 0, "max": 0}
    assert pairs(doc) == [(("outputs", "picture", "count", "max"), "limit_exceeded")]


def test_a_result_schema_belongs_to_json_only():
    doc = base()
    doc["outputs"]["picture"]["schema"] = {"type": "string"}
    assert pairs(doc) == [(("outputs", "picture", "media_type"), "value_invalid")]


def test_a_result_schema_uses_the_input_grammar_without_media():
    doc = base()
    doc["outputs"]["facts"]["schema"] = {"type": "object", "properties": {"n": {"type": "integer", "default": "x"}}}
    assert pairs(doc) == [(("outputs", "facts", "schema", "properties", "n", "default"), "default_invalid")]
    doc["outputs"]["facts"]["schema"] = {"type": "array", "max_items": 3, "items": {"type": "asset", "accepts": ["image/*"]}}
    assert pairs(doc) == [(("outputs", "facts", "schema"), "asset_in_result_schema")]


def test_purpose_and_summary_have_word_limits():
    doc = base()
    doc["purpose"] = "one two three four five six seven eight nine"
    doc["summary"] = " ".join(["w"] * 41)
    assert sorted(pairs(doc)) == [(("purpose",), "too_many_words"), (("summary",), "too_many_words")]


# ---------------------------------------------------------------------------------------------- D8: all the errors


def test_every_error_is_listed_with_its_path_in_path_order_and_the_first_found_is_primary():
    doc = base()
    doc["zzz"] = 1
    doc["inputs"]["steps"]["default"] = 999
    doc["outputs"]["picture"]["count"] = {"min": 3, "max": 2}
    del doc["capability"]
    report = check_contract(doc)
    assert [(error.path, error.code) for error in report.errors] == [
        (("capability",), "required_missing"),
        (("inputs", "steps", "default"), "default_invalid"),
        (("outputs", "picture", "count"), "count_invalid"),
        (("zzz",), "unknown_field"),
    ]
    assert report.primary is not None and report.primary.code in {"required_missing", "unknown_field"}  # structural errors come first
    with pytest.raises(ContractErrors) as caught:
        report.raise_if_any()
    assert str(caught.value) == report.primary.message
    assert [item["pointer"] for item in report.as_list()][1] == "/inputs/steps/default"


def test_a_clean_report_raises_nothing():
    report = check_contract(base())
    assert report.ok and report.as_list() == []
    report.raise_if_any()


def test_value_problems_is_the_one_value_check():
    spec = {"type": "array", "max_items": 2, "items": {"type": "integer", "minimum": 0}}
    assert list(value_problems(spec, [1, 2], ("v",))) == []
    assert [(path, code) for path, code, _ in value_problems(spec, [1, -1], ("v",))] == [(("v", 1), "value_invalid")]


# ---------------------------------------------------------------------------------------------- the bundled corpus


def test_the_corpus_is_what_was_counted():
    assert len(BUNDLED) == 39


@pytest.mark.parametrize("path", BUNDLED, ids=lambda p: p.stem)
def test_every_bundled_workflow_converts_with_zero_errors(path):
    assert check_contract(yaml.safe_load(path.read_text(encoding="utf-8"))).as_list() == []


@pytest.mark.parametrize("path", V1_FIXTURES, ids=lambda p: p.stem)
def test_every_frozen_schema_one_workflow_converts_with_zero_errors(path):
    converted, _notes = convert_1_to_2(yaml.safe_load(path.read_text(encoding="utf-8")))
    assert check_contract(converted).as_list() == []


def test_the_bundled_files_are_schema_two_and_the_frozen_copies_are_schema_one():
    assert {yaml.safe_load(path.read_text(encoding="utf-8"))["schema_version"] for path in BUNDLED} == {2}
    assert {yaml.safe_load(path.read_text(encoding="utf-8"))["schema_version"] for path in V1_FIXTURES} == {1}
    assert [p.name for p in V1_FIXTURES] == [p.name for p in BUNDLED]


def test_the_bundled_files_are_what_the_converter_makes_of_the_frozen_copies_but_for_the_named_edits():
    named = {"ace-step-1.5-xl-turbo"}
    staged = {"flux2-klein-4b-edit", "flux2-klein-edit"}     # each lists the one result stage that applies its mask
    for path in V1_FIXTURES:
        converted, _ = convert_1_to_2(yaml.safe_load(path.read_text(encoding="utf-8")))
        bundled = yaml.safe_load((ROOT / "manifests" / "workflows" / path.name).read_text(encoding="utf-8"))
        if path.stem in staged:
            assert bundled["bindings"]["comfyui"].pop("result_processing") == ["post_composite_mask"], path.name
        if path.stem == "qwen-image-2.1-inpaint":       # the feather the workflow's own graph applies, not a default of 48
            spec = converted["inputs"]["mask_feather"]
            assert spec.pop("default") == 48
            assert bundled["inputs"]["mask_feather"]["description"].startswith(spec["description"])
            spec["description"] = bundled["inputs"]["mask_feather"]["description"]
        if path.stem not in named:
            assert without_section_additions(bundled, converted) == converted, path.name
    ace, _ = convert_1_to_2(yaml.safe_load((V1_FIXTURES[0].parent / "ace-step-1.5-xl-turbo.yml").read_text(encoding="utf-8")))
    bundled = yaml.safe_load((ROOT / "manifests" / "workflows" / "ace-step-1.5-xl-turbo.yml").read_text(encoding="utf-8"))
    assert set(ace["inputs"]) - set(bundled["inputs"]) == {"prompt"}


def test_the_converted_corpus_counts_match_the_design_except_for_the_seed_count():
    counts: dict[str, int] = {}
    for path in V1_FIXTURES:
        _doc, notes = convert_1_to_2(yaml.safe_load(path.read_text(encoding="utf-8")))
        for note in notes:
            counts[f"{note.path[-1]}:{note.operation}"] = counts.get(f"{note.path[-1]}:{note.operation}", 0) + 1
    assert counts["trusted:remove"] == counts["source:remove"] == counts["schema_version:set"] == 39
    assert counts["capability:backfill"] == 34
    assert counts["requires:move"] == 30
    assert counts["accepts:add"] == 32
    assert counts["seed:add"] == 2
    assert counts["generate:add"] == 34  # the design said 33: 31 declared and mapped, and 2 added


def test_the_converted_corpus_has_no_authority_field_and_no_v1_shape():
    for path in V1_FIXTURES:
        doc, _ = convert_1_to_2(yaml.safe_load(path.read_text(encoding="utf-8")))
        assert not set(doc) & {"trusted", "source", "requires", "exposure", "trust_tier", "resource_tier", "tags"}, path.name
        assert all(spec["type"] != "image" for spec in doc["inputs"].values())
        assert all("transport" not in block and "compose" not in doc for block in doc["bindings"].values())


# ----------------------------------------------------------------------------------------------------- the converter


def load(name: str) -> dict:
    return yaml.safe_load((ROOT / "tests" / "fixtures" / "workflows_v1" / f"{name}.yml").read_text(encoding="utf-8"))


def test_the_converter_does_not_touch_its_argument_and_is_idempotent():
    original = load("flux2-klein")
    snapshot = copy.deepcopy(original)
    converted, notes = convert_1_to_2(original)
    assert original == snapshot and notes
    again, more = convert_1_to_2(converted)
    assert again == converted and more == []


def test_a_document_that_is_not_schema_one_passes_through():
    doc = base()
    assert convert_1_to_2(doc) == (doc, [])
    assert convert_1_to_2({"schema_version": 7, "x": 1})[1] == []


def test_the_capability_backfill_is_recorded_and_a_declared_capability_is_kept():
    for path in V1_FIXTURES:
        v1 = yaml.safe_load(path.read_text(encoding="utf-8"))
        doc, notes = convert_1_to_2(v1)
        assert doc["capability"] == v1.get("capability", doc["capability"]), path.name
        backfilled = any(note.operation == "backfill" for note in notes)
        assert backfilled == ("capability" not in v1)
        assert ("azizam.dev/backfill" in doc.get("annotations", {})) == backfilled


def test_a_declared_capability_is_kept():
    doc, notes = convert_1_to_2(load("yue2-song"))
    assert doc["capability"] == "music" and "annotations" not in doc
    assert not [n for n in notes if n.path == ("capability",)]


def test_requirements_move_into_the_one_binding():
    v1 = load("anima-turbo")
    doc, _ = convert_1_to_2(v1)
    assert "requires" not in doc and doc["bindings"]["comfyui"]["requires"] == v1["requires"]


def test_requirements_with_two_bindings_stay_put_so_the_check_shows_them():
    v1 = load("anima-turbo")
    v1["bindings"]["second"] = {"map": {}}
    doc, _ = convert_1_to_2(v1)
    assert pairs(doc) == [(("requires",), "unknown_field")]


def test_a_declared_media_type_becomes_the_accepted_type_and_none_means_the_old_constant():
    video, _ = convert_1_to_2(load("video-scail-2-replace"))
    assert video["inputs"]["source_video"]["accepts"] == ["video/*"] and "media_type" not in video["inputs"]["source_video"]
    edit, _ = convert_1_to_2(load("flux2-klein-edit"))
    assert edit["inputs"]["source_image"]["accepts"] == ["image/*"]


def test_the_seed_gets_the_generator_and_a_map_key_without_an_input_becomes_a_gateway_only_input():
    declared, _ = convert_1_to_2(load("flux2-klein"))
    assert declared["inputs"]["seed"]["generate"] == {"generator": "random_integer", "minimum": 0, "maximum": 4294967295}
    hidden, notes = convert_1_to_2(load("flux2-klein-edit"))
    assert hidden["inputs"]["seed"] == {"type": "integer", "minimum": 0, "settable": False,
                                        "generate": {"generator": "random_integer", "minimum": 0, "maximum": 4294967295}}
    assert ("inputs", "seed") in [n.path for n in notes if n.operation == "add"]


def test_a_seed_the_bindings_never_fill_gets_no_generator():
    doc, _ = convert_1_to_2(load("image-identity"))
    assert "generate" not in doc["inputs"]["seed"]


def test_the_mesh_document_converts_to_an_ordinary_one():
    doc, _ = convert_1_to_2(load("mesh.generate.trellis-2"))
    assert doc["inputs"]["image"] == {"label": "picture", "help": "The picture to turn into a 3D model. PNG, JPEG or WebP.",
                                      "type": "asset", "accepts": ["image/png", "image/jpeg", "image/webp"], "required": True}
    assert doc["inputs"]["extensions"]["opaque"] is True
    assert "modality" not in doc["outputs"]["mesh"]
    assert list(doc["bindings"]) == ["http-multipart"]
    binding = doc["bindings"]["http-multipart"]
    assert binding["service"] == "trellis" and binding["path"] == "/generate" and "transport" not in binding
    assert "generate" not in doc["inputs"]["seed"]


def test_a_broken_document_is_not_repaired_by_the_converter():
    v1 = load("flux2-klein")
    v1["inputs"]["steps"]["maxium"] = 3
    doc, _ = convert_1_to_2(v1)
    assert pairs(doc) == [(("inputs", "steps", "maxium"), "unknown_field")]


def test_the_converter_survives_documents_of_the_wrong_shape():
    for bad in ({"schema_version": 1}, {"schema_version": 1, "inputs": [], "outputs": 3, "bindings": []},
                {"schema_version": 1, "inputs": {"a": 5}, "bindings": {"b": None}, "requires": {}}):
        convert_1_to_2(bad)



# Base 22e712f59c3fe04e0bf2b9a5a930e666a446d271: literal byte goldens for the
# backfill table and the inline malformed documents already exercised above.
# These are NOT the separate 38-file frozen corpus goldens.
@pytest.mark.parametrize("source, expected", [
    ({"schema_version": 1, "intent": "image.edit"},
     b'{"schema_version":2,"intent":"image.edit","capability":"image","annotations":{"azizam.dev/backfill":{"capability":"intent-prefix rule, schema 1 to 2"}}}'),
    ({"schema_version": 1, "intent": "video.edit"},
     b'{"schema_version":2,"intent":"video.edit","capability":"video","annotations":{"azizam.dev/backfill":{"capability":"intent-prefix rule, schema 1 to 2"}}}'),
    ({"schema_version": 1, "intent": "mesh.generate"},
     b'{"schema_version":2,"intent":"mesh.generate","capability":"mesh","annotations":{"azizam.dev/backfill":{"capability":"intent-prefix rule, schema 1 to 2"}}}'),
    ({"schema_version": 1, "intent": "audio.generate"},
     b'{"schema_version":2,"intent":"audio.generate","capability":"audio","annotations":{"azizam.dev/backfill":{"capability":"intent-prefix rule, schema 1 to 2"}}}'),
    ({"schema_version": 1, "intent": "speech.synthesis"},
     b'{"schema_version":2,"intent":"speech.synthesis","capability":"audio","annotations":{"azizam.dev/backfill":{"capability":"intent-prefix rule, schema 1 to 2"}}}'),
    ({"schema_version": 1, "intent": "unfamiliar.run"},
     b'{"schema_version":2,"intent":"unfamiliar.run","capability":"workflow","annotations":{"azizam.dev/backfill":{"capability":"intent-prefix rule, schema 1 to 2"}}}'),
    ({"schema_version": 1, "intent": "image.edit", "capability": "declared"},
     b'{"schema_version":2,"intent":"image.edit","capability":"declared"}'),
    ({"schema_version": 1},
     b'{"schema_version":2}'),
    ({"schema_version": 1, "inputs": [], "outputs": 3, "bindings": []},
     b'{"schema_version":2,"inputs":[],"outputs":3,"bindings":[]}'),
    ({"schema_version": 1, "inputs": {"a": 5}, "bindings": {"b": None}, "requires": {}},
     b'{"schema_version":2,"inputs":{"a":5},"bindings":{"b":null},"requires":{}}'),
])
def test_schema_one_backfill_and_inline_fixture_bytes_match_base(source, expected):
    original = copy.deepcopy(source)
    converted, notes = convert_1_to_2(source)
    assert json.dumps(converted, ensure_ascii=False, separators=(",", ":")).encode("utf-8") == expected
    expected_notes = [(("schema_version",), "set", "the document dialect is 2")]
    if "intent" in source and "capability" not in source:
        expected_notes.append((("capability",), "backfill", "inferred once from the intent prefix, as the catalog did"))
    assert [(note.path, note.operation, note.reason) for note in notes] == expected_notes
    assert source == original
    assert convert_1_to_2(converted) == (converted, [])


def _capability_prefix_rules():
    return {"schema_version": 1, "prefixes": [
        {"prefix": "image.", "capability": "image"},
        {"prefix": "video.", "capability": "video"},
        {"prefix": "mesh.", "capability": "mesh"},
        {"prefix": "audio.", "capability": "audio"},
        {"prefix": "speech.", "capability": "audio"},
    ], "default": "workflow"}


def _use_capability_prefix_file(tmp_path, monkeypatch, document):
    from gateway import workflow_convert

    path = tmp_path / "capability-prefixes.yml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    # On base the attribute is absent and ignored, so behavioral assertions fail,
    # not collection or monkeypatch setup.
    monkeypatch.setattr(workflow_convert, "SCHEMA1_CAPABILITY_PREFIXES_PATH", path, raising=False)
    return path


def test_schema_one_backfill_reads_changed_data_on_each_conversion(tmp_path, monkeypatch):
    rules = _capability_prefix_rules()
    path = _use_capability_prefix_file(tmp_path, monkeypatch, rules)
    assert convert_1_to_2({"schema_version": 1, "intent": "image.edit"})[0]["capability"] == "image"
    rules["prefixes"][0]["capability"] = "changed"
    rules["default"] = "other"
    path.write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")
    assert convert_1_to_2({"schema_version": 1, "intent": "image.edit"})[0]["capability"] == "changed"
    assert convert_1_to_2({"schema_version": 1, "intent": "unfamiliar.run"})[0]["capability"] == "other"


def test_schema_one_prefix_rows_are_ordered_and_the_default_is_data(tmp_path, monkeypatch):
    rules = {"schema_version": 1, "prefixes": [
        {"prefix": "image.edit", "capability": "specific"},
        {"prefix": "image.", "capability": "broad"},
    ], "default": "fallback"}
    path = _use_capability_prefix_file(tmp_path, monkeypatch, rules)
    assert convert_1_to_2({"schema_version": 1, "intent": "image.edit"})[0]["capability"] == "specific"
    rules["prefixes"].reverse()
    path.write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")
    assert convert_1_to_2({"schema_version": 1, "intent": "image.edit"})[0]["capability"] == "broad"
    rules["prefixes"] = []
    path.write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")
    assert convert_1_to_2({"schema_version": 1, "intent": "image.edit"})[0]["capability"] == "fallback"


@pytest.mark.parametrize("change, message", [
    (lambda d: d.update(unknown=True), "unknown keys"),
    (lambda d: d.pop("default"), "default"),
    (lambda d: d["prefixes"][0].update(prefix=""), "prefixes\\[0\\].prefix"),
    (lambda d: d["prefixes"][0].update(prefix="   "), "prefixes\\[0\\].prefix"),
    (lambda d: d["prefixes"].append(dict(d["prefixes"][0])), "duplicate prefix"),
    (lambda d: d["prefixes"][0].update(unknown=True), "unknown keys"),
    (lambda d: d.pop("schema_version"), "schema_version"),
    (lambda d: d.update(schema_version=2), "schema_version"),
    (lambda d: d.update(schema_version=True), "schema_version"),
    (lambda d: d.pop("prefixes"), "prefixes"),
    (lambda d: d.update(prefixes={}), "prefixes"),
    (lambda d: d["prefixes"].append("broken"), "must be a mapping"),
    (lambda d: d["prefixes"][0].pop("prefix"), "prefixes\\[0\\].prefix"),
    (lambda d: d["prefixes"][0].update(prefix=3), "prefixes\\[0\\].prefix"),
    (lambda d: d["prefixes"][0].pop("capability"), "capability"),
    (lambda d: d["prefixes"][0].update(capability=""), "capability"),
    (lambda d: d.update(default=None), "default"),
    # A padded value would load, never match, and be written verbatim.
    (lambda d: d["prefixes"][0].update(prefix=" " + d["prefixes"][0]["prefix"]), "prefixes\\[0\\].prefix"),
    (lambda d: d["prefixes"][0].update(capability=d["prefixes"][0]["capability"] + " "), "capability"),
    (lambda d: d.update(default=" " + d["default"]), "default"),
])
def test_schema_one_capability_data_is_validated_before_use(tmp_path, monkeypatch, change, message):
    rules = _capability_prefix_rules()
    change(rules)
    _use_capability_prefix_file(tmp_path, monkeypatch, rules)
    # Even a matching first row must not hide a later malformed or duplicate row.
    with pytest.raises(ValueError, match=message):
        convert_1_to_2({"schema_version": 1, "intent": "image.edit"})


@pytest.mark.parametrize("field, message", [
    ("prefix", r"prefixes\[0\]\.prefix"),
    ("capability", r"prefixes\[0\]\.capability"),
    ("default", "default"),
])
@pytest.mark.parametrize("control", ["\t", "\r", "\n"], ids=["tab", "CR", "LF"])
def test_schema_one_capability_data_refuses_embedded_controls(tmp_path, monkeypatch, field, message, control):
    rules = _capability_prefix_rules()
    target = rules if field == "default" else rules["prefixes"][0]
    original = target[field]
    target[field] = original[:2] + control + original[2:]
    path = _use_capability_prefix_file(tmp_path, monkeypatch, rules)
    # The default must be validated even though image.edit matches the first row.
    with pytest.raises(ValueError, match=message + r".*tab, CR or LF") as caught:
        convert_1_to_2({"schema_version": 1, "intent": "image.edit"})
    assert str(path) in str(caught.value)


@pytest.mark.parametrize("text, message", [
    ("[]", "must be a mapping"),
    ("schema_version: [", "cannot read"),
])
def test_schema_one_capability_data_shape_and_yaml_errors_are_clear(tmp_path, monkeypatch, text, message):
    path = _use_capability_prefix_file(tmp_path, monkeypatch, _capability_prefix_rules())
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        convert_1_to_2({"schema_version": 1, "intent": "image.edit"})


def test_schema_one_missing_data_refuses_backfill_but_not_explicit_or_other_dialects(tmp_path, monkeypatch):
    path = _use_capability_prefix_file(tmp_path, monkeypatch, _capability_prefix_rules())
    path.unlink()
    with pytest.raises(ValueError, match="cannot read"):
        convert_1_to_2({"schema_version": 1, "intent": "image.edit"})
    assert convert_1_to_2({"schema_version": 1, "intent": "image.edit", "capability": "kept"})[0]["capability"] == "kept"
    assert convert_1_to_2({"schema_version": 2, "intent": "image.edit"}) == (
        {"schema_version": 2, "intent": "image.edit"}, [])
    assert convert_1_to_2({"schema_version": 1})[0] == {"schema_version": 2}


# ------------------------------------------------------------------------------------------- the token test


def forbidden_names() -> set[str]:
    names, families = vocabulary()
    services = yaml.safe_load((ROOT / "registry" / "services.yml").read_text(encoding="utf-8"))["records"]
    ids = {str(record["id"]).lower() for record in services if isinstance(record, dict) and record.get("id")}
    return {name for name in names | families | ids if len(name) >= 4} | {"seed"}


def hits(text: str, names: set[str]) -> list[str]:
    lowered = text.lower()
    return sorted(name for name in names if re.search(r"(?<![a-z0-9_-])" + re.escape(name) + r"(?![a-z0-9_-])", lowered))


def test_the_token_test_sees_the_names_it_is_built_from():
    names = forbidden_names()
    assert {"trellis", "comfyui", "node-graph", "image.edit", "image", "flux2-klein", "litellm"} <= names
    assert hits("a binding for trellis and a seed", names) == ["seed", "trellis"]


@pytest.mark.parametrize("path", [ROOT / "gateway" / "workflow_contract.py", SCHEMA_PATH], ids=lambda p: p.name)
def test_the_contract_names_no_model_engine_service_workflow_or_modality(path):
    # ``gateway`` is the package and the in-process binding key, not an engine.
    assert hits(path.read_text(encoding="utf-8"), forbidden_names() - {"gateway"}) == []


# ---------------------------------------------------------------------------------------------------------- command


def test_the_command_checks_the_bundled_directory(capsys):
    assert command.main(["check", str(ROOT / "manifests" / "workflows")]) == 0
    out = capsys.readouterr().out
    assert out.count("0 errors") == 39 and "converted from schema 1" not in out


def test_the_command_reports_every_error_with_path_and_code(tmp_path, capsys):
    v1 = load("flux2-klein")
    v1["inputs"]["steps"]["maxium"] = 3
    v1["exposure"] = "default"
    (tmp_path / "bad.yml").write_text(yaml.safe_dump(v1), encoding="utf-8")
    assert command.main(["check", str(tmp_path), "--json"]) == 1
    import json

    (result,) = json.loads(capsys.readouterr().out)
    assert sorted((e["pointer"], e["code"]) for e in result["errors"]) == [("/exposure", "unknown_field"), ("/inputs/steps/maxium", "unknown_field")]


def test_the_command_converts_to_a_new_file_and_never_overwrites(tmp_path, capsys):
    source = ROOT / "tests" / "fixtures" / "workflows_v1" / "mesh.generate.trellis-2.yml"
    target = tmp_path / "out.yml"
    assert command.main(["convert", str(source), "-o", str(target)]) == 0
    assert "rename" in capsys.readouterr().err
    assert check_contract(yaml.safe_load(target.read_text(encoding="utf-8"))).ok
    assert command.main(["convert", str(source), "-o", str(target)]) == 2
    assert command.main(["check", str(tmp_path / "missing.yml")]) == 2


def test_the_command_prints_converted_yaml_without_an_output_file(capsys):
    assert command.main(["convert", str(ROOT / "tests" / "fixtures" / "workflows_v1" / "flux2-klein.yml")]) == 0
    assert yaml.safe_load(capsys.readouterr().out)["schema_version"] == 2


# ------------------------------------------------------------------------------------------------------ review round 1


def with_result_schema(schema: dict) -> dict:
    doc = base()
    doc["outputs"]["facts"]["schema"] = schema
    return doc


@pytest.mark.parametrize("schema, path", [
    ({"type": "object", "properties": ["bad"]}, ("outputs", "facts", "schema", "properties")),
    ({"type": "integer", "properties": "bad"}, ("outputs", "facts", "schema", "properties")),
    ({"type": "object", "properties": {"a": {"type": "object", "properties": ["bad"]}}},
     ("outputs", "facts", "schema", "properties", "a", "properties")),
    ({"type": "array", "max_items": 2, "items": {"type": "object", "properties": 5}}, ("outputs", "facts", "schema", "items", "properties")),
])
def test_a_malformed_result_schema_is_reported_not_a_crash(schema, path):
    errors = check_contract(with_result_schema(schema)).errors
    assert [e.path for e in errors] == [path]


def test_the_command_reports_a_malformed_result_schema(tmp_path, capsys):
    doc = with_result_schema({"type": "object", "properties": ["bad"]})
    path = tmp_path / "bad.yml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    assert command.main(["check", str(path)]) == 1
    assert "/outputs/facts/schema/properties" in capsys.readouterr().out


def test_the_raised_errors_are_the_reports_errors_when_the_report_is_over_the_limit():
    doc = base()
    for index in range(105):
        doc[f"extra{index:03d}"] = 1
    report = check_contract(doc)
    assert len(report.errors) == 101 and report.errors[-1].code == "too_many_errors"
    with pytest.raises(ContractErrors) as raised:
        report.raise_if_any()
    assert list(raised.value.errors) == list(report.errors)
    assert [e.code for e in raised.value.errors].count("too_many_errors") == 1


def two_flat_bindings() -> dict:
    v1 = load("mesh.generate.trellis-2")
    v1["bindings"] = {
        "alpha": {"transport": {"kind": "http-multipart", "path": "/a"}, "map": {}},
        "beta": {"transport": {"kind": "http-multipart", "path": "/b"}, "map": {}},
    }
    return v1


def test_two_bindings_that_flatten_to_one_key_both_stay_and_the_check_says_so():
    converted, notes, report = convert_and_check(two_flat_bindings())
    assert converted["bindings"]["alpha"]["transport"]["path"] == "/a"
    assert converted["bindings"]["beta"]["transport"]["path"] == "/b"
    assert "http-multipart" not in converted["bindings"]
    assert sorted(n.path for n in notes if n.operation == "conflict") == [("bindings", "alpha"), ("bindings", "beta")]
    assert [(e.path, e.code) for e in report.errors if e.code == "conversion_conflict"] == [
        (("bindings", "alpha"), "conversion_conflict"), (("bindings", "beta"), "conversion_conflict")]
    assert not report.ok


def test_one_flat_binding_still_converts():
    v1 = two_flat_bindings()
    del v1["bindings"]["beta"]
    converted, notes, report = convert_and_check(v1)
    assert list(converted["bindings"]) == ["http-multipart"] and not [n for n in notes if n.operation == "conflict"]


@pytest.mark.parametrize("count, expected", [
    ({"min": 3.0, "max": 2.0}, [(("outputs", "picture", "count"), "count_invalid")]),
    ({"min": 1.0, "max": 17.0}, [(("outputs", "picture", "count", "max"), "count_over_limit")]),
    ({"min": 17.0, "max": 20.0}, [(("outputs", "picture", "count", "max"), "count_over_limit"),
                                  (("outputs", "picture", "count", "min"), "count_over_limit")]),
])
def test_an_integral_float_count_gets_the_same_meaning_as_an_integer(count, expected):
    doc = base()
    doc["outputs"]["picture"]["count"] = count
    context = ContractContext(max_assets=16)
    assert pairs(doc, context) == expected


def test_a_float_count_with_a_fraction_is_left_to_the_schema():
    doc = base()
    doc["outputs"]["picture"]["count"] = {"min": 1.5, "max": 2}
    assert pairs(doc) == [(("outputs", "picture", "count", "min"), "type_mismatch")]


def generated(spec_extra: dict, low: int, high: int) -> dict:
    return with_input("n", {"type": "integer", "generate": {"generator": "random_integer", "minimum": low, "maximum": high}, **spec_extra})


@pytest.mark.parametrize("extra, low, high", [
    ({"enum": [1]}, 2, 2),
    ({"step": 2, "offset": 1}, 2, 2),
    ({"step": 5}, 6, 9),
    ({"enum": [1, 9]}, 2, 8),
])
def test_a_generator_whose_values_the_input_would_all_refuse_is_refused(extra, low, high):
    assert pairs(generated(extra, low, high)) == [(("inputs", "n", "generate"), "generator_range_empty")]


@pytest.mark.parametrize("extra, low, high", [
    ({"enum": [2]}, 2, 2),
    ({"step": 2, "offset": 1}, 3, 3),
    ({"step": 5}, 5, 9),
    ({"enum": [1, 9]}, 2, 9),
    ({}, 2, 2),
])
def test_a_generator_with_one_value_the_input_accepts_is_valid(extra, low, high):
    assert pairs(generated(extra, low, high)) == []


def test_a_grid_with_no_integer_on_it_is_refused_however_long_the_range():
    # step 2 from 0.5 holds only halves; a bounded search of the first points used to call 0..10000 feasible.
    assert pairs(generated({"step": 2, "offset": 0.5}, 0, 10000)) == [(("inputs", "n", "generate"), "generator_range_empty")]
    assert pairs(generated({"step": 2, "offset": 0.5}, 0, 10**12)) == [(("inputs", "n", "generate"), "generator_range_empty")]


@pytest.mark.parametrize("extra, low, high, feasible", [
    ({"step": 3, "offset": 1}, 0, 10**9, True),
    ({"step": 1000000, "offset": 7}, 8, 999999, False),
    ({"step": 1000000, "offset": 7}, 8, 1000007, True),
    ({"step": 0.5}, 3, 3, True),
    ({"step": 1.5}, 1, 1, False),
    ({"step": 1.5, "offset": 0.5}, 2, 2, True),
    ({"step": 0.5, "offset": 0.25}, 0, 10**6, False),
    ({"step": 4, "offset": 2.0}, 5, 6, True),
])
def test_feasibility_is_decided_exactly(extra, low, high, feasible):
    from gateway.workflow_contract import _generates_a_value
    assert _generates_a_value({"type": "integer", **extra}, low, high) is feasible


@pytest.mark.parametrize("spec_extra", [{"step": 2, "offset": 0.5}, {"step": 1.5}, {"step": 3, "offset": 1}, {"step": 0.5, "offset": 0.25}])
def test_the_exact_grid_and_the_validator_judge_a_sample_alike(spec_extra):
    from gateway.generated_defaults import _first_on_grid
    spec = {"type": "integer", **spec_extra}
    first = _first_on_grid(-50, Fraction(spec["step"]), Fraction(spec.get("offset", 0)))
    sample = range(-50, 50)
    on_grid = {n for n in sample if validate_input_value("n", n, spec) is None}
    assert (min(on_grid) if on_grid else None) == (first if first is not None and first < 50 else None)


@pytest.mark.parametrize("field_, bad, expected", [
    ("maximum", 10**400, []),
    ("step", 1e-320, []),  # decimal meaning: 1e-320 divides every integer, so the range holds a value
    ("step", float("nan"), [(("inputs", "x", "step"), "number_not_finite")]),
])
def test_arithmetic_on_a_huge_or_odd_number_reports_instead_of_raising(field_, bad, expected):
    # 10**400 overflowed math.isfinite, 1e-320 overflowed ceil(inf) and NaN raised ValueError.
    spec = {"type": "integer", "minimum": 1, "generate": {"generator": "random_integer", "minimum": 1, "maximum": 2}}
    spec[field_] = bad
    if field_ == "step":
        spec["maximum"] = 2
    assert pairs(with_input("x", spec)) == expected


def test_a_huge_bound_on_the_generate_rule_is_exact():
    spec = {"type": "integer", "generate": {"generator": "random_integer", "minimum": 1, "maximum": 10**400}}
    assert pairs(with_input("x", spec)) == []
    spec = {"type": "integer", "maximum": 10**400, "generate": {"generator": "random_integer", "minimum": 5, "maximum": 2}}
    assert pairs(with_input("x", spec)) == [(("inputs", "x", "generate"), "generator_range_empty")]


def test_a_binding_that_would_flatten_onto_an_existing_key_keeps_both_and_says_so():
    v1 = two_flat_bindings()
    del v1["bindings"]["beta"]
    v1["bindings"]["http-multipart"] = {"service": "other", "map": {}}
    converted, notes, report = convert_and_check(v1)
    assert converted["bindings"]["alpha"]["transport"]["path"] == "/a"
    assert converted["bindings"]["http-multipart"] == {"service": "other", "map": {}}
    assert [(e.path, e.code) for e in report.errors if e.code == "conversion_conflict"] == [
        (("bindings", "alpha"), "conversion_conflict"), (("bindings", "http-multipart"), "conversion_conflict")]


def test_structural_errors_and_conflicts_share_one_cap_and_one_marker():
    v1 = two_flat_bindings()
    for index in range(105):
        v1.setdefault("inputs", {})[f"i{index:03d}"] = {"type": "integer", "maxium": 1}
    _, notes, report = convert_and_check(v1)
    conflicts = [e for e in report.errors if e.code == "conversion_conflict"]
    markers = [e for e in report.errors if e.code == "too_many_errors"]
    assert len(conflicts) == 2 or markers  # the conflicts may sit in the omitted tail; the count below is exact
    raw = check_contract(convert_1_to_2(v1)[0])
    omitted = int(raw.errors[-1].message.split()[0]) + 2
    assert len(markers) == 1 and len(report.errors) == 101
    assert markers[0].message == f"{omitted} more errors are not listed"


def test_a_broken_bound_hides_the_generator_range_error_it_would_cause():
    spec = {"type": "integer", "minimum": 1.5, "generate": {"generator": "random_integer", "minimum": 1, "maximum": 1}}
    assert pairs(with_input("x", spec)) == [(("inputs", "x", "minimum"), "integer_expected")]


def test_a_decimal_step_with_a_negative_decimal_offset_is_feasible_when_the_validator_accepts_it():
    # 1.1 means 11/10 to the author: (1 - -0.1) / 1.1 = 1. Binary-float fractions called this grid empty.
    spec = {"type": "integer", "step": 1.1, "offset": -0.1}
    assert validate_input_value("n", 1, spec) is None
    assert pairs(generated({"step": 1.1, "offset": -0.1}, 1, 1)) == []


def test_a_singleton_range_beyond_float_precision_is_judged_as_the_validator_judges_it():
    spec = {"type": "integer", "step": 3}
    n = 2**53 + 1  # a multiple of 3; float modulo rounded it and refused it
    assert validate_input_value("n", n, spec) is None
    assert pairs(generated({"step": 3}, n, n)) == []
    assert validate_input_value("n", n + 1, spec) is not None
    assert pairs(generated({"step": 3}, n + 1, n + 1)) == [(("inputs", "n", "generate"), "generator_range_empty")]


def test_feasibility_and_validation_agree_over_a_sweep_of_grids():
    from gateway.workflow_contract import _generates_a_value
    steps = [1, 2, 3, 7, 0.1, 0.25, 1.1, 2.5, 10**30, 3 * 10**15]
    offsets = [0, -1, -0.1, 0.5, 1.1, 5, 12, 10**15]
    values = [-7, -1, 0, 1, 2, 3, 5, 6, 10, 11, 12, 100, 2**53 - 1, 2**53, 2**53 + 1, 2**53 + 3, 10**30, 10**30 + 1, 10**30 + 11]
    checked = 0
    for step in steps:
        for offset in offsets:
            spec = {"type": "integer", "step": step, "offset": offset}
            for n in values:
                assert _generates_a_value(spec, n, n) is (validate_input_value("n", n, spec) is None), (step, offset, n)
                checked += 1
    assert checked == len(steps) * len(offsets) * len(values)


def test_huge_integers_and_steps_give_a_message_or_a_report_error_not_an_exception():
    huge = 10**400
    assert pairs(generated({"enum": [huge], "step": 2}, huge, huge)) == []
    assert validate_input_value("n", huge, {"type": "integer", "step": 2}) is None
    assert validate_input_value("n", huge + 1, {"type": "integer", "step": 2}) is not None
    assert validate_input_value("n", 1, {"type": "integer", "enum": [1], "step": huge}) is not None
    assert pairs(generated({"enum": [1], "step": huge}, 1, 1)) == [(("inputs", "n", "generate"), "generator_range_empty")]


def test_a_decimal_step_accepts_what_float_modulo_wrongly_refused():
    assert validate_input_value("x", 0.3, {"type": "number", "step": 0.1}) is None
    assert validate_input_value("x", 0.35, {"type": "number", "step": 0.1}) is not None
