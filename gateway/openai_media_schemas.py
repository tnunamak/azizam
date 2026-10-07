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

from typing import Any

JsonObjectSchema = dict[str, Any]


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
    return {
        "type": "object",
        "required": ["prompt"],
        "properties": {
            "image": {
                "type": "string",
                "format": "binary",
                "description": "Single image. Repeat the field name 'image[]' for multiple.",
            },
            "mask": {"type": "string", "format": "binary"},
            "prompt": {"type": "string"},
            "model": {"type": "string"},
            "n": {"type": "integer", "minimum": 1, "maximum": 10},
            "size": {"type": "string"},
            "response_format": {"type": "string", "enum": ["url", "b64_json"]},
            "user": {"type": "string"},
        },
        "additionalProperties": True,
    }


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
    "requestBody": _multipart_request(
        {
            "type": "object",
            "required": ["image"],
            "properties": {
                "image": {"type": "string", "format": "binary"},
                "model": {"type": "string"},
                "n": {"type": "integer", "minimum": 1, "maximum": 10},
                "size": {"type": "string"},
                "response_format": {"type": "string", "enum": ["url", "b64_json"]},
                "user": {"type": "string"},
            },
            "additionalProperties": True,
        }
    ),
    "responses": {
        # Routed through a configurable edit-capable workflow (no local
        # model dedicated to "variations" exists) with a fixed generic
        # prompt -- see proxy_comfy_variations_request in proxy.py.
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
        },
        "additionalProperties": True,
    }


def _video_create_multipart_request_schema() -> JsonObjectSchema:
    schema = _video_create_request_schema()
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
        "200": {
            "description": "Deletion confirmation.",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "required": ["id", "object", "deleted"],
                        "properties": {
                            "id": {"type": "string"},
                            "object": {"type": "string", "enum": ["video"]},
                            "deleted": {"type": "boolean"},
                        },
                    }
                }
            },
        },
        **_common_error_responses(),
    },
}


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
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["prompt", "video"],
                    "properties": {
                        "prompt": {"type": "string"},
                        "video": {"type": "string", "format": "binary"},
                    },
                    "additionalProperties": True,
                }
            },
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["prompt", "video"],
                    "properties": {
                        "prompt": {"type": "string"},
                        "video": {
                            "type": "object",
                            "required": ["id"],
                            "properties": {"id": {"type": "string"}},
                        },
                    },
                    "additionalProperties": True,
                }
            },
        },
    },
    "responses": {
        "200": {
            "description": "New video job rendered from an uploaded or existing video.",
            "content": {"application/json": {"schema": _video_status_schema()}},
        },
        **_common_error_responses(),
    },
}


VIDEOS_EXTEND_OPENAPI: JsonObjectSchema = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["prompt", "video"],
                    "properties": {
                        "prompt": {"type": "string", "description": "What happens in the new footage."},
                        "video": {
                            "type": "object",
                            "required": ["id"],
                            "properties": {"id": {"type": "string"}},
                        },
                        "seconds": {"type": ["string", "number"], "description": "Seconds to add."},
                        "context_seconds": {"type": "number", "description": "Overlap window taken from the source end (default 1)."},
                    },
                    "additionalProperties": True,
                }
            },
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["prompt", "video"],
                    "properties": {
                        "prompt": {"type": "string"},
                        "video": {"type": "string", "format": "binary"},
                    },
                    "additionalProperties": True,
                }
            },
        },
    },
    "responses": {
        "200": {
            "description": "New video job: the source followed by the new footage. `seconds` is the stitched total.",
            "content": {"application/json": {"schema": _video_status_schema()}},
        },
        **_common_error_responses(),
    },
}
