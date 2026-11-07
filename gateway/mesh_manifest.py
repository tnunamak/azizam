"""The bundled mesh workflow manifest: validation and typed input binding.

The manifest is data. It owns the logical inputs, their defaults and ranges, the image and mesh
modalities, the output media type, and one mapping from logical inputs to the form fields of the
single service that serves them. Its only transport is a closed ``http-multipart`` POST to a
relative path. Nothing here knows an engine: the service ID and the form field names are values.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

from gateway.manifests import ContractErrors, ErrorCollector, PathPart

MESH_INTENT = "mesh.generate"
MESH_MEDIA_TYPE = "model/gltf-binary"
TRANSPORT_KIND = "http-multipart"

_TOP_LEVEL_REQUIRED = ("schema_version", "id", "intent", "version", "inputs", "outputs", "bindings")
_TOP_LEVEL_OPTIONAL = (
    "min_gateway_version", "capability", "display_name", "purpose", "summary", "description",
    "technical_description", "source", "trusted", "license", "action_label",
)
_INPUT_KEYS = frozenset({"label", "help", "type", "required", "default", "minimum", "maximum", "enum", "modality", "recordable"})
_INPUT_TYPES = frozenset({"image", "integer", "string", "object"})
_SERVICE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_TRANSPORT_PATH = re.compile(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*")
_INTEGER_LIMIT = 2**63

_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"


class MeshManifestError(ValueError):
    """The bundled manifest is malformed. The gateway refuses to serve the route."""


class MeshContractErrors(ContractErrors, MeshManifestError):
    """Every error of one manifest, each with a path and a code. It is a ``MeshManifestError`` too, with the first
    error's message, so the caller that refuses the route on a ``MeshManifestError`` sees what it saw before."""


class MeshInputError(ValueError):
    """A caller's multipart input is invalid for the manifest. Maps to 400."""

    def __init__(self, code: str, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.param = param


@dataclass(frozen=True)
class MeshManifest:
    id: str
    intent: str
    version: str
    manifest_hash: str
    inputs: Mapping[str, Mapping[str, Any]]
    service_id: str
    transport_path: str
    mapping: Mapping[str, str]
    output_media_type: str
    data: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def image_input(self) -> str:
        return next(name for name, spec in self.inputs.items() if spec.get("type") == "image")

    def mapped_inputs(self) -> tuple[str, ...]:
        return tuple(self.mapping)


def load_mesh_manifest(path: str | Path) -> MeshManifest:
    manifest_path = Path(path)
    try:
        with manifest_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as error:
        raise MeshManifestError(f"mesh manifest {manifest_path} cannot be read: {error}") from error
    return validate_mesh_manifest(raw)


def validate_mesh_manifest(raw: Any) -> MeshManifest:
    errors = ErrorCollector()
    parsed = None
    with errors.stop_at_a_crash_after_an_error():
        parsed = _validate_manifest_into(raw, errors)
    errors.raise_if_any(MeshContractErrors)
    assert parsed is not None
    return parsed


def _validate_manifest_into(raw: Any, errors: ErrorCollector) -> MeshManifest | None:
    """Check the manifest, adding every error to ``errors``. A part that depends on a broken part is not checked, so one
    cause does not give a list of effects. Returns the manifest when there is no error."""
    if not isinstance(raw, Mapping):
        errors.add((), "type_mismatch", "mesh manifest must be a mapping")
        return None
    _closed(raw, _TOP_LEVEL_REQUIRED, _TOP_LEVEL_OPTIONAL, "manifest", (), errors)
    if "schema_version" in raw and raw["schema_version"] != 1:
        errors.add(("schema_version",), "version_unsupported", "mesh manifest schema_version must be 1")
    if "intent" in raw and raw["intent"] != MESH_INTENT:
        errors.add(("intent",), "intent_mismatch", f"mesh manifest intent must be {MESH_INTENT!r}")
    manifest_id = _text(raw["id"], "id", ("id",), errors) if "id" in raw else None
    version = _text(raw["version"], "version", ("version",), errors) if "version" in raw else None
    inputs = _validate_inputs(raw["inputs"], errors) if "inputs" in raw else None
    output_media_type = _validate_outputs(raw["outputs"], errors) if "outputs" in raw else None
    bindings = _validate_bindings(raw["bindings"], inputs, errors) if "bindings" in raw else None
    if errors.errors:
        return None
    assert manifest_id is not None and version is not None and inputs is not None
    assert output_media_type is not None and bindings is not None
    service_id, transport_path, mapping = bindings

    canonical = json.dumps(_plain(raw), sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return MeshManifest(
        id=manifest_id,
        intent=MESH_INTENT,
        version=version,
        manifest_hash=hashlib.sha256(canonical).hexdigest(),
        inputs=inputs,
        service_id=service_id,
        transport_path=transport_path,
        mapping=mapping,
        output_media_type=output_media_type,
        data=_plain(raw),
    )


def _validate_inputs(raw: Any, errors: ErrorCollector) -> dict[str, dict[str, Any]] | None:
    """The checked inputs, or ``None`` when any input has an error (they are all checked)."""
    if not isinstance(raw, Mapping) or not raw:
        errors.add(("inputs",), "type_mismatch", "mesh manifest inputs must be a non-empty mapping")
        return None
    before = len(errors)
    inputs: dict[str, dict[str, Any]] = {}
    for name, spec in raw.items():
        checked = _validate_input(name, spec, errors)
        if checked is not None:
            inputs[name] = checked
    if len(errors) != before:
        return None
    images = [name for name, spec in inputs.items() if spec["type"] == "image"]
    if len(images) != 1 or inputs[images[0]].get("required") is not True:
        errors.add(("inputs",), "image_input_count", "mesh manifest needs exactly one required image input")
        return None
    return inputs


def _validate_input(name: Any, spec: Any, errors: ErrorCollector) -> dict[str, Any] | None:
    """One input, or ``None`` after the first error it has (the later rules read what the first one checks)."""
    at = ("inputs", str(name))
    if not isinstance(name, str) or not _FIELD_NAME.fullmatch(name):
        errors.add(at, "name_invalid", f"input name {name!r} is not a plain field name")
        return None
    if not isinstance(spec, Mapping):
        errors.add(at, "type_mismatch", f"input {name} must be a mapping")
        return None
    before = len(errors)
    _closed(spec, ("type",), tuple(sorted(_INPUT_KEYS - {"type"})), f"input {name}", at, errors)
    if len(errors) != before:
        return None
    kind = spec["type"]
    if not isinstance(kind, str) or kind not in _INPUT_TYPES:
        errors.add(at + ("type",), "type_unsupported", f"input {name} type must be one of {sorted(_INPUT_TYPES)}")
        return None
    checked = _plain(dict(spec))
    if kind == "image":
        if checked.get("modality") != "image":
            errors.add(at + ("modality",), "modality_invalid", f"input {name} of type image must declare modality image")
            return None
    elif "modality" in checked:
        errors.add(at + ("modality",), "modality_invalid", f"input {name} may declare a modality only when it is an image")
        return None
    if kind == "integer" and not _check_integer_spec(name, checked, at, errors):
        return None
    if kind == "string" and checked.get("enum") is not None and not _check_enum(name, checked["enum"], str, at, errors):
        return None
    if "default" in checked and not _check_value(name, checked, checked["default"], at, errors):
        return None
    return checked


def _validate_outputs(raw: Any, errors: ErrorCollector) -> str | None:
    if not isinstance(raw, Mapping) or len(raw) != 1:
        errors.add(("outputs",), "output_count", "mesh manifest outputs must name exactly one output")
        return None
    (name, spec), = raw.items()
    at = ("outputs", str(name))
    if not isinstance(spec, Mapping) or set(spec) != {"modality", "media_type"}:
        errors.add(at, "unknown_field", f"output {name} must declare modality and media_type only")
        return None
    if spec["modality"] != "mesh" or spec["media_type"] != MESH_MEDIA_TYPE:
        errors.add(at, "output_type_invalid", f"output {name} must be modality mesh with media type {MESH_MEDIA_TYPE}")
        return None
    return MESH_MEDIA_TYPE


def _validate_bindings(raw: Any, inputs: Mapping[str, Any] | None,
                       errors: ErrorCollector) -> tuple[str, str, dict[str, str]] | None:
    """The service id, transport path and form-field map. ``inputs`` is ``None`` when the inputs have an error: the rules
    that read an input are then not checked."""
    if not isinstance(raw, Mapping) or len(raw) != 1:
        errors.add(("bindings",), "binding_count", "mesh manifest must hold exactly one binding")
        return None
    before = len(errors)
    (service_id, binding), = raw.items()
    at = ("bindings", str(service_id))
    if not isinstance(service_id, str) or not _SERVICE_ID.fullmatch(service_id):
        errors.add(at, "name_invalid", f"binding key {service_id!r} is not a service id")
    if not isinstance(binding, Mapping):
        errors.add(at, "type_mismatch", "binding must be a mapping")
        return None
    _closed(binding, ("transport", "map"), (), f"binding {service_id}", at, errors)

    path = None
    if "transport" in binding:
        path = _validate_transport(binding["transport"], at + ("transport",), errors)
    targets = _validate_map(binding["map"], inputs, at + ("map",), errors) if "map" in binding else None
    if len(errors) != before or path is None or targets is None:
        return None
    return service_id, path, targets


def _validate_transport(transport: Any, at: tuple[PathPart, ...], errors: ErrorCollector) -> str | None:
    if not isinstance(transport, Mapping):
        errors.add(at, "type_mismatch", "transport must be a mapping")
        return None
    before = len(errors)
    _closed(transport, ("kind", "path"), (), "transport", at, errors)
    if "kind" in transport and transport["kind"] != TRANSPORT_KIND:
        errors.add(at + ("kind",), "transport_kind_invalid", f"transport kind must be {TRANSPORT_KIND!r}")
    path = transport.get("path")
    if "path" in transport and (
        not isinstance(path, str) or not _TRANSPORT_PATH.fullmatch(path) or {".", ".."} & set(path.split("/"))
    ):
        errors.add(at + ("path",), "transport_path_invalid", "transport path must be a canonical slash-prefixed relative path")
    return path if len(errors) == before else None


def _validate_map(mapping: Any, inputs: Mapping[str, Any] | None, at: tuple[PathPart, ...],
                  errors: ErrorCollector) -> dict[str, str] | None:
    if not isinstance(mapping, Mapping) or not mapping:
        errors.add(at, "type_mismatch", "binding map must be a non-empty mapping")
        return None
    before = len(errors)
    targets: dict[str, str] = {}
    for logical, field_name in mapping.items():
        here = at + (str(logical),)
        if inputs is not None and logical not in inputs:
            errors.add(here, "reference_unknown", f"map names {logical!r}, which is not an input")
            continue
        if inputs is not None and inputs[logical]["type"] == "object":
            errors.add(here, "input_not_mappable", f"input {logical} is an object and cannot be mapped to a form field")
            continue
        if not isinstance(field_name, str) or not _FIELD_NAME.fullmatch(field_name):
            errors.add(here, "name_invalid", f"map target for {logical} must be a plain form field name")
            continue
        if field_name in targets.values():
            errors.add(here, "target_duplicate", f"map target {field_name!r} is used twice")
            continue
        targets[logical] = field_name
    if inputs is not None:
        image = next(name for name, spec in inputs.items() if spec["type"] == "image")
        # An image that is mapped to a bad target has its own error above; it is not "unmapped" as well.
        if image not in mapping:
            errors.add(at, "image_unmapped", "the image input must be mapped")
    return targets if len(errors) == before else None


def _check_integer_spec(name: str, spec: Mapping[str, Any], at: tuple[PathPart, ...], errors: ErrorCollector) -> bool:
    for key in ("minimum", "maximum", "default"):
        value = spec.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            errors.add(at + (key,), "type_mismatch", f"input {name} {key} must be an integer")
            return False
    return spec.get("enum") is None or _check_enum(name, spec["enum"], int, at, errors)


def _check_enum(name: str, values: Any, kind: type, at: tuple[PathPart, ...], errors: ErrorCollector) -> bool:
    if not isinstance(values, list) or not values:
        errors.add(at + ("enum",), "enum_invalid", f"input {name} enum must be a non-empty list")
        return False
    for index, value in enumerate(values):
        if isinstance(value, bool) or not isinstance(value, kind):
            errors.add(at + ("enum", index), "type_mismatch", f"input {name} enum holds a value of the wrong type")
            return False
    return True


def _check_value(name: str, spec: Mapping[str, Any], value: Any, at: tuple[PathPart, ...], errors: ErrorCollector) -> bool:
    # A default must satisfy the same rules a caller's value does.
    try:
        _coerce(name, spec, value)
    except MeshInputError as error:
        errors.add(at + ("default",), "default_invalid", f"input {name} default is invalid: {error}")
        return False
    return True


def _closed(mapping: Mapping[str, Any], required: tuple[str, ...], optional: tuple[str, ...], where: str,
            at: tuple[PathPart, ...], errors: ErrorCollector) -> None:
    unknown = set(mapping) - set(required) - set(optional)
    if unknown:
        errors.add(at, "unknown_field", f"{where} has unknown keys {sorted(unknown)}")
    missing = [key for key in required if key not in mapping]
    if missing:
        errors.add(at, "required_field_missing", f"{where} is missing {missing}")


def _text(value: Any, where: str, at: tuple[PathPart, ...], errors: ErrorCollector) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.add(at, "value_empty", f"{where} must be a non-empty string")
        return None
    return value.strip()


def _plain(value: Any) -> Any:
    return copy.deepcopy(json.loads(json.dumps(value, default=str)))


# --- caller input -------------------------------------------------------------

@dataclass(frozen=True)
class ImageAsset:
    """A validated image part: the bytes plus the filename and content type it is sent with."""

    data: bytes
    filename: str
    content_type: str


@dataclass(frozen=True)
class MeshRequest:
    """A caller's accepted inputs, typed. ``image`` is the only file; the rest are scalars or objects."""

    image: ImageAsset
    values: Mapping[str, Any]
    unmapped_supplied: tuple[str, ...] = ()


def bind_request(manifest: MeshManifest, supplied: Mapping[str, Any], image: ImageAsset | None) -> MeshRequest:
    """Check the caller's fields against the manifest. Unknown or unmapped fields are refused, never ignored.

    ``supplied`` holds the scalar and object fields by logical name; ``image`` is the file part.
    """
    for name in supplied:
        if name not in manifest.inputs:
            raise MeshInputError("unknown_field", f"{name} is not a field of this route", param=name)
    if image is None:
        raise MeshInputError("missing_image", "the image part is required", param=manifest.image_input)
    values: dict[str, Any] = {}
    for name, spec in manifest.inputs.items():
        if spec["type"] == "image":
            continue
        if name in supplied:
            if name not in manifest.mapping:
                raise MeshInputError("unsupported_input", f"{name} is not supported by this route", param=name)
            values[name] = _coerce(name, spec, supplied[name])
        elif "default" in spec:
            values[name] = copy.deepcopy(spec["default"])
    return MeshRequest(image=image, values=values)


def _coerce(name: str, spec: Mapping[str, Any], value: Any) -> Any:
    kind = spec["type"]
    if kind == "integer":
        if isinstance(value, bool):
            raise MeshInputError("invalid_input", f"{name} must be an integer", param=name)
        if isinstance(value, str):
            if not re.fullmatch(r"-?[0-9]+", value.strip()):
                raise MeshInputError("invalid_input", f"{name} must be an integer", param=name)
            value = int(value.strip())
        if not isinstance(value, int) or abs(value) >= _INTEGER_LIMIT:
            raise MeshInputError("invalid_input", f"{name} must be an integer", param=name)
    elif kind == "string":
        if not isinstance(value, str):
            raise MeshInputError("invalid_input", f"{name} must be text", param=name)
    elif kind == "object":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as error:
                raise MeshInputError("invalid_input", f"{name} must be a JSON object", param=name) from error
        if not isinstance(value, dict):
            raise MeshInputError("invalid_input", f"{name} must be a JSON object", param=name)
        return value
    minimum, maximum = spec.get("minimum"), spec.get("maximum")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise MeshInputError("invalid_input", f"{name} is outside its range", param=name)
    enum = spec.get("enum")
    if enum is not None and value not in enum:
        raise MeshInputError("invalid_input", f"{name} is not an allowed value", param=name)
    return value


def sniff_image(data: bytes, *, max_bytes: int) -> ImageAsset:
    """Accept PNG, JPEG or WebP by its leading bytes. The filename and content type come from what was read."""
    if not data:
        raise MeshInputError("invalid_image", "the image part is empty", param="image")
    if len(data) > max_bytes:
        raise MeshInputError("image_too_large", "the image is larger than this route accepts", param="image")
    if data.startswith(_PNG):
        return ImageAsset(data, "input.png", "image/png")
    if data.startswith(_JPEG):
        return ImageAsset(data, "input.jpg", "image/jpeg")
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ImageAsset(data, "input.webp", "image/webp")
    raise MeshInputError("unsupported_image", "the image must be PNG, JPEG or WebP", param="image")


def downstream_form(manifest: MeshManifest, request: MeshRequest) -> tuple[dict[str, str], ImageAsset]:
    """The flat scalar fields and the image part the service receives, keyed by its form field names."""
    fields: dict[str, str] = {}
    for logical, field_name in manifest.mapping.items():
        if logical == manifest.image_input or logical not in request.values:
            continue
        fields[field_name] = _canonical_scalar(request.values[logical])
    return fields, request.image


def _canonical_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MeshInputError("invalid_input", "non-finite numbers cannot be sent")
        return repr(value)
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
