"""The mesh route's wire: form decoding, wire error codes, scalar encoding and the picture check.

The workflow is data, and the registry validates it like any other (``manifests.enforce_contract``). The mesh frontend
profile (``mesh_api.mesh_profile_errors``) says which valid workflows this wire can carry. What stays here is the wire itself:
an integer read from form text, an object read from JSON text, the six error codes the member sees, the PNG, JPEG and WebP
check of the one file part, and the scalar encoding of the form fields sent on. Nothing here knows an engine: the service id
and the form field names are values of the workflow.
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from gateway import media_inputs

MESH_INTENT = "mesh.generate"
MESH_MEDIA_TYPE = "model/gltf-binary"

_INTEGER_LIMIT = 2**63
# The picture types the wire carries and the neutral name each is sent under (the caller's own name is never sent on).
_PICTURE_NAMES = {"image/png": "input.png", "image/jpeg": "input.jpg", "image/webp": "input.webp"}


class MeshManifestError(ValueError):
    """The workflow cannot be served by the mesh route: the frontend profile refused it. The route is then unavailable."""


class MeshInputError(ValueError):
    """A caller's multipart input is invalid for the workflow. Maps to 400."""

    def __init__(self, code: str, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.param = param


class MeshRoute(Protocol):
    """What the wire reads of the workflow it serves (``mesh_api.MeshWorkflow``)."""

    @property
    def inputs(self) -> Mapping[str, Mapping[str, Any]]: ...

    @property
    def mapping(self) -> Mapping[str, str]: ...

    @property
    def image_input(self) -> str: ...


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


def bind_request(manifest: MeshRoute, supplied: Mapping[str, Any], image: ImageAsset | None) -> MeshRequest:
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
        if name == manifest.image_input:
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


def sniff_image(data: bytes, *, max_bytes: int, param: str = "image") -> ImageAsset:
    """Accept PNG, JPEG or WebP by its leading bytes. The filename and content type come from what was read. The bytes
    are measured by the media resolver's own measure, so the gateway has one signature check; this wire carries three types
    of what it knows. ``param`` is the name of the picture input, which a refusal names."""
    if not data:
        raise MeshInputError("invalid_image", "the image part is empty", param=param)
    if len(data) > max_bytes:
        raise MeshInputError("image_too_large", "the image is larger than this route accepts", param=param)
    media_type = media_inputs.measure(data).media_type
    if media_type not in _PICTURE_NAMES:
        raise MeshInputError("unsupported_image", "the image must be PNG, JPEG or WebP", param=param)
    return ImageAsset(data, _PICTURE_NAMES[media_type], media_type)


def downstream_form(manifest: MeshRoute, request: MeshRequest) -> tuple[dict[str, str], ImageAsset]:
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
