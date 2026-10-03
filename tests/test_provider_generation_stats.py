from __future__ import annotations

import asyncio
from decimal import Decimal

import httpx

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


def _run(coro):
    return asyncio.run(coro)


def test_lookup_generation_cost_reads_configured_total_cost(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"total_cost": "0.0015"}})

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lookup_generation_cost(
                _provider(cost_class="usage-metered"),
                "gen-123",
                client=client,
            )

    cost = _run(call())

    assert cost.usd is not None
    assert cost.usd == Decimal("0.0015")
    assert cost.source == "provider-generation-stats"
    assert cost.cost_class == "usage-metered"
    assert str(seen[0].url) == "https://provider.test/api/v1/generation?id=gen-123"
    assert seen[0].headers["authorization"] == "Bearer test-key"


def test_lookup_generation_cost_accepts_zero_cost(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"total_cost": 0}})

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lookup_generation_cost(_provider(), "gen-free", client=client)

    cost = _run(call())

    assert cost.usd == 0
    assert cost.source == "provider-generation-stats"


def test_lookup_generation_cost_unknown_when_cost_missing(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {}})

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lookup_generation_cost(_provider(), "gen-missing", client=client)

    cost = _run(call())

    assert cost.usd is None
    assert cost.source == "provider-generation-stats-unavailable"


def test_lookup_generation_cost_unknown_when_response_is_malformed(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lookup_generation_cost(_provider(), "gen-bad-json", client=client)

    cost = _run(call())

    assert cost.usd is None
    assert cost.source == "provider-generation-stats-unavailable"


def test_lookup_generation_cost_unknown_on_error_status(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "missing"})

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lookup_generation_cost(_provider(), "gen-missing", client=client)

    cost = _run(call())

    assert cost.usd is None
    assert cost.source == "provider-generation-stats-unavailable"


def test_lookup_generation_cost_unknown_on_timeout(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lookup_generation_cost(_provider(), "gen-timeout", client=client)

    cost = _run(call())

    assert cost.usd is None
    assert cost.source == "provider-generation-stats-unavailable"


def test_lookup_generation_cost_does_not_follow_redirects(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://elsewhere.test"})

    async def call():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            return await lookup_generation_cost(_provider(), "gen-redirect", client=client)

    cost = _run(call())

    assert cost.usd is None
    assert cost.source == "provider-generation-stats-unavailable"


def test_lookup_generation_cost_unknown_for_nonfinite_reported_costs(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    async def call(value: str):
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"total_cost": value}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lookup_generation_cost(_provider(), f"gen-{value}", client=client)

    for value in ("Infinity", "-Infinity", "NaN"):
        cost = _run(call(value))
        assert cost.usd is None
        assert cost.source == "provider-generation-stats-unavailable"


def test_lookup_generation_cost_unknown_for_too_large_reported_cost(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"total_cost": "1e1000"}})

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lookup_generation_cost(_provider(), "gen-huge", client=client)

    cost = _run(call())

    assert cost.usd is None
    assert cost.source == "provider-generation-stats-unavailable"
