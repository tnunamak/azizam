"""Resolve the final inference hop from the same service data the owner edits."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway import secret_store, service_registry
from gateway.engine_check import join_url
from gateway.litellm_substrate import _engine_request_settings


class InferenceConnectionError(ValueError):
    """A configured inference credential cannot be used safely."""


@dataclass(frozen=True)
class InferenceConnection:
    url: str
    headers: dict[str, str]
    read_timeout: float


def inference_base_url(config: Mapping[str, Any], fallback_address: str = "") -> str:
    """Use the same effective base as direct routing, including the protocol path."""
    base = service_registry.service_base_url(config)
    if base:
        return base
    if config.get("address"):
        return join_url(str(config["address"]), str(config.get("path_prefix") or ""))
    return fallback_address


def inference_connection(
    record: Mapping[str, Any] | None, operation_path: str, *,
    fallback_address: str, fallback_timeout: float, db_path: Path,
    env: Callable[[str], str | None],
) -> InferenceConnection:
    """Use one record snapshot, binding inference credentials to its origin.

    The fallback serves pre-registry callers. A registered service always
    supplies the final address, prefix, headers and reply timeout itself.
    Client and internal-hop credentials never enter this function.
    """
    config = (record or {}).get("config") or {}
    base = inference_base_url(config, fallback_address)
    settings = _engine_request_settings(config)
    headers = {"content-type": "application/json"}
    headers.update({name.lower(): value for name, value in settings.get("extra_headers", {}).items()})
    origin = service_registry.origin_of(base)
    if origin and config.get("credential_origin") == origin:
        key = None
        if config.get("api_key_secret_id"):
            try:
                key = secret_store.get_provider_secret(
                    str(config["api_key_secret_id"]), db_path=db_path, expected_origin=origin,
                )
            except (KeyError, PermissionError):
                raise InferenceConnectionError("The engine credential is unavailable. Re-enter it in Connection.") from None
        elif config.get("api_key_env"):
            key = env(str(config["api_key_env"]))
        if (config.get("api_key_secret_id") or config.get("api_key_env")) and not key:
            raise InferenceConnectionError("The engine credential is unavailable. Re-enter it in Connection.")
        if key:
            headers["authorization"] = f"Bearer {key}"
    return InferenceConnection(
        url=join_url(base, operation_path), headers=headers,
        read_timeout=settings.get("timeout", fallback_timeout),
    )
