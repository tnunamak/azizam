"""A service's loaded-model probe is data on its record (``loaded_model_probe``), with one protocol default."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import proxy

PROPS = {"endpoint": "/props", "model_key": "model_path"}
BODY = {"model_path": "/models/a.bin", "data": [{"id": "served-id"}]}


def test_the_probe_is_the_record_field_or_the_openai_compatible_default():
    assert proxy._loaded_model_probe({"loaded_model_probe": PROPS}) == PROPS
    assert proxy._loaded_model_probe({}) == {"endpoint": "/v1/models", "model_key": "data.0.id"}
    assert proxy._loaded_model_probe(None) == {"endpoint": "/v1/models", "model_key": "data.0.id"}
    for broken in ("x", {"endpoint": "props", "model_key": "k"}, {"endpoint": "/p"}, {"endpoint": "/p", "model_key": ""}):
        assert proxy._loaded_model_probe({"loaded_model_probe": broken}) is None, broken


def test_the_seed_reads_each_service_through_its_probe():
    calls = []

    def probe(backend, endpoint, timeout):
        calls.append((backend, endpoint))
        return BODY

    backends = {"declared": {"loaded_model_probe": PROPS}, "plain": {}, "broken": {"loaded_model_probe": "x"}}
    with patch.object(proxy, "config", {"backends": backends}), \
            patch.object(proxy, "_engine_probe_json_sync", probe), patch.dict(proxy._loaded_state, clear=True):
        assert proxy.currently_loaded_model("declared") == "/models/a.bin"
        assert proxy.currently_loaded_model("plain") == "served-id"
        assert proxy.currently_loaded_model("broken") is None
    assert calls == [("declared", "/props"), ("plain", "/v1/models")]


def test_the_current_model_reads_each_service_through_its_probe():
    calls = []

    async def probe(backend, endpoint, timeout):
        calls.append((backend, endpoint))
        return BODY

    backends = {"declared": {"loaded_model_probe": PROPS}, "plain": {}, "broken": {"loaded_model_probe": {"endpoint": "/p"}}}
    with patch.object(proxy, "config", {"backends": backends}), patch.object(proxy, "_engine_probe_json", probe), \
            patch.object(proxy.DERIVED, "LLM_BACKEND_HOSTS", {name: "127.0.0.1" for name in backends}), \
            patch.dict(proxy._ENGINE_SERVED_ALIASES, clear=True):
        assert asyncio.run(proxy.current_llm_model_for_backend("declared")) == "/models/a.bin"
        assert asyncio.run(proxy.current_llm_model_for_backend("plain")) == "served-id"
        assert asyncio.run(proxy.current_llm_model_for_backend("broken")) == ""
    assert calls == [("declared", "/props"), ("plain", "/v1/models")]


def test_the_egress_map_allows_exactly_the_probe_endpoint():
    captured = {}

    def project(services, *, extra_probe_paths=None):
        captured["paths"] = extra_probe_paths

    with patch.object(proxy.engine_egress, "project", project):
        proxy._project_egress({})
    paths = captured["paths"]
    assert paths({"config": {"loaded_model_probe": PROPS}}) == ["/props"]
    assert paths({"config": {}}) == ["/v1/models"]
    assert paths({"loaded_model_probe": PROPS}) == ["/props"]
    assert paths({"config": {"loaded_model_probe": {"endpoint": "props", "model_key": "k"}}}) == []
