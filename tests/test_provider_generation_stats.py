from __future__ import annotations

import httpx
import pytest

from gateway.provider_reconciliation import lookup_generation_cost
from gateway.providers import ProviderRef


def _provider(**overrides: object) -> ProviderRef:
    config = {
        "generation_stats": {
            "path": "/generation",
            "id_param": "id",
            "cost_path": ["data", "total_cost"],
        }
    }
    data = {
        "id": "remote",
        "base_url": "https://provider.test/api/v1",
        "protocols": ("openai-compatible",),
        "api_key_env": "PROVIDER_STATS_KEY",
        "cost_class": "paid-remote",
        "price": {},
        "config": config,
    }
    data.update(overrides)
    return ProviderRef(**data)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_lookup_generation_cost_reads_configured_total_cost(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"total_cost": "0.0015"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cost = await lookup_generation_cost(_provider(), "gen-123", client=client)

    assert cost.usd is not None
    assert str(cost.usd) == "0.0015"
    assert cost.source == "provider-generation-stats"
    assert str(seen[0].url) == "https://provider.test/api/v1/generation?id=gen-123"
    assert seen[0].headers["authorization"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_lookup_generation_cost_accepts_zero_cost(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"total_cost": 0}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cost = await lookup_generation_cost(_provider(), "gen-free", client=client)

    assert cost.usd == 0
    assert cost.source == "provider-generation-stats"


@pytest.mark.asyncio
async def test_lookup_generation_cost_unknown_when_cost_missing(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cost = await lookup_generation_cost(_provider(), "gen-missing", client=client)

    assert cost.usd is None
    assert cost.source == "provider-generation-stats-unavailable"


@pytest.mark.asyncio
async def test_lookup_generation_cost_unknown_when_response_is_malformed(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cost = await lookup_generation_cost(_provider(), "gen-bad-json", client=client)

    assert cost.usd is None
    assert cost.source == "provider-generation-stats-unavailable"


@pytest.mark.asyncio
async def test_lookup_generation_cost_unknown_on_error_status(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "missing"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cost = await lookup_generation_cost(_provider(), "gen-missing", client=client)

    assert cost.usd is None
    assert cost.source == "provider-generation-stats-unavailable"


@pytest.mark.asyncio
async def test_lookup_generation_cost_unknown_on_timeout(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        cost = await lookup_generation_cost(_provider(), "gen-timeout", client=client)

    assert cost.usd is None
    assert cost.source == "provider-generation-stats-unavailable"


@pytest.mark.asyncio
async def test_lookup_generation_cost_does_not_follow_redirects(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://elsewhere.test"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    ) as client:
        cost = await lookup_generation_cost(_provider(), "gen-redirect", client=client)

    assert cost.usd is None
    assert cost.source == "provider-generation-stats-unavailable"
