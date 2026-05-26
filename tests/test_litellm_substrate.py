from __future__ import annotations

from gateway.litellm_substrate import compile_litellm_substrate_config
from gateway.policy import AppProfile, FallbackPolicy, StatusVisibilityPolicy


def test_compiles_openai_compatible_local_service() -> None:
    compiled = compile_litellm_substrate_config(
        {
            "local-llama": {
                "name": "Local llama.cpp",
                "capabilities": ["openai"],
                "config": {
                    "protocol": "openai-compatible",
                    "base_url": "http://127.0.0.1:8081/v1",
                    "api_key_env": "LOCAL_LLM_API_KEY",
                    "models": ["llama3:8b"],
                },
            }
        }
    )

    assert compiled["model_list"] == [
        {
            "model_name": "llama3:8b",
            "litellm_params": {
                "model": "openai/llama3:8b",
                "api_base": "http://127.0.0.1:8081/v1",
                "api_key": "os.environ/LOCAL_LLM_API_KEY",
            },
            "model_info": {
                "id": "local-llama:llama3:8b",
                "base_model": "llama3:8b",
                "metadata": {
                    "gateway_service_id": "local-llama",
                    "gateway_service_name": "Local llama.cpp",
                    "gateway_source": "ui",
                    "gateway_priority": 100,
                    "gateway_model_metadata": {},
                },
            },
        }
    ]
    assert compiled["metadata"]["source_of_truth"] == (
        "gateway service registry and app profiles"
    )


def test_multiple_deployments_can_share_one_model_group() -> None:
    compiled = compile_litellm_substrate_config(
        {
            "local-a": {
                "priority": 10,
                "capabilities": ["openai"],
                "config": {
                    "protocol": "openai",
                    "base_url": "http://127.0.0.1:8001/v1",
                    "models": [
                        {
                            "id": "fast",
                            "model_group": "assistant-default",
                            "upstream_model": "llama3:8b-q4",
                        }
                    ],
                },
            },
            "local-b": {
                "priority": 20,
                "capabilities": ["openai"],
                "config": {
                    "protocol": "openai",
                    "base_url": "http://127.0.0.1:8002/v1",
                    "models": [
                        {
                            "id": "quality",
                            "model_group": "assistant-default",
                            "upstream_model": "llama3:70b-q4",
                        }
                    ],
                },
            },
        }
    )

    assert [entry["model_name"] for entry in compiled["model_list"]] == [
        "assistant-default",
        "assistant-default",
    ]
    assert [entry["litellm_params"]["api_base"] for entry in compiled["model_list"]] == [
        "http://127.0.0.1:8001/v1",
        "http://127.0.0.1:8002/v1",
    ]
    assert [entry["model_info"]["base_model"] for entry in compiled["model_list"]] == [
        "llama3:8b-q4",
        "llama3:70b-q4",
    ]


def test_preserves_gateway_policy_metadata_without_exposing_secrets() -> None:
    compiled = compile_litellm_substrate_config(
        {
            "cloud-openai": {
                "name": "OpenAI",
                "source": "provisioned",
                "capabilities": ["openai"],
                "config": {
                    "protocol": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "models": [{"id": "gpt-4.1-mini"}],
                    "metadata": {"api_key": "sk-should-not-leak"},
                },
                "secret_refs": {"api_key": "<external-secret>"},
            }
        },
        profiles=[
            AppProfile(
                id="family",
                models=frozenset({"gpt-4.1-mini"}),
                fallback=FallbackPolicy(cloud_allowed=True),
                status_visibility=StatusVisibilityPolicy(level="standard"),
            )
        ],
        policy_metadata={
            "profile_source": "admin.db",
            "credential": "opk_should_not_leak",
            "fallback": {"cloud_allowed": True},
        },
    )

    rendered = repr(compiled)
    assert "sk-should-not-leak" not in rendered
    assert "opk_should_not_leak" not in rendered
    assert compiled["model_list"][0]["litellm_params"]["api_key"] == (
        "os.environ/AI_GATEWAY_DOWNSTREAM_API_KEY"
    )
    assert compiled["metadata"]["gateway"]["profiles"] == [
        {
            "id": "family",
            "protocols": None,
            "workflows": None,
            "models": ["gpt-4.1-mini"],
            "backends": None,
            "allow": {
                "intents": None,
                "exposure": None,
                "trust_tiers": None,
                "resource_tiers": None,
                "tags": None,
            },
            "fallback": {"cloud_allowed": True},
            "native_passthrough": {
                "enabled": False,
                "allowed_backends": [],
                "allowed_operations": [],
                "audit": True,
            },
            "status_visibility": {"level": "standard"},
            "target_policy": {
                "visibility": "catalog",
                "activation": "may_activate",
            },
            "model_access": {"mode": "catalog"},
        }
    ]
    assert compiled["metadata"]["gateway"]["policy"] == {
        "profile_source": "admin.db",
        "credential": "<redacted>",
        "fallback": {"cloud_allowed": True},
    }
