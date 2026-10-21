"""A speech engine's voices, from its data, without guessing engine identities.

An engine record may declare ``voices`` (the complete list; an id or ``{id, name, ...}``),
``voice_map`` (accepted spelling -> voice id) and ``default_voice``. The speech section may
declare ``voice_aliases`` (older spelling -> the spelling it means), applied before the map.

A model may be served by an ordered list of engines (a tier). A voice is available on the model
when some engine of it accepts the voice; the first such engine, in the configured order, serves it.
"""

from collections.abc import Mapping, Sequence
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


def _listed(specs: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Every listed voice of the engines, by lower-case id; the first engine's record of an id wins."""
    listed: dict[str, dict[str, Any]] = {}
    for spec in specs:
        for record in voice_records(spec):
            listed.setdefault(record["id"].lower(), record)
    return listed


def speech_voice_catalog(*specs: Mapping[str, Any]) -> SpeechVoiceCatalog:
    """Picker choices for a model served by ``specs`` in order: exactly the voices its engines list.

    Each choice shows its name (or its id as written); accepted spellings are not choices. The
    default is the first engine's. Another id is allowed when an engine has no voice list.
    """
    default = specs[0].get("default_voice") if specs else None
    return {
        "voices": [{"id": record["id"], "name": str(record.get("name") or record["id"])} for record in _listed(specs).values()],
        "default_voice": str(default) if default else None,
        "allow_other": any(not voice_records(spec) for spec in specs),
    }


def voice_listing(specs: Sequence[Mapping[str, Any]], aliases: Mapping[str, str]) -> list[dict[str, Any]]:
    """``/v1/audio/voices`` entries for a model served by ``specs``: every voice one of its engines accepts.

    First the listed voices in engine order, then the accepted spellings (an engine's ``voice_map``)
    that no engine lists, then the older spellings (``aliases``) that resolve to one of those.
    """
    listed = _listed(specs)
    entries = [
        {
            "voice_id": record["id"],
            # `id` is what Open WebUI reads ({v['id']: v['name']}); `voice_id` is kept for existing clients.
            "id": record["id"],
            "name": record.get("name") or record["id"],
            "language": record.get("language"),
            "gender": record.get("gender"),
            "description": record.get("description"),
            "mapped_to": record["id"],
            "alias": False,
        }
        for record in listed.values()
    ]
    spellings: dict[str, str] = {}
    for spec in specs:
        for name, native in (spec.get("voice_map") or {}).items():
            if str(name).lower() not in listed:
                spellings.setdefault(str(name).lower(), str(native))
    for name, native in spellings.items():
        record = listed.get(native.lower(), {})
        entries.append({
            "voice_id": name, "id": name, "name": name.replace("_", " ").title(),
            "language": record.get("language"), "gender": record.get("gender"),
            "description": f"Accepted spelling of '{native}'", "mapped_to": native, "alias": True,
        })
    for alias, target in sorted(aliases.items()):
        if alias in listed or alias in spellings or (target not in listed and target not in spellings):
            continue
        record = listed.get(target) or listed.get(spellings.get(target, "").lower(), {})
        entries.append({
            "voice_id": alias, "id": alias, "name": alias.replace("_", " ").title(),
            "language": record.get("language"), "gender": record.get("gender"),
            "description": f"Legacy alias of '{target}'", "mapped_to": target, "alias": True,
        })
    return entries
