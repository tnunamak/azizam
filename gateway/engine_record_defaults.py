"""The values an engine's service record may leave out, from ``registry/engine_record_defaults.yml``.

The file is data and ``registry/engine_record_defaults.schema.json`` is its closed schema. This module holds no value of
its own: a missing file, a broken file or a missing key is an error, never a guess. An engine address is not a default.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, Mapping

import jsonschema
import yaml

_REGISTRY_DIR = Path(__file__).resolve().parent.parent / "registry"
BUNDLED_PATH = _REGISTRY_DIR / "engine_record_defaults.yml"
SCHEMA_PATH = _REGISTRY_DIR / "engine_record_defaults.schema.json"


@functools.lru_cache(maxsize=4)
def _read(path: Path, schema_path: Path, modified_ns: int) -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    jsonschema.Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8"))).validate(document)
    return {capability: dict(values) for capability, values in document["defaults"].items()}


def release_defaults(path: Path = BUNDLED_PATH) -> dict[str, dict[str, Any]]:
    """Defaults by capability role key, not selected deployment id, checked against the schema."""
    return _read(path, SCHEMA_PATH, path.stat().st_mtime_ns)


def record_value(capability: str, record: Mapping[str, Any] | None, key: str) -> Any:
    """The record's value, else the default of the *capability* role key (not a deployment id); ``KeyError`` if absent."""
    record = record or {}
    if record.get(key) is not None:
        return record[key]
    return release_defaults()[capability][key]


def role_status_panels() -> dict[str, list[dict[str, Any]]]:
    """The release status panels of each capability role that declares some."""
    return {capability: [dict(panel) for panel in values["status_panels"]]
            for capability, values in release_defaults().items() if values.get("status_panels")}


def record_status_panels(capability: str, record: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The panels the status page shows for *record*: its own ``status_panels``, else the role's release panels."""
    own = (record or {}).get("status_panels")
    if isinstance(own, (list, tuple)):
        return [dict(panel) for panel in own if isinstance(panel, Mapping)]
    return role_status_panels().get(capability, [])
