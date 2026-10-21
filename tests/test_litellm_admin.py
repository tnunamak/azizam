"""The thin LiteLLM virtual-key admin client against a fake sidecar /key/* API.

These pin the transport contract: master-key auth header, request shapes,
response normalization, and the typed error mapping the admin endpoints rely
on. The real client talks over httpx to the FK litellm-admin protocol fake; a
call the fake does not expect fails the test at teardown.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from gateway.backends.llm_execution import LiteLlmRuntimeConfig
from gateway.backends.litellm_admin import LiteLLMAdminError, LiteLLMKeyAdminClient
from tests.support.fake_upstream import FakeUpstream, FakeUpstreams, fake_upstreams, scenario_from_data  # noqa: F401

MASTER_KEY = "sk-master"
PROVENANCE = {"source": "hand-written", "reference": "https://docs.litellm.ai/docs/proxy/virtual_keys"}


def _sidecar(fakes: FakeUpstreams, method: str, path: str, response: dict, *, query: dict | None = None) -> FakeUpstream:
    match: dict[str, Any] = {"method": method, "path": path, "headers": {"x-litellm-key": f"Bearer {MASTER_KEY}"}}
    if query is not None:
        match["query"] = query
    scenario = scenario_from_data({
        "protocol": "litellm-admin", "name": f"admin-{method.lower()}{path.replace('/', '-')}", "provenance": PROVENANCE,
        "exchanges": [{"id": "call", "match": match, "response": response}],
    })
    return fakes.asgi(scenario, host="litellm.fk.test")


def _client(base_url: str = "http://litellm.fk.test", *, enabled: bool = True, api_key: str = MASTER_KEY) -> LiteLLMKeyAdminClient:
    return LiteLLMKeyAdminClient(LiteLlmRuntimeConfig(enabled=enabled, base_url=base_url, api_key=api_key, service_id="litellm"))


def test_list_keys_sends_master_key_and_normalizes(fake_upstreams) -> None:
    fake = _sidecar(fake_upstreams, "GET", "/key/list", {"json": {"keys": [{"key_name": "sk-...abcd", "key_alias": "batch", "spend": 1.5}]}},
                    query={"page": "1", "size": "100", "return_full_object": "true"})

    keys = asyncio.run(_client().list_keys())

    assert keys == [{"key_name": "sk-...abcd", "key_alias": "batch", "spend": 1.5}]
    [call] = fake.calls("call")
    assert call.headers["content-type"] == "application/json" and call.headers["accept"] == "application/json"


def test_list_keys_accepts_bare_list(fake_upstreams) -> None:
    _sidecar(fake_upstreams, "GET", "/key/list", {"json": [{"key_alias": "x"}]})
    assert asyncio.run(_client().list_keys()) == [{"key_alias": "x"}]


def test_list_keys_coerces_string_rows(fake_upstreams) -> None:
    _sidecar(fake_upstreams, "GET", "/key/list", {"json": {"keys": ["sk-abc123"]}})
    assert asyncio.run(_client().list_keys()) == [{"token": "sk-abc123", "key_name": "sk-abc123"}]


def test_generate_key_returns_secret_and_posts_spec(fake_upstreams) -> None:
    fake = _sidecar(fake_upstreams, "POST", "/key/generate", {"json": {"key": "sk-new-secret", "key_alias": "batch"}})

    created = asyncio.run(_client().generate_key({"key_alias": "batch", "max_budget": 5.0}))

    assert created == {"key": "sk-new-secret", "key_alias": "batch"}
    [call] = fake.calls("call")
    assert call.json == {"key_alias": "batch", "max_budget": 5.0}


def test_generate_key_without_key_field_is_error(fake_upstreams) -> None:
    _sidecar(fake_upstreams, "POST", "/key/generate", {"json": {"key_alias": "batch"}})

    with pytest.raises(LiteLLMAdminError) as error:
        asyncio.run(_client().generate_key({"key_alias": "batch"}))

    assert (error.value.status_code, error.value.code) == (502, "litellm_bad_response")


def test_delete_keys_posts_keys(fake_upstreams) -> None:
    fake = _sidecar(fake_upstreams, "POST", "/key/delete", {"json": {"deleted_keys": ["sk-1"]}})

    result = asyncio.run(_client().delete_keys(keys=["sk-1"]))

    assert result == {"deleted_keys": ["sk-1"]}
    [call] = fake.calls("call")
    assert call.json == {"keys": ["sk-1"]}


def test_delete_without_keys_or_aliases_raises_before_any_call(fake_upstreams) -> None:
    fake = _sidecar(fake_upstreams, "POST", "/key/delete", {"json": {}})
    fake.waive("call")

    with pytest.raises(LiteLLMAdminError) as error:
        asyncio.run(_client().delete_keys())

    assert error.value.status_code == 400
    assert fake.requests == []


def test_disabled_substrate_raises_unconfigured_without_a_call() -> None:
    with pytest.raises(LiteLLMAdminError) as error:
        asyncio.run(_client(enabled=False).list_keys())
    assert (error.value.code, error.value.status_code) == ("litellm_unconfigured", 503)


def test_missing_master_key_raises_unconfigured_without_a_call(fake_upstreams) -> None:
    """No illegal empty 'Bearer ' header reaches httpx, so this is not reported as 'unreachable'."""
    fake = _sidecar(fake_upstreams, "GET", "/key/list", {"json": {}})
    fake.waive("call")

    with pytest.raises(LiteLLMAdminError) as error:
        asyncio.run(_client(api_key="").list_keys())

    assert (error.value.code, error.value.status_code) == ("litellm_unconfigured", 503)
    assert "master key" in error.value.message.lower()
    assert fake.requests == []


def test_unreachable_sidecar_maps_to_503(fake_upstreams) -> None:
    """The peer drops the connection before answering."""
    _sidecar(fake_upstreams, "GET", "/key/list", {"disconnect_before_headers": True})

    with pytest.raises(LiteLLMAdminError) as error:
        asyncio.run(_client().list_keys())

    assert (error.value.code, error.value.status_code) == ("litellm_unreachable", 503)


def test_master_key_rejected_maps_to_auth_failed(fake_upstreams) -> None:
    _sidecar(fake_upstreams, "GET", "/key/list", {"status": 401, "json": {"error": {"message": "bad key"}}})

    with pytest.raises(LiteLLMAdminError) as error:
        asyncio.run(_client().list_keys())

    assert (error.value.code, error.value.status_code, error.value.upstream_status) == ("litellm_admin_auth_failed", 502, 401)


def test_upstream_error_surfaces_message(fake_upstreams) -> None:
    _sidecar(fake_upstreams, "POST", "/key/generate", {"status": 400, "json": {"error": {"message": "no such team"}}})

    with pytest.raises(LiteLLMAdminError) as error:
        asyncio.run(_client().generate_key({"team_id": "ghost"}))

    assert (error.value.code, error.value.message, error.value.upstream_status) == ("litellm_key_api_error", "LiteLLM key API returned HTTP 400.", 400)


def test_enabled_property_requires_base_url() -> None:
    assert not _client(base_url="").enabled
    assert _client().enabled


def test_unverified_content_does_not_block_management_and_prevents_team_assignment(monkeypatch, fake_upstreams):
    from gateway import litellm_content
    checked = []
    async def unsafe(base_url, **kwargs):
        checked.append(kwargs)
        return 'database_team_unsafe'
    monkeypatch.setattr(litellm_content.gate, 'check', unsafe)
    # Listing and deletion still perform their requests; deletion also reruns
    # the check so removing an offending key repairs execution immediately.
    listing = _sidecar(fake_upstreams, 'GET', '/key/list', {'json': {'keys': []}},
                       query={'page': '1', 'size': '100', 'return_full_object': 'true'})
    assert asyncio.run(_client().list_keys()) == []
    assert len(listing.requests) == 1
    deleting = _sidecar(fake_upstreams, 'POST', '/key/delete', {'json': {'deleted_keys': ['fixture']}})
    assert asyncio.run(_client().delete_keys(keys=['fixture'])) == {'deleted_keys': ['fixture']}
    assert len(deleting.requests) == 1
    with pytest.raises(LiteLLMAdminError) as error:
        asyncio.run(_client().generate_key({'team_id': 'unsafe-team'}))
    assert error.value.code == 'litellm_content_unverified'
    assert len(deleting.requests) == 1
    assert checked == [{'force': True}, {'force': True}]
