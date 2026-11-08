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
- **generated inputs**: an input the manifest declares with a ``generate`` rule takes the request's value of the same
  name; when the request names none, ``resolve_generated`` fills it (``gateway/generated_defaults.py``). A job draws it
  once when it is accepted and keeps it in its envelope, so a resumed job passes the stored block as ``stored`` and
  reads it. The synchronous route has no envelope and draws on each request;
- merges the ``extensions.com.azizam.workflow`` block under the extension rules (``audio_extensions``), refusing the names
  the standard fields own.

Errors are ``InvalidAudioRequest`` with the request field that is wrong (``param``), so a caller can answer them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from gateway import generated_defaults
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


def generated_block(body: Mapping[str, Any], *, manifest: Mapping[str, Any],
                    stored: Mapping[str, Mapping[str, Any]] | None = None,
                    source: generated_defaults.Source | None = None) -> dict[str, dict[str, Any]]:
    """The envelope ``generated`` block for one request: the generated inputs the request left empty, each filled once.

    ``manifest`` must be the ``effective_manifest`` of the workflow. ``stored`` is a block a job already kept; its values
    are read back, not drawn again."""
    declared = manifest["inputs"]
    given = {name: body[name] for name in generated_defaults.generated_inputs(declared) if name in body}
    return generated_defaults.resolve_generated(declared, given, stored=stored, source=source)


def manifest_inputs(body: Mapping[str, Any], *, manifest: Mapping[str, Any],
                    stored: Mapping[str, Mapping[str, Any]] | None = None,
                    source: generated_defaults.Source | None = None) -> dict[str, Any]:
    """The typed inputs the effective manifest declares, read from one public audio request. ``body`` is not modified.

    ``manifest`` must be the ``effective_manifest`` of the workflow. ``stored`` and ``source`` are for generated inputs
    (``generated_block``)."""
    declared = manifest["inputs"]
    text, prompt = _read_request(body)
    duration = _duration(body, declared) if "duration" in declared else None

    inputs: dict[str, Any] = {}
    _place_text(inputs, declared, prompt=prompt, tags=text["tags"], lyrics=text["lyrics"])
    if duration is not None:
        inputs["duration"] = duration
    for name, spec in generated_defaults.generated_inputs(declared).items():
        if name in body and generated_defaults.caller_may_set(spec):
            inputs[name] = body[name]
    inputs.update(generated_defaults.values(generated_block(body, manifest=manifest, stored=stored, source=source)))
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
