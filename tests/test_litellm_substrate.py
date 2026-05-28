from __future__ import annotations

from gateway.litellm_substrate import (
    compile_litellm_config_from_registry,
    compile_litellm_substrate_config,
    litellm_config_path_from_registry,
    write_litellm_config_artifact,
)


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
        }
    ]


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
    assert [entry["litellm_params"]["model"] for entry in compiled["model_list"]] == [
        "openai/llama3:8b-q4",
        "openai/llama3:70b-q4",
    ]


def test_openai_compatible_host_port_and_model_id_compile() -> None:
    compiled = compile_litellm_substrate_config(
        {
            "embeddings": {
                "name": "Embeddings",
                "config": {
                    "protocol": "openai-compatible",
                    "host": "127.0.0.1",
                    "port": 5061,
                    "model_id": "nomic-embed-text-v1.5",
                },
            }
        }
    )

    assert compiled["model_list"][0]["model_name"] == "nomic-embed-text-v1.5"
    assert compiled["model_list"][0]["litellm_params"]["api_base"] == (
        "http://127.0.0.1:5061/v1"
    )
    assert compiled["model_list"][0]["litellm_params"]["api_key"] == "sk-local"


def test_keeps_litellm_settings_minimal() -> None:
    compiled = compile_litellm_substrate_config(
        {
            "embeddings": {
                "name": "Embeddings",
                "config": {
                    "protocol": "openai-compatible",
                    "base_url": "http://127.0.0.1:5061/v1",
                    "models": ["nomic-embed-text-v1.5"],
                },
            }
        }
    )

    assert compiled["litellm_settings"] == {"drop_params": True}
    assert "router_settings" not in compiled
    assert "metadata" not in compiled


def test_references_external_secrets_without_exposing_secret_values() -> None:
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
    )

    rendered = repr(compiled)
    assert "sk-should-not-leak" not in rendered
    assert compiled["model_list"][0]["litellm_params"]["api_key"] == (
        "os.environ/AI_GATEWAY_DOWNSTREAM_API_KEY"
    )
    assert "metadata" not in compiled


def test_compiles_from_registry_and_writes_litellm_config_artifact(tmp_path) -> None:
    config_path = tmp_path / "generated" / "litellm.yml"
    registry = {
        "services": {
            "litellm": {
                "id": "litellm",
                "kind": "llm-substrate",
                "capabilities": ["llm.execution"],
                "config": {"config_path": str(config_path)},
            },
            "local": {
                "id": "local",
                "name": "Local OpenAI-compatible server",
                "capabilities": ["openai"],
                "config": {
                    "protocol": "openai-compatible",
                    "base_url": "http://127.0.0.1:8080/v1",
                    "models": [{"id": "assistant-default", "upstream_model": "qwen"}],
                },
            },
        }
    }

    compiled = compile_litellm_config_from_registry(registry)
    path = write_litellm_config_artifact(
        registry,
        base_dir=tmp_path,
    )

    assert compiled["model_list"][0]["model_name"] == "assistant-default"
    assert path == config_path
    rendered = config_path.read_text(encoding="utf-8")
    assert "assistant-default" in rendered
    assert "http://127.0.0.1:8080/v1" in rendered


def test_litellm_config_path_defaults_under_base_dir(tmp_path) -> None:
    path = litellm_config_path_from_registry({"services": {}}, base_dir=tmp_path)

    assert path == tmp_path / "data" / "litellm.config.yaml"
