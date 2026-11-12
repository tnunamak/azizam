"""The words the gateway core needs for a route, a kind or an intent, read from the rows that own them.

A route row (``registry/protocol_frontends.yml``) names the intent it serves and the intents its handler admits and
receipts under; a media job intent row (``registry/media_job_intents.yml``) names the modality of the route it
submits to. Core code that needs one of those words asks for it by the public path of the route, never by spelling
the word. A path no row owns raises, so a missing row stops the work instead of running it unlabelled.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

from gateway import capability_ids, media_job_intents
from gateway.mesh_manifest import MESH_MEDIA_TYPE

FRONTENDS_PATH = Path(__file__).resolve().parent.parent / "registry" / "protocol_frontends.yml"


@functools.lru_cache(maxsize=1)
def _route_items() -> dict[str, dict[str, Any]]:
    """Every registered route item of the shipped rows, by each public path it is served at."""
    document = yaml.safe_load(FRONTENDS_PATH.read_text(encoding="utf-8"))
    items: dict[str, dict[str, Any]] = {}
    for record in document.get("records") or ():
        for item in record.get("registered_routes") or ():
            for path in (item.get("canonical"), item.get("alias")):
                if path:
                    items.setdefault(str(path), item)
    return items


def _item(path: str) -> dict[str, Any]:
    try:
        return _route_items()[path]
    except KeyError:
        raise LookupError(f"no row in {FRONTENDS_PATH.name} serves {path}") from None


def route_intent(path: str) -> str:
    """The intent the row that serves ``path`` declares (its wire operation, such as the speech or translation route's)."""
    intent = _item(path).get("intent")
    if not intent:
        raise LookupError(f"the row serving {path} declares no intent")
    return str(intent)


def route_execution_intent(path: str, role: str) -> str:
    """The intent the row serving ``path`` declares for ``role`` (``admission`` or ``receipt``)."""
    value = (_item(path).get("execution_intents") or {}).get(role)
    if not value:
        raise LookupError(f"the row serving {path} declares no {role} intent")
    return str(value)


def route_modality(path: str) -> str:
    """The modality (and kind, and resource class) of the media job intents submitted to ``path``."""
    for row in media_job_intents.ROWS:
        if row.route == path:
            return row.modality
    raise LookupError(f"no row in {media_job_intents.DATA_PATH.name} submits to {path}")


def route_submit_intent(path: str) -> str:
    """The first intent the media job rows submit to ``path``, in the published order."""
    for row in media_job_intents.ROWS:
        if row.route == path:
            return row.intent
    raise LookupError(f"no row in {media_job_intents.DATA_PATH.name} submits to {path}")


# The modality words, which are also the stored kind words and the resource classes, named by the route that submits them.
IMAGE = route_modality("/v1/images/generations")
VIDEO = route_modality("/v1/videos")
AUDIO = route_modality("/v1/audio/generate")

# The 3D modality: the one the media type of a finished 3D model belongs to (registry/capability_words.yml).
MESH = capability_ids.load_media_type_modalities()[MESH_MEDIA_TYPE]

# The intents of the image, video and audio routes, and the prefixes their families share.
IMAGE_GENERATE = route_execution_intent("/v1/images/generations", "admission")
IMAGE_EDIT = route_execution_intent("/v1/images/edits", "admission")
VIDEO_GENERATE = route_submit_intent("/v1/videos")
AUDIO_GENERATE = route_execution_intent("/v1/audio/generate", "admission")
AUDIO_TRANSLATIONS = route_intent("/v1/audio/translations")
IMAGE_PREFIX = f"{IMAGE}."
VIDEO_PREFIX = f"{VIDEO}."
AUDIO_PREFIX = f"{AUDIO}."


@functools.lru_cache(maxsize=1)
def _frontend_rows() -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(FRONTENDS_PATH.read_text(encoding="utf-8"))
    return {str(row["id"]): row for row in document.get("frontends") or ()}


def result_header(frontend_id: str, key: str) -> str:
    """The response header a frontend row names for the result fact ``key`` (``result_headers`` of the row)."""
    try:
        return str(_frontend_rows()[frontend_id]["result_headers"][key])
    except KeyError:
        raise LookupError(f"row {frontend_id} in {FRONTENDS_PATH.name} names no result header {key}") from None


def result_receipt_name(frontend_id: str, key: str) -> str:
    """The receipt metadata name a frontend row gives the result fact ``key`` (``result_receipt`` of the row)."""
    try:
        return str(_frontend_rows()[frontend_id]["result_receipt"][key])
    except KeyError:
        raise LookupError(f"row {frontend_id} in {FRONTENDS_PATH.name} names no receipt name for {key}") from None
