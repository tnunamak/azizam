"""Engine behaviours that are service-record fields, not backend ids.

Every case runs the same configuration under the owner's service id and under
a new id; the behaviour must be identical, and must change only when the field
changes. The field, never the name, selects the behaviour.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

import proxy

SERVICE_IDS = ["kobold", "brand-new-llm"]


def _swap_service(**driver_config) -> dict:
    return {
        "host": "127.0.0.1",
        "port": 5051,
        "models": [
            {"id": "relative", "model_group": "relative", "activation_model": "models/rel.gguf"},
            {"id": "absolute", "model_group": "absolute", "activation_model": "/models/abs.gguf"},
        ],
        "lifecycle": {"driver": "systemd", "driver_config": {"unit": "x.service", **driver_config}},
    }


def _activation(service_id: str, model: str, **driver_config):
    with patch.object(proxy, "LLM_BACKEND_HOSTS", {service_id: "http://127.0.0.1:5051"}), patch.object(
        proxy, "config", {"backends": {service_id: _swap_service(**driver_config)}}
    ):
        return proxy._configured_litellm_local_model_target(model)


@pytest.mark.parametrize("service_id", SERVICE_IDS)
class TestActivationRequiresAbsolutePath:
    def test_a_relative_model_path_is_cleared_when_the_field_is_true(self, service_id):
        target = _activation(service_id, "relative", activation_requires_absolute_path=True)
        assert (target.backend, target.model) == (service_id, None)

    def test_an_absolute_model_path_is_kept_when_the_field_is_true(self, service_id):
        target = _activation(service_id, "absolute", activation_requires_absolute_path=True)
        assert target.model == "/models/abs.gguf"

    def test_a_relative_model_path_is_kept_without_the_field(self, service_id):
        assert _activation(service_id, "relative").model == "models/rel.gguf"
        assert _activation(service_id, "relative", activation_requires_absolute_path=False).model == "models/rel.gguf"


def _path_service(port: int, **fields) -> dict:
    return {"host": "127.0.0.1", "port": port, "state_type": "llama_cpp", **fields}


def _seed_from_name(backend: str) -> None:
    proxy._loaded_state[backend] = {"model": f"/models/{backend}.gguf", "mmproj": None, "ts": proxy.time.time()}


@pytest.mark.parametrize("service_id", SERVICE_IDS)
class TestLoadedModelIdentity:
    """`loaded_model_identity: path` makes a service report its loaded model as a path."""

    def _patched(self, service_id: str, **fields):
        backends = {service_id: _path_service(5051, **fields), "plain": _path_service(5052)}
        return patch.object(proxy, "config", {"backends": backends}), patch.object(
            proxy, "_seed_loaded_state", _seed_from_name
        )

    def test_the_loaded_model_is_read_from_the_probe_for_a_path_service(self, service_id):
        cfg, seed = self._patched(service_id, loaded_model_identity="path")
        with cfg, seed:
            proxy._loaded_state.clear()
            assert asyncio.run(proxy._currently_loaded_model_for(service_id)) == f"/models/{service_id}.gguf"
            assert asyncio.run(proxy._currently_loaded_model_for("plain")) is None

    def test_no_probe_without_the_field_whatever_the_id(self, service_id):
        cfg, seed = self._patched(service_id)
        with cfg, seed:
            proxy._loaded_state.clear()
            assert asyncio.run(proxy._currently_loaded_model_for(service_id)) is None

    def test_state_is_cached_per_service_not_shared(self, service_id):
        cfg, seed = self._patched(service_id, loaded_model_identity="path")
        with cfg, seed:
            proxy._loaded_state.clear()
            proxy._loaded_state["plain"] = {"model": "/models/other.gguf", "mmproj": None, "ts": proxy.time.time()}
            assert proxy.currently_loaded_model(service_id) == f"/models/{service_id}.gguf"
            assert proxy.currently_loaded_model("plain") == "/models/other.gguf"

    def test_current_model_id_comes_from_the_service_own_url(self, service_id):
        async def served(base_url: str) -> str:
            return "served-id" if base_url == "http://127.0.0.1:5051" else "wrong-service"

        cfg, seed = self._patched(service_id, loaded_model_identity="path")
        with cfg, seed, patch.object(proxy, "get_served_model_id", served):
            proxy._loaded_state.clear()
            assert asyncio.run(proxy.current_llm_model_for_backend(service_id)) == "served-id"
            assert asyncio.run(proxy.effective_llm_model_for_host("http://127.0.0.1:5051")) == "served-id"
            assert asyncio.run(proxy.effective_llm_model_for_host("http://127.0.0.1:5052")) == ""

    def test_availability_ids_follow_the_field(self, service_id):
        cfg, seed = self._patched(service_id, loaded_model_identity="path")
        with cfg, seed, patch.object(proxy, "_role_catalog_ids_for_loaded_path", lambda **_: set()):
            proxy._loaded_state.clear()
            ids = asyncio.run(proxy._current_loaded_llm_model_ids(service_id))
        assert {f"/models/{service_id}.gguf", f"{service_id}.gguf"} <= ids
