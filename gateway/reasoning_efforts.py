"""Reasoning order from the policy schema and a subscription's advertised ceiling."""
from __future__ import annotations

import json
from pathlib import Path

_SCHEMA = Path(__file__).resolve().parents[1] / "registry/defaults/reasoning.schema.json"
EFFORTS = tuple(value for value in json.loads(_SCHEMA.read_text())["properties"]["default_effort"]["enum"]
                if value is not None)


def capped_effort(requested: str | None, advertised: list[str], top_effort: str | None) -> str | None:
    """The level a request above the ceiling is lowered to, or None when nothing changes.

    The ceiling only lowers: a request at or below it is left to the model's own checks. Above it, the request gets the
    highest level the model offers at or below the ceiling, else the model's least level.
    """
    if top_effort is None or requested not in EFFORTS or EFFORTS.index(requested) <= EFFORTS.index(top_effort):
        return None
    ordered = [effort for effort in EFFORTS if effort in advertised]
    if not ordered:
        return None
    effective = [effort for effort in ordered if EFFORTS.index(effort) <= EFFORTS.index(top_effort)]
    target = effective[-1] if effective else ordered[0]
    return target if target != requested else None
