"""Compile Gateway service data into a LiteLLM substrate fragment.

This module is intentionally pure and data-oriented. It does not import
``proxy.py``, read environment variables, or contact LiteLLM. The Gateway
registry remains the source of truth; the returned dict is a runtime adapter
shape that could be handed to LiteLLM's ``model_list``/router config.

Explicit limitations:

* Only OpenAI-compatible chat-style services are represented.
* Secrets are emitted as external references, never resolved values.
* Gateway policy/Profile enforcement stays in the gateway.
* Non-LLM workflow/media/capability routing stays in the gateway.
"""

from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Iterable, Mapping
from typing import Any

import yaml

from gateway.service_registry import (
    is_litellm_service_record,
    normalize_service_record,
    normalize_string_list,
)

OPENAI_COMPATIBLE_PROTOCOLS = frozenset({"openai", "openai-compatible"})
SECRET_PLACEHOLDER = "<external-secret>"
DEFAULT_GATEWAY_LIFECYCLE_BASE_URL = "http://127.0.0.1:5000/internal/litellm/local"
DEFAULT_GATEWAY_CURRENT_BASE_URL = "http://127.0.0.1:5000/internal/litellm/current/v1"
DEFAULT_GATEWAY_LIFECYCLE_API_KEY_REF = "os.environ/AI_GATEWAY_INTERNAL_LITELLM_KEY"
DEFAULT_LITELLM_KEY_HEADER = "X-Litellm-Key"
# Bare filename: the artifact lives in the caller-provided ``base_dir`` (the
# gateway's state dir — next to ``admin.db``), so it follows the same isolation
# as every other store instead of being pinned to the repo ``data/`` directory.
DEFAULT_LITELLM_CONFIG_PATH = Path("litellm.config.yaml")


def compile_litellm_substrate_config(
    services: Mapping[str, Any],
) -> dict[str, Any]:
    """Return a LiteLLM-compatible config fragment from Gateway-owned data."""

    service_records = _normalize_services(services)
    entries: list[dict[str, Any]] = []

    for service in sorted(service_records, key=lambda row: (row["priority"], row["id"])):
        if not service.get("enabled", True):
            continue
        if not _is_openai_compatible(service):
            continue

        deployments = _deployment_records(service)
        if not deployments:
            continue
        entries.extend(_model_list_entries(service, deployments))
    entries.extend(_current_loaded_entries(service_records))

    return {
        "model_list": entries,
        "litellm_settings": {
            "drop_params": True,
        },
        "general_settings": {
            "litellm_key_header_name": DEFAULT_LITELLM_KEY_HEADER,
            # Forwards the gateway's cancellation-correlation header
            # (X-AI-Gateway-Request-Id) through to the internal gateway hop
            # behind LiteLLM. LiteLLM does not propagate a cancel signal of
            # its own on this version (its non-stream disconnect canceller is
            # dead code), so the gateway carries the id itself and races its
            # own reads against it. See gateway/backends/cancellation.py.
            "forward_client_headers_to_llm_api": True,
        },
    }


def compile_litellm_config_from_registry(
    registry: Mapping[str, Any],
) -> dict[str, Any]:
    services = registry.get("services") if isinstance(registry, Mapping) else {}
    return compile_litellm_substrate_config(services if isinstance(services, Mapping) else {})


def litellm_known_model_names(registry: Mapping[str, Any]) -> frozenset[str]:
    """Return the set of ``model_name`` values in the compiled LiteLLM config.

    These are the IDs a virtual-key ``models`` allowlist entry must match.
    Returns an empty frozenset when the registry has no LiteLLM-routable models
    (caller should treat empty as "no catalog available, skip validation").
    """
    compiled = compile_litellm_config_from_registry(registry)
    return frozenset(
        str(entry["model_name"])
        for entry in compiled.get("model_list", [])
        if isinstance(entry, Mapping) and entry.get("model_name")
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
    base_dir: Path,
) -> Path:
    path = litellm_config_path_from_registry(registry, base_dir=base_dir)
    compiled = compile_litellm_config_from_registry(registry)
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
        and is_litellm_service_record(raw_id, service)
    ]
    if not candidates:
        return {}
    return sorted(candidates, key=lambda item: int(item.get("priority") or 100))[0]


def _normalize_services(services: Mapping[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw_id, raw in services.items():
        if not isinstance(raw, Mapping):
            continue
        service_id = str(raw.get("id") or raw_id or "").strip()
        if service_id:
            out.append(normalize_service_record(service_id, raw))
    return out


def _is_openai_compatible(service: Mapping[str, Any]) -> bool:
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    protocol = str(config.get("protocol") or "").lower()
    if protocol in OPENAI_COMPATIBLE_PROTOCOLS:
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
            item = dict(raw)
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
            or item.get("id")
            or upstream_model
        ).strip()
        out.append(
            {
                "id": str(item.get("id") or f"{service['id']}:{index}"),
                "model_group": model_group,
                "upstream_model": upstream_model,
                "gateway_lifecycle": bool(
                    item.get("gateway_lifecycle")
                    or item.get("local_lifecycle")
                    or item.get("lifecycle_backed")
                ),
                "gateway_backend": str(
                    item.get("gateway_backend")
                    or item.get("backend")
                    or item.get("service_id")
                    or service["id"]
                ),
            }
        )
    return out


def _current_loaded_service(service_records: list[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    for service in sorted(service_records, key=lambda row: (row["priority"], row["id"])):
        if not service.get("enabled", True):
            continue
        config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        if not config.get("gateway_lifecycle"):
            continue
        if _is_openai_compatible(service):
            return service
    return None


def _current_loaded_entries(service_records: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    service = _current_loaded_service(service_records)
    if service is None:
        return []
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    api_key_ref = str(
        config.get("gateway_lifecycle_api_key_ref")
        or DEFAULT_GATEWAY_LIFECYCLE_API_KEY_REF
    )
    entries: list[dict[str, Any]] = []
    for alias, fast in (("current", False), ("current-fast", True)):
        entries.append(
            {
                "model_name": alias,
                "litellm_params": {
                    "model": f"openai/{alias}",
                    "api_base": DEFAULT_GATEWAY_CURRENT_BASE_URL,
                    "api_key": api_key_ref,
                },
                "model_info": {
                    "gateway_current_loaded": True,
                    "gateway_current_loaded_fast": fast,
                    "gateway_lifecycle": True,
                },
            }
        )
    return entries


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
        lifecycle_backed = bool(
            deployment.get("gateway_lifecycle") or config.get("gateway_lifecycle")
        )
        upstream_model = str(deployment["upstream_model"])
        gateway_backend = str(deployment.get("gateway_backend") or service["id"])
        litellm_params: dict[str, Any] = {
            "model": _litellm_model_name(upstream_model),
        }
        model_info: dict[str, Any] = {}
        if lifecycle_backed:
            litellm_params["api_base"] = _gateway_lifecycle_api_base(
                config,
                backend=gateway_backend,
            )
            litellm_params["api_key"] = str(
                config.get("gateway_lifecycle_api_key_ref")
                or DEFAULT_GATEWAY_LIFECYCLE_API_KEY_REF
            )
            model_info.update(
                {
                    "gateway_lifecycle": True,
                    "gateway_backend": gateway_backend,
                    "gateway_upstream_model": upstream_model,
                }
            )
        else:
            if base_url:
                litellm_params["api_base"] = base_url
            if api_key_ref:
                litellm_params["api_key"] = api_key_ref
            elif base_url:
                # The OpenAI SDK requires a key even for no-auth custom endpoints.
                # Container DNS and LAN addresses are as valid as loopback here.
                litellm_params["api_key"] = "sk-local"

        entry: dict[str, Any] = {
            "model_name": str(deployment["model_group"]),
            "litellm_params": litellm_params,
        }
        if model_info:
            entry["model_info"] = model_info
        out.append(entry)
    return out


def _gateway_lifecycle_api_base(config: Mapping[str, Any], *, backend: str) -> str:
    base = str(
        config.get("gateway_lifecycle_base_url")
        or DEFAULT_GATEWAY_LIFECYCLE_BASE_URL
    ).rstrip("/")
    if base.endswith("/v1"):
        return base
    return f"{base}/{backend}/v1"


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


def _api_key_ref(config: Mapping[str, Any], secret_refs: Mapping[str, Any]) -> str:
    env_name = config.get("api_key_env") or config.get("api_key_ref")
    if env_name:
        return f"os.environ/{env_name}"
    if secret_refs.get("api_key") == SECRET_PLACEHOLDER:
        return "os.environ/AI_GATEWAY_DOWNSTREAM_API_KEY"
    return ""
