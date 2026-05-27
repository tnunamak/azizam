from __future__ import annotations

from gateway.backends.llm_execution import (
    LITELLM_BODY_EXECUTION_PATHS,
    LITELLM_DISCOVERY_PATHS,
    LITELLM_SUPPORTED_PATHS,
    litellm_headers,
    litellm_requested,
    litellm_runtime_from_registry,
    litellm_url,
)


def test_litellm_requested_is_explicit_and_path_limited() -> None:
    assert litellm_requested(
        mode="litellm",
        request_path="/v1/chat/completions",
    )
    assert litellm_requested(mode="enabled", request_path="/v1/embeddings")
    assert not litellm_requested(mode="enabled", request_path="/v1/responses")
    assert not litellm_requested(mode="enabled", request_path="/v1/messages")
    assert not litellm_requested(mode="legacy", request_path="/v1/chat/completions")
    assert not litellm_requested(mode="litellm", request_path="/v1/images/generations")
    assert "/v1/chat/completions" in LITELLM_BODY_EXECUTION_PATHS
    assert "/v1/models" in LITELLM_DISCOVERY_PATHS
    assert "/v1/images/generations" not in LITELLM_SUPPORTED_PATHS


def test_litellm_url_joins_base_path_and_query() -> None:
    assert litellm_url(
        base_url="http://127.0.0.1:4000/",
        path="/v1/chat/completions",
        query="trace=1",
    ) == "http://127.0.0.1:4000/v1/chat/completions?trace=1"


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
        fallback_mode="legacy",
        fallback_base_url="",
    )

    assert runtime.enabled
    assert runtime.base_url == "http://127.0.0.1:4011"
    assert runtime.api_key == "sk-internal"
    assert runtime.service_id == "litellm"
    assert runtime.source == "service_registry"


def test_litellm_runtime_env_is_only_bootstrap_fallback() -> None:
    runtime = litellm_runtime_from_registry(
        None,
        env={},
        fallback_mode="litellm",
        fallback_base_url="http://127.0.0.1:4011/",
        fallback_api_key="sk-env",
    )

    assert runtime.enabled
    assert runtime.base_url == "http://127.0.0.1:4011"
    assert runtime.api_key == "sk-env"
    assert runtime.source == "env"


def test_litellm_runtime_requires_base_url_env_when_declared() -> None:
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
        env={},
    )

    assert not runtime.enabled


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


def test_litellm_runtime_uses_highest_priority_enabled_service() -> None:
    runtime = litellm_runtime_from_registry(
        {
            "services": {
                "slow": {
                    "id": "slow",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "priority": 20,
                    "capabilities": ["llm.execution"],
                    "config": {"base_url": "http://127.0.0.1:4002"},
                },
                "fast": {
                    "id": "fast",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "priority": 5,
                    "capabilities": ["llm.execution"],
                    "config": {"base_url": "http://127.0.0.1:4001"},
                },
            }
        },
        env={},
    )

    assert runtime.enabled
    assert runtime.service_id == "fast"
    assert runtime.base_url == "http://127.0.0.1:4001"


def test_litellm_runtime_skips_disabled_or_missing_base_url_service() -> None:
    runtime = litellm_runtime_from_registry(
        {
            "services": {
                "disabled": {
                    "id": "disabled",
                    "kind": "llm-substrate",
                    "enabled": False,
                    "priority": 1,
                    "capabilities": ["llm.execution"],
                    "config": {"base_url": "http://127.0.0.1:4001"},
                },
                "missing-url": {
                    "id": "missing-url",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "priority": 2,
                    "capabilities": ["llm.execution"],
                    "config": {},
                },
                "usable": {
                    "id": "usable",
                    "kind": "llm-substrate",
                    "enabled": True,
                    "priority": 3,
                    "capabilities": ["llm.execution"],
                    "config": {"base_url": "http://127.0.0.1:4003"},
                },
            }
        },
        env={},
    )

    assert runtime.enabled
    assert runtime.service_id == "usable"
    assert runtime.base_url == "http://127.0.0.1:4003"
