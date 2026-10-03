"""Provider configuration helpers for the admin API."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from fastapi import status

from gateway import provider_catalog, service_registry
from gateway.litellm_substrate import secret_env_name_for_provider


class ProviderConfigurationError(ValueError):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def provider_secret_id(provider_id: str) -> str:
    return f"provider:{service_registry.canonical_provider_id(provider_id)}:api_key"


def provider_secret_env(provider_id: str) -> str:
    return secret_env_name_for_provider(provider_id, provider_secret_id(provider_id))


def provider_service_from_body(body: Mapping[str, Any]) -> dict[str, Any]:
    preset_id = str(body.get("preset_id") or body.get("id") or "").strip()
    catalog = provider_catalog.load_provider_catalog()
    preset = catalog.get(preset_id)
    if preset is None:
        raise ProviderConfigurationError(
            status.HTTP_400_BAD_REQUEST,
            "unknown_provider_preset",
            f"provider preset {preset_id!r} was not found",
        )
    provider_id = service_registry.canonical_provider_id(str(body.get("provider_id") or preset.id))
    base_url = str(body.get("base_url") or preset.base_url).strip().rstrip("/")
    if not base_url:
        raise ProviderConfigurationError(
            status.HTTP_400_BAD_REQUEST,
            "missing_provider_base_url",
            "base_url is required",
        )
    key_mode = str(body.get("key_mode") or "secret").strip()
    api_key_env = str(body.get("api_key_env") or "").strip()
    secret_id = provider_secret_id(provider_id)
    config: dict[str, Any] = {
        "remote_provider": True,
        "provider_type": "paid-remote",
        "cost_class": "paid-remote",
        "preset_id": preset.id,
        "provider_id": provider_id,
        "protocol": "openai-compatible" if preset.protocol == "openai-chat" else preset.protocol,
        "protocols": [preset.protocol],
        "base_url": base_url,
        "litellm_provider": preset.litellm_provider,
        "auth": dict(preset.auth),
        "auth_test": dict(preset.auth_test),
        "model_listing": dict(preset.model_listing),
        "docs_url": preset.docs_url,
        "key_url": preset.key_url,
        "billing_dimensions": list(preset.billing_dimensions),
        "recommendations": [dict(row) for row in preset.recommendations],
        "models": model_records_from_body(body, provider_id),
        "model_policy_schema_version": service_registry.MODEL_POLICY_SCHEMA_VERSION,
    }
    price = price_config_from_body(body, provider_id)
    _validate_enabled_model_prices(config["models"], price)
    if price:
        config["price"] = price
    if key_mode == "env":
        if not service_registry.ENV_VAR_NAME_RE.fullmatch(api_key_env):
            raise ProviderConfigurationError(
                status.HTTP_400_BAD_REQUEST,
                "invalid_api_key_env",
                "api_key_env must be an environment variable name",
            )
        config["api_key_env"] = api_key_env
    else:
        config["api_key_secret_id"] = secret_id
    return {
        "id": provider_id,
        "name": str(body.get("name") or preset.display_name),
        "kind": "provider",
        "enabled": bool(body.get("enabled", True)),
        "capabilities": ["openai"] if preset.protocol == "openai-chat" else [preset.protocol],
        "config": config,
        "secret_refs": ({"api_key_env": api_key_env} if key_mode == "env" else {"api_key_secret_id": secret_id}),
    }


def model_records_from_body(body: Mapping[str, Any], provider_id: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    raw_models = body.get("models") or []
    if not isinstance(raw_models, list):
        raise ProviderConfigurationError(
            status.HTTP_400_BAD_REQUEST,
            "invalid_provider_models",
            "models must be a list",
        )
    for item in raw_models:
        if not isinstance(item, Mapping):
            continue
        upstream = str(item.get("upstream_model") or item.get("id") or "").strip()
        if not upstream:
            continue
        public_id = str(item.get("model_group") or item.get("id") or upstream).strip()
        if not public_id.startswith(f"{provider_id}/"):
            public_id = f"{provider_id}/{public_id}"
        row = {"id": public_id, "model_group": public_id, "upstream_model": upstream}
        enabled = item.get("enabled")
        if enabled is not None:
            row["enabled"] = bool(enabled)
        records.append(row)
    return records


def price_config_from_body(body: Mapping[str, Any], provider_id: str) -> dict[str, Any]:
    models: dict[str, dict[str, Any]] = {}
    raw_models = body.get("models") or []
    if not isinstance(raw_models, list):
        return {}
    for item in raw_models:
        if not isinstance(item, Mapping) or not isinstance(item.get("price"), Mapping):
            continue
        public_id = str(item.get("model_group") or item.get("id") or item.get("upstream_model") or "").strip()
        if not public_id:
            continue
        if not public_id.startswith(f"{provider_id}/"):
            public_id = f"{provider_id}/{public_id}"
        models[public_id] = dict(item["price"])
    return {"models": models} if models else {}


def _validate_enabled_model_prices(models: list[dict[str, Any]], price: Mapping[str, Any]) -> None:
    prices = price.get("models") if isinstance(price.get("models"), Mapping) else {}
    for model in models:
        if model.get("enabled") is False:
            continue
        model_id = str(model.get("model_group") or model.get("id") or "").strip()
        row = prices.get(model_id) if isinstance(prices, Mapping) else None
        if not isinstance(row, Mapping):
            raise ProviderConfigurationError(
                status.HTTP_400_BAD_REQUEST,
                "missing_model_price",
                f"enabled paid model {model_id!r} requires editable input and output prices",
            )
        input_price = _finite_non_negative_price(
            row.get("input_usd_per_million_tokens", row.get("input_per_million_usd"))
        )
        output_price = _finite_non_negative_price(
            row.get("output_usd_per_million_tokens", row.get("output_per_million_usd"))
        )
        if input_price is None or output_price is None:
            raise ProviderConfigurationError(
                status.HTTP_400_BAD_REQUEST,
                "missing_model_price",
                f"enabled paid model {model_id!r} requires finite non-negative input and output prices",
            )


def _finite_non_negative_price(value: Any) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite() or price < 0:
        return None
    return price


def expected_generation(body: Mapping[str, Any]) -> int | None:
    raw = body.get("expected_generation", None)
    if raw in (None, ""):
        return None
    return int(raw)
