"""Cheap provider checks and model-list projections driven by catalog data."""

from __future__ import annotations

import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx


class ProviderListingError(ValueError):
    """A safe operator-facing error, without upstream bodies or credentials."""

    def __init__(self, message: str, *, code: str = "provider_connection_failed", status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def validate_provider_base_url(value: str) -> str:
    base = value.strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname:
        raise ProviderListingError("Enter an HTTP or HTTPS provider endpoint.")
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ProviderListingError("The endpoint must not contain credentials, a query, or a fragment.")
    try:
        parsed.port
    except ValueError:
        raise ProviderListingError("Enter a valid provider endpoint port.") from None
    return base


def canonical_provider_base_url(value: str) -> str:
    base = validate_provider_base_url(value)
    parsed = urlsplit(base)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80
    path = (parsed.path or "").rstrip("/")
    authority_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{authority_host}:{port}{path}"


def provider_auth_headers(config: Mapping[str, Any], api_key: str) -> dict[str, str]:
    if not api_key or any(ch in api_key for ch in "\r\n"):
        raise ProviderListingError("Set a valid provider key before running Gateway check.")
    auth = config.get("auth") if isinstance(config.get("auth"), Mapping) else {}
    header = str(auth.get("header") or "Authorization")
    prefix = str(auth.get("prefix") if auth.get("prefix") is not None else "Bearer")
    if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header) or any(ch in prefix for ch in "\r\n"):
        raise ProviderListingError("The provider authentication settings are invalid.")
    return {header: f"{prefix} {api_key}" if prefix else api_key}


def _at_path(value: Any, path: str) -> Any:
    for key in path.split(".") if path else ():
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _price(value: Any, rule: Any) -> str | None:
    if not isinstance(rule, Mapping) or not isinstance(rule.get("path"), str):
        return None
    raw = _at_path(value, rule["path"])
    if raw is None or isinstance(raw, bool):
        return None
    try:
        rate, scale = Decimal(str(raw)), Decimal(str(rule.get("scale", "1")))
        if not rate.is_finite() or not scale.is_finite() or rate < 0 or scale <= 0:
            return None
        return format(rate * scale, "f")
    except (InvalidOperation, ValueError):
        return None


def project_models(
    payload: Any,
    listing: Mapping[str, Any],
    *,
    provider_id: str,
    deny_substring: str | None = None,
) -> list[dict[str, Any]]:
    rows = _at_path(payload, str(listing.get("items_path", "data")))
    if not isinstance(rows, list):
        raise ProviderListingError("The provider returned an unsupported model list.", code="provider_model_list_invalid")
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    price_rules = listing.get("prices") if isinstance(listing.get("prices"), Mapping) else {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if deny_substring and deny_substring in str(row):
            continue
        model_id = _at_path(row, str(listing.get("id_path") or "id"))
        if not isinstance(model_id, str) or not model_id.strip() or model_id in seen:
            continue
        seen.add(model_id)
        name = _at_path(row, str(listing.get("name_path") or listing.get("id_path") or "id"))
        prices = {key: _price(row, rule) for key, rule in price_rules.items()}
        # Unknown values stay absent from the usable price table, never become zero.
        price = {key: value for key, value in prices.items() if value is not None}
        public_id = f"{provider_id}/{model_id}"
        models.append({
            "id": public_id,
            "upstream_model": model_id,
            "name": name if isinstance(name, str) and name else model_id,
            "enabled": False,
            "price": price,
            "price_input": price.get("input_usd_per_million_tokens"),
            "price_output": price.get("output_usd_per_million_tokens"),
        })
    return models


async def probe_provider(
    config: Mapping[str, Any], api_key: str, *, client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    base = validate_provider_base_url(str(config.get("base_url") or ""))
    listing = config.get("model_listing")
    if not isinstance(listing, Mapping):
        raise ProviderListingError("Configure a model-listing endpoint before running Gateway check.")
    auth_headers = provider_auth_headers(config, api_key)
    owned_client = client is None
    client = client or httpx.AsyncClient(timeout=10, follow_redirects=False, trust_env=False)

    async def get_json(spec: Mapping[str, Any]) -> Any:
        path = str(spec.get("path") or "")
        if not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
            raise ProviderListingError("The model-listing path is invalid.")
        if str(spec.get("method") or "GET").upper() != "GET":
            raise ProviderListingError("Gateway check only supports a read-only GET endpoint.")
        headers = httpx.Headers(spec.get("headers") if isinstance(spec.get("headers"), Mapping) else {})
        headers.update(auth_headers)
        response = await client.get(f"{base}{path}", headers=headers, timeout=10, follow_redirects=False)
        if response.status_code in {401, 403}:
            raise ProviderListingError("The provider rejected the key. Check the key and its permissions.", code="provider_auth_failed")
        if response.status_code == 429:
            raise ProviderListingError("The provider rate limited Gateway check. Try again later.")
        if response.status_code != 200:
            raise ProviderListingError(f"The provider returned HTTP {response.status_code}. Check the endpoint and try again.")
        try:
            return response.json()
        except ValueError:
            raise ProviderListingError("The provider returned an unsupported model list.", code="provider_model_list_invalid") from None

    try:
        auth_test = config.get("auth_test")
        if isinstance(auth_test, Mapping) and auth_test.get("path"):
            await get_json(auth_test)
        payload = await get_json(listing)
        provider_id = str(config.get("provider_id") or "")
        models = project_models(payload, listing, provider_id=provider_id, deny_substring=api_key)
        more = bool(_at_path(payload, str(listing.get("has_more_path") or "has_more")))
        return {
            "ok": True, "reachable": True, "checked_at": int(time.time()),
            "models": models, "partial": more,
            "message": f"Listed {len(models)} models." + (" The provider has more models on another page." if more else ""),
        }
    except httpx.TimeoutException:
        raise ProviderListingError("Gateway check timed out. Check the endpoint and try again.") from None
    except httpx.HTTPError:
        raise ProviderListingError("Gateway check could not reach the provider. Check the endpoint and try again.") from None
    finally:
        if owned_client:
            await client.aclose()
