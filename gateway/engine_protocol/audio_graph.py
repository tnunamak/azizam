"""The public ``/v1/audio/generate`` body, read as a workflow manifest's typed inputs (the route edge, D1).

This module is the only place that knows the audio route's legacy spellings. It turns the public body into the inputs the
manifest declares, and the node-graph adapter compiles those inputs (``node_graph.compile_workflow``). The adapter names no
intent and reads no alias.

What it does, in the order the route checks them:

- refuses unknown top-level fields and names the first one; refuses a non-string text field; checks ``type``,
  ``response_format`` and ``seed`` (the same messages and ``param`` values as the route);
- ``prompt`` falls back to ``input`` then ``tags``; a request with no text is refused;
- routes the text: the prompt goes to the manifest's ``prompt`` input; ``tags`` goes to its ``tags`` input when the request
  names tags. Each is placed on its own, so a workflow whose prompt and tags reach different nodes gets both. When the
  manifest declares no ``prompt``, a request without tags sends its prompt to the ``tags`` input. ``lyrics`` and ``tags`` reach only manifests that declare them, as the route has always ignored them
  elsewhere;
- reads ``duration`` against the **effective** manifest (``effective_manifest``): the manifest's bounds, narrowed by the
  workflow's configured limits (the proxy's ``audio.workflows.<name>``). The compiler reads the same effective manifest,
  so a direct compile and this route refuse the same duration by the same name;
- **the unset-seed rule**: when the manifest declares ``seed`` and the request names none, a random seed inside the
  declared bounds is drawn here, once per request, before the job is saved. A resumed job therefore keeps the seed it was given;
- merges the ``extensions.com.azizam.workflow`` block under the extension rules (``audio_extensions``), refusing the names
  the standard fields own.

Errors are ``InvalidAudioRequest`` with the request field that is wrong (``param``), so a caller can answer them.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Any, Mapping

from gateway.audio_extensions import detect_unknown_audio_fields, validate_audio_workflow_extensions
from gateway.engine_protocol.errors import ProtocolError
from gateway.extensions import ExtensionError
from gateway.manifests import ManifestError, apply_deployment_inputs, validate_input_value

AUDIO_TYPES = ("music", "sfx", "ambient")
TEXT_FIELDS = ("prompt", "input", "tags", "lyrics")
SEED_LIMIT = 2**32 - 1
# The public route's default duration when neither the deployment nor the manifest names one.
ROUTE_DEFAULT_DURATION = 30


class InvalidAudioRequest(ProtocolError):
    """The request cannot be read. ``param`` names the field that is wrong."""

    code = "invalid_request"

    def __init__(self, message: str, *, param: str | None = None) -> None:
        super().__init__(message)
        self.param = param


@dataclass(frozen=True)
class AudioLimits:
    """Per-workflow duration policy (the proxy's ``audio.workflows.<name>`` settings). ``None`` means the deployment sets
    nothing, so the manifest's own default or bound stands."""

    default_duration: float | None = None
    min_seconds: float | None = None
    max_seconds: float | None = None


def effective_manifest(manifest: Mapping[str, Any], limits: AudioLimits) -> dict[str, Any]:
    """The one effective input spec of an audio workflow: the manifest with the deployment's duration policy applied.

    The deployment may narrow the manifest's duration bounds and choose the default; it may not widen them (see
    ``manifests.apply_deployment_inputs``, which raises at load). With no default from either side, the route's default
    applies. Both the route edge (``manifest_inputs``) and the compiler read the result, so they agree.
    """
    narrowing: dict[str, Any] = {}
    if "duration" in manifest["inputs"]:
        declared = manifest["inputs"]["duration"]
        for key, value in (("minimum", limits.min_seconds), ("maximum", limits.max_seconds),
                           ("default", limits.default_duration)):
            if value is not None:
                narrowing[key] = value
        if "default" not in narrowing and "default" not in declared:
            narrowing["default"] = ROUTE_DEFAULT_DURATION
    return apply_deployment_inputs(dict(manifest), {"duration": narrowing} if narrowing else {})


def manifest_inputs(body: Mapping[str, Any], *, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The typed inputs the effective manifest declares, read from one public audio request. ``body`` is not modified.

    ``manifest`` must be the ``effective_manifest`` of the workflow."""
    declared = manifest["inputs"]
    text, prompt = _read_request(body)
    duration = _duration(body, declared) if "duration" in declared else None

    inputs: dict[str, Any] = {}
    _place_text(inputs, declared, prompt=prompt, tags=text["tags"], lyrics=text["lyrics"])
    if duration is not None:
        inputs["duration"] = duration
    if "seed" in declared:
        inputs["seed"] = body["seed"] if "seed" in body else _draw_seed(declared["seed"])
    try:
        validation = validate_audio_workflow_extensions(body, manifest)
    except (ExtensionError, ManifestError) as error:
        raise InvalidAudioRequest(str(error), param="extensions") from error
    if validation.rejected:
        name, reason = next(iter(validation.rejected.items()))
        raise InvalidAudioRequest(reason, param=f"extensions.{validation.namespace}.{name}")
    inputs.update(validation.accepted)
    return inputs


def check_request(body: Mapping[str, Any]) -> None:
    """Refuse a request that no workflow could read: the checks that need no manifest, in the route's order.

    The route runs this before it resolves the model, so a bad body is refused the same way whichever model it names.
    ``manifest_inputs`` runs the same checks."""
    _read_request(body)


def _read_request(body: Mapping[str, Any]) -> tuple[dict[str, str | None], str]:
    unknown = detect_unknown_audio_fields(body)
    if unknown:
        name, reason = next(iter(unknown.items()))
        raise InvalidAudioRequest(reason, param=name)
    text = _text_fields(body)
    prompt = text["prompt"] or text["input"] or text["tags"] or ""
    _check_type(body)
    _check_response_format(body)
    _check_seed(body)
    if not prompt:
        raise InvalidAudioRequest("prompt, input, or tags is required")
    return text, prompt


def _text_fields(body: Mapping[str, Any]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for name in TEXT_FIELDS:
        value = body.get(name)
        if value is None:
            out[name] = None
            continue
        if not isinstance(value, str):
            raise InvalidAudioRequest(f"{name} must be a string", param=name)
        out[name] = value.strip()
    return out


def _place_text(inputs: dict[str, Any], declared: Mapping[str, Any], *, prompt: str, tags: str | None,
                lyrics: str | None) -> None:
    """Route the text to the manifest's inputs. See the module docstring for the rule."""
    if "prompt" in declared:
        inputs["prompt"] = prompt
    if tags is not None and "tags" in declared:
        inputs["tags"] = tags  # set after prompt: where both map to one node, the tags win, as they always have
    elif "prompt" not in declared and "tags" in declared:
        inputs["tags"] = prompt
    if lyrics is not None and "lyrics" in declared:
        inputs["lyrics"] = lyrics


def _draw_seed(spec: Mapping[str, Any]) -> int:
    """A random seed inside both the public range and the manifest's declared bounds.

    The bounds may be fractional, so the integers inside them run from the ceiling of the minimum to the floor of the
    maximum. When no integer is inside both ranges the manifest is at fault, not the request: ``ManifestError``."""
    low = max(0, math.ceil(spec.get("minimum", 0)))
    high = min(SEED_LIMIT, math.floor(spec.get("maximum", SEED_LIMIT)))
    if low > high:
        raise ManifestError("seed bounds leave no integer inside the public seed range")
    return random.randint(low, high)


def _check_type(body: Mapping[str, Any]) -> None:
    audio_type = body.get("type", "music")
    if not isinstance(audio_type, str) or audio_type not in AUDIO_TYPES:
        raise InvalidAudioRequest("type must be music, sfx, or ambient", param="type")


def _check_response_format(body: Mapping[str, Any]) -> None:
    if "response_format" in body and body["response_format"] != "native":
        raise InvalidAudioRequest("response_format must be 'native'; audio generation does not convert encodings",
                                  param="response_format")


def _check_seed(body: Mapping[str, Any]) -> None:
    if "seed" not in body:
        return
    seed = body["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= SEED_LIMIT:
        raise InvalidAudioRequest(f"seed must be an integer between 0 and {SEED_LIMIT}", param="seed")


def _duration(body: Mapping[str, Any], declared: Mapping[str, Any]) -> float:
    """The duration the request names, or the effective default. Checked against the effective spec, by the same rule
    the compiler uses, so the refusal names the same input and the same bound."""
    spec = declared["duration"]
    raw = body.get("duration")
    if raw is None:
        raw = spec["default"]
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise InvalidAudioRequest("duration must be a finite number", param="duration") from None
    reason = validate_input_value("duration", value, spec)
    if reason is not None:
        raise InvalidAudioRequest(reason, param="duration")
    return value


__all__ = ["AUDIO_TYPES", "AudioLimits", "InvalidAudioRequest", "check_request", "effective_manifest", "manifest_inputs"]
