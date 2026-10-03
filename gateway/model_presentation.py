"""Human catalog labels and placement from operator-authored service data."""

from collections.abc import Mapping
from ipaddress import ip_address
from typing import Any
from urllib.parse import urljoin, urlsplit

from gateway.service_registry import is_litellm_service_record, is_remote_provider_config


def service_health_endpoint(service: Mapping[str, Any]) -> str | None:
    """Resolve only a declared health path over either registry connection shape."""
    config = service.get("config")
    connection = {**(config if isinstance(config, Mapping) else {}), **service}
    endpoint = connection.get("health_endpoint")
    if not endpoint:
        return None
    endpoint = str(endpoint)
    if endpoint.startswith(("http://", "https://")):
        return endpoint
    base_url = str(connection.get("base_url") or "").rstrip("/")
    if not base_url:
        host = str(connection.get("host") or "").strip()
        if not host:
            return None
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = connection.get("port")
        authority = f"{host}:{port}" if port is not None else host
        scheme = str(connection.get("scheme") or "http")
        base_path = str(connection.get("base_path") or "").strip("/")
        base_url = f"{scheme}://{authority}/{base_path}".rstrip("/")
    # Leading slash means origin-relative; relative paths use the declared base.
    return urljoin(f"{base_url}/", endpoint)


def engine_label(service: Mapping[str, Any], service_id: str | None) -> str | None:
    config = service.get("config")
    for source in (service, config if isinstance(config, Mapping) else {}):
        for field in ("display_name", "name"):
            name = str(source.get(field) or "").strip()
            if name and name != service_id:
                return name
    return None


def model_locality(service: Mapping[str, Any], model: Mapping[str, Any] | None) -> str:
    config = service.get("config")
    sources = (model or {}, service, config if isinstance(config, Mapping) else {})
    for source in sources:
        value = str(source.get("locality") or source.get("placement") or "").strip().lower()
        if value in {"local", "remote"}:
            return value
    if any(is_remote_provider_config(source) for source in sources):
        return "remote"
    # A routing substrate's own address says nothing about its model placement.
    if is_litellm_service_record(service.get("id"), service) or any(
        source.get("execution_substrate") or source.get("managed")
        or str(source.get("kind") or "") == "llm-substrate"
        for source in sources
    ):
        return "unknown"
    for source in (service, sources[2]):
        endpoint = str(source.get("base_url") or source.get("url") or "")
        try:
            host = urlsplit(endpoint).hostname if endpoint else str(source.get("host") or "")
        except ValueError:
            continue
        if not host:
            continue
        if host.lower().rstrip(".") == "localhost":
            return "local"
        try:
            if ip_address(host).is_loopback:
                return "local"
        except ValueError:
            pass
    # A LAN host, container name, or public URL does not prove model placement.
    return "unknown"
