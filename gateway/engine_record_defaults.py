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
    return {service_id: dict(values) for service_id, values in document["defaults"].items()}


def release_defaults(path: Path = BUNDLED_PATH) -> dict[str, dict[str, Any]]:
    """Every service id's defaults, checked against the schema."""
    return _read(path, SCHEMA_PATH, path.stat().st_mtime_ns)


def record_value(service_id: str, record: Mapping[str, Any] | None, key: str) -> Any:
    """The record's own value for *key*, else the release default of *service_id*; ``KeyError`` when neither names one."""
    record = record or {}
    if record.get(key) is not None:
        return record[key]
    return release_defaults()[service_id][key]
