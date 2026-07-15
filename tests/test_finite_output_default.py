"""Finite aggregate-output defaults are selected from canonical service data."""

from __future__ import annotations

import copy
import json
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx
import pytest
import yaml

import proxy
import responses_translator
from gateway import service_registry
from gateway.transforms import run_request_pipeline


_DEFAULT_CAP = 16_384
_DEFAULT_TRANSFORMS = [
    {
        "kind": "param-map",
        "from": "max_completion_tokens",
        "to": "max_tokens",
        "only_if_destination_absent": True,
    },
    {"kind": "default-params", "set": {"max_tokens": _DEFAULT_CAP}},
]


class _FakeClient:
    async def aclose(self) -> None:
        return None


class _FakeResponse:
    status_code = 200
    headers = {"content-type": "application/json"}

    async def aread(self) -> bytes:
        return b'{"choices":[{"message":{"role":"assistant","content":"ok"}}]}'

    async def aclose(self) -> None:
        return None


def _service_config() -> dict[str, Any]:
    return {
        "models": [
            {
                "id": "default",
                "model_group": "default",
                "upstream_model": "default.gguf",
                "activation_model": "/models/default.gguf",
                "transforms": _DEFAULT_TRANSFORMS,
            },
            {
                "id": "coder",
                "model_group": "coder",
                "upstream_model": "coder.gguf",
                "activation_model": "/models/coder.gguf",
            },
            {
                "id": "rp",
                "model_group": "rp",
                "upstream_model": "rp.gguf",
                "activation_model": "/models/rp.gguf",
            },
            {
                "id": "default-fast",
                "model_group": "default-fast",
                "base_model_id": "default",
                "upstream_model": "default.gguf",
                "activation_model": "/models/default.gguf",
                "policy_overlay": [
                    {"kind": "default-params", "set": {"reasoning_effort": "none"}}
                ],
            },
            {
                "id": "current",
                "model_group": "current",
                "base_model_id": "default",
                "upstream_model": "default.gguf",
            },
            {
                "id": "current-fast",
                "model_group": "current-fast",
                "base_model_id": "default",
                "upstream_model": "default.gguf",
                "policy_overlay": [
                    {"kind": "default-params", "set": {"reasoning_effort": "none"}}
                ],
            },
        ],
        "capabilities": ["chat", "completions"],
    }


def _litellm_registry() -> dict[str, Any]:
    return {
        "services": {
            "litellm": {
                "id": "litellm",
                "kind": "llm-substrate",
                "enabled": True,
                "capabilities": ["llm.execution"],
                "config": {"base_url": "http://fake-litellm"},
            }
        }
    }


@contextmanager
def _local_model_config() -> Iterator[None]:
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(proxy, "config", {"backends": {"local": _service_config()}})
        )
        stack.enter_context(
            patch.object(proxy, "LLM_BACKEND_HOSTS", {"local": "http://fake-local"})
        )
        yield


def _apply_model_policy(body: dict[str, Any]) -> dict[str, Any]:
    with _local_model_config():
        target = proxy._resolve_request_target(body["model"])
        assert target is not None
        return run_request_pipeline(
            list(target.transform_specs),
            copy.deepcopy(body),
        )


def _forward_to_fake_upstream(body: dict[str, Any]) -> dict[str, Any]:
    captured: list[dict[str, Any]] = []

    async def fake_send(execution_request, **_kwargs):
        captured.append(json.loads(execution_request.body))
        return _FakeClient(), _FakeResponse()

    client = TestClient(proxy.app)
    with _local_model_config(), patch.object(proxy, "AUTH_ENABLED", False), patch.object(
        proxy.ADMIN_STORE, "get_service_registry", _litellm_registry
    ), patch.object(proxy, "ensure_target", AsyncMock(return_value=True)), patch.object(
        proxy, "send_llm_execution_request", fake_send
    ), patch.object(proxy, "_service_availability_response", lambda *_args, **_kwargs: None):
        response = client.post("/v1/chat/completions", json=body)

    assert response.status_code == 200, response.text
    assert len(captured) == 1
    return captured[0]


def _forward_current_to_fake_upstream(
    body: dict[str, Any], *, loaded_model: str = "manual.gguf"
) -> dict[str, Any]:
    captured: list[dict[str, Any]] = []

    async def fake_send(execution_request, **_kwargs):
        captured.append(json.loads(execution_request.body))
        return _FakeClient(), _FakeResponse()

    client = TestClient(proxy.app)
    with _local_model_config(), patch.object(proxy, "AUTH_ENABLED", False), patch.object(
        proxy, "get_active_backend", AsyncMock(return_value="local")
    ), patch.object(
        proxy, "current_llm_model_for_backend", AsyncMock(return_value=loaded_model)
    ), patch.object(
        proxy, "_service_availability_response", lambda *_args, **_kwargs: None
    ), patch.object(proxy, "send_llm_execution_request", fake_send):
        response = client.post("/v1/chat/completions", json=body)

    assert response.status_code == 200, response.text
    assert len(captured) == 1
    return captured[0]


def test_bootstrap_seed_keeps_the_default_model_output_policy_as_data() -> None:
    root = Path(__file__).resolve().parent.parent
    bootstrap = yaml.safe_load((root / "proxy_config.yml").read_text(encoding="utf-8"))
    registry = service_registry.build_service_registry_seed(bootstrap)
    models = registry["services"]["kobold"]["config"]["models"]
    default = next(model for model in models if model["id"] == "default")
    default_fast = next(model for model in models if model["id"] == "default-fast")
    current = next(model for model in models if model["id"] == "current")
    current_fast = next(model for model in models if model["id"] == "current-fast")
    coder = next(model for model in models if model["id"] == "coder")
    rp = next(model for model in models if model["id"] == "rp")

    assert default["transforms"] == _DEFAULT_TRANSFORMS
    assert default_fast["base_model_id"] == "default"
    assert current["base_model_id"] == "default"
    assert current_fast["base_model_id"] == "default"
    assert default_fast["policy_overlay"] == current_fast["policy_overlay"]
    assert "transforms" not in coder
    assert "transforms" not in rp


@pytest.mark.parametrize("explicit_cap", [None, 0, 19])
def test_default_model_forwards_omitted_or_explicit_max_tokens(
    explicit_cap: int | None,
) -> None:
    body: dict[str, Any] = {"model": "default", "messages": []}
    if explicit_cap is not None:
        body["max_tokens"] = explicit_cap

    forwarded = _forward_to_fake_upstream(body)

    assert forwarded["max_tokens"] == (
        _DEFAULT_CAP if explicit_cap is None else explicit_cap
    )


@pytest.mark.parametrize(
    ("body", "expected_cap"),
    [
        ({"model": "default", "messages": [], "max_completion_tokens": 0}, 0),
        ({"model": "default", "messages": [], "max_completion_tokens": 19}, 19),
        (
            {
                "model": "default",
                "messages": [],
                "max_tokens": 0,
                "max_completion_tokens": 19,
            },
            0,
        ),
    ],
)
def test_explicit_chat_completion_caps_remain_authoritative(
    body: dict[str, Any], expected_cap: int
) -> None:
    forwarded = _forward_to_fake_upstream(body)

    assert forwarded["max_tokens"] == expected_cap
    assert "max_completion_tokens" not in forwarded


@pytest.mark.parametrize(
    ("response_request", "expected_cap"),
    [
        ({"max_output_tokens": 0}, 0),
        ({"max_output_tokens": 19}, 19),
        ({"max_completion_tokens": 0}, 0),
        ({"max_completion_tokens": 19}, 19),
        ({"max_output_tokens": 0, "max_completion_tokens": 19}, 0),
    ],
)
def test_responses_output_cap_spellings_normalize_before_defaulting(
    response_request: dict[str, int], expected_cap: int
) -> None:
    canonical = responses_translator.to_chat_completions(
        {"model": "default", "input": "shape only", **response_request}
    )

    assert canonical["max_tokens"] == expected_cap
    assert _forward_to_fake_upstream(canonical)["max_tokens"] == expected_cap


def test_responses_max_completion_tokens_reaches_chat_route_before_defaulting() -> None:
    captured: list[dict[str, Any]] = []

    async def fake_forward(_request, _auth_context, cc_body, *, stream):
        assert stream is False
        captured.append(copy.deepcopy(cc_body))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-finite-cap",
                "object": "chat.completion",
                "model": "default",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            },
            request=httpx.Request("POST", "http://gateway/v1/chat/completions"),
        )

    client = TestClient(proxy.app)
    with patch.object(proxy, "AUTH_ENABLED", False), patch.object(
        proxy, "_forward_chat_completions", fake_forward
    ):
        response = client.post(
            "/v1/responses",
            json={
                "model": "default",
                "input": "shape only",
                "max_completion_tokens": 19,
                "store": False,
            },
        )

    assert response.status_code == 200, response.text
    assert captured == [
        {
            "model": "default",
            "messages": [{"role": "user", "content": "shape only"}],
            "max_tokens": 19,
        }
    ]


def test_responses_normalization_precedes_alias_defaulting_at_final_egress() -> None:
    canonical = responses_translator.to_chat_completions(
        {"model": "default-fast", "input": "shape only", "max_output_tokens": 0}
    )

    forwarded = _forward_to_fake_upstream(canonical)

    assert forwarded["max_tokens"] == 0
    assert forwarded["thinking_budget_tokens"] == 0


def test_default_policy_is_idempotent_and_does_not_change_reasoning_fields() -> None:
    body = {
        "model": "default",
        "messages": [],
        "thinking_budget_tokens": 2048,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    first = _apply_model_policy(body)
    second = _apply_model_policy(first)

    assert first == second
    assert second["max_tokens"] == _DEFAULT_CAP
    assert second["thinking_budget_tokens"] == 2048
    assert second["chat_template_kwargs"] == {"enable_thinking": False}


def test_default_policy_does_not_leak_to_coder_or_rp() -> None:
    default = _apply_model_policy({"model": "default", "messages": []})
    coder = _apply_model_policy({"model": "coder", "messages": []})
    rp = _apply_model_policy({"model": "rp", "messages": []})

    assert default["max_tokens"] == _DEFAULT_CAP
    assert "max_tokens" not in coder
    assert "max_tokens" not in rp


@pytest.mark.parametrize("model", ["default-fast", "current-fast"])
def test_fast_aliases_inherit_the_base_cap_and_default_reasoning_only_when_omitted(
    model: str,
) -> None:
    if model == "current-fast":
        forwarded = _forward_current_to_fake_upstream({"model": model, "messages": []})
    else:
        forwarded = _forward_to_fake_upstream({"model": model, "messages": []})

    assert forwarded["max_tokens"] == _DEFAULT_CAP
    assert forwarded["thinking_budget_tokens"] == 0
    assert forwarded["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize(("cap_field", "explicit_cap"), [("max_tokens", 0), ("max_completion_tokens", 19)])
def test_current_alias_keeps_explicit_caps_and_never_activates(
    cap_field: str,
    explicit_cap: int,
) -> None:
    forwarded = _forward_current_to_fake_upstream(
        {"model": "current", "messages": [], cap_field: explicit_cap},
        loaded_model="manual-experiment.gguf",
    )

    assert forwarded == {
        "model": "manual-experiment.gguf",
        "messages": [],
        "max_tokens": explicit_cap,
    }


def test_fast_alias_explicit_reasoning_wins_over_the_declarative_default() -> None:
    forwarded = _forward_current_to_fake_upstream(
        {"model": "current-fast", "messages": [], "reasoning_effort": "high"}
    )

    assert forwarded["max_tokens"] == _DEFAULT_CAP
    assert forwarded["thinking_budget_tokens"] == 8192
    assert "chat_template_kwargs" not in forwarded


def test_named_litellm_request_runs_request_policy_exactly_once_before_egress() -> None:
    calls = 0
    original_pipeline = proxy._run_request_pipeline

    def counted_pipeline(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return original_pipeline(*args, **kwargs)

    with patch.object(proxy, "_run_request_pipeline", counted_pipeline):
        forwarded = _forward_to_fake_upstream({"model": "default", "messages": []})

    assert calls == 1
    assert forwarded["max_tokens"] == _DEFAULT_CAP
