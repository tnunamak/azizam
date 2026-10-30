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

MESH_INTENT = "mesh.generate"
MESH_MEDIA_TYPE = "model/gltf-binary"
TRANSPORT_KIND = "http-multipart"

_TOP_LEVEL_REQUIRED = ("schema_version", "id", "intent", "version", "inputs", "outputs", "bindings")
_TOP_LEVEL_OPTIONAL = (
    "min_gateway_version", "capability", "display_name", "purpose", "summary", "description",
    "technical_description", "source", "trusted", "license",
)
_INPUT_KEYS = frozenset({"label", "help", "type", "required", "default", "minimum", "maximum", "enum", "modality"})
_INPUT_TYPES = frozenset({"image", "integer", "string", "object"})
_SERVICE_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_TRANSPORT_PATH = re.compile(r"/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*")
_INTEGER_LIMIT = 2**63

_PNG = b"\x89PNG\r\n\x1a\n"
_JPEG = b"\xff\xd8\xff"


class MeshManifestError(ValueError):
    """The bundled manifest is malformed. The gateway refuses to serve the route."""


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
    if not isinstance(raw, Mapping):
        raise MeshManifestError("mesh manifest must be a mapping")
    _closed(raw, _TOP_LEVEL_REQUIRED, _TOP_LEVEL_OPTIONAL, "manifest")
    if raw["schema_version"] != 1:
        raise MeshManifestError("mesh manifest schema_version must be 1")
    if raw["intent"] != MESH_INTENT:
        raise MeshManifestError(f"mesh manifest intent must be {MESH_INTENT!r}")
    manifest_id = _text(raw["id"], "id")
    version = _text(raw["version"], "version")

    inputs = _validate_inputs(raw["inputs"])
    output_media_type = _validate_outputs(raw["outputs"])
    service_id, transport_path, mapping = _validate_bindings(raw["bindings"], inputs)

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


def _validate_inputs(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping) or not raw:
        raise MeshManifestError("mesh manifest inputs must be a non-empty mapping")
    inputs: dict[str, dict[str, Any]] = {}
    for name, spec in raw.items():
        if not isinstance(name, str) or not _FIELD_NAME.fullmatch(name):
            raise MeshManifestError(f"input name {name!r} is not a plain field name")
        if not isinstance(spec, Mapping):
            raise MeshManifestError(f"input {name} must be a mapping")
        _closed(spec, ("type",), tuple(sorted(_INPUT_KEYS - {"type"})), f"input {name}")
        kind = spec["type"]
        if kind not in _INPUT_TYPES:
            raise MeshManifestError(f"input {name} type must be one of {sorted(_INPUT_TYPES)}")
        checked = _plain(dict(spec))
        if kind == "image":
            if checked.get("modality") != "image":
                raise MeshManifestError(f"input {name} of type image must declare modality image")
        elif "modality" in checked:
            raise MeshManifestError(f"input {name} may declare a modality only when it is an image")
        if kind == "integer":
            _check_integer_spec(name, checked)
        if kind == "string" and checked.get("enum") is not None:
            _check_enum(name, checked["enum"], str)
        if "default" in checked:
            _check_value(name, checked, checked["default"])
        inputs[name] = checked
    images = [name for name, spec in inputs.items() if spec["type"] == "image"]
    if len(images) != 1 or inputs[images[0]].get("required") is not True:
        raise MeshManifestError("mesh manifest needs exactly one required image input")
    return inputs


def _validate_outputs(raw: Any) -> str:
    if not isinstance(raw, Mapping) or len(raw) != 1:
        raise MeshManifestError("mesh manifest outputs must name exactly one output")
    (name, spec), = raw.items()
    if not isinstance(spec, Mapping) or set(spec) != {"modality", "media_type"}:
        raise MeshManifestError(f"output {name} must declare modality and media_type only")
    if spec["modality"] != "mesh" or spec["media_type"] != MESH_MEDIA_TYPE:
        raise MeshManifestError(f"output {name} must be modality mesh with media type {MESH_MEDIA_TYPE}")
    return MESH_MEDIA_TYPE


def _validate_bindings(raw: Any, inputs: Mapping[str, Any]) -> tuple[str, str, dict[str, str]]:
    if not isinstance(raw, Mapping) or len(raw) != 1:
        raise MeshManifestError("mesh manifest must hold exactly one binding")
    (service_id, binding), = raw.items()
    if not isinstance(service_id, str) or not _SERVICE_ID.fullmatch(service_id):
        raise MeshManifestError(f"binding key {service_id!r} is not a service id")
    if not isinstance(binding, Mapping):
        raise MeshManifestError("binding must be a mapping")
    _closed(binding, ("transport", "map"), (), f"binding {service_id}")

    transport = binding["transport"]
    if not isinstance(transport, Mapping):
        raise MeshManifestError("transport must be a mapping")
    _closed(transport, ("kind", "path"), (), "transport")
    if transport["kind"] != TRANSPORT_KIND:
        raise MeshManifestError(f"transport kind must be {TRANSPORT_KIND!r}")
    path = transport["path"]
    if not isinstance(path, str) or not _TRANSPORT_PATH.fullmatch(path) or {".", ".."} & set(path.split("/")):
        raise MeshManifestError("transport path must be a canonical slash-prefixed relative path")

    mapping = binding["map"]
    if not isinstance(mapping, Mapping) or not mapping:
        raise MeshManifestError("binding map must be a non-empty mapping")
    targets: dict[str, str] = {}
    for logical, field_name in mapping.items():
        if logical not in inputs:
            raise MeshManifestError(f"map names {logical!r}, which is not an input")
        if inputs[logical]["type"] == "object":
            raise MeshManifestError(f"input {logical} is an object and cannot be mapped to a form field")
        if not isinstance(field_name, str) or not _FIELD_NAME.fullmatch(field_name):
            raise MeshManifestError(f"map target for {logical} must be a plain form field name")
        if field_name in targets.values():
            raise MeshManifestError(f"map target {field_name!r} is used twice")
        targets[logical] = field_name
    image = next(name for name, spec in inputs.items() if spec["type"] == "image")
    if image not in targets:
        raise MeshManifestError("the image input must be mapped")
    return service_id, path, targets


def _check_integer_spec(name: str, spec: Mapping[str, Any]) -> None:
    for key in ("minimum", "maximum", "default"):
        value = spec.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise MeshManifestError(f"input {name} {key} must be an integer")
    if spec.get("enum") is not None:
        _check_enum(name, spec["enum"], int)


def _check_enum(name: str, values: Any, kind: type) -> None:
    if not isinstance(values, list) or not values:
        raise MeshManifestError(f"input {name} enum must be a non-empty list")
    for value in values:
        if isinstance(value, bool) or not isinstance(value, kind):
            raise MeshManifestError(f"input {name} enum holds a value of the wrong type")


def _check_value(name: str, spec: Mapping[str, Any], value: Any) -> None:
    # A default must satisfy the same rules a caller's value does.
    try:
        _coerce(name, spec, value)
    except MeshInputError as error:
        raise MeshManifestError(f"input {name} default is invalid: {error}") from error


def _closed(mapping: Mapping[str, Any], required: tuple[str, ...], optional: tuple[str, ...], where: str) -> None:
    unknown = set(mapping) - set(required) - set(optional)
    if unknown:
        raise MeshManifestError(f"{where} has unknown keys {sorted(unknown)}")
    missing = [key for key in required if key not in mapping]
    if missing:
        raise MeshManifestError(f"{where} is missing {missing}")


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MeshManifestError(f"{where} must be a non-empty string")
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
