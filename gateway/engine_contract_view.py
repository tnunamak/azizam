"""Small runtime seam between v2 records and the legacy backend projection.

The registry remains authoritative. This adapter reads either shape without
writing a second lifecycle copy or changing the registry migration.
"""

from __future__ import annotations

from typing import Any, Mapping


def process_config(service: Mapping[str, Any]) -> Mapping[str, Any]:
    process = service.get("process")
    if not isinstance(process, Mapping):
        config = service.get("config")
        process = config.get("process") if isinstance(config, Mapping) else None
    if isinstance(process, Mapping):
        return process
    lifecycle = service.get("lifecycle")
    if not isinstance(lifecycle, Mapping):
        return {"driver": "none"}
    driver = str(lifecycle.get("driver") or "external").lower()
    if driver == "external":
        return {"driver": "none"}
    config = lifecycle.get("driver_config")
    return {"driver": driver, **(config if isinstance(config, Mapping) else {})}


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
