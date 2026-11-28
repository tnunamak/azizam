from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal

import httpx

from gateway import admin_store, secret_store
from gateway.provider_catalog import load_provider_catalog, normalize_provider_preset
from gateway.provider_configuration import provider_service_from_body
from gateway.provider_listing import canonical_provider_base_url
from gateway.provider_reconciliation import lookup_generation_cost
from gateway.providers import ProviderRef
from gateway.providers import resolve_provider_api_key


def _provider(**overrides: object) -> ProviderRef:
    config = {
        "generation_stats": {
            "path": "/generation",
            "id_param": "id",
            "cost_path": ["data", "total_cost"],
        },
        "auth": {"header": "Authorization", "prefix": "Bearer"},
        "credential_origin": "https://provider.test:443",
    }
    config.update(overrides.pop("config", {}) if isinstance(overrides.get("config"), dict) else {})
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




def test_openrouter_generation_stats_is_catalog_data_and_round_trips_without_key():
    preset = load_provider_catalog()["openrouter"]
    assert preset.generation_stats == {
        "path": "/generation",
        "id_param": "id",
        "cost_path": ["data", "total_cost"],
    }

    service = provider_service_from_body({
        "preset_id": "openrouter",
        "api_key": "sk-should-not-appear",
        "models": [
            {
                "id": "anthropic/claude-sonnet",
                "price": {
                    "input_usd_per_million_tokens": "3",
                    "output_usd_per_million_tokens": "15",
                },
            }
        ],
    })

    assert service["config"]["generation_stats"] == preset.generation_stats
    assert "sk-should-not-appear" not in repr(service["config"]["generation_stats"])


def test_generation_stats_schema_rejects_invalid_declarations():
    raw = load_provider_catalog()["openrouter"].to_dict()
    raw["generation_stats"] = {"path": "https://provider.test/generation", "id_param": "id", "cost_path": ["data"]}
    try:
        normalize_provider_preset(raw)
    except ValueError as exc:
        assert "generation_stats.path" in str(exc)
    else:  # pragma: no cover - assertion helper for unittest-free runner
        raise AssertionError("invalid generation_stats path was accepted")

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




def test_lookup_generation_cost_uses_configured_auth_header_without_vendor_branch(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "test-key")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"total_cost": "0.002"}})

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lookup_generation_cost(
                _provider(config={
                    "generation_stats": {
                        "path": "/generation",
                        "id_param": "id",
                        "cost_path": ["data", "total_cost"],
                    },
                    "auth": {"header": "x-api-key", "prefix": ""},
                }),
                "gen-header",
                client=client,
            )

    cost = _run(call())

    assert cost.usd == Decimal("0.002")
    assert seen[0].headers["x-api-key"] == "test-key"
    assert "authorization" not in seen[0].headers

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
    for destination in (
        "https://provider.test/redirected",
        "https://elsewhere.test/redirected",
    ):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request, seen=seen, destination=destination) -> httpx.Response:
            seen.append(request)
            if len(seen) == 1:
                return httpx.Response(302, headers={"location": destination})
            return httpx.Response(200, json={"data": {"total_cost": "0.05"}})

        async def call(handler=handler):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                follow_redirects=True,
            ) as client:
                return await lookup_generation_cost(_provider(), "gen-redirect", client=client)

        cost = _run(call())

        assert [(r.method, str(r.url)) for r in seen] == [
            ("GET", "https://provider.test/api/v1/generation?id=gen-redirect")
        ]
        assert seen[0].headers["authorization"] == "Bearer test-key"
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


def test_lookup_generation_cost_uses_stored_encrypted_key(tmp_path, monkeypatch):
    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    monkeypatch.setattr(admin_store, "DB_PATH", admin_db)
    secret_store.put_provider_secret(
        "provider:remote:api_key",
        "stored-stats-key",
        db_path=admin_db,
        approved_origin="https://provider.test/api/v1",
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"total_cost": "0.0042"}})

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lookup_generation_cost(
                _provider(api_key_env=None, api_key_secret_id="provider:remote:api_key"),
                "gen-stored",
                client=client,
            )

    cost = _run(call())

    assert cost.usd == Decimal("0.0042")
    assert seen[0].headers["authorization"] == "Bearer stored-stats-key"


def test_lookup_generation_cost_refuses_stored_key_after_origin_retarget(tmp_path, monkeypatch):
    admin_db = tmp_path / "admin.db"
    admin_store.init_db(admin_db)
    monkeypatch.setattr(admin_store, "DB_PATH", admin_db)
    secret_store.put_provider_secret(
        "provider:remote:api_key",
        "stored-stats-key",
        db_path=admin_db,
        approved_origin="https://provider.test/api/v1",
    )
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": {"total_cost": "0.0042"}})

    async def call():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await lookup_generation_cost(
                _provider(
                    base_url="https://evil.test/api/v1",
                    api_key_env=None,
                    api_key_secret_id="provider:remote:api_key",
                ),
                "gen-stored",
                client=client,
            )

    cost = _run(call())

    assert cost.usd is None
    assert cost.source == "provider-generation-stats-unavailable"
    assert seen == []


def test_env_provider_key_refuses_stale_origin_metadata(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "env-key")
    provider = _provider(
        base_url="https://evil.test/api/v1",
        api_key_env="PROVIDER_STATS_KEY",
        config={
            "credential_origin": "https://provider.test:443",
            "generation_stats": {
                "path": "/generation",
                "id_param": "id",
                "cost_path": ["data", "total_cost"],
            },
        },
    )

    assert resolve_provider_api_key(provider) == ""


def test_env_provider_key_refuses_missing_origin_metadata(monkeypatch):
    monkeypatch.setenv("PROVIDER_STATS_KEY", "env-key")
    config = dict(_provider().config)
    config.pop("credential_origin", None)
    provider = replace(_provider(), config=config)

    assert resolve_provider_api_key(provider) == ""


def test_ipv6_canonical_provider_base_url_is_idempotent():
    url = canonical_provider_base_url("https://[::1]:8443/v1")

    assert url == "https://[::1]:8443/v1"
    assert canonical_provider_base_url(url) == url
