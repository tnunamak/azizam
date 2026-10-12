"""A LiteLLM virtual key (sk-...) that is no gateway credential is delegated.

The gateway forwards it to the LLM substrate as X-Litellm-Key and never lets
it unlock gateway-owned routes. The substrate is an FK fake behind the real
registry; the credential lookup runs on a real, empty admin store; the
protocol frontends reach the chat route through their real loopback hop.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

import proxy
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401 - fixture
from tests.support.gateway import gateway_client, register_litellm, use_admin_db

VIRTUAL_KEY = "sk-virtual-test"
MODEL = "delegated-model"
# A public peer: the LAN bypass must not be what lets a request through.
PUBLIC_PEER = ("203.0.113.7", 50000)


@pytest.fixture
def gateway(fake_upstreams, monkeypatch, tmp_path):
    db = use_admin_db(monkeypatch, tmp_path)
    monkeypatch.setattr(proxy, "AUTH_ENABLED", True)
    monkeypatch.setattr(proxy, "LAN_BYPASS_ENABLED", False)
    fake_upstreams.router.add(f"127.0.0.1:{proxy.PROXY_PORT}", proxy.app)

    def serve(name: str):
        fake = fake_upstreams.asgi(load_scenario(f"openai-compatible/{name}", model=MODEL, virtual_key=VIRTUAL_KEY), host="litellm.fk.test")
        register_litellm(monkeypatch, db, fake.base_url)
        return fake

    return serve


def _call(method: str, path: str, **kwargs) -> httpx.Response:
    async def call() -> httpx.Response:
        async with gateway_client(client=PUBLIC_PEER) as client:
            return await client.request(method, path, headers={"authorization": f"Bearer {VIRTUAL_KEY}"}, **kwargs)

    return asyncio.run(call())


def test_litellm_virtual_key_reaches_litellm_with_delegated_header(gateway) -> None:
    litellm = gateway("delegated-chat")

    response = _call("POST", "/v1/chat/completions", json={"model": MODEL, "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 200, response.text
    assert response.json()["choices"][0]["message"] == {"role": "assistant", "content": "ok"}
    [forwarded] = litellm.calls("chat")
    assert forwarded.headers["x-litellm-key"] == f"Bearer {VIRTUAL_KEY}"
    assert "authorization" not in forwarded.headers
    assert forwarded.json == {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}


def test_litellm_virtual_key_does_not_unlock_gateway_media_routes(gateway) -> None:
    response = _call("POST", "/v1/images/generations", json={"model": "image-test", "prompt": "cat"})

    assert response.status_code == 401


def test_litellm_virtual_key_rejected_on_durable_media_job_routes(gateway) -> None:
    """Delegation is wired only into LLM-execution routes; the durable media/job
    surface authenticates without it, so a virtual key gets 401 there."""
    media_job_routes = [
        ("POST", "/gateway/v1/media/jobs"),
        ("GET", "/gateway/v1/media/jobs"),
        ("POST", "/gateway/v1/media/jobs/image-generations"),
        ("GET", "/gateway/v1/media/jobs/mj_probe"),
        ("GET", "/gateway/v1/media/jobs/mj_probe/events"),
        ("POST", "/gateway/v1/media/jobs/mj_probe/events"),
        ("POST", "/gateway/v1/media/jobs/mj_probe/cancel"),
        ("GET", "/gateway/v1/media/jobs/mj_probe/content"),
    ]

    statuses = {
        (method, path): _call(method, path, **({"json": {}} if method == "POST" else {})).status_code
        for method, path in media_job_routes
    }

    assert statuses == {route: 401 for route in media_job_routes}


def test_litellm_virtual_key_model_list_is_litellm_catalog_only(gateway) -> None:
    litellm = gateway("delegated-models")

    with patch.object(proxy, "_build_workflow_model_entries", lambda: [{"id": "media-workflow"}]):
        response = _call("GET", "/v1/models")

    assert response.status_code == 200, response.text
    assert [entry["id"] for entry in response.json()["data"]] == [MODEL]
    assert len(litellm.calls("models")) == 1


def test_anthropic_messages_accepts_litellm_virtual_key_without_gateway_profile_policy(gateway) -> None:
    litellm = gateway("delegated-chat")

    with patch.object(proxy, "_protocol_policy_decision", side_effect=AssertionError("gateway policy should be skipped")):
        response = _call("POST", "/v1/messages", json={"model": MODEL, "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["type"], body["role"], body["model"], body["content"], body["stop_reason"]) == (
        "message", "assistant", MODEL, [{"type": "text", "text": "ok"}], "end_turn",
    )
    [forwarded] = litellm.calls("chat")
    assert forwarded.headers["x-litellm-key"] == f"Bearer {VIRTUAL_KEY}"
    assert forwarded.json["messages"] == [{"role": "user", "content": "hi"}]


def test_ollama_tags_uses_litellm_filtered_catalog_for_virtual_key(gateway) -> None:
    litellm = gateway("delegated-models")

    with patch.object(proxy, "_protocol_policy_decision", side_effect=AssertionError("gateway policy should be skipped")):
        response = _call("GET", "/api/tags")

    assert response.status_code == 200, response.text
    assert [model["name"] for model in response.json()["models"]] == [MODEL]
    assert len(litellm.calls("models")) == 1
