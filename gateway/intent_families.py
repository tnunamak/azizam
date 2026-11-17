"""Resolve opaque intent names against exact and single-segment family rows."""

from __future__ import annotations

from typing import Mapping, TypeVar

_Row = TypeVar("_Row")


def resolve_intent(
    intent: str, *, exact: Mapping[str, _Row], families: Mapping[str, _Row],
) -> _Row | None:
    """Return an exact row, otherwise its family row, or None for malformed names."""
    if not isinstance(intent, str):
        raise TypeError("intent must be a string")
    parts = intent.split(".")
    if len(parts) < 2 or not all(parts) or any(char.isspace() for char in intent):
        return None
    if intent in exact:
        return exact[intent]
    return families.get(parts[0])
