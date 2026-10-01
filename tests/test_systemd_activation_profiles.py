from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.lifecycle_drivers import SwapContext, SystemdDriver


def _service() -> dict:
    return {
        "host": "127.0.0.1",
        "port": 5051,
        "lifecycle": {
            "driver": "systemd",
            "driver_config": {
                "scope": "user",
                "unit": "llama-bee.service",
                "dropin": "95-activation.conf",
                "health_endpoint": "/v1/models",
                "activation_env_keys": {
                    "model": "LLAMA_BEE_MODEL",
                    "aux": "LLAMA_BEE_MMPROJ",
                },
                "activation_profiles": {
                    "kv-q5-q4": {
                        "LLAMA_BEE_CTK": "q5_0",
                        "LLAMA_BEE_CTV": "q4_1",
                        "LLAMA_BEE_DRAFT_CTK": "q5_0",
                        "LLAMA_BEE_DRAFT_CTV": "q4_1",
                        "LLAMA_BEE_CTX": 202752,
                    },
                    "kv-q5-q4-rp": {
                        "LLAMA_BEE_CTK": "q5_0",
                        "LLAMA_BEE_CTV": "q4_1",
                        "LLAMA_BEE_CTX": 217088,
                    },
                    "kv-q8": {
                        "LLAMA_BEE_CTK": "q8_0",
                        "LLAMA_BEE_CTV": "q8_0",
                        "LLAMA_BEE_CTX": 145408,
                    },
                },
            },
        },
    }


def test_named_profile_resolves_to_driver_owned_env_and_unknown_fails_closed() -> None:
    driver = SystemdDriver()
    q8 = driver.build_activation(
        _service(),
        model="/models/qwen.gguf",
        profile="kv-q8",
    )

    assert q8 == {
        "env": {
            "LLAMA_BEE_MODEL": "/models/qwen.gguf",
            "LLAMA_BEE_MMPROJ": "",
            "LLAMA_BEE_CTK": "q8_0",
            "LLAMA_BEE_CTV": "q8_0",
            "LLAMA_BEE_CTX": "145408",
        },
        "profile": "kv-q8",
    }
    assert driver.build_activation(
        _service(),
        model="/models/qwen.gguf",
        profile="not-configured",
    ) is None


def test_exact_model_aux_and_profile_is_noop_but_profile_change_restarts(tmp_path: Path) -> None:
    driver = SystemdDriver()
    service = _service()
    model = "/models/qwen.gguf"
    q5 = driver.build_activation(service, model=model, profile="kv-q5-q4")
    q8 = driver.build_activation(service, model=model, profile="kv-q8")
    assert q5 is not None and q8 is not None

    dropin = tmp_path / "95-activation.conf"
    dropin.write_text(driver._dropin_text(q5))
    ctx_q5 = SwapContext(
        loaded_model=model,
        target_model=model,
        target_profile="kv-q5-q4",
    )
    with patch.object(driver, "_dropin_path", return_value=dropin), patch(
        "asyncio.create_subprocess_exec"
    ) as subprocess_exec:
        assert asyncio.run(driver.swap(service, q5, ctx=ctx_q5)) is True
    subprocess_exec.assert_not_called()

    process = AsyncMock()
    process.returncode = 0
    process.communicate = AsyncMock(return_value=(b"", b""))
    response = MagicMock(status_code=200)
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    client_context = MagicMock()
    client_context.__aenter__ = AsyncMock(return_value=client)
    client_context.__aexit__ = AsyncMock(return_value=False)
    ctx_q8 = SwapContext(
        loaded_model=model,
        target_model=model,
        target_profile="kv-q8",
    )
    with patch.object(driver, "_dropin_path", return_value=dropin), patch(
        "asyncio.create_subprocess_exec", return_value=process
    ) as subprocess_exec, patch("httpx.AsyncClient", return_value=client_context):
        assert asyncio.run(driver.swap(service, q8, ctx=ctx_q8)) is True

    assert subprocess_exec.call_count == 2
    assert subprocess_exec.call_args_list[1].args == (
        "systemctl",
        "--user",
        "restart",
        "llama-bee.service",
    )
    assert dropin.read_text() == driver._dropin_text(q8)
