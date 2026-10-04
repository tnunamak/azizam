"""Which enabled services are relied on by the gateway's configured routes.

Discovery alone is not usage: an unused engine may advertise capabilities.
This projection takes resolved aliases and explicit routing data, without
probing engines or changing the operator's registry.
"""
from collections.abc import Iterable, Mapping
from typing import Any

from gateway.alias_policy import service_model_records
from gateway.capabilities import Capability


def relied_on_service_ids(*, registry: Mapping[str, Any],
                         runtime_config: Mapping[str, Any],
                         capabilities: Iterable[Capability],
                         selected_defaults: Iterable[str] = (),
                         selected_execution_dependencies: Iterable[str] = ()) -> list[str]:
    services = registry.get("services") or {}
    enabled = {str(key) for key, row in services.items()
               if isinstance(row, Mapping) and row.get("enabled", True)}
    used = {cap.backend for cap in capabilities
            if "alias" in cap.tags and cap.backend in enabled}
    used.update(service_id for service_id in selected_defaults if service_id in enabled)
    # Execution substrates are route dependencies even when the caller-visible
    # model belongs to another engine. Selection comes from the execution path,
    # never from a service name, discovery, or an assumed substrate type.
    used.update(service_id for service_id in selected_execution_dependencies if service_id in enabled)
    routing = runtime_config.get("routing") or {}
    if isinstance(routing, Mapping):
        used.update(str(value) for key, value in routing.items()
                    if key.endswith("_backend") and isinstance(value, str) and value in enabled)
    # Runtime roles/models are catalog metadata, not dispatch targets. Real
    # registered deployments arrive through selected_defaults from the same
    # registry projection used by execution, including single-model fallbacks.
    for service_id in enabled:
        service = services[service_id]
        config = service.get("config") or {}
        if isinstance(config, Mapping) and any(row.get("enabled", True) for row in service_model_records(config)):
            used.add(service_id)
    return sorted(used)
