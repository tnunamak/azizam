"""Resolve opaque intent names against exact and single-segment family rows."""

from __future__ import annotations

import re
from typing import Mapping, TypeVar

_Row = TypeVar("_Row")


def resolve_intent(
    intent: str, *, exact: Mapping[str, _Row], families: Mapping[str, _Row],
) -> _Row | None:
    """Return an exact row, otherwise its family row, or None for malformed names."""
    if not isinstance(intent, str):
        raise TypeError("intent must be a string")
    parts = intent.split(".")
    if len(parts) < 2 or any(
        re.fullmatch(r"[A-Za-z0-9_]+", part, flags=re.ASCII) is None for part in parts
    ):
        return None
    if intent in exact:
        return exact[intent]
    if any(".".join(parts[:length]) in exact for length in range(2, len(parts))):
        return None
    return families.get(parts[0])
