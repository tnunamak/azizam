from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import yaml

import proxy
from gateway.litellm_substrate import compile_litellm_config_from_registry
from gateway.service_registry import build_service_registry_seed


ROOT = Path(__file__).resolve().parent.parent


def test_configured_aliases_share_one_uncensored_model_and_sticky_profile() -> None:
    config = yaml.safe_load((ROOT / "proxy_config.yml").read_text())
    kobold = config["backends"]["kobold"]
    records = {record["id"]: record for record in kobold["models"]}
    model_file = "Qwen3.8-27B-Uncensored-Q4_K_M.gguf"

    assert kobold["lifecycle"]["driver_config"]["dropin"] == "95-activation.conf"
    # One GGUF serves every chat role so role changes never swap models.
    for model_id in (
        "default", "coder", "default-fast", "coder-q5", "coder-unchained", "rp",
    ):
        record = records[model_id]
        assert record["activation_profile"] == "kv-q5-q4", model_id
        assert record["context_window"] == 202752, model_id
        assert record["activation_model"].endswith(model_file), model_id
        assert record["activation_mmproj"].endswith("Uncensored-mmproj-F16.gguf"), model_id
    assert records["coder-q8"]["activation_profile"] == "kv-q8"
    assert records["coder-q8"]["context_window"] == 145408
    assert records["coder-q8"]["upstream_model"] == "coder-q8"
    assert records["rp"]["upstream_model"] == "rp"
    assert records["rp"]["backend_model"] == model_file
    assert "activation_model" not in records["current"]
    assert "activation_profile" not in records["current"]

    profiles = kobold["lifecycle"]["driver_config"]["activation_profiles"]
    assert profiles["kv-q5-q4"]["LLAMA_BEE_DRAFT_CTK"] == "q5_0"
    assert profiles["kv-q5-q4"]["LLAMA_BEE_DRAFT_CTV"] == "q4_1"
    assert profiles["kv-q5-q4"]["LLAMA_BEE_CTX"] == 202752
    assert profiles["kv-q8"]["LLAMA_BEE_CTX"] == 145408

    for role_id in (
        "default", "coder", "default-fast", "coder-q5", "coder-q8", "rp",
    ):
        role = config["roles"][role_id]
        assert role["activation_profile"] == records[role_id]["activation_profile"]
        assert role["context_window"] == records[role_id]["context_window"]
        assert role["model"] == records[role_id]["activation_model"]


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
        assert asyncio.run(proxy._context_window_for_model_id("rp")) == 202752


def test_context_catalog_reads_a_distinct_profile_limit_from_config() -> None:
    # Self-contained: a profile with its own limit must win over the shared one,
    # independent of which profiles the shipped config assigns to roles.
    config = yaml.safe_load((ROOT / "proxy_config.yml").read_text())
    kobold = config["backends"]["kobold"]
    profiles = kobold["lifecycle"]["driver_config"]["activation_profiles"]
    profiles["kv-test-large"] = {**profiles["kv-q5-q4"], "LLAMA_BEE_CTX": 217088}
    for record in kobold["models"]:
        if record["id"] == "rp":
            record["activation_profile"] = "kv-test-large"
            record["context_window"] = 217088
    config["roles"]["rp"]["activation_profile"] = "kv-test-large"
    config["roles"]["rp"]["context_window"] = 217088
    with patch.object(proxy, "config", config), patch.object(
        proxy, "ROLES", config["roles"]
    ):
        assert asyncio.run(proxy._context_window_for_model_id("rp")) == 217088
        assert asyncio.run(proxy._context_window_for_model_id("coder-q5")) == 202752


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
