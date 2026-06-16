"""Step 5 tests — Tier-1 per-backend transform packs.

GATE: Each named transform (ollama-unsupported-params, drop-null-params,
llamacpp-cache-prompt, mistral-penalty-clamp, tabby-penalty-floor) must:
  1. Apply correctly when its condition is met.
  2. Be a no-op when the `when:` condition is unmet (or when the target param
     is already in range / absent).
  3. NOT alter the current 8-service config behavior — these are additive,
     suggested-on-add, not auto-attached.

The tests are unit tests against the REGISTRY (not integration tests against
the running proxy).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from gateway.transforms import (
    REGISTRY,
    run_request_pipeline,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _req(name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Run the named transform on a deep copy of body."""
    return run_request_pipeline([name], copy.deepcopy(body))


# ---------------------------------------------------------------------------
# ollama-unsupported-params
# ---------------------------------------------------------------------------


class TestOllamaUnsupportedParams:
    NAME = "ollama-unsupported-params"

    def test_registered(self) -> None:
        primitives = REGISTRY.resolve(self.NAME)
        assert len(primitives) == 1

    @pytest.mark.parametrize("param", ["n", "logprobs", "top_logprobs", "logit_bias", "tool_choice", "user"])
    def test_drops_each_unsupported_param(self, param: str) -> None:
        body = {param: "some_value", "model": "llama3", "messages": []}
        result = _req(self.NAME, body)
        assert param not in result, f"Expected {param!r} to be dropped"

    def test_drops_all_six_params_together(self) -> None:
        body = {
            "n": 2,
            "logprobs": True,
            "top_logprobs": 3,
            "logit_bias": {1234: 5},
            "tool_choice": "auto",
            "user": "u123",
            "model": "llama3",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        result = _req(self.NAME, body)
        for param in ["n", "logprobs", "top_logprobs", "logit_bias", "tool_choice", "user"]:
            assert param not in result
        # Other params preserved
        assert result["model"] == "llama3"
        assert result["messages"] == [{"role": "user", "content": "Hello"}]

    def test_absent_param_is_noop(self) -> None:
        body = {"model": "llama3", "messages": []}
        result = _req(self.NAME, body)
        assert result == body

    def test_does_not_touch_unrelated_params(self) -> None:
        body = {"model": "llama3", "temperature": 0.7, "max_tokens": 512}
        result = _req(self.NAME, body)
        assert result == body


# ---------------------------------------------------------------------------
# drop-null-params
# ---------------------------------------------------------------------------


class TestDropNullParams:
    NAME = "drop-null-params"

    def test_registered(self) -> None:
        primitives = REGISTRY.resolve(self.NAME)
        assert len(primitives) == 1

    def test_drops_null_valued_keys(self) -> None:
        body = {"model": "llama3", "temperature": None, "max_tokens": None, "stream": True}
        result = _req(self.NAME, body)
        assert "temperature" not in result
        assert "max_tokens" not in result
        assert result["model"] == "llama3"
        assert result["stream"] is True

    def test_preserves_false_and_zero(self) -> None:
        """False and 0 are not None — must NOT be stripped."""
        body = {"model": "llama3", "stream": False, "n": 0, "temperature": 0.0}
        result = _req(self.NAME, body)
        assert "stream" in result
        assert result["stream"] is False
        assert "n" in result
        assert result["n"] == 0
        assert "temperature" in result
        assert result["temperature"] == 0.0

    def test_preserves_empty_string_and_empty_list(self) -> None:
        """Empty string and empty list are not None — must NOT be stripped."""
        body = {"model": "llama3", "user": "", "tools": []}
        result = _req(self.NAME, body)
        assert "user" in result
        assert "tools" in result

    def test_empty_body_is_noop(self) -> None:
        result = _req(self.NAME, {})
        assert result == {}

    def test_no_null_values_is_noop(self) -> None:
        body = {"model": "llama3", "temperature": 0.7}
        result = _req(self.NAME, body)
        assert result == body

    def test_all_null_keys_removed(self) -> None:
        body = {"a": None, "b": None, "c": "keep"}
        result = _req(self.NAME, body)
        assert "a" not in result
        assert "b" not in result
        assert result["c"] == "keep"


# ---------------------------------------------------------------------------
# llamacpp-cache-prompt
# ---------------------------------------------------------------------------


class TestLlamaCppCachePrompt:
    NAME = "llamacpp-cache-prompt"

    def test_registered(self) -> None:
        primitives = REGISTRY.resolve(self.NAME)
        assert len(primitives) == 1

    def test_sets_cache_prompt_true_when_absent(self) -> None:
        body = {"model": "llama3", "messages": []}
        result = _req(self.NAME, body)
        assert result["cache_prompt"] is True

    def test_does_not_overwrite_existing_false(self) -> None:
        """setdefault semantics — explicit false must be preserved."""
        body = {"model": "llama3", "cache_prompt": False}
        result = _req(self.NAME, body)
        assert result["cache_prompt"] is False

    def test_does_not_overwrite_existing_true(self) -> None:
        body = {"model": "llama3", "cache_prompt": True}
        result = _req(self.NAME, body)
        assert result["cache_prompt"] is True

    def test_does_not_touch_unrelated_params(self) -> None:
        body = {"model": "llama3", "temperature": 0.5}
        result = _req(self.NAME, body)
        assert result["temperature"] == 0.5
        assert result["cache_prompt"] is True


# ---------------------------------------------------------------------------
# mistral-penalty-clamp
# ---------------------------------------------------------------------------


class TestMistralPenaltyClamp:
    NAME = "mistral-penalty-clamp"

    def test_registered(self) -> None:
        primitives = REGISTRY.resolve(self.NAME)
        # Two primitives: one for frequency_penalty, one for presence_penalty
        assert len(primitives) == 2

    def test_frequency_penalty_above_max_clamped(self) -> None:
        body = {"model": "mistral", "frequency_penalty": 5.0}
        result = _req(self.NAME, body)
        assert result["frequency_penalty"] == 2.0

    def test_frequency_penalty_below_min_clamped(self) -> None:
        body = {"model": "mistral", "frequency_penalty": -5.0}
        result = _req(self.NAME, body)
        assert result["frequency_penalty"] == -2.0

    def test_frequency_penalty_in_range_unchanged(self) -> None:
        body = {"model": "mistral", "frequency_penalty": 1.0}
        result = _req(self.NAME, body)
        assert result["frequency_penalty"] == 1.0

    def test_presence_penalty_clamped(self) -> None:
        body = {"model": "mistral", "presence_penalty": 3.0}
        result = _req(self.NAME, body)
        assert result["presence_penalty"] == 2.0

    def test_presence_penalty_in_range_unchanged(self) -> None:
        body = {"model": "mistral", "presence_penalty": -1.5}
        result = _req(self.NAME, body)
        assert result["presence_penalty"] == -1.5

    def test_absent_params_are_noop(self) -> None:
        body = {"model": "mistral", "temperature": 0.7}
        result = _req(self.NAME, body)
        assert result == body

    def test_boundary_values_unchanged(self) -> None:
        body = {"model": "mistral", "frequency_penalty": -2.0, "presence_penalty": 2.0}
        result = _req(self.NAME, body)
        assert result["frequency_penalty"] == -2.0
        assert result["presence_penalty"] == 2.0


# ---------------------------------------------------------------------------
# tabby-penalty-floor
# ---------------------------------------------------------------------------


class TestTabbyPenaltyFloor:
    NAME = "tabby-penalty-floor"

    def test_registered(self) -> None:
        primitives = REGISTRY.resolve(self.NAME)
        # Two primitives: one for frequency_penalty, one for presence_penalty
        assert len(primitives) == 2

    def test_frequency_penalty_below_zero_clamped(self) -> None:
        body = {"model": "tabby", "frequency_penalty": -0.5}
        result = _req(self.NAME, body)
        assert result["frequency_penalty"] == 0.0

    def test_frequency_penalty_at_zero_unchanged(self) -> None:
        body = {"model": "tabby", "frequency_penalty": 0.0}
        result = _req(self.NAME, body)
        assert result["frequency_penalty"] == 0.0

    def test_frequency_penalty_positive_unchanged(self) -> None:
        body = {"model": "tabby", "frequency_penalty": 1.5}
        result = _req(self.NAME, body)
        assert result["frequency_penalty"] == 1.5

    def test_presence_penalty_below_zero_clamped(self) -> None:
        body = {"model": "tabby", "presence_penalty": -1.0}
        result = _req(self.NAME, body)
        assert result["presence_penalty"] == 0.0

    def test_presence_penalty_positive_unchanged(self) -> None:
        body = {"model": "tabby", "presence_penalty": 0.5}
        result = _req(self.NAME, body)
        assert result["presence_penalty"] == 0.5

    def test_absent_params_are_noop(self) -> None:
        body = {"model": "tabby", "temperature": 0.7}
        result = _req(self.NAME, body)
        assert result == body

    def test_no_max_cap(self) -> None:
        """tabby-penalty-floor only has a floor (min=0), no upper cap."""
        body = {"model": "tabby", "frequency_penalty": 100.0}
        result = _req(self.NAME, body)
        assert result["frequency_penalty"] == 100.0


# ---------------------------------------------------------------------------
# Non-alteration guard — none of these attach to the current config
# ---------------------------------------------------------------------------


class TestTier1PacksNonAlteration:
    """None of the Tier-1 packs should auto-attach to existing services.

    These transforms are registered in REGISTRY but are NOT in any existing
    service's transform_ids list.  The guard: running a baseline body through
    them (as opt-in) must not affect a body that doesn't trigger their when:.
    """

    def test_baseline_chat_request_unaffected_by_ollama(self) -> None:
        body = {
            "model": "qwen3",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
            "stream": True,
        }
        result = run_request_pipeline(["ollama-unsupported-params"], copy.deepcopy(body))
        # The baseline body has none of the 6 dropped params — should be unchanged
        assert result == body

    def test_baseline_chat_request_unaffected_by_drop_null_params(self) -> None:
        body = {
            "model": "qwen3",
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
        }
        result = run_request_pipeline(["drop-null-params"], copy.deepcopy(body))
        # No null values — unchanged
        assert result == body

    def test_baseline_request_penalty_in_range_for_mistral(self) -> None:
        body = {
            "model": "mistral",
            "frequency_penalty": 0.5,
            "presence_penalty": -0.5,
        }
        result = run_request_pipeline(["mistral-penalty-clamp"], copy.deepcopy(body))
        # Both in [-2, 2] — unchanged
        assert result == body

    def test_baseline_request_penalty_in_range_for_tabby(self) -> None:
        body = {
            "model": "tabby",
            "frequency_penalty": 0.5,
            "presence_penalty": 1.0,
        }
        result = run_request_pipeline(["tabby-penalty-floor"], copy.deepcopy(body))
        # Both >= 0 — unchanged
        assert result == body
