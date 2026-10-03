from __future__ import annotations

from gateway.litellm_substrate import (
    compile_litellm_config_from_registry,
    compile_litellm_substrate_config,
    litellm_config_path_from_registry,
    litellm_known_model_names,
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


def test_no_auth_container_engine_gets_sdk_placeholder_key() -> None:
    compiled = compile_litellm_substrate_config({"chat": {"config": {
        "protocol": "openai-compatible", "base_url": "http://engine:18001/v1",
        "models": ["fresh-test"],
    }}})
    params = compiled["model_list"][0]["litellm_params"]
    assert params["api_base"] == "http://engine:18001/v1"
    assert params["api_key"] == "sk-local"


def test_missing_custom_endpoint_does_not_get_placeholder_key() -> None:
    compiled = compile_litellm_substrate_config({"chat": {"config": {
        "protocol": "openai-compatible", "models": ["fresh-test"],
    }}})
    params = compiled["model_list"][0]["litellm_params"]
    assert "api_base" not in params
    assert "api_key" not in params


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
    assert compiled["general_settings"] == {
        "litellm_key_header_name": "X-Litellm-Key",
        "forward_client_headers_to_llm_api": True,
    }
    assert "metadata" not in compiled


def test_forwards_gateway_request_id_header_through_litellm() -> None:
    """LiteLLM 1.86.2 is a separate sidecar; the gateway relies on this
    setting to carry its cancellation-correlation header
    (X-AI-Gateway-Request-Id) through to the internal gateway hop behind
    LiteLLM, since LiteLLM does not propagate a cancel signal of its own on
    this version. See gateway/backends/cancellation.py.
    """
    compiled = compile_litellm_substrate_config({})
    assert compiled["general_settings"]["forward_client_headers_to_llm_api"] is True


def test_lifecycle_service_exposes_current_loaded_aliases() -> None:
    compiled = compile_litellm_substrate_config(
        {
            "local": {
                "id": "local",
                "name": "Local mutable engine",
                "capabilities": ["openai"],
                "config": {
                    "protocol": "openai-compatible",
                    "gateway_lifecycle": True,
                    "models": [{"id": "default", "upstream_model": "loaded.gguf"}],
                },
            }
        }
    )

    entries = {entry["model_name"]: entry for entry in compiled["model_list"]}
    assert entries["current"]["litellm_params"] == {
        "model": "openai/current",
        "api_base": "http://127.0.0.1:5000/internal/litellm/current/v1",
        "api_key": "os.environ/AI_GATEWAY_INTERNAL_LITELLM_KEY",
    }
    assert entries["current"]["model_info"]["gateway_current_loaded"] is True
    assert entries["current-fast"]["litellm_params"]["model"] == "openai/current-fast"
    assert entries["current-fast"]["model_info"]["gateway_current_loaded_fast"] is True


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
    # The default artifact lives directly in the caller-provided base_dir (the
    # gateway's state dir, next to admin.db) so it honors store isolation
    # instead of being pinned to a repo-relative data/ directory.
    path = litellm_config_path_from_registry({"services": {}}, base_dir=tmp_path)

    assert path == tmp_path / "litellm.config.yaml"


def test_lifecycle_backed_models_route_through_gateway_internal_target() -> None:
    compiled = compile_litellm_substrate_config(
        {
            "local-kobold": {
                "id": "local-kobold",
                "name": "Local mutable GGUF engine",
                "capabilities": ["openai"],
                "config": {
                    "protocol": "openai-compatible",
                    "base_url": "http://127.0.0.1:5051/v1",
                    "models": [
                        {
                            "id": "coder",
                            "model_group": "coder",
                            "upstream_model": "Qwen3.6-27B-UD-Q4_K_XL-mtp.gguf",
                            "gateway_lifecycle": True,
                            "gateway_backend": "kobold",
                        }
                    ],
                },
            }
        }
    )

    assert compiled is not None
    entry = compiled["model_list"][0]
    assert entry["model_name"] == "coder"
    assert entry["litellm_params"] == {
        "model": "openai/Qwen3.6-27B-UD-Q4_K_XL-mtp.gguf",
        "api_base": "http://127.0.0.1:5000/internal/litellm/local/kobold/v1",
        "api_key": "os.environ/AI_GATEWAY_INTERNAL_LITELLM_KEY",
    }
    assert entry["model_info"] == {
        "gateway_lifecycle": True,
        "gateway_backend": "kobold",
        "gateway_upstream_model": "Qwen3.6-27B-UD-Q4_K_XL-mtp.gguf",
    }


def test_lifecycle_backed_service_can_override_gateway_internal_target() -> None:
    compiled = compile_litellm_substrate_config(
        {
            "local": {
                "id": "local",
                "name": "Local mutable engine",
                "capabilities": ["openai"],
                "config": {
                    "protocol": "openai-compatible",
                    "gateway_lifecycle": True,
                    "gateway_lifecycle_base_url": "http://gateway.internal/v1",
                    "gateway_lifecycle_api_key_ref": "os.environ/CUSTOM_INTERNAL_KEY",
                    "models": {"default": "loaded-model.gguf"},
                },
            }
        }
    )

    assert compiled is not None
    entry = compiled["model_list"][0]
    assert entry["litellm_params"]["api_base"] == "http://gateway.internal/v1"
    assert entry["litellm_params"]["api_key"] == "os.environ/CUSTOM_INTERNAL_KEY"
    assert entry["model_info"]["gateway_lifecycle"] is True
    assert entry["model_info"]["gateway_backend"] == "local"


def test_litellm_known_model_names_returns_model_name_set() -> None:
    registry = {
        "services": {
            "openai-compat": {
                "name": "Test Backend",
                "enabled": True,
                "capabilities": ["chat"],
                "config": {
                    "protocol": "openai",
                    "host": "localhost",
                    "port": 11434,
                    "models": {"llama3": "llama3:8b", "mistral": "mistral:7b"},
                },
            }
        }
    }
    names = litellm_known_model_names(registry)
    assert isinstance(names, frozenset)
    assert "llama3" in names
    assert "mistral" in names


def test_litellm_known_model_names_empty_for_no_services() -> None:
    names = litellm_known_model_names({})
    assert names == frozenset()


def test_litellm_known_model_names_empty_for_non_openai_service() -> None:
    registry = {
        "services": {
            "comfyui": {
                "name": "ComfyUI",
                "enabled": True,
                "capabilities": ["image_generation"],
                "config": {"protocol": "comfyui", "host": "localhost"},
            }
        }
    }
    names = litellm_known_model_names(registry)
    assert names == frozenset()
