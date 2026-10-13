"""Engine behaviours that are service-record fields, not backend ids.

Every case runs the same configuration under the owner's service id and under
a new id; the behaviour must be identical, and must change only when the field
changes. The field, never the name, selects the behaviour.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from unittest.mock import AsyncMock, patch

import pytest

import proxy
from tests.support.fake_upstream import fake_upstreams, load_scenario  # noqa: F401

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
    proxy._loaded_state[backend] = {"model": f"/models/{backend}.gguf", "aux": None, "ts": proxy.time.time()}


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
            proxy._loaded_state["plain"] = {"model": "/models/other.gguf", "aux": None, "ts": proxy.time.time()}
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


PGREP_OUTPUT = (
    "111 /usr/bin/other-server --port 5099 --mmproj /models/wrong.gguf\n"
    "222 /usr/bin/model-server --model /models/m.gguf --port 5051 --mmproj /models/proj.gguf --jinja\n"
)
AUX_FIELD = {"probe": "process-cmdline", "process": "model-server", "flag": "--mmproj"}


class _Pgrep:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        return type("Result", (), {"stdout": PGREP_OUTPUT})()


@pytest.mark.parametrize("service_id", SERVICE_IDS)
class TestAuxProbe:
    """The `aux` field says how to read a service's loaded aux file."""

    def _run(self, service_id: str, **fields):
        pgrep = _Pgrep()
        backends = {service_id: _path_service(5051, **fields)}
        proxy._loaded_state.clear()
        with patch.object(proxy, "config", {"backends": backends}), patch.object(
            proxy, "_BACKEND_STATE_STRATEGIES", {"llama_cpp": {"endpoint": "/props", "model_key": "model_path"}}
        ), patch.object(proxy, "_seed_loaded_state", wraps=proxy._seed_loaded_state), patch.object(
            proxy.subprocess, "run", pgrep
        ), patch("urllib.request.urlopen", side_effect=OSError("no server")):
            return proxy._currently_loaded_aux_for(service_id), pgrep

    def test_aux_is_read_from_the_process_on_the_service_port(self, service_id):
        aux, pgrep = self._run(service_id, aux=AUX_FIELD)
        assert aux == "/models/proj.gguf"
        assert pgrep.calls == [["pgrep", "-af", "model-server"]]

    def test_no_aux_probe_without_the_field_whatever_the_id(self, service_id):
        aux, pgrep = self._run(service_id)
        assert aux is None
        assert pgrep.calls == []

    def test_a_probe_missing_its_process_or_flag_reads_nothing(self, service_id):
        for broken in ({**AUX_FIELD, "process": ""}, {**AUX_FIELD, "flag": ""}):
            aux, _pgrep = self._run(service_id, aux=broken)
            assert aux is None


IMAGE_BODY = {
    "model": "vision-model",
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "describe"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
    ]}],
}


def _vision_service(**fields) -> dict:
    return {
        "host": "127.0.0.1", "port": 5051, "capabilities": ["chat"],
        "model_policy_schema_version": 1,
        "models": [{"id": "vision-model", "model_group": "vision-model", "upstream_model": "vision-model",
                    "activation_model": "/models/m.gguf", "activation_mmproj": "/models/p.gguf"}],
        "lifecycle": {"driver": "systemd", "driver_config": {"unit": "x.service"}},
        **fields,
    }


@pytest.mark.parametrize("service_id", SERVICE_IDS)
class TestVisionViaAux:
    """`aux.grants_input_modalities: [image]` lets a loaded aux file add image input."""

    def _prepare(self, service_id: str, *, aux_loaded: bool | None, **fields):
        with patch.object(proxy, "config", {"backends": {service_id: _vision_service(**fields)}}), patch.object(
            proxy, "LLM_BACKEND_HOSTS", {service_id: "http://127.0.0.1:5051"}
        ), patch.object(proxy, "get_active_backend", AsyncMock(return_value=service_id)), patch.object(
            proxy, "loaded_aux_present", return_value=aux_loaded
        ), patch.object(proxy, "vision_capable_llm_backends", return_value=[]):
            return asyncio.run(proxy._prepare_litellm_local_llm_request(
                proxy.NO_AUTH_CONTEXT, request_path="/v1/chat/completions",
                body_json=IMAGE_BODY, activate_target=False,
            ))

    def test_a_target_with_an_aux_file_takes_images_when_the_field_grants_image(self, service_id):
        error, _body = self._prepare(
            service_id, aux_loaded=None, aux={"grants_input_modalities": ["image"]})
        assert error is None

    def test_a_target_with_an_aux_file_is_refused_images_without_the_grant(self, service_id):
        error, _body = self._prepare(service_id, aux_loaded=None)
        assert error.status_code == 400
        assert json.loads(error.body)["error"]["code"] == "vision_unsupported"

    def test_an_unrelated_aux_grant_is_not_image_input(self, service_id):
        error, _body = self._prepare(service_id, aux_loaded=None, aux={"grants_input_modalities": ["audio"]})
        assert error.status_code == 400

    def test_the_active_service_is_asked_for_its_loaded_aux_only_when_the_field_grants_image(self, service_id):
        body = {**IMAGE_BODY, "model": "not-a-local-model"}
        for aux, asked in (({"grants_input_modalities": ["image"]}, True), (None, False)):
            fields = {"aux": aux} if aux else {}
            loaded = patch.object(proxy, "loaded_aux_present", return_value=True)
            with patch.object(proxy, "config", {"backends": {service_id: _vision_service(**fields)}}), patch.object(
                proxy, "LLM_BACKEND_HOSTS", {service_id: "http://127.0.0.1:5051"}
            ), patch.object(proxy, "get_active_backend", AsyncMock(return_value=service_id)), patch.object(
                proxy, "vision_capable_llm_backends", return_value=[]
            ), loaded as present:
                asyncio.run(proxy._prepare_litellm_local_llm_request(
                    proxy.NO_AUTH_CONTEXT, request_path="/v1/chat/completions",
                    body_json=body, activate_target=False,
                ))
            assert present.called is asked, aux


@pytest.mark.parametrize("service_id", SERVICE_IDS)
class TestContextWindowProbe:
    """`context_window_probe.endpoint` makes a service report its own context window."""

    def _read(self, fake, service_id: str, **fields):
        backends = {service_id: _path_service(5051, **fields), "other": _path_service(5052, **fields)}
        with patch.object(proxy, "config", {"backends": backends}), patch.object(
            proxy, "LLM_BACKEND_HOSTS", {service_id: fake.base_url, "other": "http://unreachable.fk.test"}
        ), patch.object(proxy, "_MODEL_CONTEXT_CACHE", {}):
            first = asyncio.run(proxy._context_window_for_model_id("m", {"azizam_backend": service_id}))
            second = asyncio.run(proxy._context_window_for_model_id("m", {"azizam_backend": service_id}))
            return first, second, sorted(proxy._MODEL_CONTEXT_CACHE)

    def test_the_window_is_read_from_the_service_own_url_once_and_cached_per_service(self, service_id, fake_upstreams):
        fake = fake_upstreams.asgi(load_scenario("openai-compatible/served-model-context", model="m"), host="ctx.fk.test")
        first, second, cache_keys = self._read(fake, service_id, context_window_probe={"endpoint": "/v1/models"})
        assert (first, second) == (32768, 32768)
        assert cache_keys == [service_id]
        assert [(call.method, call.path) for call in fake.calls("models")] == [("GET", "/v1/models")]

    def test_no_probe_without_the_field_whatever_the_id(self, service_id, fake_upstreams):
        fake = fake_upstreams.asgi(load_scenario("openai-compatible/no-calls"), host="ctx2.fk.test")
        assert self._read(fake, service_id)[:2] == (None, None)

    def test_an_endpoint_that_is_not_a_path_is_ignored(self, service_id, fake_upstreams):
        fake = fake_upstreams.asgi(load_scenario("openai-compatible/no-calls"), host="ctx3.fk.test")
        assert self._read(fake, service_id, context_window_probe={"endpoint": "http://x/y"})[:2] == (None, None)
