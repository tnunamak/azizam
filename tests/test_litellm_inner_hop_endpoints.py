"""The gateway lifecycle hop serves every OpenAI-compatible endpoint a backend's capabilities declare.

The endpoint set is one table (``gateway/capability_ids.INNER_HOP_ENDPOINTS``). A path it does not serve
answers 404 ``endpoint_not_served`` before the key check, not the 401 of a credentials error.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

import proxy
from gateway.capability_ids import INNER_HOP_ENDPOINTS, capabilities_without_inner_endpoint, inner_hop_endpoints_for

KEY = {"AZIZAM_INTERNAL_LITELLM_KEY": "internal-secret"}
AUTH = {"Authorization": "Bearer internal-secret"}


class _Client:
    async def aclose(self) -> None:
        return None


class _Response:
    status_code = 200
    headers = {"content-type": "application/json"}

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def aread(self) -> bytes:
        return json.dumps(self._payload).encode()

    async def aclose(self) -> None:
        return None


@pytest.fixture
def hop(monkeypatch):
    sent: list = []

    async def fake_send(execution_request, *, cancellation_token=None):
        sent.append(execution_request)
        return _Client(), _Response({"object": "list", "data": [], "model": "m"})

    backends = {**(proxy.config.get("backends") or {}),
                "emb": {"host": "127.0.0.1", "port": 5099, "capabilities": ["chat", "embed"], "enabled": True},
                "chatonly": {"host": "127.0.0.1", "port": 5098, "capabilities": ["chat"], "enabled": True},
                "embonly": {"host": "127.0.0.1", "port": 5097, "capabilities": ["openai", "embeddings"], "enabled": True}}
    monkeypatch.setitem(proxy.config, "backends", backends)
    # the real builder, not a hand-set list: the hop serves the members that capability_ids.INNER_HOP_ENDPOINTS names
    monkeypatch.setattr(proxy, "INNER_HOP_BACKEND_HOSTS", proxy._backend_host_map(backends, proxy._is_inner_hop_backend))
    monkeypatch.setattr(proxy, "send_llm_execution_request", fake_send)
    monkeypatch.setattr(proxy, "_service_availability_response", lambda *a, **k: None)
    monkeypatch.setattr(proxy, "ensure_target", AsyncMock(return_value=True))
    with patch.dict(os.environ, KEY, clear=False):
        yield TestClient(proxy.app), sent


@pytest.mark.parametrize("endpoint,body", [
    ("/chat/completions", {"model": "openai/m", "messages": []}),
    ("/completions", {"model": "openai/m", "prompt": "hi"}),
    ("/embeddings", {"model": "openai/m", "input": "hi"}),
])
def test_each_declared_endpoint_is_forwarded_to_the_same_path_on_the_engine(hop, endpoint, body) -> None:
    client, sent = hop
    response = client.post(f"/internal/litellm/local/emb/v1{endpoint}", headers=AUTH, json=body)
    assert response.status_code == 200, response.text
    assert [request.url for request in sent] == [f"http://127.0.0.1:5099/v1{endpoint}"]
    assert json.loads(sent[0].body)["model"] == "m"


@pytest.mark.parametrize("method,path", [
    ("post", "/internal/litellm/local/emb/v1/images/generations"),
    ("post", "/internal/litellm/local/emb/v1/audio/speech"),
    ("post", "/internal/litellm/local/emb/v1/rerank"),
    ("post", "/internal/litellm/local/emb"),
    ("post", "/internal/litellm/local/emb/v1/embeddings/extra"),
])
def test_a_path_the_hop_does_not_serve_answers_404_before_the_key_check(hop, method, path) -> None:
    client, sent = hop
    for headers in ({}, AUTH):
        response = getattr(client, method)(path, headers=headers)
        assert response.status_code == 404, (path, response.status_code, response.text)
        assert response.json()["error"]["code"] == "endpoint_not_served"
    assert sent == []


def test_a_served_endpoint_still_needs_the_internal_key(hop) -> None:
    client, sent = hop
    response = client.post("/internal/litellm/local/emb/v1/embeddings", json={"model": "m", "input": "x"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert sent == []


def test_an_embeddings_only_member_is_served_by_the_hop_through_the_real_host_map_builder(hop) -> None:
    """Failed before the fix: the hop's host map listed chat-capable backends only, so an embeddings-only member
    answered 404 backend_not_found."""
    client, sent = hop
    assert "embonly" in proxy.INNER_HOP_BACKEND_HOSTS and "embonly" not in proxy.LLM_BACKEND_HOSTS
    response = client.post("/internal/litellm/local/embonly/v1/embeddings", headers=AUTH,
                           json={"model": "openai/m", "input": "hi"})
    assert response.status_code == 200, response.text
    assert [request.url for request in sent] == ["http://127.0.0.1:5097/v1/embeddings"]


def test_the_hop_host_map_is_built_from_the_inner_hop_table() -> None:
    """Which backends the hop lists follows ``INNER_HOP_ENDPOINTS``, not a second word list."""
    backends = {
        "chat": {"host": "h", "port": 1, "capabilities": ["chat"]},
        "emb": {"host": "h", "port": 2, "capabilities": ["embeddings"]},
        "word": {"host": "h", "port": 3, "capabilities": ["text.embeddings"]},
        "rerank": {"host": "h", "port": 4, "capabilities": ["rerank"]},
        "marker": {"host": "h", "port": 5, "capabilities": ["openai"]},
        "off": {"host": "h", "port": 6, "capabilities": ["chat"], "enabled": False},
        "noport": {"host": "h", "capabilities": ["chat"]},
        "litellm": {"host": "h", "port": 7, "capabilities": ["chat"]},
    }
    assert sorted(proxy._backend_host_map(backends, proxy._is_inner_hop_backend)) == ["chat", "emb", "word"]
    assert sorted(proxy._backend_host_map(backends, proxy._is_chat_capable_native_backend)) == ["chat"], (
        "LLM_BACKEND_HOSTS keeps listing chat-capable backends only")


def test_a_backend_that_does_not_declare_the_capability_answers_404_not_served(hop) -> None:
    client, sent = hop
    response = client.post("/internal/litellm/local/chatonly/v1/embeddings", headers=AUTH,
                           json={"model": "openai/m", "input": "hi"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "endpoint_not_served"
    assert sent == []


def test_the_endpoint_set_comes_from_the_capability_table() -> None:
    assert inner_hop_endpoints_for(["chat"]) == frozenset(INNER_HOP_ENDPOINTS["chat"])
    assert inner_hop_endpoints_for(["text.embeddings"]) == frozenset({"/embeddings"})
    assert inner_hop_endpoints_for(["rerank", "image"]) == frozenset()
    assert capabilities_without_inner_endpoint(["chat", "rerank", "embeddings", "image"]) == ["rerank", "image"]


def test_a_marker_word_that_is_no_capability_is_not_refused() -> None:
    """``openai`` marks OpenAI compatibility (the house word list ``[openai, chat, completions]``); it needs no endpoint."""
    assert capabilities_without_inner_endpoint(["openai", "chat", "completions"]) == []
    assert capabilities_without_inner_endpoint(["openai", "embeddings"]) == []
    assert capabilities_without_inner_endpoint(["openai", "rerank"]) == ["rerank"]
