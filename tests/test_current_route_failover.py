"""`current` skips a refused or empty backend and serves the next eligible one, in priority order."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402
from tests.support.config_snapshot import derived_patch  # noqa: E402

HOSTS = {"a": "http://127.0.0.1:59001", "b": "http://127.0.0.1:59002", "c": "http://127.0.0.1:59003"}


def _resolve(*, healthy, models, overrides=None, order=("a", "b", "c"), hosts=None):
    """Run the resolver with stubbed seams; returns (target, error, health_calls, model_calls)."""
    health_calls: list[str] = []
    model_calls: list[str] = []

    async def health(bid):
        health_calls.append(bid)
        return bid in healthy

    async def model(bid):
        model_calls.append(bid)
        return models.get(bid, "")

    async def run():
        with patch.object(proxy, "_llm_backend_candidates", lambda: list(order)), \
             patch.object(proxy, "check_service_health", health), \
             patch.object(proxy, "current_llm_model_for_backend", model), \
             patch.object(proxy, "_operator_service_availability_overrides", lambda: overrides or {}), \
             derived_patch("LLM_BACKEND_HOSTS", HOSTS if hosts is None else hosts):
            return await proxy._current_loaded_llm_target(cast(Any, object()))

    target, error = asyncio.run(run())
    return target, error, health_calls, model_calls


@pytest.mark.parametrize("state", ["disabled", "maintenance", "draining"])
def test_overridden_first_candidate_falls_through_to_second(state):
    target, error, _, model_calls = _resolve(
        healthy={"a", "b"},
        models={"a": "model-a", "b": "model-b"},
        overrides={"a": {"state": state}},
    )
    assert error is None
    assert (target.backend, target.model, target.host) == ("b", "model-b", HOSTS["b"])
    assert model_calls == ["b"]


def test_first_healthy_without_loaded_model_falls_through():
    target, error, _, model_calls = _resolve(healthy={"a", "b"}, models={"b": "model-b"})
    assert error is None
    assert target.backend == "b" and target.model == "model-b"
    assert model_calls == ["a", "b"]


def test_first_eligible_wins_and_later_candidates_are_not_probed():
    target, error, health_calls, model_calls = _resolve(
        healthy={"a", "b"}, models={"a": "model-a", "b": "model-b"},
    )
    assert error is None and target.backend == "a"
    assert health_calls == ["a"]
    assert model_calls == ["a"]


def test_all_overridden_returns_first_refusal_and_reads_no_model():
    target, error, _, model_calls = _resolve(
        healthy={"a", "b"},
        models={"a": "model-a", "b": "model-b"},
        overrides={
            "a": {"state": "maintenance", "retry_after_seconds": 33},
            "b": {"state": "draining", "retry_after_seconds": 77},
        },
    )
    assert target is None
    assert error.status_code == 503
    assert error.headers["retry-after"] == "33"
    assert model_calls == []


def test_none_healthy_returns_no_active_backend_error():
    target, error, _, model_calls = _resolve(healthy=set(), models={"a": "model-a"})
    assert target is None
    assert error.status_code == 503
    assert b"no local LLM backend is active" in error.body
    assert model_calls == []


def test_unhealthy_first_overridden_second_loaded_third():
    target, error, _, model_calls = _resolve(
        healthy={"b", "c"},
        models={"b": "model-b", "c": "model-c"},
        overrides={"b": {"state": "disabled"}},
    )
    assert error is None
    assert target.backend == "c" and target.model == "model-c"
    assert model_calls == ["c"]


def test_candidate_missing_from_hosts_is_skipped():
    target, error, _, _ = _resolve(
        healthy={"a", "b"},
        models={"a": "model-a", "b": "model-b"},
        hosts={"b": HOSTS["b"]},
    )
    assert error is None and target.backend == "b"


# -- vision gate and background removal use the same selector ------------------------------------------

IMAGE_BODY = {
    "model": "not-a-local-model",
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "look"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGVsbG8="}},
    ]}],
}


def _vision_gate(*, healthy, models, overrides=None, vision_members=("b",)):
    """Run the vision gate with stubbed seams; returns (error, backends the gate asked about)."""
    judged: list[Any] = []

    async def health(bid):
        return bid in healthy

    async def model(bid):
        return models.get(bid, "")

    def members():
        return list(vision_members)

    def aux_grants(backend):
        judged.append(backend)
        return False

    async def run():
        with patch.object(proxy, "_llm_backend_candidates", lambda: ["a", "b", "c"]), \
             patch.object(proxy, "check_service_health", health), \
             patch.object(proxy, "current_llm_model_for_backend", model), \
             patch.object(proxy, "_operator_service_availability_overrides", lambda: overrides or {}), \
             patch.object(proxy, "vision_capable_llm_backends", members), \
             patch.object(proxy, "_aux_grants_vision", aux_grants), \
             derived_patch("LLM_BACKEND_HOSTS", HOSTS):
            return await proxy._prepare_litellm_local_llm_request(
                proxy.NO_AUTH_CONTEXT, request_path="/v1/chat/completions",
                body_json=IMAGE_BODY, activate_target=False,
            )

    error, _ = asyncio.run(run())
    return error, judged


def test_vision_gate_judges_the_second_backend_when_the_first_is_drained():
    # The drained first backend is text-only; the loaded second backend takes images, so the gate returns early.
    error, judged = _vision_gate(
        healthy={"a", "b"}, models={"a": "model-a", "b": "model-b"},
        overrides={"a": {"state": "draining"}},
    )
    assert error is None
    assert "a" not in judged


def test_vision_gate_does_not_pass_on_a_drained_vision_backend():
    # b is vision capable but drained and a is empty: nothing is servable, so the gate gets no backend.
    error, judged = _vision_gate(
        healthy={"a", "b"}, models={"b": "model-b"}, overrides={"b": {"state": "disabled"}},
    )
    assert judged == [None]  # no target: the gate judged "no active backend", as before


def test_vision_gate_without_any_healthy_backend_keeps_the_no_target_path():
    error, judged = _vision_gate(healthy=set(), models={})
    assert judged == [None]


def _decide_background(*, healthy, models, overrides=None, local_swap=("a", "b", "c")):
    """Run the background-removal decision; returns (decision, hosts it posted to, models it sent)."""
    import httpx
    posted: list[tuple[str, str]] = []

    async def health(bid):
        return bid in healthy

    async def model(bid):
        return models.get(bid, "")

    class Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            posted.append((url, json["model"]))
            return httpx.Response(200, json={"choices": [{"message": {"content":
                '{"remove_background": true, "confidence": 0.9, "reason": "logo"}'}}]})

    async def run():
        with patch.object(proxy, "_image_background_removal_enabled", lambda: True), \
             patch.object(proxy, "_llm_backend_candidates", lambda: ["a", "b", "c"]), \
             patch.object(proxy, "check_service_health", health), \
             patch.object(proxy, "current_llm_model_for_backend", model), \
             patch.object(proxy, "_operator_service_availability_overrides", lambda: overrides or {}), \
             patch.object(proxy, "_is_local_swap_backend", lambda bid: bid in local_swap), \
             patch.object(proxy, "_require_egress_lease", lambda service: None), \
             patch.object(proxy.engine_egress, "async_client", Client), \
             derived_patch("LLM_BACKEND_HOSTS", HOSTS):
            return await proxy.decide_image_background_removal("a logo", auth_context=proxy.NO_AUTH_CONTEXT)

    decision = asyncio.run(run())
    return decision, posted


def test_background_removal_goes_to_the_second_backend_when_the_first_is_drained():
    decision, posted = _decide_background(
        healthy={"a", "b"}, models={"a": "model-a", "b": "model-b"},
        overrides={"a": {"state": "draining"}},
    )
    assert decision.source == "llm"
    assert posted == [(f"{HOSTS['b']}/v1/chat/completions", "model-b")]


def test_background_removal_skips_a_non_local_first_backend():
    decision, posted = _decide_background(
        healthy={"a", "b"}, models={"a": "model-a", "b": "model-b"}, local_swap={"b"},
    )
    assert decision.source == "llm"
    assert posted == [(f"{HOSTS['b']}/v1/chat/completions", "model-b")]


def test_background_removal_with_nothing_servable_falls_back_and_sends_nothing():
    decision, posted = _decide_background(
        healthy={"a", "b"}, models={"b": "model-b"}, overrides={"b": {"state": "disabled"}},
    )
    assert decision.source == "llm_unavailable"
    assert posted == []
