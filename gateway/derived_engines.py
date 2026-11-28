"""Speech and transcription engines derived from service records.

An engine an owner adds is a service record: its address, its enabled flag and the models it offers
(``config.models``) already live there. The speech route looks a model up in ``model_aliases.speech.engines`` and the
transcription route in ``model_aliases.transcriptions.engines``, so the effective config gets one engine entry for
each model of each enabled record that declares the capability of that route. The record is the one home of the
engine; nothing is stored twice. One rule serves both capabilities (``derive_engines``); speech adds its voice list.

An entry the owner wrote by hand in the section's ``engines`` wins when it has the same id: the two are never
merged field by field. A name the owner's section resolves by a stable name or an OpenAI pattern is the owner's too.
A derived speech entry never sets ``custom_voices`` (a cloned voice needs a server that accepts a reference clip,
which a record does not say), so a derived engine accepts no custom voice. A derived transcription entry carries
only ``service`` and ``model``.

Pure: no config loading, no network.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from gateway import domain_rows, stable_aliases

# The capability word the speech route selects its service by (the same word ``proxy._SPEECH_CAPABILITY`` names).
SPEECH_CAPABILITY = domain_rows.route_intent("/v1/audio/speech")
# The same for the transcription route (``proxy._TRANSCRIPTION_CAPABILITY``).
TRANSCRIPTION_CAPABILITY = domain_rows.route_intent("/v1/audio/transcriptions")


def _norm(name: Any) -> str:
    return str(name or "").strip().lower()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _enabled(record: Mapping[str, Any]) -> bool:
    value = record.get("enabled")
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off"}
    return True if value is None else bool(value)


def _declares(record: Mapping[str, Any], capability: str) -> bool:
    """Whether the record declares ``capability`` by the rule ``proxy.backends_for_capability`` selects by: the
    compound word itself, or each of its dot-separated parts (``["audio", "speech"]`` for ``audio.speech``)."""
    capabilities = record.get("capabilities")
    words = {_norm(word) for word in (capabilities if isinstance(capabilities, list) else [])}
    return capability in words or all(part in words for part in capability.split("."))


def _stated(record: Mapping[str, Any], key: str) -> Any:
    """What the record's config states for ``key``, else what it has observed; None when neither does."""
    for source in (_mapping(record.get("config")), _mapping(record.get("observations"))):
        value = source.get(key)
        if value:
            return value
    return None


def derive_engines(
    services: Mapping[str, Any] | None, capability: str, *, only: set[str] | None = None, include_disabled: bool = False,
) -> dict[str, dict[str, Any]]:
    """``{engine id: {service, model}}`` for the enabled records in ``services`` that declare ``capability``.

    The engine id is the model's public name (``model_group``, else ``id``), the entry's ``model`` is the name the
    server is called with (``upstream_model``, else the public name). A record with no model row gives no entry, nor does a wildcard ("*") row. When
    two records offer the same public name the lower service id keeps it. ``only`` limits the records to those ids.
    A disabled record is left out unless ``include_disabled`` (the validator asks for the names a record defines).
    """
    out: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    records = _mapping(services)
    for service_id in sorted(str(key) for key in records):
        record = _mapping(records[service_id])
        if (only is not None and service_id not in only) or not (include_disabled or _enabled(record)):
            continue
        if not _declares(record, capability):
            continue
        rows = _mapping(record.get("config")).get("models")
        for row in rows if isinstance(rows, list) else []:
            row = _mapping(row)
            if row.get("execution_only") is True:
                continue
            public = str(row.get("model_group") or row.get("id") or "").strip()
            upstream = str(row.get("upstream_model") or "").strip() or public
            # A "*" row exposes whatever the server serves (service_form_to_raw); a request names one model.
            if not public or "*" in public or "*" in upstream or _norm(public) in seen:
                continue
            seen.add(_norm(public))
            out[public] = {"service": service_id, "model": upstream}
    return out


def derive_speech_engines(
    services: Mapping[str, Any] | None, *, only: set[str] | None = None, include_disabled: bool = False,
) -> dict[str, dict[str, Any]]:
    """``{engine id: {service, model, voices?, default_voice?}}`` for the enabled speech records in ``services``
    (the rule of ``derive_engines``, plus the record's voice list and default voice)."""
    out = derive_engines(services, SPEECH_CAPABILITY, only=only, include_disabled=include_disabled)
    records = _mapping(services)
    for entry in out.values():
        record = _mapping(records.get(entry["service"]))
        voices = _stated(record, "voices")
        if isinstance(voices, list):
            entry["voices"] = copy.deepcopy(voices)
        default = _stated(record, "default_voice")
        if default:
            entry["default_voice"] = str(default).strip()
    return out


def derive_transcription_engines(
    services: Mapping[str, Any] | None, *, only: set[str] | None = None, include_disabled: bool = False,
) -> dict[str, dict[str, Any]]:
    """``{engine id: {service, model}}`` for the enabled transcription records in ``services``."""
    return derive_engines(services, TRANSCRIPTION_CAPABILITY, only=only, include_disabled=include_disabled)


def with_derived_engines(
    model_aliases: Mapping[str, Any] | None, section: str, derived: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """A copy of ``model_aliases`` whose ``section`` (``speech`` or ``transcriptions``) ``engines`` also hold ``derived``.

    A hand-written entry with the same (case-folded) id wins whole, and so does a name the owner's own section
    already resolves (a stable name, or a match of one of its OpenAI patterns): a derived id that equals such a name
    is left out, because the engines table is read before the patterns and would take the name over. Nothing changes
    when ``derived`` is empty.
    """
    out = copy.deepcopy(dict(_mapping(model_aliases)))
    if not derived:
        return out
    target = dict(_mapping(out.get(section)))
    hand = dict(_mapping(target.get("engines")))
    taken = {_norm(key) for key in hand}
    free = {key: spec for key, spec in derived.items()
            if _norm(key) not in taken and not stable_aliases.owner_resolves_name(out, section, key)}
    target["engines"] = {**free, **hand}
    out[section] = target
    return out


def with_derived_speech_engines(
    model_aliases: Mapping[str, Any] | None, derived: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """``with_derived_engines`` for the ``speech`` section."""
    return with_derived_engines(model_aliases, "speech", derived)


def with_derived_transcription_engines(
    model_aliases: Mapping[str, Any] | None, derived: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """``with_derived_engines`` for the ``transcriptions`` section."""
    return with_derived_engines(model_aliases, "transcriptions", derived)
