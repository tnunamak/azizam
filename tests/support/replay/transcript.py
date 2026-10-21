"""Transcript files: build one from a recording, and validate one on disk.

A transcript is a fake-upstream scenario whose ``provenance.source`` is
``transcript``; the fake replays it with strict matching (``matches_transcript``).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from tests.support.fake_upstream.protocols import PROTOCOLS
from tests.support.fake_upstream.scenario import SCENARIO_DIR, ScenarioError, scenario_from_data

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts" / "ci"))
import replay_scan  # noqa: E402

from . import allowlist  # noqa: E402

RECORDER_VERSION = "rp1/1"
REVIEW_WINDOW_DAYS = 180
PROVENANCE_KEYS = {"source", "reference", "protocol", "engine", "engine_version", "identity_source",
                   "recorded_on", "request_set_hash", "recorder_version", "owner", "review_by"}
UNVERIFIED = "UNVERIFIED"


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def set_hash(request_set: Any) -> str:
    return hashlib.sha256(canonical(request_set)).hexdigest()


def validate(data: dict[str, Any], today: dt.date | None = None) -> None:
    """Raise ValueError unless ``data`` is a complete, normalized, in-policy transcript."""
    today = today or dt.date.today()
    try:
        scenario_from_data(data)  # structure, protocol paths and body parts
    except ScenarioError as error:
        raise ValueError(str(error)) from error
    provenance = data["provenance"]
    if provenance.get("source") != "transcript" or set(provenance) != PROVENANCE_KEYS:
        raise ValueError(f"provenance needs exactly {sorted(PROVENANCE_KEYS)} with source 'transcript'")
    if any(not isinstance(provenance[key], str) or not provenance[key].strip() for key in PROVENANCE_KEYS):
        raise ValueError("every provenance field is a non-empty string (use UNVERIFIED when unknown)")
    if provenance["protocol"] != data["protocol"]:
        raise ValueError("provenance.protocol differs from the scenario protocol")
    if not re.fullmatch(r"[0-9a-f]{64}", provenance["request_set_hash"]):
        raise ValueError("request_set_hash must be a SHA-256 hex digest")
    recorded, review = dt.date.fromisoformat(provenance["recorded_on"]), dt.date.fromisoformat(provenance["review_by"])
    if recorded > today or not recorded <= review <= recorded + dt.timedelta(days=REVIEW_WINDOW_DAYS):
        raise ValueError("review_by must fall within 180 days after recorded_on")
    normalizer = allowlist.Normalizer(data["protocol"])
    for exchange in data["exchanges"]:
        _exchange(exchange, data["protocol"], normalizer)
    raw = canonical(data)
    if len(raw) > replay_scan.METADATA_LIMIT:
        raise ValueError("transcript metadata exceeds 256 KiB")
    found = replay_scan.categories(raw)
    if found:
        raise ValueError(f"transcript contains disallowed data: {sorted(found)}")


def _exchange(exchange: dict[str, Any], protocol: str, normalizer: allowlist.Normalizer) -> None:
    match, response = exchange["match"], exchange["response"]
    if set(match) != {"method", "path", "query", "headers", "json"}:
        raise ValueError("a transcript exchange matches on method, path, query, headers and json, all of them")
    if exchange.get("repeat") or exchange.get("times", 1) != 1 or exchange.get("required", True) is not True:
        raise ValueError("a transcript exchange is required exactly once")
    if set(match["headers"]) - set(allowlist.REQUEST_HEADERS[protocol]):
        raise ValueError("request header outside the allowlist")
    if set(response.get("headers", {})) - set(allowlist.RESPONSE_HEADERS):
        raise ValueError("response header outside the allowlist")
    if "json" in response and normalizer.response(response["json"]) != response["json"]:
        raise ValueError("response json is not normalized")
    for part in response.get("body", []):
        if "event" in part:
            if normalizer.response(part["event"]) != part["event"]:
                raise ValueError("stream event is not normalized")
        elif part != {"done": True}:
            raise ValueError("transcript streams hold only events and the terminator")


def path_for(protocol: str, name: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
        raise ValueError("unsafe transcript name")
    return SCENARIO_DIR / protocol / f"{name}.json"


def write(data: dict[str, Any], today: dt.date | None = None) -> Path:
    """Validate, then create the file atomically; an existing file is never overwritten."""
    validate(data, today)
    if data["protocol"] not in PROTOCOLS:
        raise ValueError("unknown protocol")
    path = path_for(data["protocol"], data["name"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    try:
        os.link(tmp, path)  # fails if the transcript exists; review a diff, never auto-refresh
    finally:
        os.unlink(tmp)
    os.chmod(path, 0o644)
    return path
