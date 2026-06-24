"""Commodity LLM execution transport.

The gateway owns auth, Profiles, local resource policy, and receipts. This
module owns only the final HTTP hop to a selected LLM execution substrate.
Keep it narrow so the substrate boundary does not become a second router.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Mapping

import httpx

from gateway.service_registry import is_litellm_service_record

LITELLM_BODY_EXECUTION_PATHS = frozenset(
    {
        "/v1/chat/completions",
        "/v1/completions",
        "/v1/embeddings",
    }
)
LITELLM_DELEGATED_KEY_HEADER = "X-Litellm-Key"


@dataclass(frozen=True)
class LlmExecutionRequest:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes
    read_timeout: float


@dataclass(frozen=True)
class LiteLlmRuntimeConfig:
    enabled: bool = False
    base_url: str = ""
    api_key: str = ""
    service_id: str = ""


def litellm_runtime_from_registry(
    registry: Mapping[str, Any] | None,
    *,
    env: Mapping[str, str] | None = None,
) -> LiteLlmRuntimeConfig:
    env = os.environ if env is None else env
    for service in _litellm_service_candidates(registry):
        if not bool(service.get("enabled", True)):
            continue
        config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        base_url = _base_url_from_config(config, env)
        if not base_url:
            continue
        return LiteLlmRuntimeConfig(
            enabled=True,
            base_url=base_url,
            api_key=_api_key_from_config(config, env),
            service_id=str(service.get("id") or "litellm"),
        )
    return LiteLlmRuntimeConfig()


def _litellm_service_candidates(
    registry: Mapping[str, Any] | None,
) -> list[Mapping[str, Any]]:
    if not isinstance(registry, Mapping):
        return []
    services = registry.get("services")
    if not isinstance(services, Mapping):
        return []
    candidates: list[Mapping[str, Any]] = []
    for service_id, raw in services.items():
        if not isinstance(raw, Mapping):
            continue
        service = dict(raw)
        service.setdefault("id", service_id)
        if is_litellm_service_record(service_id, service):
            candidates.append(service)
    return sorted(candidates, key=lambda item: int(item.get("priority") or 100))


def _api_key_from_config(config: Mapping[str, Any], env: Mapping[str, str]) -> str:
    env_name = str(config.get("api_key_env") or config.get("api_key_ref") or "").strip()
    if env_name:
        return env.get(env_name, "")
    return ""


def _base_url_from_config(config: Mapping[str, Any], env: Mapping[str, str]) -> str:
    env_name = str(config.get("base_url_env") or config.get("api_base_env") or "").strip()
    if env_name:
        env_value = str(env.get(env_name) or "").strip()
        if env_value:
            return env_value.rstrip("/")
    return str(
        config.get("base_url")
        or config.get("api_base")
        or config.get("default_base_url")
        or ""
    ).rstrip("/")


def litellm_url(*, base_url: str, path: str, query: str = "") -> str:
    url = f"{str(base_url).rstrip('/')}/{path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"
    return url


def litellm_headers(
    source_headers: Mapping[str, str],
    *,
    api_key: str = "",
    gateway_metadata: Mapping[str, str] | None = None,
    delegated_api_key: str = "",
) -> dict[str, str]:
    headers: dict[str, str] = {
        "content-type": _header(source_headers, "content-type") or "application/json",
        "accept": _header(source_headers, "accept") or "application/json",
    }
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    if delegated_api_key:
        headers[LITELLM_DELEGATED_KEY_HEADER] = f"Bearer {delegated_api_key}"

    for name, value in (gateway_metadata or {}).items():
        if value:
            headers[f"x-ai-gateway-{name}"] = value
    return headers


async def send_llm_execution_request(
    request: LlmExecutionRequest,
) -> tuple[httpx.AsyncClient, httpx.Response]:
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=10.0,
            read=request.read_timeout,
            write=60.0,
            pool=None,
        )
    )
    req = client.build_request(
        method=request.method,
        url=request.url,
        headers=dict(request.headers),
        content=request.body,
    )
    response = await client.send(req, stream=True)
    return client, response


def _header(headers: Mapping[str, str], name: str) -> str:
    direct = headers.get(name)
    if direct is not None:
        return str(direct)
    lower_name = name.lower()
    for key, value in headers.items():
        if key.lower() == lower_name:
            return str(value)
    return ""
