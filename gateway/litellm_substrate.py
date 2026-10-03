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
    canonical_provider_id,
    is_litellm_service_record,
    is_remote_provider_config,
    normalize_service_record,
    normalize_string_list,
)

OPENAI_COMPATIBLE_PROTOCOLS = frozenset({"openai", "openai-compatible"})
REMOTE_LITELLM_PROVIDERS = frozenset({"openai", "anthropic", "openrouter"})
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
        config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        _validate_remote_compile_shape(service, config)

        deployments = _deployment_records(service)
        if not deployments:
            continue
        entries.extend(_model_list_entries(service, deployments))
    entries.extend(_current_loaded_entries(service_records))
    _validate_no_local_remote_model_name_collisions(entries)

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


def litellm_provider_model_lookup(registry: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Return gateway-owned facts for every LiteLLM-routable public model name.

    The proxy can use this before forwarding to LiteLLM so a provider-prefixed
    paid model cannot slip through a LiteLLM alias or generated config entry
    without gateway policy seeing the provider/cost class.
    """

    services = registry.get("services") if isinstance(registry, Mapping) else {}
    service_records = _normalize_services(services if isinstance(services, Mapping) else {})
    lookup: dict[str, dict[str, Any]] = {}
    for service in sorted(service_records, key=lambda row: (row["priority"], row["id"])):
        if not service.get("enabled", True) or not _is_openai_compatible(service):
            continue
        config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        remote_provider = is_remote_provider_config(config)
        provider_id = _provider_id(service, config)
        for deployment in _deployment_records(service):
            model_name = str(deployment["model_group"])
            lookup[model_name] = {
                "service_id": service["id"],
                "provider_id": provider_id if remote_provider else "",
                "provider_type": str(config.get("provider_type") or ""),
                "cost_class": str(config.get("cost_class") or config.get("provider_type") or ""),
                "remote": remote_provider,
                "api_key_env": str(config.get("api_key_env") or ""),
                "upstream_model": str(deployment["upstream_model"]),
            }
    return lookup


def litellm_provider_key_statuses(
    registry: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Report whether remote provider env-key references are set, without values."""

    environ = os.environ if environ is None else environ
    services = registry.get("services") if isinstance(registry, Mapping) else {}
    service_records = _normalize_services(services if isinstance(services, Mapping) else {})
    statuses: dict[str, dict[str, Any]] = {}
    for service in service_records:
        config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        if not is_remote_provider_config(config):
            continue
        provider_id = _provider_id(service, config)
        env_name = str(config.get("api_key_env") or "")
        statuses[provider_id] = {
            "service_id": service["id"],
            "api_key_env": env_name,
            "key_status": "set" if env_name and bool(environ.get(env_name)) else "missing",
        }
    return statuses


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
    if is_remote_provider_config(config):
        protocols = set(normalize_string_list(config.get("protocols"))) | {protocol}
        if protocols & {"openai-chat", "anthropic-messages"}:
            return True
    return "openai" in normalize_string_list(service.get("capabilities"))


def _deployment_records(service: Mapping[str, Any]) -> list[dict[str, Any]]:
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    provider_id = _provider_id(service, config)
    litellm_provider = _litellm_provider(config)
    remote_provider = is_remote_provider_config(config)
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
        if remote_provider:
            if item.get("base_model_id") not in (None, "") and not model_group.startswith(
                f"{provider_id}/"
            ):
                raise ValueError(
                    f"remote provider alias {model_group!r} must be provider-prefixed"
                )
            model_group = _provider_prefixed_model_name(provider_id, model_group)
        out.append(
            {
                "id": str(item.get("id") or f"{service['id']}:{index}"),
                "model_group": model_group,
                "upstream_model": upstream_model,
                "litellm_provider": str(item.get("litellm_provider") or litellm_provider),
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
        if lifecycle_backed and is_remote_provider_config(config):
            raise ValueError("remote providers must not use local gateway lifecycle routing")
        upstream_model = str(deployment["upstream_model"])
        litellm_provider = str(deployment.get("litellm_provider") or "")
        gateway_backend = str(deployment.get("gateway_backend") or service["id"])
        litellm_params: dict[str, Any] = {
            "model": _litellm_model_name(upstream_model, provider=litellm_provider),
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
            if is_remote_provider_config(config):
                litellm_params["num_retries"] = 0
                litellm_params["max_retries"] = 0
                model_info.update(
                    {
                        "gateway_remote_provider": True,
                        "gateway_provider_id": _provider_id(service, config),
                        "gateway_provider_type": str(config.get("provider_type") or ""),
                        "gateway_api_key_env": str(config.get("api_key_env") or ""),
                    }
                )

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


def _validate_remote_compile_shape(service: Mapping[str, Any], config: Mapping[str, Any]) -> None:
    remote_provider = is_remote_provider_config(config)
    provider = _litellm_provider(config)
    if remote_provider and service.get("kind") != "provider":
        raise ValueError(
            f"remote LiteLLM service {service.get('id')!r} must be kind 'provider'"
        )
    if provider in REMOTE_LITELLM_PROVIDERS and not remote_provider:
        raise ValueError(
            f"LiteLLM service {service.get('id')!r} looks remote but is not marked as a remote provider"
        )
    if remote_provider and _contains_fallback_config(config):
        raise ValueError(
            f"remote LiteLLM service {service.get('id')!r} must not declare LiteLLM fallbacks"
        )


def _validate_no_local_remote_model_name_collisions(entries: list[Mapping[str, Any]]) -> None:
    owners: dict[str, tuple[str, bool]] = {}
    for entry in entries:
        model_name = str(entry.get("model_name") or "").strip()
        if not model_name:
            continue
        model_info = entry.get("model_info") if isinstance(entry.get("model_info"), Mapping) else {}
        remote = bool(model_info.get("gateway_remote_provider"))
        owner = str(
            model_info.get("gateway_provider_id")
            or model_info.get("gateway_backend")
            or ""
        )
        existing = owners.get(model_name)
        if existing is not None and (existing[1] or remote):
            raise ValueError(
                f"public model name {model_name!r} is ambiguous across services "
                f"{existing[0] or 'local'} and {owner or 'local'}"
            )
        owners.setdefault(model_name, (owner, remote))


def _contains_fallback_config(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            lowered = str(key).lower()
            if lowered in {"fallback", "fallbacks", "context_window_fallbacks"}:
                return True
            if _contains_fallback_config(nested):
                return True
    elif isinstance(value, list):
        return any(_contains_fallback_config(item) for item in value)
    return False


def _provider_id(service: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    return canonical_provider_id(config.get("provider_id") or service.get("id") or "")


def _litellm_provider(config: Mapping[str, Any]) -> str:
    provider = str(
        config.get("litellm_provider")
        or config.get("provider_adapter")
        or config.get("provider_id")
        or ""
    ).strip()
    if provider == "openai-compatible":
        return "openai"
    return canonical_provider_id(provider)


def _provider_prefixed_model_name(provider_id: str, model_name: str) -> str:
    if not provider_id:
        return model_name
    prefix, separator, rest = model_name.partition("/")
    if separator and canonical_provider_id(prefix) == provider_id:
        return f"{provider_id}/{rest}"
    return f"{provider_id}/{model_name}"


def _litellm_model_name(upstream_model: str, *, provider: str = "") -> str:
    if provider:
        if upstream_model.startswith(f"{provider}/"):
            return upstream_model
        return f"{provider}/{upstream_model}"
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
