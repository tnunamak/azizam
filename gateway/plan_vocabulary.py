"""One capability vocabulary for access plans.

Runtime routes stamp plan-vocabulary intents (``image.generate``,
``speech.synthesis``...). Protocol frontends describe the same work in route
spellings (``images.generate``, ``audio.speech``...). The bundled profile
policy catalog lists, per plan intent, the ``aliases`` that mean the same
thing; this module reads that data so plans are read, validated and enforced
in the plan vocabulary whichever spelling they saved. It holds no ids itself.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

_CATALOG_PATH = Path(__file__).resolve().parent.parent / "registry" / "profile_policy_catalog.yml"


def intent_aliases_from_catalog(options: Iterable[Any]) -> dict[str, str]:
    """Map each alias spelling to its plan intent; reject ambiguous data."""
    entries = [option for option in options if isinstance(option, Mapping) and str(option.get("id") or "").strip()]
    canonical = {str(option["id"]).strip() for option in entries}
    aliases: dict[str, str] = {}
    for option in entries:
        intent = str(option["id"]).strip()
        raw = option.get("aliases") or []
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
            raise ValueError(f"intent {intent!r} aliases must be a list")
        for alias in (str(value).strip() for value in raw):
            if not alias or alias == intent:
                continue
            if alias in canonical:
                raise ValueError(f"alias {alias!r} of {intent!r} is itself a plan intent")
            if aliases.setdefault(alias, intent) != intent:
                raise ValueError(f"alias {alias!r} maps to both {aliases[alias]!r} and {intent!r}")
    return aliases


@lru_cache(maxsize=1)
def intent_aliases() -> dict[str, str]:
    raw = yaml.safe_load(_CATALOG_PATH.read_text(encoding="utf-8")) or {}
    return intent_aliases_from_catalog(raw.get("intents") or [])


def canonical_intent(value: str) -> str:
    """Return the plan intent for ``value``; unknown ids pass through unchanged."""
    return intent_aliases().get(value, value)
