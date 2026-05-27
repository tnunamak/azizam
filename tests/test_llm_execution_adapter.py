from __future__ import annotations

from gateway.backends.llm_execution import (
    LITELLM_BODY_EXECUTION_PATHS,
    litellm_headers,
    litellm_runtime_from_registry,
)


def test_litellm_body_execution_paths_are_explicit_and_limited() -> None:
    assert "/v1/chat/completions" in LITELLM_BODY_EXECUTION_PATHS
    assert "/v1/completions" in LITELLM_BODY_EXECUTION_PATHS
    assert "/v1/embeddings" in LITELLM_BODY_EXECUTION_PATHS
    assert "/v1/images/generations" not in LITELLM_BODY_EXECUTION_PATHS


def test_litellm_headers_do_not_forward_client_runtime_key() -> None:
    headers = litellm_headers(
        {
            "Authorization": "Bearer opk_client_key",
            "Content-Type": "application/json",
            "X-Api-Key": "opk_client_key",
        },
        api_key="sk-litellm-internal",
        gateway_metadata={
            "execution": "litellm",
            "profile": "family",
            "key-prefix": "sha256:abcdef",
        },
    )

    assert headers["authorization"] == "Bearer sk-litellm-internal"
    assert headers["content-type"] == "application/json"
    assert headers["x-ai-gateway-execution"] == "litellm"
    assert headers["x-ai-gateway-profile"] == "family"
    assert headers["x-ai-gateway-key-prefix"] == "sha256:abcdef"
    assert "x-api-key" not in headers


def test_litellm_runtime_prefers_gateway_service_registry() -> None:
    runtime = litellm_runtime_from_registry(
        {
            "services": {
                "litellm": {
                    "id": "litellm",
                    "name": "LiteLLM",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "priority": 5,
                    "capabilities": ["llm.execution"],
                    "config": {
                        "base_url": "http://127.0.0.1:4011/",
                        "api_key_env": "INTERNAL_LITELLM_KEY",
                    },
                }
            }
        },
        env={"INTERNAL_LITELLM_KEY": "sk-internal"},
    )

    assert runtime.enabled
    assert runtime.base_url == "http://127.0.0.1:4011"
    assert runtime.api_key == "sk-internal"
    assert runtime.service_id == "litellm"
    assert runtime.source == "service_registry"


def test_litellm_runtime_uses_registry_default_when_env_override_is_absent() -> None:
    runtime = litellm_runtime_from_registry(
        {
            "services": {
                "litellm": {
                    "id": "litellm",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "capabilities": ["llm.execution"],
                    "config": {
                        "base_url_env": "AI_GATEWAY_LITELLM_BASE_URL",
                        "default_base_url": "http://127.0.0.1:4011/",
                        "api_key_env": "AI_GATEWAY_LITELLM_API_KEY",
                    },
                }
            }
        },
        env={},
    )

    assert runtime.enabled
    assert runtime.base_url == "http://127.0.0.1:4011"
    assert runtime.api_key == ""
    assert runtime.source == "service_registry"


def test_litellm_runtime_env_override_wins_over_registry_default() -> None:
    runtime = litellm_runtime_from_registry(
        {
            "services": {
                "litellm": {
                    "id": "litellm",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "capabilities": ["llm.execution"],
                    "config": {
                        "base_url_env": "AI_GATEWAY_LITELLM_BASE_URL",
                        "default_base_url": "http://127.0.0.1:4011",
                    },
                }
            }
        },
        env={"AI_GATEWAY_LITELLM_BASE_URL": "http://127.0.0.1:4999/"},
    )

    assert runtime.enabled
    assert runtime.base_url == "http://127.0.0.1:4999"


def test_litellm_runtime_ignores_unmarked_openai_providers() -> None:
    runtime = litellm_runtime_from_registry(
        {
            "services": {
                "openai": {
                    "id": "openai",
                    "kind": "provider",
                    "enabled": True,
                    "capabilities": ["openai"],
                    "config": {"base_url": "https://api.openai.com/v1"},
                }
            }
        },
        env={},
    )

    assert not runtime.enabled
