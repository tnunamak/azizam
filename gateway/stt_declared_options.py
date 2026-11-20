"""What a transcription record says it can do, and the refusals and listings that follow from it.

A transcription engine either honours a request option or it does not. Before this module the gateway accepted every
option and the engine dropped the ones it had no input for: a ``prompt`` changed nothing, and ``verbose_json`` came back
as plain text with timing. A record now declares both in its ``chunking`` block, the block the STT execution settings
already come from:

``response_formats``
    The ``response_format`` values the engine produces, as a list (``[json, text]``). Absent: not declared, nothing is
    refused for the format.
``prompt_supported``
    ``false`` when the engine has no prompt input. Absent: not declared, a prompt is passed through as before.

With a declaration, the route refuses at request time what the alias cannot do (HTTP 400, ``invalid_request_error``,
``param`` naming the field, as OpenAI does for a format its model cannot produce). Without one, the route is unchanged
except for one backstop: a ``verbose_json`` reply that has no ``segments`` is an error naming that, never the bare reply.
The capabilities listing publishes the declaration, the one-piece audio maximum and the formats cut for long audio.

No engine or alias name is read here. Every value comes from the record.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from gateway import stt_chunking

# What a caller may ask a transcription route for when a record declares no list of its own.
OPENAI_RESPONSE_FORMATS = ("json", "text", "srt", "verbose_json", "vtt")

UNSUPPORTED_PARAMETER = "unsupported_parameter"
UNSUPPORTED_RESPONSE_FORMAT = "unsupported_response_format"
FORMAT_NOT_RETURNED = "stt_response_format_not_returned"

_NAME_LIMIT = 80


@dataclass(frozen=True)
class DeclaredOptions:
    """One record's declaration. ``None`` means the record declares nothing for that field."""

    response_formats: tuple[str, ...] | None = None
    prompt_supported: bool | None = None


@dataclass(frozen=True)
class Refusal:
    """A request the alias cannot serve, in the fields of an OpenAI error."""

    message: str
    param: str
    code: str


def declared_options(chunking: Mapping[str, Any] | None) -> DeclaredOptions:
    """Read the declaration from a record's ``chunking`` block; a malformed one is a configuration error."""
    if not chunking:
        return DeclaredOptions()
    formats = chunking.get("response_formats")
    if formats is not None:
        if (isinstance(formats, (str, bytes)) or not isinstance(formats, Sequence) or not formats
                or not all(isinstance(item, str) and item.strip() for item in formats)):
            raise stt_chunking.SttConfigurationError("STT response_formats must be a non-empty list of names")
        formats = tuple(dict.fromkeys(item.strip().lower() for item in formats))
    prompt = chunking.get("prompt_supported")
    if prompt is not None and not isinstance(prompt, bool):
        raise stt_chunking.SttConfigurationError("STT prompt_supported must be true or false")
    return DeclaredOptions(response_formats=formats, prompt_supported=prompt)


def options_of(execution: stt_chunking.SttExecutionConfig) -> DeclaredOptions:
    return declared_options(execution.backend_config.get("chunking"))


def _named(model: str) -> str:
    name = " ".join(str(model or "").split())[:_NAME_LIMIT]
    return f"'{name}'" if name else "this model"


def refuse(options: DeclaredOptions, fields: Sequence[tuple[str, str]], *, model: str) -> Refusal | None:
    """The refusal for the first option the alias cannot honour, or None. An empty prompt asks for nothing."""
    if options.prompt_supported is False and any(name == "prompt" and value.strip() for name, value in fields):
        return Refusal(
            f"Model {_named(model)} does not accept a prompt: its engine has no prompt input and would ignore it. "
            "Send the request without a prompt.",
            "prompt", UNSUPPORTED_PARAMETER)
    if options.response_formats is not None:
        for name, value in fields:
            requested = value.strip().lower()
            if name == "response_format" and requested not in options.response_formats:
                return Refusal(
                    f"Model {_named(model)} cannot return response_format '{requested[:_NAME_LIMIT]}'. "
                    f"It returns: {', '.join(options.response_formats)}.",
                    "response_format", UNSUPPORTED_RESPONSE_FORMAT)
    return None


def verbose_json_missing_segments(fields: Sequence[tuple[str, str]], payload: Any) -> bool:
    """True when ``verbose_json`` was asked for and the engine's reply is not a JSON object with a ``segments`` list.

    An empty ``segments`` list is an answer (silence has no segments); a missing key, a ``segments`` value that is not
    a list, or a reply that is not an object is a different response shape.
    """
    asked = any(name == "response_format" and value.strip().lower() == "verbose_json" for name, value in fields)
    return asked and not (isinstance(payload, Mapping) and isinstance(payload.get("segments"), list))


def format_not_returned_message(model: str) -> str:
    return (f"The transcription engine behind model {_named(model)} did not return verbose_json: its reply has no "
            "segments. Ask for another response_format, or use a model that returns verbose_json.")


def capability_limits(execution: stt_chunking.SttExecutionConfig | None) -> dict[str, Any]:
    """The transcription facts a capabilities row publishes for one record (``execution`` is None when it has none).

    ``max_audio_seconds`` is the record's declared one-piece maximum when it has no native long audio, else None.
    ``split_response_formats`` asks ``stt_chunking.request_supports_chunking`` per format, so it follows that rule.
    """
    if execution is None:
        return {"response_formats": None, "prompt_supported": None, "max_audio_seconds": None, "split_response_formats": []}
    try:
        options = options_of(execution)
    except stt_chunking.SttConfigurationError:
        options = DeclaredOptions()
    maximum = execution.max_segment_seconds if execution.max_segment_declared and not execution.native_long_audio_offline else None
    if maximum is not None and not (math.isfinite(maximum) and maximum > 0):
        maximum = None
    candidates = options.response_formats if options.response_formats is not None else OPENAI_RESPONSE_FORMATS
    split = [
        name for name in candidates
        if execution.chunking_enabled and stt_chunking.request_supports_chunking([("response_format", name)], file_count=1)
    ]
    return {
        "response_formats": list(options.response_formats) if options.response_formats is not None else None,
        "prompt_supported": options.prompt_supported,
        "max_audio_seconds": maximum,
        "split_response_formats": split,
    }
