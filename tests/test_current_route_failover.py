"""`current` skips a refused or empty backend and serves the next eligible one, in priority order."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy  # noqa: E402

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
             patch.object(proxy.DERIVED, "LLM_BACKEND_HOSTS", HOSTS if hosts is None else hosts, create=True):
            return await proxy._current_loaded_llm_target(object())

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


def test_candidate_missing_from_hosts_is_skipped_and_first_refusal_kept():
    target, error, _, _ = _resolve(
        healthy={"a", "b"},
        models={"a": "model-a", "b": "model-b"},
        hosts={"b": HOSTS["b"]},
    )
    assert error is None and target.backend == "b"
