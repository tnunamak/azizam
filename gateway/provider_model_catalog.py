"""Pure projection of configured remote-provider model rows into catalog facts."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from gateway.capabilities import RESOURCE_TIERS, Capability
from gateway import service_registry
from gateway.engine_protocol import description


def remote_provider_model_capabilities(
    service_id: str,
    service: Mapping[str, Any],
) -> list[Capability]:
    """Return model capabilities declared by one remote-provider service.

    The mapping is protocol-driven. It does not infer vendor behavior from the
    provider id; operators can author the same protocol/model data themselves.
    """

    config = service.get("config") if isinstance(service.get("config"), Mapping) else service
    if not isinstance(config, Mapping) or not service_registry.is_remote_provider_config(config):
        return []
    if not bool(service.get("enabled", True)):
        return []
    intent = _intent_for_protocol(str(config.get("protocol") or ""))
    if not intent:
        return []
    out: list[Capability] = []
    for row in _model_rows(config.get("models")):
        if row.get("enabled") is False:
            continue
        model_id = _public_model_id(row)
        if not model_id:
            continue
        traits = _model_traits(row, service_id, model_id)
        out.append(
            Capability(
                intent=intent,
                backend=service_id,
                model=model_id,
                input_modalities=traits["input_modalities"],
                output_modalities=traits["output_modalities"],
                streaming=traits["streaming"],
                resource_tier=traits["resource_tier"],
                tags=("remote",),
            )
        )
    return out


def _model_traits(row: Mapping[str, Any], service_id: str, model_id: str) -> dict[str, Any]:
    """Validate explicit row facts; defaults apply only when a key is absent."""
    location = f"service {service_id!r} model row {model_id!r}"
    traits: dict[str, Any] = {}
    for key in ("input_modalities", "output_modalities"):
        value = row.get(key, ["text"])
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ValueError(f"{location}: {key} must be a list of non-empty strings")
        traits[key] = tuple(value)
    streaming = row.get("streaming", True)
    if type(streaming) is not bool:
        raise ValueError(f"{location}: streaming must be a boolean")
    traits["streaming"] = streaming
    tier = row.get("resource_tier", "expensive")
    if not isinstance(tier, str) or tier not in RESOURCE_TIERS:
        raise ValueError(f"{location}: resource_tier must be one of {RESOURCE_TIERS!r}")
    traits["resource_tier"] = tier
    return traits


def remote_provider_model_metadata(
    service_id: str,
    service: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
    """Return catalog metadata for configured remote-provider model rows."""

    config = service.get("config") if isinstance(service.get("config"), Mapping) else service
    if not isinstance(config, Mapping) or not service_registry.is_remote_provider_config(config):
        return {}
    prices = (config.get("price") or {}).get("models") if isinstance(config.get("price"), Mapping) else {}
    prices = prices if isinstance(prices, Mapping) else {}
    provider_id = service_registry.canonical_provider_id(config.get("provider_id") or service_id)
    out: dict[str, Mapping[str, Any]] = {}
    for row in _model_rows(config.get("models")):
        model_id = _public_model_id(row)
        if not model_id:
            continue
        price = _price_for_model(model_id, row, prices)
        enriched: dict[str, Any] = {
            **dict(row),
            "id": model_id,
            "model_group": model_id,
            "service_id": service_id,
            "provider_id": provider_id,
            "locality": "remote",
        }
        if price is not None:
            enriched["price"] = price
        for key in {model_id, str(row.get("id") or ""), str(row.get("model_group") or ""), str(row.get("upstream_model") or "")}:
            if key:
                out[key] = enriched
    return out


def _intent_for_protocol(protocol: str) -> str | None:
    normalized = protocol.strip().lower()
    matches = [doc for doc in description.load_registry().values()
               if normalized == doc.id or normalized in doc.catalog_aliases]
    if len(matches) != 1:
        return None
    return matches[0].catalog_intent or None


def _model_rows(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [
            {"id": key, **(dict(item) if isinstance(item, Mapping) else {})}
            for key, item in value.items()
        ]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _public_model_id(row: Mapping[str, Any]) -> str:
    return str(row.get("model_group") or row.get("public") or row.get("id") or "").strip()


def _price_for_model(
    model_id: str,
    row: Mapping[str, Any],
    prices: Mapping[str, Any],
) -> str | None:
    raw = row.get("price") if isinstance(row.get("price"), Mapping) else prices.get(model_id)
    if not isinstance(raw, Mapping):
        return None
    input_price = _decimal_price(
        raw.get("input_usd_per_million_tokens")
        if raw.get("input_usd_per_million_tokens") is not None
        else raw.get("input_per_million_usd")
    )
    output_price = _decimal_price(
        raw.get("output_usd_per_million_tokens")
        if raw.get("output_usd_per_million_tokens") is not None
        else raw.get("output_per_million_usd")
    )
    if input_price is None and output_price is None:
        return None
    pieces: list[str] = []
    if input_price is not None:
        pieces.append(f"${_format_decimal(input_price)} input")
    if output_price is not None:
        pieces.append(f"${_format_decimal(output_price)} output")
    return " / ".join(pieces) + " per 1M tokens"


def _decimal_price(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not price.is_finite() or price < 0:
        return None
    return price


def _format_decimal(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
