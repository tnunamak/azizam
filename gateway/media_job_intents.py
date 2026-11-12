"""The media job intents and their modalities, read from ``registry/media_job_intents.yml``.

One ordered table answers every "which intents, which modality" question: the submit schema and its prose, the
unsupported-intent error, the audio dispatch set and the resource class of a job. The per-modality handlers stay
code; the rows only choose which one runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from gateway.capability_ids import AUDIO_RULE

_ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = _ROOT / "registry" / "media_job_intents.yml"
SCHEMA_PATH = _ROOT / "registry" / "media_job_intents.schema.json"


@dataclass(frozen=True)
class IntentRow:
    intent: str
    modality: str
    submittable: bool
    route: str | None = None
    component: str | None = None


def parse_rows(document: Any) -> tuple[IntentRow, ...]:
    """Validate a document against the schema and return its rows; a bad document raises ValueError."""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    try:
        jsonschema.Draft202012Validator(schema).validate(document)
    except jsonschema.ValidationError as exc:
        raise ValueError(f"{DATA_PATH.name}: {exc.message}") from exc
    rows = tuple(IntentRow(r["intent"], r["modality"], r["submittable"], r.get("route"), r.get("component"))
                 for r in document["intents"])
    names = [r.intent for r in rows]
    for name in names:
        if names.count(name) > 1:
            raise ValueError(f"{DATA_PATH.name}: duplicate intent {name!r}")
    submittable = [r for r in rows if r.submittable]
    if not submittable:
        raise ValueError(f"{DATA_PATH.name}: no submittable intent; the OpenAPI request body needs at least one")
    # The OpenAPI module keys request bodies by component, so one component must name one route and one route one
    # component; otherwise one body would silently overwrite another.
    route_of: dict[str, str] = {}
    component_of: dict[str, str] = {}
    for r in submittable:
        if route_of.setdefault(r.component, r.route) != r.route:
            raise ValueError(f"{DATA_PATH.name}: component {r.component} names two routes")
        if component_of.setdefault(r.route, r.component) != r.component:
            raise ValueError(f"{DATA_PATH.name}: route {r.route} names two components")
    return rows


def load_rows(path: Path = DATA_PATH) -> tuple[IntentRow, ...]:
    return parse_rows(yaml.safe_load(path.read_text(encoding="utf-8")))


ROWS: tuple[IntentRow, ...] = load_rows()

SUBMITTABLE_INTENTS: tuple[str, ...] = tuple(r.intent for r in ROWS if r.submittable)
MODALITY_BY_INTENT: dict[str, str] = {r.intent: r.modality for r in ROWS}
AUDIO_INTENTS: frozenset[str] = frozenset(r.intent for r in ROWS if r.modality == AUDIO_RULE["type"])


def intents_of(modality: str) -> frozenset[str]:
    return frozenset(r.intent for r in ROWS if r.modality == modality)


def submit_routes() -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """(component, route, intents) per sync route, in the order the routes first appear."""
    groups: dict[tuple[str, str], list[str]] = {}
    for r in ROWS:
        if r.submittable:
            groups.setdefault((r.component or "", r.route or ""), []).append(r.intent)
    return tuple((component, route, tuple(intents)) for (component, route), intents in groups.items())


def request_description() -> str:
    """The prose that names each sync route with its intents: ``A (x), B (y, z) or C (w).``"""
    parts = [f"`{route}` ({', '.join(intents)})" for _component, route, intents in submit_routes()]
    body = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} or {parts[-1]}"
    return f"The body you would send to the matching sync route: {body}."
