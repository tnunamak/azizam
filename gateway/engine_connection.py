"""Resolve the final inference hop from the same service data the owner edits."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gateway import secret_store, service_registry
from gateway.engine_check import join_url


@dataclass(frozen=True)
class InferenceConnection:
    url: str
    headers: dict[str, str]
    read_timeout: float


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
    address = str(config.get("address") or service_registry.service_base_url(config) or fallback_address)
    base = join_url(address, str(config.get("path_prefix") or ""))
    headers = {"content-type": "application/json"}
    for row in config.get("headers") or ():
        if isinstance(row, Mapping) and row.get("name") and isinstance(row.get("value"), str):
            headers[str(row["name"]).lower()] = row["value"]
    origin = service_registry.origin_of(address)
    if origin and config.get("credential_origin") == origin:
        key = None
        if config.get("api_key_secret_id"):
            key = secret_store.get_provider_secret(
                str(config["api_key_secret_id"]), db_path=db_path, expected_origin=origin,
            )
        elif config.get("api_key_env"):
            key = env(str(config["api_key_env"]))
        if key:
            headers["authorization"] = f"Bearer {key}"
    return InferenceConnection(
        url=join_url(base, operation_path), headers=headers,
        read_timeout=float(config.get("timeout") or fallback_timeout),
    )
