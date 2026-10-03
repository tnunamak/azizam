from __future__ import annotations

import asyncio

import httpx
import pytest

from gateway.provider_catalog import load_provider_catalog
from gateway.provider_listing import ProviderListingError, probe_provider, project_models


def _config(preset_id: str) -> dict:
    preset = load_provider_catalog()[preset_id]
    return {
        "provider_id": "test-provider", "base_url": "https://provider.invalid/v1",
        "auth": preset.auth, "model_listing": preset.model_listing,
        "auth_test": preset.auth_test,
    }


def test_real_http_probe_authenticates_before_listing_and_normalizes_price_units() -> None:
    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer synthetic-key-4f2a"
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {"label": "synthetic-key-4f2a"}})
        return httpx.Response(200, json={"data": [{
            "id": "test/model", "name": "Test model",
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
        }]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await probe_provider(_config("openrouter"), "synthetic-key-4f2a", client=client)

    result = asyncio.run(run())
    assert paths == ["/v1/key", "/v1/models"]
    assert result["reachable"] is True and result["checked_at"] > 0
    assert result["models"][0]["price_input"] == "3.000000"
    assert result["models"][0]["price_output"] == "15.000000"
    assert "synthetic-key-4f2a" not in str(result)


def test_listing_shapes_and_unknown_prices_are_data_driven() -> None:
    catalog = load_provider_catalog()
    together = project_models([{"id": "model", "display_name": "Example", "pricing": {"input": 0.3, "output": 0.6}}], catalog["together"].model_listing, provider_id="cloud")
    assert together[0]["price_input"] == "0.3"
    assert together[0]["name"] == "Example"
    typesafe = project_models({"models": [{"name": "jev-latest"}]}, catalog["typesafe"].model_listing, provider_id="cloud")
    assert typesafe[0]["upstream_model"] == "jev-latest"
    assert typesafe[0]["price_input"] is None
    assert typesafe[0]["price"] == {}


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_upstream_auth_and_error_bodies_never_escape(status: int) -> None:
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(status, json={"error": "synthetic-key-4f2a"}))) as client:
            await probe_provider(_config("openrouter"), "synthetic-key-4f2a", client=client)
    with pytest.raises(ProviderListingError) as error:
        asyncio.run(run())
    assert "synthetic-key-4f2a" not in str(error.value)


def test_auth_probe_failure_stops_before_public_model_catalog() -> None:
    paths = []
    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(401)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await probe_provider(_config("openrouter"), "synthetic-key-4f2a", client=client)
    with pytest.raises(ProviderListingError):
        asyncio.run(run())
    assert paths == ["/v1/key"]


def test_invalid_prices_remain_unknown_and_zero_is_explicit() -> None:
    listing = load_provider_catalog()["openrouter"].model_listing
    for rate, expected in [("NaN", None), ("-1", None), ("0", "0"), (None, None)]:
        model = project_models({"data": [{"id": "m", "pricing": {"prompt": rate}}]}, listing, provider_id="p")[0]
        assert model["price_input"] == expected
