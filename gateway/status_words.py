"""What the status page and the metrics sampler call the services of a capability role, from ``registry/status_words.yml``.

The file is data and ``registry/status_words.schema.json`` is its closed schema. This module holds no word of its own:
a missing file, a broken file or a missing key is an error, never a guess. Roles are capability keys, not service ids.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

import jsonschema
import yaml

_REGISTRY_DIR = Path(__file__).resolve().parent.parent / "registry"
BUNDLED_PATH = _REGISTRY_DIR / "status_words.yml"
SCHEMA_PATH = _REGISTRY_DIR / "status_words.schema.json"


@functools.lru_cache(maxsize=4)
def _read(path: Path, schema_path: Path, modified_ns: int) -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    jsonschema.Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8"))).validate(document)
    return {role: dict(words) for role, words in document["status_words"].items()}


def words(role: str, path: Path = BUNDLED_PATH) -> dict[str, Any]:
    """The words of capability role *role*, checked against the schema; ``KeyError`` for a role the file does not hold."""
    return _read(path, SCHEMA_PATH, path.stat().st_mtime_ns)[role]
