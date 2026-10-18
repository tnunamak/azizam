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

from gateway.compat import get_env

import os
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import yaml

from gateway.provider_catalog import load_provider_catalog
from gateway.secret_refs import is_secret_entry
from gateway.secret_store import credential_origin_from_url
from gateway.service_registry import (
    canonical_provider_id,
    is_litellm_service_record,
    is_remote_provider_config,
    normalize_service_record,
    normalize_string_list,
    origin_of,
    service_base_url,
)

OPENAI_COMPATIBLE_PROTOCOLS = frozenset({"openai", "openai-compatible"})
SECRET_PLACEHOLDER = "<external-secret>"
LOCAL_NO_AUTH_API_KEY = "sk-local"
DEFAULT_GATEWAY_LIFECYCLE_BASE_URL = "http://127.0.0.1:5000/internal/litellm/local"
DEFAULT_GATEWAY_CURRENT_BASE_URL = "http://127.0.0.1:5000/internal/litellm/current/v1"
DEFAULT_GATEWAY_LIFECYCLE_API_KEY_REF = "os.environ/AZIZAM_INTERNAL_LITELLM_KEY"
DEFAULT_LITELLM_KEY_HEADER = "X-Litellm-Key"
# Bare filename: the artifact lives in the caller-provided ``base_dir`` (the
# gateway's state dir — next to ``admin.db``), so it follows the same isolation
# as every other store instead of being pinned to the repo ``data/`` directory.
DEFAULT_LITELLM_CONFIG_PATH = Path("litellm.config.yaml")


@dataclass(frozen=True)
class Materializer:
    """How an executable config gets what a record only references (decision D30).

    ``header(row, origin)`` is the value of a secret header row for the origin it is sent to, or None when it
    cannot be read (the row is then left out). ``url(config)`` is a record's endpoint with its stored credentials.
    Only ``gateway/litellm_runtime.py`` builds one; this module never reads a secret, so without one it renders
    the preview: public data, a header's name with a placeholder for its value, an endpoint without credentials.
    """

    header: Callable[[Mapping[str, Any], str | None], str | None]
    url: Callable[[Mapping[str, Any]], str]


HEADER_PLACEHOLDER = "<configured>"


def compile_litellm_substrate_config(
    services: Mapping[str, Any],
    *,
    materializer: Materializer | None = None,
) -> dict[str, Any]:
    """A LiteLLM-compatible config fragment from Gateway-owned data.

    Without ``materializer`` this is the preview a management route may show: no secret is in it and none can
    be. With the runtime's, it is the executable config the router reads from a restricted file.
    """

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
        entries.extend(_model_list_entries(service, deployments, materializer))
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
            # (X-Azizam-Request-Id) through to the internal gateway hop
            # behind LiteLLM. LiteLLM does not propagate a cancel signal of
            # its own on this version (its non-stream disconnect canceller is
            # dead code), so the gateway carries the id itself and races its
            # own reads against it. See gateway/backends/cancellation.py.
            "forward_client_headers_to_llm_api": True,
        },
    }


def compile_litellm_config_from_registry(
    registry: Mapping[str, Any],
    *,
    materializer: Materializer | None = None,
) -> dict[str, Any]:
    services = registry.get("services") if isinstance(registry, Mapping) else {}
    return compile_litellm_substrate_config(services if isinstance(services, Mapping) else {}, materializer=materializer)


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


def litellm_public_model_records(
    registry: Mapping[str, Any],
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    """Return ``{public model name: (service, model record)}`` for every LiteLLM-routable model.

    The public name is the ``model_name`` LiteLLM serves, so a remote provider's
    record is keyed by its provider-prefixed name, not its raw registry id.
    """

    services = registry.get("services") if isinstance(registry, Mapping) else {}
    service_records = _normalize_services(services if isinstance(services, Mapping) else {})
    out: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for service in sorted(service_records, key=lambda row: (row["priority"], row["id"])):
        if not service.get("enabled", True) or not _is_openai_compatible(service):
            continue
        for deployment in _deployment_records(service):
            out.setdefault(str(deployment["model_group"]), (service, deployment["record"]))
    return out


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
        secret_id = str(config.get("api_key_secret_id") or "")
        env_usable = False
        if env_name and get_env(env_name, environ=environ):
            expected_origin_raw = str(config.get("credential_origin") or "").strip()
            if expected_origin_raw:
                try:
                    env_usable = (
                        credential_origin_from_url(str(config.get("base_url") or ""))
                        == credential_origin_from_url(expected_origin_raw)
                    )
                except Exception:
                    env_usable = False
        statuses[provider_id] = {
            "service_id": service["id"],
            "api_key_env": env_name,
            "api_key_secret_id": secret_id,
            "key_status": "set" if env_usable else "unknown" if secret_id else "missing",
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


def reaches_engine_directly(service_id: str, record: Mapping[str, Any]) -> bool:
    """True when the model router calls this service at its own address.

    False for a service it does not route, or one reached through the gateway's
    lifecycle hop. Both paths use the service record's connection settings.
    """
    service = normalize_service_record(service_id, record)
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    if not _is_openai_compatible(service) or config.get("gateway_lifecycle"):
        return False
    try:
        deployments = _deployment_records(service)
    except ValueError:
        return False
    return not any(deployment["gateway_lifecycle"] for deployment in deployments)


def routed_models(service_id: str, record: Mapping[str, Any]) -> tuple[list[str], str | None]:
    """The public names the model router serves for this record, and its paid provider (None when local).

    A remote provider's names carry its prefix, as the router serves them. A
    record the router does not route gives no names.
    """
    service = normalize_service_record(service_id, record)
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    if not service.get("enabled", True) or not _is_openai_compatible(service):
        return [], None
    try:
        deployments = _deployment_records(service)
    except ValueError:
        return [], None
    provider = _provider_id(service, config) if is_remote_provider_config(config) else None
    return [str(deployment["model_group"]) for deployment in deployments], provider


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
        if item.get("enabled") is False:
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
                "record": item,
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


# The router names that serve whatever the current lifecycle service has loaded,
# and whether each is the fast variant.
CURRENT_LOADED_ALIASES = (("current", False), ("current-fast", True))


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
    timeout = _current_router_timeout(service_records)
    for alias, fast in CURRENT_LOADED_ALIASES:
        entries.append(
            {
                "model_name": alias,
                "litellm_params": {
                    "model": f"openai/{alias}",
                    "api_base": DEFAULT_GATEWAY_CURRENT_BASE_URL,
                    "api_key": api_key_ref,
                    **({"timeout": timeout} if timeout is not None else {}),
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
    materializer: Materializer | None = None,
) -> list[dict[str, Any]]:
    config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
    secret_refs = (
        service.get("secret_refs") if isinstance(service.get("secret_refs"), Mapping) else {}
    )
    base_url = materializer.url(config) if materializer is not None else service_base_url(config)

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
            litellm_params["api_key"] = _lifecycle_api_key(config, litellm_params["api_base"])
            timeout = _lifecycle_router_timeout(service, config)
            if timeout is not None:
                litellm_params["timeout"] = timeout
            model_info.update(
                {
                    "gateway_lifecycle": True,
                    "gateway_backend": gateway_backend,
                    "gateway_upstream_model": upstream_model,
                }
            )
        else:
            if is_remote_provider_config(config) and not base_url:
                raise ValueError("remote provider requires an explicit api_base")
            api_key_ref = _api_key_ref(config, secret_refs, base_url, service_id=str(service.get("id") or ""))
            if base_url:
                litellm_params["api_base"] = base_url
            litellm_params.update(_engine_request_settings(config, materializer, origin=origin_of(base_url)))
            if api_key_ref:
                litellm_params["api_key"] = api_key_ref
            elif base_url:
                # The OpenAI SDK requires a key even for no-auth custom endpoints.
                # Container DNS and LAN addresses are as valid as loopback here.
                litellm_params["api_key"] = LOCAL_NO_AUTH_API_KEY
            if is_remote_provider_config(config):
                litellm_params["num_retries"] = 0
                litellm_params["max_retries"] = 0
                model_info.update(
                    {
                        "gateway_remote_provider": True,
                        "gateway_provider_id": _provider_id(service, config),
                        "gateway_provider_type": str(config.get("provider_type") or ""),
                        "gateway_api_key_env": str(config.get("api_key_env") or ""),
                        "gateway_api_key_secret_id": str(config.get("api_key_secret_id") or ""),
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


# A header whose name looks like it carries a credential: only the owner may set one (a write rule about who may
# send a key; a view hides every header value whatever its name).
CREDENTIAL_HEADER_RE = re.compile(r"auth|key|token|secret|passw|cookie|session|credential|signature", re.IGNORECASE)
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
# Headers the gateway or the HTTP layer sets itself.
TRANSPORT_HEADERS = frozenset({
    "host", "content-length", "content-type", "transfer-encoding", "connection", "upgrade", "te",
    "trailer", "keep-alive", "proxy-connection", "accept-encoding",
})


def header_problem(name: str, value: str) -> str:
    """Why a plain engine header cannot be sent, or ``""`` when it can."""
    if not _HEADER_NAME_RE.fullmatch(name):
        return "not_a_name"
    if name.lower() in TRANSPORT_HEADERS:
        return "transport"
    if CREDENTIAL_HEADER_RE.search(name):
        return "credential"
    if not value or not value.isascii() or any(ch in value for ch in "\r\n"):
        return "bad_value"
    return ""


def engine_headers(
    config: Mapping[str, Any],
    *,
    resolve: Callable[[Mapping[str, Any]], str | None] | None = None,
    preview: bool = False,
) -> dict[str, str]:
    """The request headers a service record lists (decision D29): public rows as written, secret rows resolved.

    A secret row (``{"id", "name", "secret_ref"}``) is resolved by ``resolve(row)``, which only the runtime can
    supply; without it the row is left out, or with ``preview`` it shows as a placeholder. A row that fails
    ``header_problem`` is left out: a raw service edit can store anything, and nothing malformed is sent.
    """
    out: dict[str, str] = {}
    for row in config.get("headers") or () if isinstance(config.get("headers"), list) else ():
        if not isinstance(row, Mapping) or not isinstance(row.get("name"), str):
            continue
        name = row["name"].strip()
        if is_secret_entry(row):
            value = HEADER_PLACEHOLDER if preview else (resolve(row) if resolve is not None else None)
        elif row.get("public") is True and isinstance(row.get("value"), str):
            value = row["value"]
        else:
            continue  # a legacy inline row no migration has moved: never sent, never shown
        if isinstance(value, str) and (preview or not header_problem(name, value)):
            out[name] = value
    return out


def router_reply_timeout(registry: Mapping[str, Any], model_name: str) -> float | None:
    """The reply time the record serving ``model_name`` set for the router, if any.

    The gateway's own wait for the router must be at least this long, or the
    owner's setting would be cut short by the hop in front of it.
    """
    services = registry.get("services") if isinstance(registry, Mapping) else {}
    records = _normalize_services(services if isinstance(services, Mapping) else {})
    for service in sorted(records, key=lambda row: (row["priority"], row["id"])):
        if not service.get("enabled", True) or not _is_openai_compatible(service):
            continue
        config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        try:
            deployments = _deployment_records(service)
        except ValueError:
            continue
        for deployment in deployments:
            if str(deployment["model_group"]) == model_name:
                if deployment.get("gateway_lifecycle") or config.get("gateway_lifecycle"):
                    return _lifecycle_router_timeout(service, config)
                return _engine_request_settings(config).get("timeout")
    if model_name in {alias for alias, _fast in CURRENT_LOADED_ALIASES}:
        return _current_router_timeout(records)
    return None


_READINESS_KEYS = ("ready_timeout_s", "health_timeout_s", "load_timeout_s")


def _current_router_timeout(records: list[Mapping[str, Any]]) -> float | None:
    """Allow the dynamic aliases to serve any eligible local engine's reply time."""
    budgets = []
    for service in records:
        config = service.get("config") if isinstance(service.get("config"), Mapping) else {}
        if not service.get("enabled", True) or not _is_openai_compatible(service) or is_remote_provider_config(config):
            continue
        budget = _lifecycle_router_timeout(service, config)
        if budget is None:
            # An eligible engine uses the router default. A finite alias limit
            # would silently shorten that engine's allowed reply time.
            return None
        budgets.append(budget)
    return max(budgets) if budgets else None


def _declared_readiness_s(service: Mapping[str, Any]) -> float:
    """The longest start or load budget the record declares, or 0 when it declares none."""
    from gateway.engine_contract_view import process_config, residency_config

    process = process_config(service)
    sources: list[tuple[Any, tuple[str, ...]]] = [(process, _READINESS_KEYS)]
    for holder in (process.get("legacy_lifecycle"), service.get("lifecycle")):
        if isinstance(holder, Mapping):
            sources.append((holder.get("driver_config"), _READINESS_KEYS))
    sources.append((residency_config(service).get("load_deadline"), ("max_s",)))
    budgets = [0.0]
    for source, keys in sources:
        if not isinstance(source, Mapping):
            continue
        for key in keys:
            value = source.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                budgets.append(float(value))
    return max(budgets)


def _lifecycle_router_timeout(service: Mapping[str, Any], config: Mapping[str, Any]) -> float | None:
    """How long the router waits for the gateway's lifecycle hop, or None for its default.

    The hop first makes the engine ready (a cold start or a model switch) and
    only then waits the saved reply time for the engine. A short saved reply
    time must not end a cold start, so the router waits the saved time plus the declared readiness budget. The final
    hop itself still uses exactly the saved reply time.
    """
    timeout = _engine_request_settings(config).get("timeout")
    if timeout is None:
        return None
    return timeout + _declared_readiness_s(service)


def _engine_request_settings(
    config: Mapping[str, Any], materializer: Materializer | None = None, *, origin: str | None = None,
) -> dict[str, Any]:
    """What the router sends and how long it waits, from the record's ``headers`` and ``timeout``."""
    out: dict[str, Any] = {}
    if materializer is None:
        headers = engine_headers(config, preview=True)
    else:
        headers = engine_headers(config, resolve=lambda row: materializer.header(row, origin))
    if headers:
        out["extra_headers"] = headers
    timeout = config.get("timeout")
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        out["timeout"] = float(timeout)
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
    if provider in _remote_litellm_providers() and not remote_provider:
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


def _remote_litellm_providers() -> frozenset[str]:
    return frozenset(
        canonical_provider_id(preset.litellm_provider)
        for preset in load_provider_catalog().values()
        if preset.litellm_provider
    )


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


def _credential_bound_to(config: Mapping[str, Any], api_base: str) -> bool:
    """True when the origin recorded for this record's credential is the origin of ``api_base``.

    Only the owner records an origin (see ``service_registry.bind_credential_origins``).
    A record written any other way has none, so its credential reference is not emitted.
    """
    recorded = origin_of(str(config.get("credential_origin") or "").strip())
    return bool(recorded) and recorded == origin_of(api_base)


def _lifecycle_api_key(config: Mapping[str, Any], api_base: str) -> str:
    """The key for a gateway-lifecycle deployment: the gateway's own, only for the gateway's own base."""
    if (
        origin_of(api_base) == origin_of(DEFAULT_GATEWAY_LIFECYCLE_BASE_URL)
        or _credential_bound_to(config, api_base)
    ):
        return str(
            config.get("gateway_lifecycle_api_key_ref")
            or DEFAULT_GATEWAY_LIFECYCLE_API_KEY_REF
        )
    return LOCAL_NO_AUTH_API_KEY


def _api_key_ref(
    config: Mapping[str, Any], secret_refs: Mapping[str, Any], api_base: str = "", *, service_id: str = "",
) -> str:
    if not is_remote_provider_config(config) and not _credential_bound_to(config, api_base):
        # A local record names a key; send it only to the host it was attached for.
        return ""
    env_name = config.get("api_key_env") or config.get("api_key_ref")
    if env_name:
        if is_remote_provider_config(config):
            expected_origin_raw = str(config.get("credential_origin") or "").strip()
            if not expected_origin_raw:
                raise ValueError("provider credential origin is required")
            try:
                expected_origin = credential_origin_from_url(expected_origin_raw)
                actual_origin = credential_origin_from_url(str(config.get("base_url") or ""))
            except Exception:
                raise ValueError("provider credential origin cannot be verified") from None
            if actual_origin != expected_origin:
                raise ValueError("provider credential origin does not match api_base")
        return f"os.environ/{env_name}"
    secret_id = str(config.get("api_key_secret_id") or "").strip()
    if secret_id:
        # A local engine has no provider id; the sidecar names its variable after the service id.
        env_name = secret_env_name_for_provider(str(config.get("provider_id") or service_id), secret_id)
        return f"os.environ/{env_name}"
    if secret_refs.get("api_key") == SECRET_PLACEHOLDER:
        return "os.environ/AZIZAM_DOWNSTREAM_API_KEY"
    return ""


def secret_env_name_for_provider(provider_id: str, secret_id: str) -> str:
    digest = hashlib.sha256(f"{canonical_provider_id(provider_id)}\0{secret_id}".encode("utf-8")).hexdigest()[:16]
    return f"AZIZAM_PROVIDER_SECRET_{digest.upper()}"
