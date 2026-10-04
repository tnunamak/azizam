"""Generic local model-file availability helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


LOCAL_MODEL_FILE_FIELDS = (
    "activation_model",
    "model_path",
    "path",
    "vivid_fish_model_path",
)


def model_file_availability(
    model: Mapping[str, Any],
    models: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any] | None:
    """Return an unavailable projection when a declared local file is missing.

    The check is data-driven: if a model row declares one of the generic local
    file fields, that file must exist. Policy-only rows can inherit the
    requirement from ``base_model_id``. Rows without an explicit local file stay
    unmeasured here because an upstream id is not necessarily a filesystem path.
    """

    path = _model_file_path(model, models)
    if path is None:
        return None
    if path.is_file():
        return None
    return {
        "available": False,
        "status": "unavailable",
        "reason": "Model file is not installed.",
        "measured": True,
        "enabled": True,
        "configured": True,
        "up": None,
        "idle_startable": False,
        "evidence": {"source": "registry", "kind": "missing_model_file"},
    }


def missing_model_file_caveat(
    model: Mapping[str, Any],
    models: Sequence[Mapping[str, Any]] = (),
) -> str | None:
    availability = model_file_availability(model, models)
    if availability is None:
        return None
    return str(availability["reason"])


def _model_file_path(
    model: Mapping[str, Any],
    models: Sequence[Mapping[str, Any]],
    seen: frozenset[str] = frozenset(),
) -> Path | None:
    for field in LOCAL_MODEL_FILE_FIELDS:
        value = model.get(field)
        if isinstance(value, str) and value.strip():
            return Path(value)
    base_id = model.get("base_model_id")
    if not isinstance(base_id, str) or not base_id or base_id in seen:
        return None
    base = _find_model(models, base_id)
    if base is None:
        return None
    return _model_file_path(base, models, seen | {base_id})


def _find_model(models: Sequence[Mapping[str, Any]], model_id: str) -> Mapping[str, Any] | None:
    for model in models:
        if not isinstance(model, Mapping):
            continue
        if model_id in {model.get("id"), model.get("model_group"), model.get("public")}:
            return model
    return None


__all__ = ["missing_model_file_caveat", "model_file_availability"]
