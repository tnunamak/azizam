"""Speech engines derived from service records.

A speech engine an owner adds is a service record: its address, its enabled flag and the models it offers
(``config.models``) already live there. The speech route looks a model up in ``model_aliases.speech.engines``, so the
effective config gets one engine entry for each model of each enabled record that declares the speech capability.
The record is the one home of the engine; nothing is stored twice.

An entry the owner wrote by hand in ``model_aliases.speech.engines`` wins when it has the same id: the two are never
merged field by field. A derived entry never sets ``custom_voices`` (a cloned voice needs a server that accepts a
reference clip, which a record does not say), so a derived engine accepts no custom voice.

Pure: no config loading, no network.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from gateway import domain_rows

# The capability word the speech route selects its service by (the same word ``proxy._SPEECH_CAPABILITY`` names).
SPEECH_CAPABILITY = domain_rows.route_intent("/v1/audio/speech")


def _norm(name: Any) -> str:
    return str(name or "").strip().lower()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _enabled(record: Mapping[str, Any]) -> bool:
    value = record.get("enabled")
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "0", "no", "off"}
    return True if value is None else bool(value)


def _declares_speech(record: Mapping[str, Any]) -> bool:
    """Whether the record declares the speech capability by the rule ``proxy.backends_for_capability`` selects by: the
    compound word itself, or each of its dot-separated parts (``["audio", "speech"]`` for ``audio.speech``)."""
    capabilities = record.get("capabilities")
    words = {_norm(word) for word in (capabilities if isinstance(capabilities, list) else [])}
    return SPEECH_CAPABILITY in words or all(part in words for part in SPEECH_CAPABILITY.split("."))


def _stated(record: Mapping[str, Any], key: str) -> Any:
    """What the record's config states for ``key``, else what it has observed; None when neither does."""
    for source in (_mapping(record.get("config")), _mapping(record.get("observations"))):
        value = source.get(key)
        if value:
            return value
    return None


def derive_speech_engines(
    services: Mapping[str, Any] | None, *, only: set[str] | None = None, include_disabled: bool = False,
) -> dict[str, dict[str, Any]]:
    """``{engine id: {service, model, voices?, default_voice?}}`` for the enabled speech records in ``services``.

    The engine id is the model's public name (``model_group``, else ``id``), the entry's ``model`` is the name the
    server is called with (``upstream_model``, else the public name). A record with no model row gives no entry. When
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
        if not _declares_speech(record):
            continue
        rows = _mapping(record.get("config")).get("models")
        for row in rows if isinstance(rows, list) else []:
            row = _mapping(row)
            public = str(row.get("model_group") or row.get("id") or "").strip()
            upstream = str(row.get("upstream_model") or "").strip() or public
            if not public or _norm(public) in seen:
                continue
            seen.add(_norm(public))
            entry: dict[str, Any] = {"service": service_id, "model": upstream}
            voices = _stated(record, "voices")
            if isinstance(voices, list):
                entry["voices"] = copy.deepcopy(voices)
            default = _stated(record, "default_voice")
            if default:
                entry["default_voice"] = str(default).strip()
            out[public] = entry
    return out


def with_derived_speech_engines(
    model_aliases: Mapping[str, Any] | None, derived: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """A copy of ``model_aliases`` whose ``speech.engines`` also holds the ``derived`` engines.

    A hand-written entry with the same (case-folded) id wins whole. Nothing changes when ``derived`` is empty.
    """
    out = copy.deepcopy(dict(_mapping(model_aliases)))
    if not derived:
        return out
    speech = dict(_mapping(out.get("speech")))
    hand = dict(_mapping(speech.get("engines")))
    taken = {_norm(key) for key in hand}
    speech["engines"] = {**{key: spec for key, spec in derived.items() if _norm(key) not in taken}, **hand}
    out["speech"] = speech
    return out
