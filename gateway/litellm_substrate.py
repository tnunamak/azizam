"""Compile Gateway service/profile data into a LiteLLM substrate fragment.

This module is intentionally pure and data-oriented. It does not import
``proxy.py``, read environment variables, or contact LiteLLM. The Gateway
registry remains the source of truth; the returned dict is a runtime adapter
shape that could be handed to LiteLLM's ``model_list``/router config.

Explicit limitations:

* Only OpenAI-compatible chat-style services are represented.
* Secrets are emitted as external references, never resolved values.
* Gateway policy/profile metadata is preserved for audit/debugging, but this
  compiler does not translate Gateway policy semantics into LiteLLM auth rules.
* Non-LLM workflow/media/capability routing is out of scope for this spike.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from collections.abc import Iterable, Mapping
from dataclasses import asdict, is_dataclass
from typing import Any
from urllib.parse import urlparse

import yaml

from gateway.policy import AppProfile
from gateway.service_registry import normalize_service_record, normalize_string_list

OPENAI_COMPATIBLE_PROTOCOLS = frozenset({"openai", "openai-compatible"})
SECRET_PLACEHOLDER = "<external-secret>"
DEFAULT_LITELLM_CONFIG_PATH = Path("data/litellm.config.yaml")


def compile_litellm_substrate_config(
    services: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    profiles: Mapping[str, Any] | Iterable[Any] | None = None,
    policy_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a LiteLLM-compatible config fragment from Gateway-owned data."""

    service_records = _normalize_services(services)
    profile_records = _normalize_profiles(profiles)
    entries: list[dict[str, Any]] = []
    omitted: list[dict[str, str]] = []

    for service in sorted(service_records, key=lambda row: (row["priority"], row["id"])):
        if not service.get("enabled", True):
            omitted.append({"service_id": service["id"], "reason": "service disabled"})
            continue
        if not _is_openai_compatible(service):
            omitted.append(
                {
                    "service_id": service["id"],
                    "reason": "not openai-compatible",
                }
            )
            continue

        deployments = _deployment_records(service)
        if not deployments:
            omitted.append(
                {
                    "service_id": service["id"],
                    "reason": "no compilable models",
                }
            )
            continue
        entries.extend(_model_list_entries(service, deployments))

    metadata = {
        "source": "ai-gateway",
        "source_of_truth": "gateway service registry and app profiles",
        "limitations": [
            "OpenAI-compatible LLM deployments only",
            "Secrets are referenced but not resolved",
            "Gateway policies are metadata, not LiteLLM enforcement rules",
            "Media workflows and non-chat capabilities are not compiled",
        ],
        "gateway": {
            "services": [_service_metadata(row) for row in service_records],
            "profiles": profile_records,
            "policy": _redact(policy_metadata or {}),
            "omitted_services": omitted,
        },
    }

    return {
        "model_list": entries,
        "litellm_settings": {
            "drop_params": True,
        },
        "router_settings": {
            "routing_strategy": "simple-shuffle",
        },
        "metadata": metadata,
    }


def compile_litellm_config_from_registry(
    registry: Mapping[str, Any],
    *,
    profiles: Mapping[str, Any] | Iterable[Any] | None = None,
    policy_metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    services = registry.get("services") if isinstance(registry, Mapping) else {}
    return compile_litellm_substrate_config(
        services if isinstance(services, Mapping) else {},
        profiles=profiles,
        policy_metadata=policy_metadata,
    )


def litellm_config_path_from_registry(
    registry: Mapping[str, Any],
    *,
    base_dir: Path,
) -> Path:
    service = _litellm_service_record(registry)
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    raw_path = str(config.get("config_path") or DEFAULT_LITELLM_CONFIG_PATH).strip()
    path = Path(raw_path)
    return path if path.is_absolute() else base_dir / path


def write_litellm_config_artifact(
    registry: Mapping[str, Any],
    *,
    profiles: Mapping[str, Any] | Iterable[Any] | None = None,
    base_dir: Path,
    policy_metadata: Mapping[str, Any] | None = None,
) -> Path:
    path = litellm_config_path_from_registry(registry, base_dir=base_dir)
    compiled = compile_litellm_config_from_registry(
        registry,
        profiles=profiles,
        policy_metadata=policy_metadata,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(
        yaml.safe_dump(compiled, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def _litellm_service_record(registry: Mapping[str, Any]) -> Mapping[str, Any]:
    services = registry.get("services") if isinstance(registry, Mapping) else {}
    if not isinstance(services, Mapping):
        return {}
    candidates = [
        service
        for raw_id, service in services.items()
        if isinstance(service, Mapping)
        and _is_litellm_service(raw_id, service)
    ]
    if not candidates:
        return {}
    return sorted(candidates, key=lambda item: int(item.get("priority") or 100))[0]


def _is_litellm_service(raw_id: Any, service: Mapping[str, Any]) -> bool:
    service_id = str(service.get("id") or raw_id or "").strip().lower()
    kind = str(service.get("kind") or "").strip().lower()
    capabilities = {
        str(item).strip().lower()
        for item in service.get("capabilities", ())
        if str(item).strip()
    }
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    markers = {
        str(config.get("execution") or "").strip().lower(),
        str(config.get("execution_mode") or "").strip().lower(),
        str(config.get("execution_substrate") or "").strip().lower(),
        str(config.get("provider") or "").strip().lower(),
        str(config.get("protocol") or "").strip().lower(),
    }
    return (
        service_id == "litellm"
        or kind in {"litellm", "llm-execution", "llm-substrate"}
        or "llm.execution" in capabilities
        or "litellm" in markers
    )


def _normalize_services(
    services: Mapping[str, Any] | Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(services, Mapping):
        rows = services.items()
    else:
        rows = ((row.get("id"), row) for row in services if isinstance(row, Mapping))

    out: list[dict[str, Any]] = []
    for raw_id, raw in rows:
        if not isinstance(raw, Mapping):
            continue
        service_id = str(raw.get("id") or raw_id or "").strip()
        if service_id:
            out.append(normalize_service_record(service_id, raw))
    return out


def _normalize_profiles(
    profiles: Mapping[str, Any] | Iterable[Any] | None,
) -> list[dict[str, Any]]:
    if profiles is None:
        return []
    if isinstance(profiles, Mapping):
        rows = profiles.items()
    else:
        rows = ((getattr(row, "id", None), row) for row in profiles)

    out: list[dict[str, Any]] = []
    for raw_id, raw in rows:
        if isinstance(raw, AppProfile):
            profile = _dataclass_to_plain_dict(raw)
        elif is_dataclass(raw):
            profile = _dataclass_to_plain_dict(raw)
        elif isinstance(raw, Mapping):
            profile = copy.deepcopy(dict(raw))
        else:
            continue
        profile_id = str(profile.get("id") or raw_id or "").strip()
        if profile_id:
            profile["id"] = profile_id
        out.append(_redact(profile))
    return out


def _is_openai_compatible(service: Mapping[str, Any]) -> bool:
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    protocol = str(config.get("protocol") or config.get("compatibility") or "").lower()
    if protocol in OPENAI_COMPATIBLE_PROTOCOLS:
        return True
    if bool(config.get("openai_compatible")):
        return True
    return "openai" in normalize_string_list(service.get("capabilities"))


def _deployment_records(service: Mapping[str, Any]) -> list[dict[str, Any]]:
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    raw_models = config.get("models")
    if isinstance(raw_models, Mapping):
        iterable = [
            {"id": model_id, **dict(raw)}
            if isinstance(raw, Mapping)
            else {"id": model_id, "upstream_model": raw}
            for model_id, raw in raw_models.items()
        ]
    elif isinstance(raw_models, Iterable) and not isinstance(raw_models, (str, bytes)):
        iterable = list(raw_models)
    else:
        fallback = (
            config.get("model")
            or config.get("model_id")
            or config.get("default_model")
        )
        iterable = [fallback] if fallback else []

    out: list[dict[str, Any]] = []
    for index, raw in enumerate(iterable):
        if isinstance(raw, Mapping):
            item = copy.deepcopy(dict(raw))
        elif isinstance(raw, str):
            item = {"id": raw, "upstream_model": raw}
        else:
            continue

        upstream_model = str(
            item.get("upstream_model")
            or item.get("deployment")
            or item.get("model")
            or item.get("id")
            or ""
        ).strip()
        if not upstream_model:
            continue
        model_group = str(
            item.get("model_group")
            or item.get("group")
            or item.get("public_name")
            or item.get("id")
            or upstream_model
        ).strip()
        out.append(
            {
                "id": str(item.get("id") or f"{service['id']}:{index}"),
                "model_group": model_group,
                "upstream_model": upstream_model,
                "metadata": _redact(item.get("metadata") or {}),
            }
        )
    return out


def _model_list_entries(
    service: Mapping[str, Any],
    deployments: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    secret_refs = (
        service.get("secret_refs") if isinstance(service.get("secret_refs"), Mapping) else {}
    )
    base_url = _service_base_url(config)
    api_key_ref = _api_key_ref(config, secret_refs)

    out: list[dict[str, Any]] = []
    for deployment in deployments:
        litellm_params: dict[str, Any] = {
            "model": _litellm_model_name(str(deployment["upstream_model"])),
        }
        if base_url:
            litellm_params["api_base"] = base_url
        if api_key_ref:
            litellm_params["api_key"] = api_key_ref
        elif _is_loopback_url(base_url):
            litellm_params["api_key"] = "sk-local"

        out.append(
            {
                "model_name": str(deployment["model_group"]),
                "litellm_params": litellm_params,
                "model_info": {
                    "id": f"{service['id']}:{deployment['id']}",
                    "base_model": str(deployment["upstream_model"]),
                    "metadata": {
                        "gateway_service_id": service["id"],
                        "gateway_service_name": service["name"],
                        "gateway_source": service["source"],
                        "gateway_priority": service["priority"],
                        "gateway_model_metadata": deployment["metadata"],
                    },
                },
            }
        )
    return out


def _litellm_model_name(upstream_model: str) -> str:
    if "/" in upstream_model:
        return upstream_model
    return f"openai/{upstream_model}"


def _service_base_url(config: Mapping[str, Any]) -> str:
    raw = str(
        config.get("base_url")
        or config.get("api_base")
        or config.get("default_base_url")
        or ""
    ).strip()
    if raw:
        return raw.rstrip("/")
    host = str(config.get("host") or "").strip()
    port = config.get("port")
    if not host or port is None:
        return ""
    scheme = str(config.get("scheme") or "http").strip() or "http"
    base_path = str(config.get("base_path") or "/v1").strip() or "/v1"
    return f"{scheme}://{host}:{port}/{base_path.strip('/')}".rstrip("/")


def _is_loopback_url(raw_url: str) -> bool:
    if not raw_url:
        return False
    parsed = urlparse(raw_url)
    hostname = (parsed.hostname or "").lower()
    return hostname in {"127.0.0.1", "localhost", "::1"}


def _api_key_ref(config: Mapping[str, Any], secret_refs: Mapping[str, Any]) -> str:
    env_name = config.get("api_key_env") or config.get("api_key_ref")
    if env_name:
        return f"os.environ/{env_name}"
    if secret_refs.get("api_key") == SECRET_PLACEHOLDER:
        return "os.environ/AI_GATEWAY_DOWNSTREAM_API_KEY"
    return ""


def _service_metadata(service: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": service["id"],
        "name": service["name"],
        "source": service["source"],
        "enabled": service["enabled"],
        "priority": service["priority"],
        "capabilities": list(service.get("capabilities") or []),
        "has_secret_refs": bool(service.get("secret_refs")),
    }


def _dataclass_to_plain_dict(value: Any) -> dict[str, Any]:
    return copy.deepcopy(asdict(value))


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if _is_secret_key(key_str):
                out[key_str] = "<redacted>"
            else:
                out[key_str] = _redact(item)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact(item) for item in value]
    return copy.deepcopy(value)


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return any(
        hint in lowered
        for hint in (
            "api_key",
            "authorization",
            "client_secret",
            "credential",
            "password",
            "secret",
            "token",
        )
    )
