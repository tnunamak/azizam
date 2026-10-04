"""Generic local model-file availability helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


# These field names are legacy/configured gateway-host filesystem paths by
# contract unless the row or service explicitly scopes model files to the
# backend. They are not engine names.
GATEWAY_MODEL_FILE_FIELDS = (
    "model_path",
    "path",
    "vivid_fish_model_path",
    "mmproj",
)

# Activation identifiers may be logical backend ids for externally managed
# services. Treat them as host files only when the row/service says the gateway
# manages that host process, or when the row opts into gateway file scope.
ACTIVATION_FILE_FIELDS = (
    "activation_model",
    "activation_mmproj",
)

# Generic explicit companion/dependency declarations. A multimodal projector is
# just another required local file here; the field name remains data.
DECLARED_REQUIRED_FILE_FIELDS = (
    "required_model_files",
    "required_files",
)

HOST_MANAGED_LIFECYCLE_DRIVERS = frozenset({"systemd", "command"})
BACKEND_FILE_SCOPES = frozenset({"backend", "remote", "container", "external"})


def model_file_availability(
    model: Mapping[str, Any],
    models: Sequence[Mapping[str, Any]] = (),
    *,
    service: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return an unavailable projection when a declared local file is missing.

    The check is data-driven. Gateway-host path fields, explicit required-file
    declarations, and host-managed activation files must exist. Policy-only rows
    can inherit requirements from ``base_model_id``. Rows without a declared
    gateway-host file stay unmeasured here because an upstream id may be a
    container path or logical backend id.
    """

    paths = _model_file_paths(model, models, service=service)
    missing = [path for path in paths if not path.is_file()]
    if not missing:
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
        "evidence": {
            "source": "registry",
            "kind": "missing_model_file",
            "missing_file_count": len(missing),
        },
    }


def missing_model_file_caveat(
    model: Mapping[str, Any],
    models: Sequence[Mapping[str, Any]] = (),
    *,
    service: Mapping[str, Any] | None = None,
) -> str | None:
    availability = model_file_availability(model, models, service=service)
    if availability is None:
        return None
    return str(availability["reason"])


def _model_file_paths(
    model: Mapping[str, Any],
    models: Sequence[Mapping[str, Any]],
    *,
    service: Mapping[str, Any] | None,
    seen: frozenset[str] = frozenset(),
) -> tuple[Path, ...]:
    if _backend_file_scope(model, service):
        return ()
    paths: list[Path] = []
    for field in GATEWAY_MODEL_FILE_FIELDS:
        paths.extend(_paths_from_value(model.get(field)))

    if _gateway_file_scope(model, service):
        for field in ACTIVATION_FILE_FIELDS:
            paths.extend(path for path in _paths_from_value(model.get(field)) if path.is_absolute())
    elif _host_managed_lifecycle(service):
        for field in ACTIVATION_FILE_FIELDS:
            paths.extend(path for path in _paths_from_value(model.get(field)) if path.is_absolute())

    for field in DECLARED_REQUIRED_FILE_FIELDS:
        paths.extend(_paths_from_value(model.get(field)))

    base_id = model.get("base_model_id")
    if isinstance(base_id, str) and base_id and base_id not in seen:
        base = _find_model(models, base_id)
        if base is not None:
            paths.extend(_model_file_paths(base, models, service=service, seen=seen | {base_id}))

    return tuple(dict.fromkeys(paths))


def _paths_from_value(value: Any) -> list[Path]:
    if isinstance(value, str):
        stripped = value.strip()
        return [Path(stripped)] if stripped else []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        paths: list[Path] = []
        for item in value:
            paths.extend(_paths_from_value(item))
        return paths
    if isinstance(value, Mapping):
        paths: list[Path] = []
        for key in ("path", "file", "model_path"):
            paths.extend(_paths_from_value(value.get(key)))
        return paths
    return []


def _gateway_file_scope(model: Mapping[str, Any], service: Mapping[str, Any] | None) -> bool:
    return _model_file_scope(model, service) == "gateway"


def _backend_file_scope(model: Mapping[str, Any], service: Mapping[str, Any] | None) -> bool:
    return _model_file_scope(model, service) in BACKEND_FILE_SCOPES


def _model_file_scope(model: Mapping[str, Any], service: Mapping[str, Any] | None) -> str:
    values = [model.get("model_file_scope")]
    if isinstance(service, Mapping):
        config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        values.extend([service.get("model_file_scope"), config.get("model_file_scope")])
    for value in values:
        scope = str(value or "").strip().casefold()
        if scope:
            return scope
    return ""


def _host_managed_lifecycle(service: Mapping[str, Any] | None) -> bool:
    if not isinstance(service, Mapping):
        return False
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    lifecycle = service.get("lifecycle") if isinstance(service.get("lifecycle"), Mapping) else config.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        return False
    driver = str(lifecycle.get("driver") or "").strip().casefold()
    return driver in HOST_MANAGED_LIFECYCLE_DRIVERS


def _find_model(models: Sequence[Mapping[str, Any]], model_id: str) -> Mapping[str, Any] | None:
    for model in models:
        if not isinstance(model, Mapping):
            continue
        if model_id in {model.get("id"), model.get("model_group"), model.get("public")}:
            return model
    return None


__all__ = ["missing_model_file_caveat", "model_file_availability"]
