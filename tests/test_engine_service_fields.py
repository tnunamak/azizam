"""Engine behaviours that are service-record fields, not backend ids.

Every case runs the same configuration under the owner's service id and under
a new id; the behaviour must be identical, and must change only when the field
changes. The field, never the name, selects the behaviour.
"""

from __future__ import annotations

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
