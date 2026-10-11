"""A speech engine's voices, from its data, without guessing engine identities.

An engine record may declare ``voices`` (the complete list; an id or ``{id, name, ...}``),
``voice_map`` (accepted spelling -> voice id) and ``default_voice``. The speech section may
declare ``voice_aliases`` (older spelling -> the spelling it means), applied before the map.
"""

from collections.abc import Mapping
from typing import Any

from gateway.product_contracts import SpeechVoiceCatalog


def voice_records(spec: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The engine's ``voices`` as records with an ``id``; an empty list when it declares none.

    A record without an ``id`` names no voice a request could select, so it is left out.
    """
    records = []
    for item in spec.get("voices") or ():
        record = dict(item) if isinstance(item, Mapping) else {"id": item}
        if record.get("id") not in (None, ""):
            records.append({**record, "id": str(record["id"])})
    return records


def resolve_voice(spec: Mapping[str, Any], aliases: Mapping[str, str], requested: str) -> str | None:
    """The engine voice for a requested voice (case-insensitive), or None when the engine has no such voice.

    An exact voice id wins, then an alias, then the engine's ``voice_map``. No voice selects the
    default. An unknown voice is None on an engine with a ``voices`` list and is sent unchanged
    (lower case) to an engine without one.
    """
    voice_map = {str(k).lower(): str(v) for k, v in (spec.get("voice_map") or {}).items()}
    ids = [record["id"] for record in voice_records(spec)]
    native = {v.lower(): v for v in [*voice_map.values(), *ids]}
    key = requested.lower()
    if key in native:
        return native[key]
    key = aliases.get(key, key)
    if key in voice_map:
        return voice_map[key]
    if key in native:
        return native[key]
    if not key:
        return str(spec.get("default_voice") or "")
    return None if ids else key


def speech_voice_catalog(spec: Mapping[str, Any]) -> SpeechVoiceCatalog:
    """Picker choices: exactly the engine's voices, each with its name (or its id as written)."""
    records = voice_records(spec)
    default = spec.get("default_voice")
    return {
        "voices": [{"id": record["id"], "name": str(record.get("name") or record["id"])} for record in records],
        "default_voice": str(default) if default else None,
        "allow_other": not records,
    }
