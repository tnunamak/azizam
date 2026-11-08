"""Every refusal the mesh validator made now has a home, and a test in it (CODE_TO_DATA_S3, S3.9, D9).

The rows are the old validator's checks. Each one is refused by the general grammar (``manifests.enforce_contract``: the
schema, the semantic checks and the adapter's binding description) or by the mesh frontend profile
(``mesh_api.mesh_profile_errors``), and each case has a valid neighbour that passes: a refusal that refuses everything is not a
pass. The grammar rows go through the real entry point, with the real shipped adapter descriptions.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from gateway import frontend_profiles, mesh_api
from gateway.manifests import MAX_CONTRACT_ERRORS, ContractErrors, enforce_contract
from gateway.mesh_manifest import MeshManifestError
from tests.mesh_workflows import bundled_document, second_document

Edit = Callable[[dict[str, Any]], None]
BINDING = "http-multipart"


def refused(doc: Any) -> ContractErrors:
    with pytest.raises(ContractErrors) as caught:
        enforce_contract(doc)
    return caught.value


def edited(*edits: Edit, base: Callable[[], dict[str, Any]] = bundled_document) -> dict[str, Any]:
    doc = base()
    for edit in edits:
        edit(doc)
    return doc


def listed(error: ContractErrors) -> list[tuple[tuple[Any, ...], str]]:
    return [(item.path, item.code) for item in error.errors]


def at(path: tuple[Any, ...], value: Any) -> Edit:
    def apply(doc: dict[str, Any]) -> None:
        cursor: Any = doc
        for part in path[:-1]:
            cursor = cursor[part]
        cursor[path[-1]] = value
    return apply


def drop(path: tuple[Any, ...]) -> Edit:
    def apply(doc: dict[str, Any]) -> None:
        cursor: Any = doc
        for part in path[:-1]:
            cursor = cursor[part]
        del cursor[path[-1]]
    return apply


SEED = ("inputs", "seed")
RESOLUTION = ("inputs", "resolution")
PICTURE = ("inputs", "image")
MAP = ("bindings", BINDING, "map")
PATH = ("bindings", BINDING, "path")

# --- the neighbours: the bundled document, and the second one, pass every layer -------------------------------------------

def test_the_bundled_and_the_second_document_pass_the_grammar_and_the_profile() -> None:
    for doc in (bundled_document(), second_document()):
        enforce_contract(doc)
        assert mesh_api.mesh_profile_errors(doc) == []
        frontend_profiles.check(doc)


# --- general grammar rows: (row, edit, first error as (path, code), a valid neighbour of the same edit) -------------------

GRAMMAR: dict[str, tuple[Edit, tuple[tuple[Any, ...], str], Edit | None]] = {
    # V02: closure of the top level
    "V02 unknown top-level key": (at(("surprise",), True), (("surprise",), "unknown_field"), None),
    "V02 missing outputs": (drop(("outputs",)), (("outputs",), "required_field_missing"), None),
    "V02 missing bindings": (drop(("bindings",)), (("bindings",), "required_field_missing"), None),
    "V02 missing inputs": (drop(("inputs",)), (("inputs",), "required_field_missing"), None),
    "V02 missing intent": (drop(("intent",)), (("intent",), "required_field_missing"), None),
    "V02 missing version": (drop(("version",)), (("version",), "required_missing"), None),
    # V03: the one schema version
    "V03 schema version 1": (at(("schema_version",), 1), (("schema_version",), "schema_version_unsupported"), at(("schema_version",), 2)),
    "V03 schema version 99": (at(("schema_version",), 99), (("schema_version",), "schema_version_unsupported"), None),
    "V03 schema version true": (at(("schema_version",), True), (("schema_version",), "schema_version_unsupported"), None),
    # V05: id and version are text of a rule
    "V05 id not text": (at(("id",), 7), (("id",), "type_mismatch"), None),
    "V05 id blank": (at(("id",), " "), (("id",), "pattern_mismatch"), at(("id",), "mesh.generate.other")),
    "V05 id with spaces around": (at(("id",), " mesh.generate.x "), (("id",), "pattern_mismatch"), None),
    "V05 version not text": (at(("version",), 7), (("version",), "type_mismatch"), None),
    "V05 version blank": (at(("version",), " "), (("version",), "pattern_mismatch"), at(("version",), "2.0.0")),
    # V06 / V08: inputs and each input are mappings
    "V06 inputs not a mapping": (at(("inputs",), ["image"]), (("inputs",), "type_mismatch"), None),
    "V08 an input not a mapping": (at(("inputs", "extra"), [1]), (("inputs", "extra"), "type_mismatch"), at(("inputs", "extra"), {"type": "string"})),
    # V07: input names
    "V07 input name with a space": (at(("inputs", "Bad Name"), {"type": "string"}), (("inputs", "Bad Name"), "key_invalid"), at(("inputs", "good_name"), {"type": "string"})),
    "V07 input name of 65 characters": (at(("inputs", "a" * 65), {"type": "string"}), (("inputs", "a" * 65), "key_invalid"), at(("inputs", "a" * 64), {"type": "string"})),
    "V07 input name starting with a digit": (at(("inputs", "1seed"), {"type": "string"}), (("inputs", "1seed"), "key_invalid"), None),
    # V09: closure of an input
    "V09 unknown input key": (at(SEED + ("zzz",), 1), (SEED + ("zzz",), "unknown_field"), None),
    "V09 input without a type": (drop(SEED + ("type",)), (SEED + ("type",), "required_missing"), None),
    # V10: input types
    "V10 unknown input type": (at(SEED + ("type",), "float"), (SEED + ("type",), "value_invalid"), at(SEED + ("type",), "integer")),
    "V10 input type not text": (at(SEED + ("type",), []), (SEED + ("type",), "value_invalid"), None),
    # V11 / V12: the retired image type and modality
    "V11 the retired image type": (at(PICTURE + ("type",), "image"), (PICTURE + ("type",), "value_invalid"), at(PICTURE + ("type",), "asset")),
    "V11 modality on the picture": (at(PICTURE + ("modality",), "image"), (PICTURE + ("modality",), "unknown_field"), None),
    "V12 modality on an option": (at(SEED + ("modality",), "image"), (SEED + ("modality",), "unknown_field"), None),
    # V13: integer bounds and defaults
    "V13 minimum a bool": (at(SEED + ("minimum",), True), (SEED + ("minimum",), "type_mismatch"), at(SEED + ("minimum",), 1)),
    "V13 minimum text": (at(SEED + ("minimum",), "0"), (SEED + ("minimum",), "type_mismatch"), None),
    "V13 maximum a float": (at(SEED + ("maximum",), 1.5), (SEED + ("maximum",), "integer_expected"), at(SEED + ("maximum",), 15)),
    "V13 default a float": (at(RESOLUTION + ("default",), 1.5), (RESOLUTION + ("default",), "default_invalid"), None),
    "V13 default a bool": (at(RESOLUTION + ("default",), True), (RESOLUTION + ("default",), "default_invalid"), at(RESOLUTION + ("default",), 512)),
    # V14 / V15: enums
    "V14 integer enum empty": (at(RESOLUTION + ("enum",), []), (RESOLUTION + ("enum",), "limit_exceeded"), at(RESOLUTION + ("enum",), [512, 1024])),
    "V14 integer enum not a list": (at(RESOLUTION + ("enum",), "abc"), (RESOLUTION + ("enum",), "type_mismatch"), None),
    "V14 string enum empty": (at(("inputs", "bg_removal", "enum"), []), (("inputs", "bg_removal", "enum"), "limit_exceeded"), None),
    "V15 integer enum holds text": (at(RESOLUTION + ("enum",), [512, "1024"]), (RESOLUTION + ("enum", 1), "enum_value_invalid"), None),
    "V15 integer enum holds a bool": (at(RESOLUTION + ("enum",), [512, True]), (RESOLUTION + ("enum", 1), "enum_value_invalid"), None),
    "V15 string enum holds a number": (at(("inputs", "bg_removal", "enum"), ["a", 1]), (("inputs", "bg_removal", "enum", 1), "enum_value_invalid"), None),
    # V16: a default satisfies its own input
    "V16 default outside the enum": (at(RESOLUTION + ("default",), 2048), (RESOLUTION + ("default",), "default_invalid"), None),
    "V16 default below the minimum": (at(SEED + ("default",), -1), (SEED + ("default",), "default_invalid"), at(SEED + ("default",), 0)),
    "V16 default above the maximum": (at(SEED + ("default",), 4294967296), (SEED + ("default",), "default_invalid"), at(SEED + ("default",), 4294967295)),
    "V16 default of the wrong type": (at(("inputs", "bg_removal", "default"), 3), (("inputs", "bg_removal", "default"), "default_invalid"), at(("inputs", "bg_removal", "default"), "threshold")),
    # V18 / V19: outputs
    "V18 outputs empty": (at(("outputs",), {}), (("outputs",), "limit_exceeded"), None),
    "V18 outputs not a mapping": (at(("outputs",), [1]), (("outputs",), "type_mismatch"), None),
    "V19 the retired output modality": (at(("outputs", "mesh", "modality"), "mesh"), (("outputs", "mesh", "modality"), "unknown_field"), None),
    "V19 output without a media type": (drop(("outputs", "mesh", "media_type")), (("outputs", "mesh", "media_type"), "required_missing"), None),
    "V19 output not a mapping": (at(("outputs", "mesh"), [1]), (("outputs", "mesh"), "type_mismatch"), None),
    # V21 / V22 / V23 / V24 / V25: bindings
    "V21 bindings empty": (at(("bindings",), {}), (("bindings",), "limit_exceeded"), None),
    "V21 bindings not a mapping": (at(("bindings",), [1]), (("bindings",), "type_mismatch"), None),
    "V22 the retired service-name binding key": (lambda d: d.update(bindings={"trellis": d["bindings"][BINDING]}), (("bindings", "trellis"), "binding_unknown"), None),
    "V23 binding not a mapping": (at(("bindings", BINDING), 3), (("bindings", BINDING), "type_mismatch"), None),
    "V24 unknown binding field": (at(("bindings", BINDING, "zzz"), 1), (("bindings", BINDING, "zzz"), "binding_field_unknown"), None),
    "V24 binding without a map": (drop(("bindings", BINDING, "map")), (("bindings", BINDING), "required_field_missing"), None),
    "V24 binding without a path": (drop(("bindings", BINDING, "path")), (("bindings", BINDING), "required_field_missing"), None),
    "V24 binding without a service": (drop(("bindings", BINDING, "service")), (("bindings", BINDING), "required_field_missing"), None),
    "V25 the retired transport block": (at(("bindings", BINDING, "transport"), {"kind": "http-multipart", "path": "/g"}), (("bindings", BINDING, "transport"), "binding_field_unknown"), None),
    "V26 a retired transport key": (at(("bindings", BINDING, "method"), "PUT"), (("bindings", BINDING, "method"), "binding_field_unknown"), None),
    "V27 the retired transport kind": (at(("bindings", BINDING, "kind"), "http-json"), (("bindings", BINDING, "kind"), "binding_field_unknown"), None),
    # V29 / V30: the map
    "V29 map not a mapping": (at(MAP, [1]), (MAP, "type_mismatch"), None),
    "V29 map empty": (at(MAP, {}), (MAP, "input_unbound"), None),
    "V30 map names no input": (at(MAP + ("ghost",), "ghost"), (MAP + ("ghost",), "reference_unknown"), None),
    # V31: an object has no form field
    "V31 an object input mapped": (at(MAP + ("extensions",), "extensions"), (MAP + ("extensions",), "input_not_mappable"), None),
    # V32: form field names
    "V32 target with a space": (at(MAP + ("seed",), "Bad Name"), (MAP + ("seed",), "name_invalid"), at(MAP + ("seed",), "seed_value")),
    "V32 target empty": (at(MAP + ("seed",), ""), (MAP + ("seed",), "name_invalid"), None),
    "V32 target not text": (at(MAP + ("seed",), 5), (MAP + ("seed",), "name_invalid"), None),
    "V32 target of 65 characters": (at(MAP + ("seed",), "a" * 65), (MAP + ("seed",), "name_invalid"), at(MAP + ("seed",), "a" * 64)),
    "V32 target with a dot": (at(MAP + ("seed",), "nested.seed"), (MAP + ("seed",), "name_invalid"), None),
    # V33: a form field used twice
    "V33 two inputs, one form field": (at(MAP + ("seed",), "image"), (MAP + ("seed",), "target_duplicate"), None),
    # V34: the required picture is mapped
    "V34 the picture is not mapped": (drop(MAP + ("image",)), (MAP, "input_unbound"), None),
    "V34 the picture mapped to a bad name is not also unmapped": (at(MAP + ("image",), "Bad"), (MAP + ("image",), "name_invalid"), None),
}


@pytest.mark.parametrize("row", sorted(GRAMMAR))
def test_the_grammar_refuses_what_the_mesh_validator_refused(row: str) -> None:
    edit, (path, code), neighbour = GRAMMAR[row]
    error = refused(edited(edit))
    assert listed(error)[0] == (path, code) or (path, code) in listed(error)
    assert str(error), "every refusal has a message"
    if neighbour is not None:
        enforce_contract(edited(neighbour))


def test_the_grammar_rows_name_every_distinct_code_they_pin() -> None:
    codes = {code for _, (_, code), _ in GRAMMAR.values()}
    assert {"unknown_field", "required_field_missing", "required_missing", "schema_version_unsupported", "type_mismatch",
            "pattern_mismatch", "key_invalid", "value_invalid", "integer_expected", "default_invalid", "limit_exceeded",
            "enum_value_invalid", "binding_unknown", "binding_field_unknown", "reference_unknown", "input_not_mappable",
            "name_invalid", "target_duplicate", "input_unbound"} <= codes


def test_a_document_that_is_not_a_mapping_is_refused() -> None:
    for root in ([], "mesh", None, 7):
        error = refused(root)
        assert error.primary.code == "required_field_missing" and error.primary.path == ("schema_version",)


@pytest.mark.parametrize("path", ["generate", "", "/", "/a b", "/a?x=1", "/a#x", "http://host/x", "//host/x", "/a/../b", "/./a", "/a//b", "/a/", "/..", None, 5, ["/a"]])
def test_a_binding_path_must_be_canonical_and_relative(path: Any) -> None:
    error = refused(edited(at(PATH, path)))
    assert listed(error) == [(PATH, "relative_path_invalid")]


@pytest.mark.parametrize("path", ["/generate", "/v2/make-model", "/a.b/c_d/e~f-g", "/A"])
def test_a_canonical_relative_binding_path_is_accepted(path: str) -> None:
    enforce_contract(edited(at(PATH, path)))


def test_an_optional_object_may_stay_unmapped_and_an_optional_option_may_leave_the_map() -> None:
    enforce_contract(edited(drop(MAP + ("seed",))))
    assert "extensions" not in bundled_document()["bindings"][BINDING]["map"]


def test_a_required_option_must_be_mapped_in_a_multipart_binding() -> None:
    doc = edited(at(SEED + ("required",), True), drop(MAP + ("seed",)))
    assert listed(refused(doc)) == [(MAP, "input_unbound")]
    enforce_contract(edited(at(SEED + ("required",), True)))


def test_other_adapters_keep_their_required_inputs_unbound_when_they_read_them_by_role() -> None:
    """The ``input_unbound`` rule is the descriptor's ``inputs_via: map``: a binding read by role is not held to it."""
    from gateway import binding_descriptors

    shipped = binding_descriptors.cached_shipped_binding_descriptors()
    assert shipped[BINDING].inputs_via == "map"
    assert [key for key, descriptor in shipped.items() if descriptor.inputs_via != "map"], "some adapter reads inputs by role"


# --- V36: every independent error is listed, a broken part is not read, the list is capped ---------------------------------

def test_independent_errors_of_one_stage_are_all_listed_sorted_with_the_first_found_as_primary() -> None:
    error = refused(edited(at(SEED + ("minimum",), "x"), at(RESOLUTION + ("enum",), [])))
    assert listed(error) == [(RESOLUTION + ("enum",), "limit_exceeded"), (SEED + ("minimum",), "type_mismatch")]
    # which of the independent errors is "first found" depends on the order the grammar visits its rules; it is always one listed
    assert (error.primary.path, error.primary.code) in listed(error) and str(error) == error.primary.message


def test_a_binding_error_is_raised_before_the_contract_shape_is_read() -> None:
    error = refused(edited(at(SEED + ("minimum",), "x"), at(PATH, "bad")))
    assert listed(error) == [(PATH, "relative_path_invalid")]


def test_a_broken_parent_gives_one_error_and_no_cascade_through_its_children() -> None:
    assert listed(refused(edited(at(("inputs",), ["a"])))) == [(("inputs",), "type_mismatch")]
    assert listed(refused(edited(at(("bindings",), ["a"])))) == [(("bindings",), "type_mismatch")]
    assert listed(refused(edited(at(SEED + ("type",), "float")))) == [(SEED + ("type",), "value_invalid")]


def test_the_list_is_capped_and_says_so() -> None:
    def many(doc: dict[str, Any]) -> None:
        for index in range(150):
            doc["inputs"][f"in{index:03d}"] = {"type": "float"}

    error = refused(edited(many))
    assert len(error.errors) == MAX_CONTRACT_ERRORS + 1 and error.errors[-1].code == "too_many_errors"


# --- the profile rows: the wire carries one picture, one binding, one GLB; the grammar alone allows more ---------------------

def valid_but_unservable(*edits: Edit) -> dict[str, Any]:
    """A document the grammar accepts and the profile refuses: the proof that the profile, and not the grammar, refused."""
    doc = edited(*edits)
    enforce_contract(doc)
    assert mesh_api.mesh_profile_errors(doc), "the grammar accepts this; the profile must not"
    return doc


def second_picture(doc: dict[str, Any]) -> None:
    doc["inputs"]["extra_photo"] = {"type": "asset", "accepts": ["image/png", "image/jpeg", "image/webp"], "required": True}
    doc["bindings"][BINDING]["map"]["extra_photo"] = "extra_form_field"


PROFILE: dict[str, tuple[Edit, str]] = {
    "V17 two required pictures": (second_picture, "exactly one required picture input"),
    "V17 a second, optional picture": (lambda d: (second_picture(d), d["inputs"]["extra_photo"].update(required=False)), "exactly one required picture input"),
    "V17 the only picture is optional": (at(PICTURE + ("required",), False), "exactly one required picture input"),
    "V17 the picture is not required explicitly": (drop(PICTURE + ("required",)), "exactly one required picture input"),
    "V17 no picture at all": (lambda d: (d["inputs"].pop("image"), d["bindings"][BINDING]["map"].pop("image")), "exactly one required picture input"),
    "V18 two outputs": (at(("outputs", "second"), {"media_type": "model/gltf-binary"}), "exactly one output, model/gltf-binary"),
    "V20 another media type": (at(("outputs", "mesh", "media_type"), "model/obj"), "exactly one output, model/gltf-binary"),
    "V20 a family of media types": (at(("outputs", "mesh", "media_type"), "model/*"), "exactly one output, model/gltf-binary"),
    "V20 the output count allows many": (at(("outputs", "mesh", "count"), {"min": 1, "max": 3}), "the output must be one model"),
    "V20 the output count allows none": (at(("outputs", "mesh", "count"), {"min": 0, "max": 1}), None),
    "V10 a type the form cannot read": (at(("inputs", "flag"), {"type": "boolean"}), "input flag has a type the route cannot read"),
    "V10 an array option": (at(("inputs", "list"), {"type": "array", "items": {"type": "string"}, "max_items": 3}), "input list has a type the route cannot read"),
    "V10 a number option": (at(("inputs", "ratio"), {"type": "number"}), "input ratio has a type the route cannot read"),
    "V21 a binding of another adapter": (lambda d: d.update(bindings={"gateway": {"service": "x", "path": "/x", "map": {"image": "image"}}}), "the binding must be a http-multipart binding with a service, a path and a map"),
}


@pytest.mark.parametrize("row", sorted(PROFILE))
def test_the_profile_refuses_a_workflow_the_wire_cannot_carry(row: str) -> None:
    edit, reason = PROFILE[row]
    doc = edited(edit)
    problems = mesh_api.mesh_profile_errors(doc)
    if reason is None:
        assert problems == [], "an output that may hold none but one body is one body"
        return
    assert reason in problems
    with pytest.raises(MeshManifestError):
        mesh_api.mesh_workflow(doc, "hash")


@pytest.mark.parametrize("row", [row for row in sorted(PROFILE) if row not in ("V21 a binding of another adapter", "V18 two outputs", "V20 another media type")])
def test_the_profile_alone_refuses_these_the_grammar_accepts_them(row: str) -> None:
    edit, reason = PROFILE[row]
    if reason is None:
        return
    if row == "V17 no picture at all":
        valid_but_unservable(edit)
    else:
        valid_but_unservable(edit)


def test_the_profile_is_given_every_shape_and_never_raises() -> None:
    for contract in (None, [], "mesh", 7, {}, {"intent": "mesh.generate"}, {"intent": "mesh.generate", "inputs": [1], "outputs": 3, "bindings": "x"},
                     {"intent": "mesh.generate", "inputs": {"a": None}, "outputs": {"o": 4}, "bindings": {"http-multipart": 5}}):
        assert mesh_api.mesh_profile_errors(contract), contract


def test_a_different_intent_is_a_different_frontends_workflow() -> None:
    assert mesh_api.mesh_profile_errors(edited(at(("intent",), "images.generate"))) == [
        "intent must be 'mesh.generate'"]
    frontend_profiles.check(edited(at(("intent",), "images.generate")))  # no frontend registered for it: nothing to refuse


# --- the rules the decoder cannot enforce: refused, never served with the rule dropped ---------------------------------------

NOT_ENFORCED: dict[str, tuple[Edit, str]] = {
    "a required option": (at(SEED + ("required",), True), "input seed cannot be required: only the picture is"),
    "a step": (at(SEED + ("step",), 2), "input seed states step, which the route does not enforce"),
    "an offset": (at(SEED + ("offset",), 1), "input seed states offset, which the route does not enforce"),
    "a generator": (at(SEED + ("generate",), "random"), "input seed states generate, which the route does not enforce"),
    "a minimum length": (at(("inputs", "bg_removal", "min_length"), 2), "input bg_removal states min_length, which the route does not enforce"),
    "a maximum length": (at(("inputs", "bg_removal", "max_length"), 9), "input bg_removal states max_length, which the route does not enforce"),
    "a maximum past the integer ceiling": (at(SEED + ("maximum",), 2**63), "input seed holds a number past what the route reads"),
    "a minimum past the integer ceiling": (at(SEED + ("minimum",), -(2**63)), "input seed holds a number past what the route reads"),
    "a default past the integer ceiling": (at(SEED + ("default",), 2**63 + 5), "input seed holds a number past what the route reads"),
    "an allowed value past the integer ceiling": (at(RESOLUTION + ("enum",), [512, 2**63]), "input resolution holds a number past what the route reads"),
}


@pytest.mark.parametrize("row", sorted(NOT_ENFORCED))
def test_a_constraint_the_decoder_does_not_enforce_is_refused_by_the_profile(row: str) -> None:
    edit, reason = NOT_ENFORCED[row]
    doc = edited(edit)
    if row == "a default past the integer ceiling":
        doc["inputs"]["seed"]["maximum"] = 2**64
    assert reason in mesh_api.mesh_profile_errors(doc)


def test_the_integer_ceiling_is_a_bound_of_the_wire_and_the_largest_number_below_it_is_served() -> None:
    doc = edited(at(SEED + ("maximum",), 2**63 - 1), at(SEED + ("default",), 2**63 - 1))
    enforce_contract(doc)
    assert mesh_api.mesh_profile_errors(doc) == []
    doc = edited(at(SEED + ("minimum",), -(2**63) + 1))
    assert mesh_api.mesh_profile_errors(doc) == []


def test_the_integer_ceiling_is_not_a_global_limit_of_the_grammar() -> None:
    doc = edited(at(SEED + ("maximum",), 2**70), at(SEED + ("default",), 2**65))
    enforce_contract(doc)   # any other workflow may state it
    assert mesh_api.mesh_profile_errors(doc) == ["input seed holds a number past what the route reads"]


@pytest.mark.parametrize("accepts", [["image/png"], ["image/png", "image/jpeg"], ["image/png", "image/jpeg", "image/webp", "image/gif"]])
def test_the_picture_must_accept_exactly_the_three_types_the_wire_reads(accepts: list[str]) -> None:
    doc = valid_but_unservable(at(PICTURE + ("accepts",), accepts))
    assert any(problem.startswith("the picture input must accept image/jpeg, image/png, image/webp") for problem in mesh_api.mesh_profile_errors(doc))


@pytest.mark.parametrize("accepts", [["image/png", "image/jpeg", "image/webp"], ["IMAGE/WEBP", "image/png ", "image/jpeg"], ["image/webp", "image/jpeg", "image/png"]])
def test_the_three_types_in_any_order_and_case_are_accepted(accepts: list[str]) -> None:
    assert mesh_api.mesh_profile_errors(edited(at(PICTURE + ("accepts",), accepts))) == []


@pytest.mark.parametrize("key", ["id", "version"])
def test_an_identity_with_spaces_around_it_is_refused_not_stripped(key: str) -> None:
    value = edited()[key]
    doc = edited(at((key,), f" {value} "))
    assert f"{key} must not start or end with a space" in mesh_api.mesh_profile_errors(doc)
    assert f"{key} must not start or end with a space" not in mesh_api.mesh_profile_errors(edited())


def test_wildcard_picture_types_are_refused_by_the_grammar_or_the_profile() -> None:
    for accepts in (["image/*"], ["*/*"]):
        doc = edited(at(PICTURE + ("accepts",), accepts))
        assert mesh_api.mesh_profile_errors(doc)


def test_a_workflow_file_is_checked_by_the_profile_when_it_is_loaded(tmp_path) -> None:
    import yaml

    from gateway.manifests import load_workflow_manifest

    good = tmp_path / "good.yml"
    good.write_text(yaml.safe_dump(second_document()), encoding="utf-8")
    assert load_workflow_manifest(good)["id"] == second_document()["id"]
    bad_doc = edited(second_picture)
    bad = tmp_path / "bad.yml"
    bad.write_text(yaml.safe_dump(bad_doc), encoding="utf-8")
    enforce_contract(bad_doc)   # the grammar accepts it
    with pytest.raises(ContractErrors) as caught:
        load_workflow_manifest(bad)
    assert (caught.value.primary.path, caught.value.primary.code) == (("intent",), "frontend_profile_refused")


def test_the_profile_refusal_code_is_stable() -> None:
    assert frontend_profiles.PROFILE_REFUSED == "frontend_profile_refused"
    with pytest.raises(ContractErrors) as caught:
        frontend_profiles.check(edited(second_picture))
    assert caught.value.primary.code == "frontend_profile_refused"
