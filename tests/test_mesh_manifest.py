"""The bundled mesh manifest: closed schema, one real binding, typed input binding, fail-closed refusals."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from gateway.mesh_manifest import (
    MeshInputError,
    MeshManifestError,
    bind_request,
    downstream_form,
    load_mesh_manifest,
    sniff_image,
    validate_mesh_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifests" / "workflows" / "mesh.generate.trellis-2.yml"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
WEBP = b"RIFF\x20\x00\x00\x00WEBPVP8 " + b"\x00" * 16
MIB = 1 << 20


def raw_manifest() -> dict:
    return copy.deepcopy(yaml.safe_load(MANIFEST.read_text(encoding="utf-8")))


def test_bundled_manifest_binds_one_real_service_over_closed_multipart() -> None:
    manifest = load_mesh_manifest(MANIFEST)

    assert manifest.id == "mesh.generate.trellis-2"
    assert manifest.intent == "mesh.generate"
    assert manifest.service_id == "trellis"
    assert manifest.transport_path == "/generate"
    assert dict(manifest.mapping) == {"image": "image", "seed": "seed", "resolution": "resolution", "bg_removal": "bg_removal"}
    assert manifest.output_media_type == "model/gltf-binary"
    assert manifest.image_input == "image"
    assert len(manifest.manifest_hash) == 64


def test_manifest_hash_follows_content() -> None:
    base = validate_mesh_manifest(raw_manifest())
    edited = raw_manifest()
    edited["inputs"]["resolution"]["default"] = 512
    assert validate_mesh_manifest(edited).manifest_hash != base.manifest_hash


@pytest.mark.parametrize("mutate, message", [
    (lambda m: m.update(schema_version=2), "schema_version"),
    (lambda m: m.update(intent="images.generate"), "intent"),
    (lambda m: m.update(surprise=True), "unknown keys"),
    (lambda m: m["inputs"]["image"].update(modality="video"), "modality image"),
    (lambda m: m["inputs"]["image"].update(required=False), "required image"),
    (lambda m: m["outputs"]["mesh"].update(media_type="model/obj"), "media type"),
    (lambda m: m["outputs"].update(extra={"modality": "mesh", "media_type": "model/gltf-binary"}), "exactly one output"),
])
def test_manifest_schema_refuses_drift(mutate, message) -> None:
    raw = raw_manifest()
    mutate(raw)
    with pytest.raises(MeshManifestError, match=message):
        validate_mesh_manifest(raw)


@pytest.mark.parametrize("path", ["generate", "/generate?x=1", "https://evil.example/generate", "/a/../b", "/gen erate"])
def test_transport_path_must_be_canonical_and_relative(path: str) -> None:
    raw = raw_manifest()
    raw["bindings"]["trellis"]["transport"]["path"] = path
    with pytest.raises(MeshManifestError, match="path"):
        validate_mesh_manifest(raw)


@pytest.mark.parametrize("extra", [
    {"method": "PUT"}, {"url": "http://x"}, {"headers": {"a": "b"}}, {"response": {}},
])
def test_transport_and_binding_are_closed(extra: dict) -> None:
    raw = raw_manifest()
    raw["bindings"]["trellis"]["transport"].update(extra)
    with pytest.raises(MeshManifestError):
        validate_mesh_manifest(raw)
    raw = raw_manifest()
    raw["bindings"]["trellis"].update(extra)
    with pytest.raises(MeshManifestError):
        validate_mesh_manifest(raw)


def test_transport_kind_must_be_http_multipart() -> None:
    raw = raw_manifest()
    raw["bindings"]["trellis"]["transport"]["kind"] = "http-json"
    with pytest.raises(MeshManifestError, match="transport kind"):
        validate_mesh_manifest(raw)


def test_exactly_one_binding_keyed_by_a_service_id() -> None:
    raw = raw_manifest()
    raw["bindings"]["second"] = copy.deepcopy(raw["bindings"]["trellis"])
    with pytest.raises(MeshManifestError, match="exactly one binding"):
        validate_mesh_manifest(raw)
    raw = raw_manifest()
    raw["bindings"] = {"Bad Id": raw["bindings"]["trellis"]}
    with pytest.raises(MeshManifestError, match="service id"):
        validate_mesh_manifest(raw)


def test_required_mappings_are_unique_and_flat() -> None:
    raw = raw_manifest()
    raw["bindings"]["trellis"]["map"]["seed"] = "image"
    with pytest.raises(MeshManifestError, match="used twice"):
        validate_mesh_manifest(raw)
    raw = raw_manifest()
    raw["bindings"]["trellis"]["map"]["seed"] = "nested.seed"
    with pytest.raises(MeshManifestError, match="plain form field"):
        validate_mesh_manifest(raw)
    raw = raw_manifest()
    del raw["bindings"]["trellis"]["map"]["image"]
    with pytest.raises(MeshManifestError, match="image input must be mapped"):
        validate_mesh_manifest(raw)


def test_object_inputs_cannot_be_mapped() -> None:
    raw = raw_manifest()
    raw["bindings"]["trellis"]["map"]["extensions"] = "extensions"
    with pytest.raises(MeshManifestError, match="object"):
        validate_mesh_manifest(raw)


def test_defaults_must_satisfy_their_own_range() -> None:
    raw = raw_manifest()
    raw["inputs"]["resolution"]["default"] = 2048
    with pytest.raises(MeshManifestError, match="default"):
        validate_mesh_manifest(raw)


def test_bind_applies_default_and_coerces_text_fields() -> None:
    manifest = load_mesh_manifest(MANIFEST)
    image = sniff_image(PNG, max_bytes=MIB)
    request = bind_request(manifest, {"seed": "42"}, image)
    fields, image_part = downstream_form(manifest, request)

    assert fields == {"seed": "42", "resolution": "1024"}
    assert image_part.filename == "input.png"
    assert image_part.content_type == "image/png"


def test_bind_refuses_unknown_and_unmapped_fields() -> None:
    manifest = load_mesh_manifest(MANIFEST)
    image = sniff_image(PNG, max_bytes=MIB)
    with pytest.raises(MeshInputError) as unknown:
        bind_request(manifest, {"nope": "1"}, image)
    assert unknown.value.code == "unknown_field"
    with pytest.raises(MeshInputError) as unmapped:
        bind_request(manifest, {"extensions": "{}"}, image)
    assert unmapped.value.code == "unsupported_input"


def test_bind_requires_the_image_part() -> None:
    manifest = load_mesh_manifest(MANIFEST)
    with pytest.raises(MeshInputError) as error:
        bind_request(manifest, {}, None)
    assert error.value.code == "missing_image"


@pytest.mark.parametrize("supplied, code", [
    ({"seed": "-1"}, "invalid_input"),
    ({"seed": "4294967296"}, "invalid_input"),
    ({"seed": "1.5"}, "invalid_input"),
    ({"seed": "true"}, "invalid_input"),
    ({"resolution": "768"}, "invalid_input"),
    ({"bg_removal": "auto"}, "invalid_input"),
])
def test_bind_enforces_ranges_and_enums(supplied: dict, code: str) -> None:
    manifest = load_mesh_manifest(MANIFEST)
    with pytest.raises(MeshInputError) as error:
        bind_request(manifest, supplied, sniff_image(PNG, max_bytes=MIB))
    assert error.value.code == code


def test_scalars_are_canonical_for_the_service() -> None:
    manifest = load_mesh_manifest(MANIFEST)
    request = bind_request(manifest, {"seed": "7", "resolution": "512", "bg_removal": "birefnet"},
                           sniff_image(PNG, max_bytes=MIB))
    fields, _ = downstream_form(manifest, request)
    assert fields == {"seed": "7", "resolution": "512", "bg_removal": "birefnet"}


def test_image_types_are_sniffed_from_bytes() -> None:
    assert sniff_image(JPEG, max_bytes=MIB).filename == "input.jpg"
    assert sniff_image(WEBP, max_bytes=MIB).content_type == "image/webp"
    with pytest.raises(MeshInputError) as text:
        sniff_image(b"GIF89a" + b"\x00" * 10, max_bytes=MIB)
    assert text.value.code == "unsupported_image"
    with pytest.raises(MeshInputError) as empty:
        sniff_image(b"", max_bytes=MIB)
    assert empty.value.code == "invalid_image"


def test_image_size_is_bounded() -> None:
    with pytest.raises(MeshInputError) as error:
        sniff_image(PNG + b"\x00" * 100, max_bytes=len(PNG))
    assert error.value.code == "image_too_large"


def test_the_generic_validator_accepts_the_mesh_picture() -> None:
    # The mesh manifest lives with the other workflow manifests, so the generic validator must read its image type.
    from gateway.manifests import validate_inputs

    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    result = validate_inputs(manifest, {"image": "data:image/png;base64,AAAA"})
    assert result.rejected == {}
    assert validate_inputs(manifest, {"image": 7}).rejected == {"image": "image must be an asset reference"}
