"""Provider prices must not be substituted with another endpoint's SDK rates."""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from unittest.mock import patch

import pytest

from gateway.provider_catalog import load_provider_catalog, normalize_provider_preset
from tests.support.access import plan_for  # noqa: E402
from gateway.providers import (
    BudgetExceeded, Cost, ProviderRef, estimate_chat_cost, extract_provider_cost,
    reserve_provider_spend, settle_provider_spend,
)


def _provider(*, price=None, base_url="https://custom.invalid/v1", preset_id="generic-openai"):
    return ProviderRef(
        id="custom", base_url=base_url, protocols=("openai-chat",),
        api_key_env="PRICING_REGRESSION_KEY", cost_class="paid-remote",
        price={"input_usd_per_million_tokens": "100", "output_usd_per_million_tokens": "100"} if price is None else price,
        config={"preset_id": preset_id, "credential_origin": "https://custom.invalid:443"},
    )


def _response(**usage):
    return {"model": "gpt-4.1-mini", "usage": {"prompt_tokens": 1000, "completion_tokens": 1000, "total_tokens": 2000, **usage}}


def test_custom_endpoint_uses_its_prices_instead_of_installed_sdk_vendor_prices(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    actual = extract_provider_cost(_provider(), body=_response())
    assert actual.usd == Decimal("0.200000000")
    assert actual.source == "price-table"


def test_sequential_custom_requests_cannot_under_settle_the_one_dollar_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("PRICING_REGRESSION_KEY", "synthetic-pricing-key")
    provider = _provider()
    profile = plan_for("paid", paid=sorted({provider.id}))
    ledger = tmp_path / "ledger.db"
    accepted = 0
    for _ in range(10):
        try:
            reservation = reserve_provider_spend(profile, provider, Cost(Decimal("0.20"), "price-table", provider.id), path=ledger)
        except BudgetExceeded:
            break
        cost = extract_provider_cost(provider, body=_response())
        settle_provider_spend(reservation, cost, path=ledger)
        accepted += 1
    assert accepted == 5
    with sqlite3.connect(ledger) as conn:
        rows = conn.execute("SELECT actual_usd, status FROM provider_spend").fetchall()
    assert sum(Decimal(cost) for cost, _ in rows) == Decimal("1.00")
    assert all(status == "settled" for _, status in rows)


def test_owned_prices_override_sdk_even_on_declared_official_api():
    provider = _provider(base_url="https://api.openai.com/v1", preset_id="openai")
    with patch("gateway.providers._litellm_full_usage_cost", side_effect=AssertionError("owned prices must win")):
        assert extract_provider_cost(provider, body=_response()).usd == Decimal("0.200000000")


@pytest.mark.parametrize("url,preset", [
    ("https://custom.invalid/v1", "openai"),
    ("https://api.openai.com.evil.invalid/v1", "openai"),
    ("http://api.openai.com/v1", "openai"),
    ("https://api.openai.com:8443/v1", "openai"),
    ("https://api.openai.com/other", "openai"),
    ("https://user:password@api.openai.com/v1", "openai"),
    ("api.openai.com/v1", "openai"),
    ("", "openai"),
    ("https://api.openai.com/v1", "generic-openai"),
])
def test_sdk_prices_require_catalog_authority_and_exact_official_destination(url, preset):
    with patch("gateway.providers._litellm_full_usage_cost", side_effect=AssertionError("unapproved SDK pricing")):
        cost = extract_provider_cost(_provider(price={}, base_url=url, preset_id=preset), body=_response())
    assert cost.usd is None


def test_official_sdk_fallback_normalizes_host_case_and_default_port(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    provider = _provider(price={}, base_url="https://API.OPENAI.COM:443/v1/", preset_id="openai")
    cost = extract_provider_cost(provider, body=_response())
    assert cost.usd == Decimal("0.002000000")
    assert cost.source == "litellm"


def test_custom_unknown_prices_are_denied_even_without_caps(tmp_path, monkeypatch):
    monkeypatch.setenv("PRICING_REGRESSION_KEY", "synthetic-pricing-key")
    provider = _provider(price={})
    profile = plan_for("uncapped", paid=sorted({provider.id}))
    estimate = estimate_chat_cost(provider, {"model": "gpt-4.1-mini", "messages": [], "max_tokens": 1000})
    assert estimate.usd is None
    with pytest.raises(BudgetExceeded, match="known model prices"):
        reserve_provider_spend(profile, provider, estimate, path=tmp_path / "ledger.db")
    assert not (tmp_path / "ledger.db").exists()


def test_partial_owned_prices_never_fall_back_to_official_sdk_rates():
    provider = _provider(price={"input_usd_per_million_tokens": "100"}, base_url="https://api.openai.com/v1", preset_id="openai")
    with patch("gateway.providers._litellm_full_usage_cost", side_effect=AssertionError("partial owned prices cannot borrow SDK rates")):
        assert extract_provider_cost(provider, body=_response()).usd is None
        assert estimate_chat_cost(provider, {"model": "gpt-4.1-mini", "messages": []}).source == "missing-provider-price"


def test_cached_tokens_use_the_custom_provider_cache_read_price():
    provider = _provider(price={"input_usd_per_million_tokens": "100", "output_usd_per_million_tokens": "100", "cache_read_input_usd_per_million_tokens": "10"})
    actual = extract_provider_cost(provider, body=_response(prompt_tokens_details={"cached_tokens": 250}))
    assert actual.usd == Decimal("0.177500000")


@pytest.mark.parametrize("usage", [
    {"prompt_tokens_details": {"cached_tokens": 100}},
    {"prompt_tokens_details": {"audio_tokens": 100}},
    {"completion_tokens_details": {"reasoning_tokens": 100}},
    {"future_billable_units": 1},
])
def test_unpriced_usage_dimensions_remain_unresolved_instead_of_borrowing_sdk_prices(usage):
    provider = _provider(base_url="https://api.openai.com/v1", preset_id="openai")
    assert extract_provider_cost(provider, body=_response(**usage), headers={"x-litellm-response-cost": "0.002"}).usd is None


def test_sdk_header_cost_is_not_authority_for_a_custom_provider():
    assert extract_provider_cost(_provider(), headers={"x-litellm-response-cost": "0.002"}).usd is None


@pytest.mark.parametrize("message", [
    {"annotations": [{"type": "url_citation", "url_citation": {"url": "https://example.invalid"}}]},
    {"audio": {"id": "generated-audio"}},
])
def test_non_token_response_billing_is_not_settled_from_own_text_rates(message):
    body = {**_response(), "choices": [{"message": message}]}
    assert extract_provider_cost(_provider(), body=body).usd is None


def test_declared_non_token_fee_cannot_be_ignored_in_estimates_or_settlement():
    provider = _provider(price={"input_usd_per_million_tokens": "100", "output_usd_per_million_tokens": "100", "request_usd": "0.05"})
    assert extract_provider_cost(provider, body=_response()).usd is None
    assert estimate_chat_cost(provider, {"model": "gpt-4.1-mini", "messages": [], "max_tokens": 1000}).source == "missing-provider-price"


def test_missing_usage_is_not_a_zero_bill_even_with_zero_base_rates():
    provider = _provider(price={"input_usd_per_million_tokens": "0", "output_usd_per_million_tokens": "0", "cache_read_input_usd_per_million_tokens": "100"})
    assert extract_provider_cost(provider, body={"model": "gpt-4.1-mini", "usage": {}}).usd is None
    assert extract_provider_cost(provider, body={"model": "gpt-4.1-mini", "usage": {"prompt_tokens": 0, "completion_tokens": 0}}).usd == Decimal("0")


def test_actual_provider_reported_bill_overrides_own_prices_and_sdk_header():
    cost = extract_provider_cost(_provider(), body=_response(cost="0.27"), headers={"x-litellm-response-cost": "0.002"})
    assert cost.usd == Decimal("0.27")
    assert cost.source == "provider-reported"


def test_catalog_sdk_pricing_declaration_is_explicit_and_requires_https():
    catalog = load_provider_catalog()
    assert catalog["openai"].sdk_pricing == {"api_base": "https://api.openai.com/v1"}
    assert catalog["generic-openai"].sdk_pricing is None
    raw = catalog["openai"].to_dict()
    raw["sdk_pricing"] = {"api_base": "http://api.openai.com/v1"}
    with pytest.raises(ValueError, match="official HTTPS API"):
        normalize_provider_preset(raw)
