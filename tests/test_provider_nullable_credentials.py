"""Unset optional reference fields must survive real paid model validation."""
import pytest

from gateway import provider_execution, providers, service_registry


@pytest.mark.parametrize("reference", ["api_key_env", "api_key_secret_id"])
def test_paid_model_validation_accepts_one_reference_and_an_unset_alternative(reference):
    config = {
        "remote_provider": True, "provider_id": "cloud-test",
        "protocol": "openai-compatible", "base_url": "https://cloud.test/v1",
        "api_key_env": None, "api_key_secret_id": None,
        "models": [{"id": "cloud-test/model", "model_group": "cloud-test/model",
                    "upstream_model": "model", "enabled": True}],
    }
    config[reference] = "CLOUD_TEST_KEY" if reference == "api_key_env" else "provider:cloud-test:api_key"
    registry = {"services": {"cloud-test": {
        "id": "cloud-test", "kind": "provider", "capabilities": ["openai"], "config": config,
    }}}
    provider = providers.provider_lookup(registry, "cloud-test")
    assert provider is not None
    assert provider_execution._model_declared(provider, "cloud-test/model")
    assert not provider_execution._model_declared(provider, "cloud-test/another")


def test_unset_references_without_a_credential_still_fail_closed():
    with pytest.raises(ValueError, match="requires api_key_env or api_key_secret_id"):
        service_registry.validate_provider_secret_refs("cloud-test", {
            "remote_provider": True, "api_key_env": None, "api_key_secret_id": "",
        }, {})
