"""Validated release-only policy documents, outside owner configuration sections."""
from functools import lru_cache
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping
import jsonschema
import yaml

REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry"

@lru_cache(maxsize=16)
def load(name: str) -> Mapping[str, Any]:
    """Load a startup policy. Owner settings have their own explicit consumer."""
    path = REGISTRY_DIR / (name + ".yml")
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    schema = json.loads(path.with_suffix(".schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(document)
    return MappingProxyType(document)
