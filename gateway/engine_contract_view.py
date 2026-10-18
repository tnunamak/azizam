"""Small runtime seam between v2 records and the legacy backend projection.

The registry remains authoritative. This adapter reads either shape without
writing a second lifecycle copy or changing the registry migration.
"""

from __future__ import annotations

from typing import Any, Mapping


def _legacy_lifecycle(service: Mapping[str, Any]) -> Mapping[str, Any]:
    """The pre-v2 ``lifecycle`` declaration as a process-shaped mapping."""
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    for lifecycle in (service.get("lifecycle"), config.get("lifecycle")):
        if not isinstance(lifecycle, Mapping):
            continue
        driver = str(lifecycle.get("driver") or "external").strip().lower()
        if driver in {"external", "none"}:
            continue
        driver_config = lifecycle.get("driver_config")
        return {"driver": driver, **(driver_config if isinstance(driver_config, Mapping) else {})}
    return {"driver": "none"}


def process_config(service: Mapping[str, Any]) -> Mapping[str, Any]:
    """The process declaration, resolved the way the driver factory reads it.

    A ``process`` mapping that names a driver is the v2 declaration. One that
    names none leaves the legacy ``lifecycle`` in charge, so an existing
    systemd or command record never silently falls back to External.
    """
    process = service.get("process")
    if not isinstance(process, Mapping):
        config = service.get("config")
        process = config.get("process") if isinstance(config, Mapping) else None
    if isinstance(process, Mapping) and str(process.get("driver") or "none").strip().lower() != "none":
        return process
    legacy = _legacy_lifecycle(service)
    if legacy.get("driver") == "none" and isinstance(process, Mapping):
        return process
    return legacy


def observations_config(service: Mapping[str, Any]) -> Mapping[str, Any]:
    observations = service.get("observations")
    if not isinstance(observations, Mapping):
        config = service.get("config")
        observations = config.get("observations") if isinstance(config, Mapping) else None
    return observations if isinstance(observations, Mapping) else {}


def residency_config(service: Mapping[str, Any]) -> Mapping[str, Any]:
    residency = service.get("residency")
    if not isinstance(residency, Mapping):
        config = service.get("config")
        residency = config.get("residency") if isinstance(config, Mapping) else None
    return residency if isinstance(residency, Mapping) else {}


def offerings_config(service: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    offerings = service.get("offerings")
    if not isinstance(offerings, list):
        config = service.get("config")
        offerings = config.get("offerings") if isinstance(config, Mapping) else None
    return [item for item in offerings if isinstance(item, Mapping)] if isinstance(offerings, list) else []


def connection_config(service: Mapping[str, Any]) -> Mapping[str, Any]:
    connection = service.get("connection")
    if not isinstance(connection, Mapping):
        config = service.get("config")
        connection = config.get("connection") if isinstance(config, Mapping) else None
    return connection if isinstance(connection, Mapping) else {}
