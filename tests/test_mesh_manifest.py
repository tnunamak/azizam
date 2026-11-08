"""The mesh wire (``gateway/mesh_manifest.py``): form decoding, wire error codes, scalar encoding and the picture check.

The workflow is data and the registry validates it; the mesh frontend profile says which valid workflows this wire carries.
Their tests are in ``tests/test_mesh_profile.py``. Here the wire is tested on the registry's own record.
"""

from __future__ import annotations

import pytest

from gateway import mesh_manifest
from gateway.mesh_manifest import (
    MeshInputError,
    _canonical_scalar,
    bind_request,
    downstream_form,
    sniff_image,
)
from tests.mesh_workflows import bundled_document, workflow_of

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"\x00" * 16
MIB = 1 << 20


def bundled():
    return workflow_of(bundled_document())


def refusal(call) -> MeshInputError:
    with pytest.raises(MeshInputError) as caught:
        call()
    return caught.value


def test_the_bundled_workflow_reads_as_the_route_needs_it() -> None:
    workflow = bundled()

    assert workflow.id == "mesh.generate.trellis-2"
    assert workflow.intent == "mesh.generate"
    assert workflow.service_id == "trellis"
    assert workflow.transport_path == "/generate"
    assert dict(workflow.mapping) == {"image": "image", "seed": "seed", "resolution": "resolution", "bg_removal": "bg_removal"}
    assert workflow.output_media_type == "model/gltf-binary"
    assert workflow.image_input == "image"
    assert workflow.inputs["image"]["type"] == "asset"


def test_bind_applies_default_and_coerces_text_fields() -> None:
    workflow = bundled()
    request = bind_request(workflow, {"seed": "42"}, sniff_image(PNG, max_bytes=MIB))
    fields, image_part = downstream_form(workflow, request)

    assert fields == {"seed": "42", "resolution": "1024"}
    assert (image_part.filename, image_part.content_type) == ("input.png", "image/png")


def test_an_omitted_option_with_no_default_is_absent_not_generated() -> None:
    workflow = bundled()
    request = bind_request(workflow, {}, sniff_image(PNG, max_bytes=MIB))
    assert set(request.values) == {"resolution"}, "no seed is made up, and an option with no default is not sent"
    assert "bg_removal" not in request.values


def test_a_default_is_copied_for_each_request() -> None:
    document = bundled_document()
    document["inputs"]["bg_removal"]["default"] = "threshold"
    workflow = workflow_of(document)
    one = bind_request(workflow, {}, sniff_image(PNG, max_bytes=MIB)).values
    assert one["bg_removal"] == "threshold"
    assert workflow.inputs["bg_removal"]["default"] == "threshold"


def test_bind_names_the_unknown_unmapped_and_missing_by_the_wire_codes() -> None:
    workflow = bundled()
    image = sniff_image(PNG, max_bytes=MIB)
    unknown = refusal(lambda: bind_request(workflow, {"nope": "1"}, image))
    assert (unknown.code, str(unknown), unknown.param) == ("unknown_field", "nope is not a field of this route", "nope")
    unmapped = refusal(lambda: bind_request(workflow, {"extensions": "{}"}, image))
    assert (unmapped.code, str(unmapped), unmapped.param) == ("unsupported_input", "extensions is not supported by this route", "extensions")
    missing = refusal(lambda: bind_request(workflow, {}, None))
    assert (missing.code, str(missing), missing.param) == ("missing_image", "the image part is required", "image")


def test_the_missing_image_names_the_pictures_own_input() -> None:
    document = bundled_document()
    document["inputs"]["photo"] = document["inputs"].pop("image")
    document["bindings"]["http-multipart"]["map"]["photo"] = document["bindings"]["http-multipart"]["map"].pop("image")
    assert refusal(lambda: bind_request(workflow_of(document), {}, None)).param == "photo"


def test_an_unknown_field_is_named_before_a_missing_image() -> None:
    assert refusal(lambda: bind_request(bundled(), {"nope": "1"}, None)).code == "unknown_field"


@pytest.mark.parametrize("supplied, message", [
    ({"seed": "-1"}, "seed is outside its range"),
    ({"seed": "4294967296"}, "seed is outside its range"),
    ({"seed": "9223372036854775807"}, "seed is outside its range"),
    ({"seed": "9223372036854775808"}, "seed must be an integer"),
    ({"seed": "-9223372036854775808"}, "seed must be an integer"),
    ({"seed": "1.5"}, "seed must be an integer"),
    ({"seed": "true"}, "seed must be an integer"),
    ({"seed": "+1"}, "seed must be an integer"),
    ({"seed": "1e3"}, "seed must be an integer"),
    ({"seed": ""}, "seed must be an integer"),
    ({"seed": True}, "seed must be an integer"),
    ({"seed": 1.5}, "seed must be an integer"),
    ({"seed": None}, "seed must be an integer"),
    ({"resolution": "768"}, "resolution is not an allowed value"),
    ({"resolution": 768}, "resolution is not an allowed value"),
    ({"bg_removal": "auto"}, "bg_removal is not an allowed value"),
    ({"bg_removal": 7}, "bg_removal must be text"),
])
def test_bind_enforces_types_ranges_and_enums_with_the_wire_wording(supplied: dict, message: str) -> None:
    error = refusal(lambda: bind_request(bundled(), supplied, sniff_image(PNG, max_bytes=MIB)))
    assert (error.code, str(error), error.param) == ("invalid_input", message, message.split()[0])


@pytest.mark.parametrize("supplied, expected", [
    ({"seed": " 7 "}, 7), ({"seed": "0"}, 0), ({"seed": "4294967295"}, 4294967295), ({"seed": 5}, 5), ({"seed": "007"}, 7),
])
def test_bind_accepts_the_neighbours_of_each_integer_refusal(supplied: dict, expected: int) -> None:
    assert bind_request(bundled(), supplied, sniff_image(PNG, max_bytes=MIB)).values["seed"] == expected


def test_a_range_is_checked_before_the_allowed_values() -> None:
    document = bundled_document()
    document["inputs"]["seed"]["enum"] = [1, 2]
    error = refusal(lambda: bind_request(workflow_of(document), {"seed": "-3"}, sniff_image(PNG, max_bytes=MIB)))
    assert str(error) == "seed is outside its range", "the wire has always said the range first, where the general rule says the enum"


def test_an_object_option_is_read_from_json_text() -> None:
    spec = {"type": "object"}
    assert mesh_manifest._coerce("opts", spec, '{"a": 1}') == {"a": 1}
    assert mesh_manifest._coerce("opts", spec, {"a": 1}) == {"a": 1}
    for bad in ("{nope", "[1]", "7", 7, None):
        error = refusal(lambda bad=bad: mesh_manifest._coerce("opts", spec, bad))
        assert (error.code, str(error), error.param) == ("invalid_input", "opts must be a JSON object", "opts")


def test_scalars_are_canonical_for_the_service() -> None:
    workflow = bundled()
    request = bind_request(workflow, {"seed": "7", "resolution": "512", "bg_removal": "birefnet"}, sniff_image(PNG, max_bytes=MIB))
    fields, _ = downstream_form(workflow, request)
    assert fields == {"seed": "7", "resolution": "512", "bg_removal": "birefnet"}


@pytest.mark.parametrize("value, text", [
    (True, "true"), (False, "false"), (42, "42"), (-3, "-3"), (1.5, "1.5"), ("abc", "abc"), ({"b": 1, "a": [1, 2]}, '{"a":[1,2],"b":1}'),
    ([1, "x"], '[1,"x"]'),
])
def test_the_form_encoding_of_each_kind_of_value(value, text: str) -> None:
    assert _canonical_scalar(value) == text


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_number_that_cannot_be_written_is_refused_without_a_param(value) -> None:
    error = refusal(lambda: _canonical_scalar(value))
    assert (error.code, str(error), error.param) == ("invalid_input", "non-finite numbers cannot be sent", None)


def test_only_mapped_values_are_sent_and_each_under_its_target() -> None:
    document = bundled_document()
    document["bindings"]["http-multipart"]["map"].update(seed="seed_value", resolution="detail")
    workflow = workflow_of(document)
    fields, _ = downstream_form(workflow, bind_request(workflow, {"seed": "3"}, sniff_image(PNG, max_bytes=MIB)))
    assert fields == {"seed_value": "3", "detail": "1024"}


def test_image_types_are_sniffed_from_bytes_and_named_neutrally() -> None:
    assert sniff_image(PNG, max_bytes=MIB).filename == "input.png"
    assert sniff_image(JPEG, max_bytes=MIB).filename == "input.jpg"
    webp = sniff_image(WEBP, max_bytes=MIB)
    assert (webp.filename, webp.content_type, webp.data) == ("input.webp", "image/webp", WEBP)


@pytest.mark.parametrize("data", [b"GIF89a" + b"\x00" * 10, b"RIFF\x20\x00\x00\x00WAVEfmt ", b"RIFF\x20\x00\x00\x00WEB", b"\x00" * 20, b"ftypmp42" * 3])
def test_a_file_that_is_not_one_of_the_three_is_refused(data: bytes) -> None:
    error = refusal(lambda: sniff_image(data, max_bytes=MIB))
    assert (error.code, str(error), error.param) == ("unsupported_image", "the image must be PNG, JPEG or WebP", "image")


def test_an_empty_image_is_refused_before_its_size_and_type() -> None:
    error = refusal(lambda: sniff_image(b"", max_bytes=MIB))
    assert (error.code, str(error)) == ("invalid_image", "the image part is empty")


def test_the_image_size_bound_is_exact() -> None:
    assert sniff_image(PNG, max_bytes=len(PNG)).data == PNG
    error = refusal(lambda: sniff_image(PNG + b"\x00", max_bytes=len(PNG)))
    assert (error.code, str(error), error.param) == ("image_too_large", "the image is larger than this route accepts", "image")


def test_a_refusal_names_the_picture_input_it_is_given() -> None:
    assert refusal(lambda: sniff_image(b"", max_bytes=MIB, param="photo")).param == "photo"


def test_the_size_check_comes_before_the_type_check() -> None:
    assert refusal(lambda: sniff_image(b"GIF89a" + b"\x00" * 100, max_bytes=10)).code == "image_too_large"


def test_the_sniffer_is_the_resolvers_not_a_second_one() -> None:
    from gateway import media_inputs

    assert mesh_manifest.media_inputs is media_inputs
    for data in (PNG, JPEG, WEBP):
        assert sniff_image(data, max_bytes=MIB).content_type == media_inputs.measure(data).media_type


def test_the_module_no_longer_holds_a_validator_a_hash_or_a_manifest_class() -> None:
    gone = ("MeshManifest", "load_mesh_manifest", "validate_mesh_manifest", "MeshContractErrors", "_validate_manifest_into",
            "_validate_inputs", "_validate_input", "_validate_outputs", "_validate_bindings", "_validate_transport", "_validate_map",
            "_check_integer_spec", "_check_enum", "_check_value", "_closed", "_text", "_plain", "manifest_hash")
    assert [name for name in gone if hasattr(mesh_manifest, name)] == []
    source = open(mesh_manifest.__file__, encoding="utf-8").read()
    assert "hashlib" not in source and "yaml" not in source and "errors.add" not in source
    assert not any(token in source for token in ("ErrorCollector", "ContractErrors"))
