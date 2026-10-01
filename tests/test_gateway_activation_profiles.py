from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import yaml

import proxy
from gateway.litellm_substrate import compile_litellm_config_from_registry
from gateway.service_registry import build_service_registry_seed


ROOT = Path(__file__).resolve().parent.parent


def test_configured_aliases_select_sticky_profiles_and_rp_keeps_abliterated_model() -> None:
    config = yaml.safe_load((ROOT / "proxy_config.yml").read_text())
    kobold = config["backends"]["kobold"]
    records = {record["id"]: record for record in kobold["models"]}

    assert kobold["lifecycle"]["driver_config"]["dropin"] == "95-activation.conf"
    for model_id in ("default", "coder", "default-fast", "coder-q5"):
        assert records[model_id]["activation_profile"] == "kv-q5-q4"
        assert records[model_id]["context_window"] == 202752
    assert records["rp"]["activation_profile"] == "kv-q5-q4-rp"
    assert records["rp"]["context_window"] == 217088
    assert records["coder-q8"]["activation_profile"] == "kv-q8"
    assert records["coder-q8"]["context_window"] == 145408
    assert records["coder-q8"]["upstream_model"] == "coder-q8"
    assert records["rp"]["upstream_model"] == "rp"
    assert records["rp"]["backend_model"] == (
        "Huihui-Qwen3.6-27B-abliterated-UD-Q4_K_XL-MTP.gguf"
    )
    assert records["rp"]["activation_model"].endswith(
        "Huihui-Qwen3.6-27B-abliterated-UD-Q4_K_XL-MTP.gguf"
    )
    assert "activation_model" not in records["current"]
    assert "activation_profile" not in records["current"]

    profiles = kobold["lifecycle"]["driver_config"]["activation_profiles"]
    assert profiles["kv-q5-q4"]["LLAMA_BEE_DRAFT_CTK"] == "q5_0"
    assert profiles["kv-q5-q4"]["LLAMA_BEE_DRAFT_CTV"] == "q4_1"
    assert profiles["kv-q5-q4"]["LLAMA_BEE_CTX"] == 202752
    assert profiles["kv-q5-q4-rp"]["LLAMA_BEE_CTX"] == 217088
    assert profiles["kv-q8"]["LLAMA_BEE_CTX"] == 145408

    for role_id in ("default", "coder", "default-fast", "coder-q5", "coder-q8", "rp"):
        role = config["roles"][role_id]
        assert role["activation_profile"] == records[role_id]["activation_profile"]
        assert role["context_window"] == records[role_id]["context_window"]


def test_q8_alias_resolves_profile_as_part_of_outer_activation_identity() -> None:
    config = yaml.safe_load((ROOT / "proxy_config.yml").read_text())
    with patch.object(proxy, "config", config), patch.object(
        proxy,
        "LLM_BACKEND_HOSTS",
        {"kobold": "http://127.0.0.1:5051"},
    ):
        target = proxy._configured_litellm_local_model_target("coder-q8")

    assert target is not None
    assert target.backend == "kobold"
    assert target.profile == "kv-q8"
    assert target.model == config["roles"]["coder-q8"]["model"]


def test_context_catalog_uses_each_profile_limit_without_live_probe() -> None:
    config = yaml.safe_load((ROOT / "proxy_config.yml").read_text())
    with patch.object(proxy, "config", config):
        assert asyncio.run(proxy._context_window_for_model_id("coder-q5")) == 202752
        assert asyncio.run(proxy._context_window_for_model_id("coder-q8")) == 145408
        assert asyncio.run(proxy._context_window_for_model_id("rp")) == 217088


def test_seeded_litellm_compilation_keeps_unique_profile_internal_aliases() -> None:
    config = yaml.safe_load((ROOT / "proxy_config.yml").read_text())
    compiled = compile_litellm_config_from_registry(build_service_registry_seed(config))

    entry = next(
        item for item in compiled["model_list"] if item["model_name"] == "coder-q8"
    )
    assert entry["litellm_params"]["model"] == "openai/coder-q8"
    assert entry["litellm_params"]["api_base"].endswith(
        "/internal/litellm/local/kobold/v1"
    )
    assert entry["model_info"]["gateway_lifecycle"] is True

    rp_entry = next(item for item in compiled["model_list"] if item["model_name"] == "rp")
    assert rp_entry["litellm_params"]["model"] == "openai/rp"
    assert rp_entry["litellm_params"]["api_base"].endswith(
        "/internal/litellm/local/kobold/v1"
    )
