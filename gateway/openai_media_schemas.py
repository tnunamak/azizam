"""OpenAPI schema fragments for OpenAI-compatible media endpoints.

These routes are wired with ``request: Request`` so FastAPI cannot infer their
shapes. The fragments here are advertised via ``openapi_extra=`` so the public
``/openapi.json`` describes each endpoint honestly:

* Request bodies match what each route actually accepts (JSON for chat/TTS/image
  generations, multipart for image edits and STT).
* Response media types reflect what the route actually returns (audio bytes for
  TTS, JSON for transcriptions, JSON for image generations, etc.) rather than
  the FastAPI default of ``application/json: {schema: {}}``.
* Errors use the OpenAI-shaped envelope ``{"error": {message, type, ...}}``.

The schemas are intentionally permissive (``additionalProperties: True``) so we
do not drift from real OpenAI request payloads as they evolve, while still
giving SDK generators and clients the right top-level entry points.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any, Mapping

import yaml

JsonObjectSchema = dict[str, Any]


@dataclass(frozen=True)
class MediaSchemaRegistry:
    endpoints: Mapping[str, JsonObjectSchema]
    wire_error_params: Mapping[str, Mapping[str, str]]


class _FragmentLoader(yaml.SafeLoader):
    """Safe YAML without duplicate/merged keys that could hide reviewed data."""


def _unique_fragment_mapping(loader: _FragmentLoader, node: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        if key_node.tag == "tag:yaml.org,2002:merge":
            raise ValueError("schema fragments do not allow merge keys")
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str) or key == "<<" or key in result:
            raise ValueError(f"schema fragments require unique string mapping keys: {key!r}")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_FragmentLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_fragment_mapping,
)


def _fragment_mapping(
    value: Any, label: str, allowed: set[str] | None = None,
    required: set[str] | frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) or not key for key in value):
        raise ValueError(f"{label}: expected a mapping with nonempty string keys")
    if (allowed is not None and set(value) - allowed) or required - set(value):
        unknown = set(value) - allowed if allowed is not None else set()
        raise ValueError(f"{label}: unknown fields {sorted(unknown)}; missing fields {sorted(required - set(value))}")
    return value


def _validate_fragment_schema(value: Any, label: str, depth: int = 0) -> None:
    if depth > 32:
        raise ValueError(f"{label}: schema nesting is too deep")
    schema = _fragment_mapping(value, label, {
        "type", "properties", "required", "additionalProperties", "enum",
        "minimum", "maximum", "description", "format", "items",
    }, {"type"})
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    known_types = {"object", "array", "string", "number", "integer", "boolean", "null"}
    if not types or any(not isinstance(item, str) or item not in known_types for item in types) or len(set(types)) != len(types):
        raise ValueError(f"{label}: invalid type")
    for key in ("description", "format"):
        if key in schema and not isinstance(schema[key], str):
            raise ValueError(f"{label}.{key}: expected text")
    properties = _fragment_mapping(schema.get("properties", {}), f"{label}.properties")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ValueError(f"{label}.required: expected property names")
    if len(set(required)) != len(required) or set(required) - set(properties):
        raise ValueError(f"{label}.required: duplicate or undeclared property")
    if any(key in schema for key in ("properties", "required", "additionalProperties")) and "object" not in types:
        raise ValueError(f"{label}: object keywords require object type")
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
        raise ValueError(f"{label}.additionalProperties: expected a boolean")
    for key, child in properties.items():
        _validate_fragment_schema(child, f"{label}.properties.{key}", depth + 1)
    if "items" in schema:
        if "array" not in types:
            raise ValueError(f"{label}: items requires array type")
        _validate_fragment_schema(schema["items"], f"{label}.items", depth + 1)
    if "enum" in schema:
        values = schema["enum"]
        if not isinstance(values, list) or not values:
            raise ValueError(f"{label}.enum: expected nonempty scalar list")
        for item in values:
            if item is not None and type(item) not in (str, bool, int, float):
                raise ValueError(f"{label}.enum: expected JSON scalars")
            if type(item) is float and not math.isfinite(item):
                raise ValueError(f"{label}.enum: expected finite scalars")
    for key in ("minimum", "maximum"):
        if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
            raise ValueError(f"{label}.{key}: expected a finite number")
    if "minimum" in schema and "maximum" in schema and schema["minimum"] > schema["maximum"]:
        raise ValueError(f"{label}: minimum exceeds maximum")


def _validate_fragment_content(value: Any, label: str) -> None:
    content = _fragment_mapping(value, label)
    if not content:
        raise ValueError(f"{label}: expected at least one content type")
    for media_type, entry in content.items():
        if not re.fullmatch(r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+", media_type):
            raise ValueError(f"{label}: invalid content type")
        entry = _fragment_mapping(entry, f"{label}.{media_type}", {"schema"}, {"schema"})
        _validate_fragment_schema(entry["schema"], f"{label}.{media_type}.schema")


def load_media_schema_registry(path: Path) -> MediaSchemaRegistry:
    """Load a closed data-only format; it cannot resolve imports or references."""
    try:
        document = yaml.load(path.read_text(encoding="utf-8"), Loader=_FragmentLoader)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise ValueError(f"{path}: invalid schema fragment YAML: {exc}") from exc
    document = _fragment_mapping(
        document, str(path), {
            "schema_version", "endpoints", "wire_error_params", "wire_error_param_endpoints",
        },
        {"schema_version", "endpoints"},
    )
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise ValueError(f"{path}: unsupported schema_version")
    endpoints = _fragment_mapping(document["endpoints"], f"{path}: endpoints")
    if not endpoints:
        raise ValueError(f"{path}: expected at least one endpoint")
    for key, fragment in endpoints.items():
        if not re.fullmatch(r"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE) /[^\s?#]*", key):
            raise ValueError(f"{path}: invalid HTTP method/path key")
        label = f"{path}: endpoints.{key}"
        fragment = _fragment_mapping(fragment, label, {"requestBody", "responses"})
        if not fragment:
            raise ValueError(f"{label}: empty endpoint fragment")
        if "requestBody" in fragment:
            body = _fragment_mapping(fragment["requestBody"], f"{label}.requestBody", {"required", "content"}, {"required", "content"})
            if type(body["required"]) is not bool:
                raise ValueError(f"{label}: requestBody.required must be boolean")
            _validate_fragment_content(body["content"], f"{label}.requestBody.content")
        if "responses" in fragment:
            responses = _fragment_mapping(fragment["responses"], f"{label}.responses")
            if not responses:
                raise ValueError(f"{label}: empty responses")
            for status, response in responses.items():
                if not re.fullmatch(r"[1-5][0-9]{2}|default", status):
                    raise ValueError(f"{label}: invalid response status")
                response = _fragment_mapping(response, f"{label}.{status}", {"description", "content"}, {"description", "content"})
                if not isinstance(response["description"], str):
                    raise ValueError(f"{label}.{status}: description must be text")
                _validate_fragment_content(response["content"], f"{label}.{status}.content")
    endpoint_label = f"{path}: wire_error_param_endpoints"
    error_endpoints = _fragment_mapping(
        document.get("wire_error_param_endpoints", {}), endpoint_label,
    )
    for domain, endpoint in error_endpoints.items():
        if not isinstance(endpoint, str) or not re.fullmatch(
            r"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE) /[^\s?#]*", endpoint,
        ):
            raise ValueError(f"{endpoint_label}.{domain}: invalid HTTP method/path key")
    error_params: dict[str, Mapping[str, str]] = {}
    if "wire_error_params" in document:
        label = f"{path}: wire_error_params"
        domains = _fragment_mapping(document["wire_error_params"], label)
        if not domains:
            raise ValueError(f"{label}: expected at least one domain")
        for domain, value in domains.items():
            group_label = f"{label}.{domain}"
            group = _fragment_mapping(value, group_label)
            if not group:
                raise ValueError(f"{group_label}: expected at least one parameter")
            endpoint = error_endpoints.get(domain)
            request_properties = None
            if endpoint is not None and endpoint not in endpoints:
                raise ValueError(f"{endpoint_label}.{domain}: endpoint {endpoint!r} is not in endpoints")
            if endpoint is not None:
                # A public parameter may belong to any supported request content type.
                request_properties = {
                    name
                    for entry in endpoints[endpoint].get("requestBody", {}).get("content", {}).values()
                    for name in entry["schema"].get("properties", {})
                }
            for logical, public in group.items():
                if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", logical):
                    raise ValueError(f"{group_label}.{logical}: invalid logical parameter")
                if not isinstance(public, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\[\]-]{0,127}", public):
                    raise ValueError(f"{group_label}.{logical}: invalid public parameter")
                if request_properties is not None and public not in request_properties:
                    raise ValueError(
                        f"{group_label}.{logical}: public parameter {public!r} is not a request "
                        f"property of endpoint {endpoint!r}"
                    )
            error_params[domain] = MappingProxyType(dict(group))
    if unknown := sorted(set(error_endpoints) - set(error_params)):
        raise ValueError(f"{endpoint_label}: no wire_error_params group for {unknown}")
    return MediaSchemaRegistry(
        endpoints=MappingProxyType(deepcopy(endpoints)),
        wire_error_params=MappingProxyType(error_params),
    )


def _load_schema_fragments(path: Path) -> dict[str, JsonObjectSchema]:
    """Compatibility facade: return independently owned endpoint fragments."""
    return deepcopy(dict(load_media_schema_registry(path).endpoints))


def wire_error_param(
    domain: str, logical_param: str, *, registry: MediaSchemaRegistry | None = None,
) -> str:
    """Resolve one exact algorithm error key; missing configuration never falls back."""
    source = _MEDIA_SCHEMA_REGISTRY if registry is None else registry
    try:
        return source.wire_error_params[domain][logical_param]
    except KeyError as exc:
        raise KeyError(f"wire_error_params[{domain!r}][{logical_param!r}]") from exc


def _endpoint_fragment(
    method: str, path: str, *, fragments: dict[str, JsonObjectSchema] | None = None,
) -> JsonObjectSchema:
    source = _SCHEMA_FRAGMENTS if fragments is None else fragments
    return deepcopy(source[f"{method} {path}"])


_MEDIA_SCHEMA_REGISTRY = load_media_schema_registry(
    Path(__file__).resolve().parent.parent / "registry" / Path(__file__).with_suffix(".yml").name,
)
_SCHEMA_FRAGMENTS = deepcopy(dict(_MEDIA_SCHEMA_REGISTRY.endpoints))

# Validate the shipped exception adapters before accepting any requests.
wire_error_param("identity_scene", "reference_photos")
wire_error_param("segment_chain", "source_clip")


_ERROR_SCHEMA: JsonObjectSchema = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["message", "type"],
            "properties": {
                "message": {"type": "string"},
                "type": {"type": "string"},
                "param": {"type": ["string", "null"]},
                "code": {"type": ["string", "null"]},
            },
            "additionalProperties": True,
        }
    },
}


def _json_error_response(description: str) -> JsonObjectSchema:
    return {
        "description": description,
        "content": {"application/json": {"schema": _ERROR_SCHEMA}},
    }


def _common_error_responses() -> dict[str, JsonObjectSchema]:
    return {
        "400": _json_error_response("Invalid request (OpenAI error envelope)."),
        "401": _json_error_response("Authentication failed."),
        "403": _json_error_response("Authenticated but not permitted by profile policy."),
        "404": _json_error_response("Resource not found."),
        "503": _json_error_response("Backend unavailable."),
    }


def _json_request(schema: JsonObjectSchema, *, required: bool = True) -> JsonObjectSchema:
    return {
        "required": required,
        "content": {"application/json": {"schema": schema}},
    }


def _audio_speech_request_schema() -> JsonObjectSchema:
    return {
        "type": "object",
        "required": ["input"],
        "properties": {
            "model": {"type": "string", "description": "TTS model id, e.g. 'tts-1'."},
            "input": {"type": "string", "description": "Text to synthesize."},
            "voice": {
                "description": (
                    "A built-in OpenAI voice name, or a custom voice object {\"id\": \"voice_...\"} "
                    "created with POST /v1/audio/voices (a bare voice_<id> string also works)."
                ),
                "anyOf": [
                    {"type": "string"},
                    {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}},
                ],
            },
            "response_format": {
                "type": "string",
                "enum": ["mp3", "opus", "aac", "flac", "wav", "pcm"],
                "default": "mp3",
                "description": "Audio encoding for synthesized speech.",
            },
            "stream_format": {
                "type": "string",
                "enum": ["audio", "sse"],
                "description": (
                    "audio: chunked audio bytes. sse: speech.audio.delta events. "
                    "Engines that synthesize incrementally send audio while speaking "
                    "(response_format=pcm streams by default); others answer once."
                ),
            },
            "speed": {"type": "number"},
            "instructions": {"type": "string"},
            "language": {"type": "string"},
        },
        "additionalProperties": True,
    }


AUDIO_SPEECH_OPENAPI: JsonObjectSchema = {
    "requestBody": _json_request(_audio_speech_request_schema()),
    "responses": {
        "200": {
            "description": "Synthesized audio bytes in the requested response_format.",
            "content": {
                "audio/mpeg": {"schema": {"type": "string", "format": "binary"}},
                "audio/ogg": {"schema": {"type": "string", "format": "binary"}},
                "audio/aac": {"schema": {"type": "string", "format": "binary"}},
                "audio/flac": {"schema": {"type": "string", "format": "binary"}},
                "audio/wav": {"schema": {"type": "string", "format": "binary"}},
                "audio/pcm": {"schema": {"type": "string", "format": "binary"}},
                "text/event-stream": {"schema": {"type": "string"}},
            },
        },
        **_common_error_responses(),
    },
}


def _multipart_audio_input_schema(*, translation: bool = False) -> JsonObjectSchema:
    base: JsonObjectSchema = {
        "type": "object",
        "required": ["file"],
        "properties": {
            "file": {
                "type": "string",
                "format": "binary",
                "description": "Audio file (mp3, wav, m4a, webm, ogg, flac).",
            },
            "model": {
                "type": "string",
                "description": "STT model id (e.g. 'whisper-1').",
            },
            "language": {"type": "string"},
            "prompt": {"type": "string"},
            "response_format": {
                "type": "string",
                "enum": ["json", "text", "srt", "verbose_json", "vtt"],
            },
            "temperature": {"type": "number"},
            "stream": {
                "type": "boolean",
                "description": (
                    "true: reply with server-sent events (transcript.text.delta, then "
                    "transcript.text.done). Streaming engines send deltas while decoding; "
                    "others send the whole text in one delta."
                ),
            },
        },
        "additionalProperties": True,
    }
    if translation:
        base["description"] = (
            "Translation endpoint: input audio is translated to English text."
        )
    else:
        # Speaker diarization (model gpt-4o-transcribe-diarize, alias stt-diarize).
        props = base["properties"]
        props["response_format"]["enum"] = ["json", "text", "srt", "verbose_json", "vtt", "diarized_json"]
        props["chunking_strategy"] = {
            "description": "\"auto\" or {\"type\": \"server_vad\"}. Accepted by the diarize model; the service always chunks long audio itself.",
            "oneOf": [{"type": "string", "enum": ["auto"]}, {"type": "object"}],
        }
        props["known_speaker_names[]"] = {
            "type": "array",
            "items": {"type": "string"},
            "description": "Diarize model: names for the speakers in known_speaker_references[] (1 to 4, same count).",
        }
        props["known_speaker_references[]"] = {
            "type": "array",
            "items": {"type": "string"},
            "description": "Diarize model: base64 data URLs of 2 to 10 s of each known speaker.",
        }
    return base


def _multipart_request(schema: JsonObjectSchema) -> JsonObjectSchema:
    return {
        "required": True,
        "content": {"multipart/form-data": {"schema": schema}},
    }


def _transcription_response_schema() -> JsonObjectSchema:
    return {
        "oneOf": [
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": True,
            },
            {
                "type": "string",
                "description": "Plain text body when response_format=text/srt/vtt.",
            },
        ]
    }


AUDIO_TRANSCRIPTIONS_OPENAPI: JsonObjectSchema = {
    "requestBody": _multipart_request(_multipart_audio_input_schema()),
    "responses": {
        "200": {
            "description": "Transcription result. Shape depends on response_format.",
            "content": {
                "application/json": {"schema": _transcription_response_schema()},
                "text/plain": {"schema": {"type": "string"}},
            },
        },
        **_common_error_responses(),
        "413": _json_error_response("Audio exceeds a configured upload, decode, duration, segment or format cap."),
        "503": {
            **_json_error_response("Backend unavailable, or shared STT capacity is busy (resource_admission_denied)."),
            "headers": {"Retry-After": {
                "description": "Retry delay in seconds when shared STT capacity is busy.",
                "schema": {"type": "integer", "example": 5},
            }},
        },
    },
}


AUDIO_TRANSLATIONS_OPENAPI: JsonObjectSchema = {
    "requestBody": _multipart_request(_multipart_audio_input_schema(translation=True)),
    "responses": {
        "200": {
            "description": "English translation of the input audio.",
            "content": {
                "application/json": {"schema": _transcription_response_schema()},
                "text/plain": {"schema": {"type": "string"}},
            },
        },
        **_common_error_responses(),
        "413": _json_error_response("Audio exceeds a configured upload, decode, duration, segment or format cap."),
        "503": {
            **_json_error_response("Backend unavailable, or shared STT capacity is busy (resource_admission_denied)."),
            "headers": {"Retry-After": {
                "description": "Retry delay in seconds when shared STT capacity is busy.",
                "schema": {"type": "integer", "example": 5},
            }},
        },
    },
}


def _voice_entry_schema() -> JsonObjectSchema:
    return {
        "type": "object",
        "required": ["voice_id", "name"],
        "properties": {
            "voice_id": {"type": "string"},
            "name": {"type": "string"},
            "language": {"type": "string"},
            "gender": {"type": "string"},
            "description": {"type": "string"},
            "mapped_to": {"type": "string"},
        },
        "additionalProperties": True,
    }


VOICES_LIST_OPENAPI: JsonObjectSchema = {
    "responses": {
        "200": {
            "description": "Available TTS voices acceptable to /v1/audio/speech.",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["voices"],
                        "properties": {
                            "voices": {
                                "type": "array",
                                "items": _voice_entry_schema(),
                            },
                            "object": {"type": "string", "enum": ["list"]},
                            "data": {
                                "type": "array",
                                "description": "The caller's own custom voices (audio.voice objects), newest first; page with after and limit.",
                                "items": {"type": "object", "additionalProperties": True},
                            },
                            "first_id": {"type": ["string", "null"]},
                            "last_id": {"type": ["string", "null"]},
                            "has_more": {"type": "boolean"},
                        },
                        "additionalProperties": True,
                    }
                }
            },
        },
        **_common_error_responses(),
    }
}


def _images_generate_request_schema() -> JsonObjectSchema:
    return {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "model": {"type": "string"},
            "n": {"type": "integer", "minimum": 1, "maximum": 10},
            "size": {"type": "string", "description": "e.g. '1024x1024'."},
            "response_format": {"type": "string", "enum": ["url", "b64_json"]},
            "quality": {"type": "string"},
            "style": {"type": "string"},
            "user": {"type": "string"},
        },
        "additionalProperties": True,
    }


def _image_object_schema() -> JsonObjectSchema:
    return {
        "type": "object",
        "properties": {
            "url": {"type": "string", "format": "uri"},
            "b64_json": {"type": "string"},
            "revised_prompt": {"type": "string"},
        },
        "additionalProperties": True,
    }


def _images_response_schema() -> JsonObjectSchema:
    return {
        "type": "object",
        "required": ["data"],
        "properties": {
            "created": {"type": "integer"},
            "data": {"type": "array", "items": _image_object_schema()},
        },
        "additionalProperties": True,
    }


IMAGES_GENERATIONS_OPENAPI: JsonObjectSchema = {
    "requestBody": _json_request(_images_generate_request_schema()),
    "responses": {
        "200": {
            "description": "OpenAI-shaped image generation result.",
            "content": {
                "application/json": {"schema": _images_response_schema()}
            },
        },
        **_common_error_responses(),
    },
}


def _images_edit_request_schema() -> JsonObjectSchema:
    return _endpoint_fragment("POST", "/v1/images/edits")["requestBody"]["content"]["multipart/form-data"]["schema"]


IMAGES_EDITS_OPENAPI: JsonObjectSchema = {
    "requestBody": _multipart_request(_images_edit_request_schema()),
    "responses": {
        "200": {
            "description": "OpenAI-shaped image edit result.",
            "content": {
                "application/json": {"schema": _images_response_schema()}
            },
        },
        **_common_error_responses(),
    },
}


IMAGES_VARIATIONS_OPENAPI: JsonObjectSchema = {
    "requestBody": _endpoint_fragment("POST", "/v1/images/variations")["requestBody"],
    "responses": {
        # Routed through a configurable edit-capable workflow (no local
        # model dedicated to "variations" exists) with a fixed generic
        # prompt -- see proxy_graph_image_variations_request in proxy.py.
        "200": {
            "description": "OpenAI-shaped image variation result.",
            "content": {
                "application/json": {"schema": _images_response_schema()}
            },
        },
        **_common_error_responses(),
    },
}


SIGNED_IMAGE_CONTENT_OPENAPI: JsonObjectSchema = {
    "responses": {
        "200": {
            "description": (
                "Raw image bytes (PNG/JPEG). Auth is the signed `expires`+`signature` "
                "query string returned alongside the original generation."
            ),
            "content": {
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                },
            },
        },
        "400": _json_error_response("Invalid signed URL."),
        "403": _json_error_response("Signature expired or invalid."),
        "404": _json_error_response("Asset evicted or not found."),
    }
}


SIGNED_THUMBNAIL_OPENAPI: JsonObjectSchema = {
    "responses": {
        "200": {
            "description": (
                "A WebP preview of the asset, 480 px on the long side at most. Auth is the "
                "signed `expires`+`sig` query string a thumbnail URL carries; a content URL "
                "signature does not open this route."
            ),
            "content": {"image/webp": {"schema": {"type": "string", "format": "binary"}}},
        },
        "304": {"description": "The preview has not changed since the `If-None-Match` ETag."},
        "400": _json_error_response("Invalid signed URL."),
        "403": _json_error_response("Signature expired or invalid."),
        "404": _json_error_response("No preview for this asset."),
    }
}


def _audio_generate_request_schema() -> JsonObjectSchema:
    return {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "model": {"type": "string", "description": "Audio generation workflow id."},
            "tags": {"type": "string"},
            "lyrics": {"type": "string"},
            "duration": {"type": "number"},
            "seed": {"type": "integer"},
            "response_format": {"type": "string"},
        },
        "additionalProperties": True,
    }


AUDIO_GENERATE_OPENAPI: JsonObjectSchema = {
    "requestBody": _json_request(_audio_generate_request_schema()),
    "responses": {
        "200": {
            "description": (
                "Either a JSON envelope with an asset/URL handle or raw audio bytes "
                "depending on the configured workflow."
            ),
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "url": {"type": "string", "format": "uri"},
                            "asset_id": {"type": "string"},
                            "format": {"type": "string"},
                        },
                        "additionalProperties": True,
                    }
                },
                "audio/wav": {"schema": {"type": "string", "format": "binary"}},
                "audio/mpeg": {"schema": {"type": "string", "format": "binary"}},
            },
        },
        **_common_error_responses(),
    },
}


AUDIO_MODELS_OPENAPI: JsonObjectSchema = {
    "responses": {
        "200": {
            "description": "Available audio generation workflow ids.",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["data"],
                        "properties": {
                            "data": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "owned_by": {"type": "string"},
                                        "permission": {"type": "string"},
                                        "max_seconds": {"type": "number"},
                                        "min_seconds": {"type": "number"},
                                    },
                                    "additionalProperties": True,
                                },
                            }
                        },
                        "additionalProperties": True,
                    }
                }
            },
        },
        **_common_error_responses(),
    }
}


def _video_create_request_schema() -> JsonObjectSchema:
    return {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "prompt": {"type": "string"},
            "model": {"type": "string"},
            "size": {"type": "string", "description": "e.g. '1280x720'."},
            "seconds": {"type": ["number", "string"]},
            "seed": {"type": "integer"},
            "input_reference": {
                "type": "string",
                "description": "Reference image/video as URL, path, data URI, or base64 string.",
            },
            # Reviewed public wire contract, independent of the control registry.
            "extensions": {
                "type": "object",
                "properties": {
                    "com.azizam.video": {
                        "type": "object",
                        "properties": {
                            "audio": {"type": "boolean", "default": True},
                        },
                        "additionalProperties": False,
                    },
                },
                "additionalProperties": True,
            },
        },
        "additionalProperties": True,
    }


def _video_create_multipart_request_schema() -> JsonObjectSchema:
    schema = _video_create_request_schema()
    # Nested extension controls are accepted only by the JSON create route.
    schema["properties"].pop("extensions")
    schema["properties"] = {
        **schema["properties"],
        "input_reference": {
            "type": "string",
            "format": "binary",
            "description": "Optional reference asset upload accepted by the OpenAI Python SDK.",
        },
    }
    return schema


def _video_status_schema() -> JsonObjectSchema:
    return {
        "type": "object",
        "required": ["id", "status"],
        "properties": {
            "id": {"type": "string"},
            "object": {"type": "string"},
            "status": {
                "type": "string",
                "enum": ["queued", "in_progress", "completed", "failed"],
            },
            "progress": {"type": "number"},
            "created_at": {"type": "integer"},
            "model": {"type": "string"},
            "size": {"type": "string"},
            "seconds": {"type": ["number", "string"]},
            "error": {"type": ["object", "string", "null"]},
        },
        "additionalProperties": True,
    }


VIDEOS_CREATE_OPENAPI: JsonObjectSchema = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {"schema": _video_create_request_schema()},
            "multipart/form-data": {"schema": _video_create_multipart_request_schema()},
        },
    },
    "responses": {
        "202": {
            "description": "Sora-shaped video job submitted.",
            "content": {"application/json": {"schema": _video_status_schema()}},
        },
        "200": {
            "description": "Sora-shaped video job (already complete or attached).",
            "content": {"application/json": {"schema": _video_status_schema()}},
        },
        **_common_error_responses(),
    },
}


VIDEOS_STATUS_OPENAPI: JsonObjectSchema = {
    "responses": {
        "200": {
            "description": "Current Sora-shaped status for a previously submitted job.",
            "content": {"application/json": {"schema": _video_status_schema()}},
        },
        **_common_error_responses(),
    }
}


VIDEOS_CONTENT_OPENAPI: JsonObjectSchema = {
    "responses": {
        "200": {
            "description": "Rendered video bytes (mp4) once status=completed.",
            "content": {
                "video/mp4": {"schema": {"type": "string", "format": "binary"}},
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                },
            },
        },
        **_common_error_responses(),
    }
}


def _video_list_response_schema() -> JsonObjectSchema:
    return {
        "type": "object",
        "required": ["object", "data"],
        "properties": {
            "object": {"type": "string", "enum": ["list"]},
            "data": {"type": "array", "items": _video_status_schema()},
            "first_id": {"type": ["string", "null"]},
            "last_id": {"type": ["string", "null"]},
            "has_more": {"type": "boolean"},
        },
        "additionalProperties": True,
    }


VIDEOS_LIST_OPENAPI: JsonObjectSchema = {
    "parameters": [
        {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100}},
        {"name": "after", "in": "query", "schema": {"type": "string"}},
        {"name": "order", "in": "query", "schema": {"type": "string", "enum": ["asc", "desc"]}},
    ],
    "responses": {
        "200": {
            "description": "Video jobs submitted via this route since the gateway process last restarted.",
            "content": {"application/json": {"schema": _video_list_response_schema()}},
        },
        **_common_error_responses(),
    },
}


VIDEOS_DELETE_OPENAPI: JsonObjectSchema = {
    "responses": {
        "200": _endpoint_fragment("DELETE", "/v1/videos/{video_id}")["responses"]["200"],
        **_common_error_responses(),
    },
}


VIDEO_DELETED_OBJECT = (
    VIDEOS_DELETE_OPENAPI["responses"]["200"]["content"]["application/json"]["schema"]
    ["properties"]["object"]["enum"][0]
)


VIDEOS_REMIX_OPENAPI: JsonObjectSchema = {
    "responses": {
        "200": {
            "description": (
                "New video job rendered from a completed source video. "
                "`remixed_from_video_id` names the source."
            ),
            "content": {"application/json": {"schema": _video_status_schema()}},
        },
        # 501 video_remix_not_configured when videos.remix_workflow is unset.
        **_common_error_responses(),
    },
}


VIDEOS_EDIT_OPENAPI: JsonObjectSchema = {
    "requestBody": _endpoint_fragment("POST", "/v1/videos/edits")["requestBody"],
    "responses": {
        "200": {
            "description": "New video job rendered from an uploaded or existing video.",
            "content": {"application/json": {"schema": _video_status_schema()}},
        },
        **_common_error_responses(),
    },
}


VIDEOS_EXTEND_OPENAPI: JsonObjectSchema = {
    "requestBody": _endpoint_fragment("POST", "/v1/videos/extensions")["requestBody"],
    "responses": {
        "200": {
            "description": "New video job: the source followed by the new footage. `seconds` is the stitched total.",
            "content": {"application/json": {"schema": _video_status_schema()}},
        },
        **_common_error_responses(),
    },
}
