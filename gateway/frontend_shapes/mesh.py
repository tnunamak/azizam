"""The reviewed wire of a one-picture, one-body route: a picture in as one multipart file part, one finished model out.

Two shapes use it: the LocalAI ``/v1/3d/generations`` shape (a JSON answer that carries the model) and the raw multipart
shape (the model bytes as the body). ``mesh_profile`` is what a contract must be for this wire to carry it: the profile
that ``mesh_api.mesh_profile_errors`` used to hold, now selected by the row's selector instead of by an intent word.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any

from starlette.responses import JSONResponse, Response

from gateway.frontend_shapes import _wire
from gateway.frontend_shapes._base import Shape
from gateway.mesh_manifest import MESH_MEDIA_TYPE as OUTPUT_MEDIA_TYPE
from gateway.workflow_runtime_types import CallerContext, JobService, RunJobView

_MESH_ADAPTER = "http-multipart"

_ROUTE_TYPES = frozenset({"asset", "integer", "string", "object"})
# The picture types the route sniffs (``sniff_image``). The picture input must accept exactly these: a type the route cannot
# read is never advertised, and a type it reads is never refused by the contract.
_SNIFFED_PICTURES = frozenset({"image/png", "image/jpeg", "image/webp"})
# The wire reads an integer below this (``mesh_manifest``): a contract bound, default or allowed value past it is refused.
_INTEGER_LIMIT = 2**63
# Constraints of the schema that the route's form decoding does not enforce. A contract that states one is refused, never
# served with the rule silently dropped.
_UNENFORCED_KEYS = ("step", "offset", "generate", "min_length", "max_length")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _picture_accepts_exactly(accepts: Any) -> bool:
    return isinstance(accepts, (list, tuple)) and all(isinstance(item, str) for item in accepts) \
        and {item.strip().lower() for item in accepts} == _SNIFFED_PICTURES


def _input_problems(name: Any, spec: Any, mapped: bool) -> list[str]:
    """What the route cannot do with one input: a spec that is not a mapping, a type it cannot read, an object that is mapped
    to a form field, a constraint its decoding does not enforce, a number past what the wire reads, a required option."""
    if not isinstance(name, str) or not name:
        return [f"input name {name!r} is not text"]
    if not isinstance(spec, Mapping):
        return [f"input {name} must be a mapping"]
    kind = spec.get("type")
    if not isinstance(kind, str) or kind not in _ROUTE_TYPES:
        return [f"input {name} has a type the route cannot read"]
    if kind == "asset":
        # The picture's acceptance is checked by the profile, for the one required picture. Its byte cap is read only as
        # an integer (``max_image_bytes``); the grammar also admits 1.0, which the route would silently ignore.
        if spec.get("max_bytes") is not None and not _is_int(spec["max_bytes"]):
            return [f"input {name} max_bytes must be an integer"]
        return []
    problems: list[str] = []
    if spec.get("required") is True:
        problems.append(f"input {name} cannot be required: only the picture is")
    if kind == "object" and mapped:
        problems.append(f"input {name} is an object and cannot be mapped to a form field")
    for key in _UNENFORCED_KEYS:
        if key in spec:
            problems.append(f"input {name} states {key}, which the route does not enforce")
    if kind == "integer":
        for key in ("minimum", "maximum", "default"):
            if spec.get(key) is not None and not _is_int(spec[key]):
                problems.append(f"input {name} {key} must be an integer")
        numbers = [spec.get(key) for key in ("minimum", "maximum", "default")]
        numbers += list(spec["enum"]) if isinstance(spec.get("enum"), (list, tuple)) else []
        if any(_is_int(number) and abs(number) >= _INTEGER_LIMIT for number in numbers):
            problems.append(f"input {name} holds a number past what the route reads")
    if kind == "string" and "default" in spec and spec["default"] is not None and not isinstance(spec["default"], str):
        problems.append(f"input {name} default must be text")
    enum = spec.get("enum")
    if enum is not None and (not isinstance(enum, (list, tuple)) or not enum):
        problems.append(f"input {name} enum must be a non-empty list")
    return problems


def _output_count_problem(spec: Any) -> bool:
    """Whether a stated output count allows other than one body."""
    count = spec.get("count") if isinstance(spec, Mapping) else None
    if count is None:
        return False
    return not isinstance(count, Mapping) or count.get("max") != 1 or (count.get("min") not in (None, 0, 1))


def mesh_profile(contract: Mapping[str, Any]) -> list[str]:
    """Why the mesh frontend cannot serve this workflow, from the contract only: the wire carries one file part and calls
    one engine over one multipart POST, and answers with one GLB body. Empty when it can. A contract of any shape gets an
    answer here, never an exception."""
    if not isinstance(contract, Mapping):
        return ["the contract must be a mapping"]
    problems: list[str] = []
    for key in ("id", "version"):
        value = contract.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{key} must be a non-empty string")
        elif value != value.strip():
            problems.append(f"{key} must not start or end with a space")
    raw_inputs = contract.get("inputs")
    inputs = raw_inputs if isinstance(raw_inputs, Mapping) else {}
    if not inputs:
        problems.append("inputs must be a non-empty mapping")
    pictures = [name for name, spec in inputs.items() if isinstance(spec, Mapping) and spec.get("type") == "asset"]
    required = [name for name in pictures if inputs[name].get("required") is True]
    if len(pictures) != 1 or len(required) != 1:
        problems.append("exactly one required picture input")
    bindings = contract.get("bindings") if isinstance(contract.get("bindings"), Mapping) else {}
    if len(bindings) != 1:
        problems.append("exactly one binding")
    binding = next(iter(bindings.values()), None) if len(bindings) == 1 else None
    if len(bindings) == 1 and (next(iter(bindings)) != _MESH_ADAPTER or not isinstance(binding, Mapping)
                               or not isinstance(binding.get("service"), str) or not isinstance(binding.get("path"), str)
                               or not isinstance(binding.get("map"), Mapping)):
        problems.append(f"the binding must be a {_MESH_ADAPTER} binding with a service, a path and a map")
    mapping = binding.get("map") if isinstance(binding, Mapping) and isinstance(binding.get("map"), Mapping) else {}
    for name, spec in inputs.items():
        problems.extend(_input_problems(name, spec, name in mapping))
    for logical, target in mapping.items():
        if logical not in inputs:
            problems.append(f"map names {logical!r}, which is not an input")
        if not isinstance(target, str) or not target:
            problems.append(f"map target for {logical} must be a form field name")
    if len(bindings) == 1 and len(required) == 1 and required[0] not in mapping:
        problems.append("the picture input must be mapped")
    if len(required) == 1 and not _picture_accepts_exactly(inputs[required[0]].get("accepts")):
        problems.append(f"the picture input must accept {', '.join(sorted(_SNIFFED_PICTURES))} and nothing else")
    outputs = contract.get("outputs") if isinstance(contract.get("outputs"), Mapping) else {}
    media = [spec.get("media_type") for spec in outputs.values() if isinstance(spec, Mapping)]
    if len(outputs) != 1 or media != [OUTPUT_MEDIA_TYPE]:
        problems.append(f"exactly one output, {OUTPUT_MEDIA_TYPE}")
    elif _output_count_problem(next(iter(outputs.values()))):
        problems.append("the output must be one model")
    return problems




class _OnePicture(Shape):
    """A picture in as one multipart file part named ``image``, one finished model out."""

    body = "multipart"
    files = "one"
    transport_fields = frozenset({"model", "response_format", "user"})
    timeout_code = "generation_timeout"
    save_gate = True

    def profile(self, contract: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(mesh_profile(contract))

    def model_ref(self, view: RunJobView):
        refs = self.media_refs(view)
        return refs[0] if len(refs) == 1 else None


class LocalAI3D(_OnePicture):
    """The LocalAI ``/v1/3d/generations`` shape: the answer is ``{"created", "data": [{"b64_json"}]}``."""

    shape_id = "localai.3d.generations"

    async def render_success(self, view: RunJobView, jobs: JobService, caller: CallerContext) -> Response:
        ref = self.model_ref(view)
        if ref is None:
            return _wire.error_response(500, "The result is not one model", kind="api_error", code="result_shape")
        data = base64.b64encode(self.read(view, ref, jobs, caller)).decode("ascii")
        return JSONResponse({"created": view.created_at_s, "data": [{"b64_json": data, "media_type": ref.media_type}]})


class RawGlb(_OnePicture):
    """The raw multipart shape: the finished model is the response body."""

    shape_id = "multipart.glb"

    async def render_success(self, view: RunJobView, jobs: JobService, caller: CallerContext) -> Response:
        ref = self.model_ref(view)
        if ref is None:
            return _wire.error_response(500, "The result is not one model", kind="api_error", code="result_shape")
        return Response(content=self.read(view, ref, jobs, caller), media_type=ref.media_type)
