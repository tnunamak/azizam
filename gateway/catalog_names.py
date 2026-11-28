"""Names the gateway publishes about itself in model listings, read from ``registry/catalog_names.yml``.

Two facts live there: the owner a listed record shows in ``owned_by`` when it declares none, and the two router names that
serve whatever model the lifecycle service has loaded. Code keeps the protocol field names (``owned_by``) and nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
import yaml

_REGISTRY_DIR = Path(__file__).resolve().parent.parent / "registry"
BUNDLED_PATH = _REGISTRY_DIR / "catalog_names.yml"
SCHEMA_PATH = _REGISTRY_DIR / "catalog_names.schema.json"


@dataclass(frozen=True)
class CatalogNames:
    owner: str
    loaded_aliases: tuple[tuple[str, bool], ...]  # (name, fast), in declared order

    @property
    def loaded_alias(self) -> str:
        return next(name for name, fast in self.loaded_aliases if not fast)

    @property
    def loaded_fast_alias(self) -> str:
        return next(name for name, fast in self.loaded_aliases if fast)


def load(path: Path = BUNDLED_PATH) -> CatalogNames:
    """Read and validate the names; a bad file stops startup with its path in the message."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator(json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))).validate(document)
        rows = tuple((row["name"], row["fast"]) for row in document["loaded_model_aliases"])
        if sorted(fast for _, fast in rows) != [False, True]:
            raise ValueError("loaded_model_aliases needs exactly one fast row and one row that is not fast")
        if len({name for name, _ in rows}) != len(rows):
            raise ValueError("loaded_model_aliases names must differ")
    except (OSError, UnicodeError, ValueError, TypeError, yaml.YAMLError, jsonschema.ValidationError) as error:
        raise ValueError(f"{path}: invalid catalog names: {error}") from error
    return CatalogNames(owner=document["catalog_owner"], loaded_aliases=rows)


NAMES = load()


def catalog_owner(declared: Any = None) -> str:
    """The owner a record declares, else the catalog default."""
    return declared if isinstance(declared, str) and declared.strip() else NAMES.owner


def loaded_model_aliases() -> tuple[tuple[str, bool], ...]:
    return NAMES.loaded_aliases
