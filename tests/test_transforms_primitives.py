"""Unit tests for each transform primitive in gateway/transforms.py.

Coverage:
  - value-map: hit / miss / default
  - drop-params: present key removed, absent key no-op
  - default-params: sets missing key; does NOT overwrite existing
  - param-map: rename / nested set / when-guard
  - message-inject: prepend / append / append-or-insert (both branches)
  - clamp: below-min / above-max / within-range
  - when-predicate: all ops (present, absent, equals, in, non_empty, gt, lt)
  - phase routing: request-phase primitive is a no-op on response path and vice-versa
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from gateway.transforms import (
    ClampTransform,
    DefaultParamsTransform,
    DropParamsTransform,
    MessageInjectTransform,
    ParamMapTransform,
    TransformContext,
    TransformRegistry,
    ValueMapTransform,
    _build_primitive,
    _eval_when,
    run_request_pipeline,
    run_response_pipeline,
)

CTX = TransformContext()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _req(cfg: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Build primitive from cfg and call on_request."""
    return _build_primitive(cfg).on_request(copy.deepcopy(body), CTX)


def _resp(cfg: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    """Build primitive from cfg and call on_response."""
    return _build_primitive(cfg).on_response(copy.deepcopy(body), CTX)


# ---------------------------------------------------------------------------
# 2.1 value-map
# ---------------------------------------------------------------------------


class TestValueMap:
    CFG = {
        "kind": "value-map",
        "param": "voice",
        "map": {"alloy": "neutral_female", "echo": "casual_male"},
        "default": "neutral_female",
    }

    def test_hit(self) -> None:
        result = _req(self.CFG, {"voice": "alloy"})
        assert result["voice"] == "neutral_female"

    def test_hit_different_key(self) -> None:
        result = _req(self.CFG, {"voice": "echo"})
        assert result["voice"] == "casual_male"

    def test_miss_falls_back_to_default(self) -> None:
        result = _req(self.CFG, {"voice": "unknown_voice"})
        assert result["voice"] == "neutral_female"

    def test_missing_param_sets_default(self) -> None:
        result = _req(self.CFG, {"model": "tts-1"})
        assert result["voice"] == "neutral_female"

    def test_no_default_and_miss_preserves_value(self) -> None:
        cfg = {
            "kind": "value-map",
            "param": "fmt",
            "map": {"a": "alpha"},
        }
        result = _req(cfg, {"fmt": "z"})
        # No default: unknown value is kept as-is
        assert result["fmt"] == "z"

    def test_does_not_touch_other_params(self) -> None:
        result = _req(self.CFG, {"voice": "alloy", "model": "tts-1"})
        assert result["model"] == "tts-1"

    def test_response_phase_noop_when_phase_is_request(self) -> None:
        result = _resp(self.CFG, {"voice": "alloy"})
        # Should remain alloy (request primitive, response path → no-op)
        assert result["voice"] == "alloy"

    def test_response_phase_applies_when_phase_is_response(self) -> None:
        cfg = {**self.CFG, "phase": "response"}
        result = _resp(cfg, {"voice": "alloy"})
        assert result["voice"] == "neutral_female"


# ---------------------------------------------------------------------------
# 2.2 drop-params
# ---------------------------------------------------------------------------


class TestDropParams:
    CFG = {
        "kind": "drop-params",
        "drop": ["n", "logprobs", "top_logprobs"],
    }

    def test_drops_present_key(self) -> None:
        result = _req(self.CFG, {"n": 2, "model": "gpt"})
        assert "n" not in result

    def test_drops_all_listed_keys(self) -> None:
        result = _req(self.CFG, {"n": 1, "logprobs": True, "top_logprobs": 3, "model": "gpt"})
        assert "n" not in result
        assert "logprobs" not in result
        assert "top_logprobs" not in result
        assert result["model"] == "gpt"

    def test_absent_key_is_noop(self) -> None:
        body = {"model": "gpt"}
        result = _req(self.CFG, body)
        assert result == {"model": "gpt"}

    def test_response_phase_applied_when_configured(self) -> None:
        cfg = {**self.CFG, "phase": "response"}
        result = _resp(cfg, {"n": 1, "model": "gpt"})
        assert "n" not in result

    def test_request_phase_noop_on_response_path(self) -> None:
        result = _resp(self.CFG, {"n": 1, "model": "gpt"})
        assert "n" in result  # not dropped


# ---------------------------------------------------------------------------
# 2.3 default-params
# ---------------------------------------------------------------------------


class TestDefaultParams:
    CFG = {
        "kind": "default-params",
        "set": {"temperature": 0.7, "top_p": 0.9},
    }

    def test_sets_absent_key(self) -> None:
        result = _req(self.CFG, {"model": "gpt"})
        assert result["temperature"] == 0.7
        assert result["top_p"] == 0.9

    def test_does_not_overwrite_existing(self) -> None:
        result = _req(self.CFG, {"temperature": 1.0, "model": "gpt"})
        assert result["temperature"] == 1.0  # must not be overwritten

    def test_nested_path(self) -> None:
        cfg = {
            "kind": "default-params",
            "set": {"extra_body.separate_reasoning": True},
        }
        result = _req(cfg, {"model": "gpt"})
        assert result["extra_body"]["separate_reasoning"] is True

    def test_nested_path_does_not_overwrite_existing_nested(self) -> None:
        cfg = {
            "kind": "default-params",
            "set": {"extra_body.separate_reasoning": True},
        }
        body = {"extra_body": {"separate_reasoning": False}}
        result = _req(cfg, body)
        assert result["extra_body"]["separate_reasoning"] is False  # preserved

    def test_partial_nested_set_preserves_sibling(self) -> None:
        cfg = {
            "kind": "default-params",
            "set": {"chat_template_kwargs.enable_thinking": False},
        }
        body = {"chat_template_kwargs": {"some_other": "value"}}
        result = _req(cfg, body)
        assert result["chat_template_kwargs"]["enable_thinking"] is False
        assert result["chat_template_kwargs"]["some_other"] == "value"


# ---------------------------------------------------------------------------
# 2.4 param-map
# ---------------------------------------------------------------------------


class TestParamMap:
    def test_rename_top_level(self) -> None:
        cfg = {"kind": "param-map", "from": "max_tokens", "to": "options.num_predict"}
        result = _req(cfg, {"max_tokens": 512, "model": "llama"})
        assert "max_tokens" not in result
        assert result["options"]["num_predict"] == 512

    def test_nested_source(self) -> None:
        cfg = {
            "kind": "param-map",
            "from": "response_format.json_schema.schema",
            "to": "guided_json",
        }
        body = {"response_format": {"json_schema": {"schema": {"type": "object"}}}}
        result = _req(cfg, body)
        assert result["guided_json"] == {"type": "object"}
        assert result["response_format"]["json_schema"] == {}

    def test_absent_source_is_noop(self) -> None:
        cfg = {"kind": "param-map", "from": "nonexistent", "to": "other"}
        body = {"model": "gpt"}
        result = _req(cfg, body)
        assert result == {"model": "gpt"}

    def test_when_guard_skips_when_condition_false(self) -> None:
        cfg = {
            "kind": "param-map",
            "from": "max_tokens",
            "to": "num_predict",
            "when": [{"field": "stream", "op": "equals", "value": True}],
        }
        # stream is not True → should NOT rename
        result = _req(cfg, {"max_tokens": 100})
        assert "max_tokens" in result
        assert "num_predict" not in result

    def test_when_guard_applies_when_condition_true(self) -> None:
        cfg = {
            "kind": "param-map",
            "from": "max_tokens",
            "to": "num_predict",
            "when": [{"field": "stream", "op": "equals", "value": True}],
        }
        result = _req(cfg, {"max_tokens": 100, "stream": True})
        assert "max_tokens" not in result
        assert result["num_predict"] == 100

    def test_response_phase(self) -> None:
        cfg = {
            "kind": "param-map",
            "phase": "response",
            "from": "old_field",
            "to": "new_field",
        }
        result = _resp(cfg, {"old_field": "val"})
        assert "old_field" not in result
        assert result["new_field"] == "val"

    def test_request_phase_noop_on_response_path(self) -> None:
        cfg = {"kind": "param-map", "from": "old_field", "to": "new_field"}
        result = _resp(cfg, {"old_field": "val"})
        assert result == {"old_field": "val"}


# ---------------------------------------------------------------------------
# 2.5 message-inject
# ---------------------------------------------------------------------------


class TestMessageInject:
    def _messages(self, msgs: list[dict]) -> dict:
        return {"messages": copy.deepcopy(msgs)}

    def test_prepend(self) -> None:
        cfg = {"kind": "message-inject", "role": "system", "text": "Be helpful.", "mode": "prepend"}
        body = self._messages([{"role": "user", "content": "Hi"}])
        result = _req(cfg, body)
        assert result["messages"][0] == {"role": "system", "content": "Be helpful."}
        assert result["messages"][1]["role"] == "user"

    def test_append(self) -> None:
        cfg = {"kind": "message-inject", "role": "user", "text": "end note", "mode": "append"}
        body = self._messages([{"role": "user", "content": "Hi"}])
        result = _req(cfg, body)
        assert result["messages"][-1] == {"role": "user", "content": "end note"}

    def test_append_or_insert_existing_system_appends_text(self) -> None:
        """When a system message already exists, text is appended to its content."""
        cfg = {"kind": "message-inject", "role": "system", "text": "/no_think", "mode": "append-or-insert"}
        body = self._messages([
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
        ])
        result = _req(cfg, body)
        sys_msg = next(m for m in result["messages"] if m["role"] == "system")
        assert "/no_think" in sys_msg["content"]
        assert "You are helpful." in sys_msg["content"]

    def test_append_or_insert_no_system_inserts_at_index_0(self) -> None:
        """When no system message exists, insert at index 0 (proxy.py:5283 behaviour)."""
        cfg = {"kind": "message-inject", "role": "system", "text": "/no_think", "mode": "append-or-insert"}
        body = self._messages([
            {"role": "user", "content": "Hello"},
        ])
        result = _req(cfg, body)
        assert result["messages"][0] == {"role": "system", "content": "/no_think"}
        assert result["messages"][1]["role"] == "user"

    def test_invalid_mode_raises(self) -> None:
        cfg = {"kind": "message-inject", "role": "system", "text": "x", "mode": "unknown"}
        with pytest.raises(ValueError, match="Unknown message-inject mode"):
            _req(cfg, {"messages": [{"role": "user", "content": "hi"}]})

    def test_no_messages_field_is_noop(self) -> None:
        cfg = {"kind": "message-inject", "role": "system", "text": "x", "mode": "prepend"}
        body = {"model": "gpt"}
        result = _req(cfg, body)
        assert "messages" not in result

    def test_response_phase_noop_for_request_primitive(self) -> None:
        cfg = {"kind": "message-inject", "role": "system", "text": "x", "mode": "prepend"}
        body = {"messages": [{"role": "user", "content": "hi"}]}
        original = copy.deepcopy(body)
        result = _resp(cfg, body)
        assert result == original


# ---------------------------------------------------------------------------
# 2.6 clamp
# ---------------------------------------------------------------------------


class TestClamp:
    def test_below_min_clamped_up(self) -> None:
        cfg = {"kind": "clamp", "param": "frequency_penalty", "min": 0.0}
        result = _req(cfg, {"frequency_penalty": -1.0})
        assert result["frequency_penalty"] == 0.0

    def test_above_max_clamped_down(self) -> None:
        cfg = {"kind": "clamp", "param": "temperature", "max": 2.0}
        result = _req(cfg, {"temperature": 5.0})
        assert result["temperature"] == 2.0

    def test_within_range_unchanged(self) -> None:
        cfg = {"kind": "clamp", "param": "temperature", "min": 0.0, "max": 2.0}
        result = _req(cfg, {"temperature": 1.0})
        assert result["temperature"] == 1.0

    def test_min_boundary_unchanged(self) -> None:
        cfg = {"kind": "clamp", "param": "temperature", "min": 0.0, "max": 2.0}
        result = _req(cfg, {"temperature": 0.0})
        assert result["temperature"] == 0.0

    def test_max_boundary_unchanged(self) -> None:
        cfg = {"kind": "clamp", "param": "temperature", "min": 0.0, "max": 2.0}
        result = _req(cfg, {"temperature": 2.0})
        assert result["temperature"] == 2.0

    def test_non_numeric_is_noop(self) -> None:
        cfg = {"kind": "clamp", "param": "temperature", "min": 0.0}
        result = _req(cfg, {"temperature": "hot"})
        assert result["temperature"] == "hot"

    def test_absent_param_is_noop(self) -> None:
        cfg = {"kind": "clamp", "param": "temperature", "min": 0.0}
        result = _req(cfg, {"model": "gpt"})
        assert "temperature" not in result

    def test_only_min_specified(self) -> None:
        cfg = {"kind": "clamp", "param": "p", "min": 1.0}
        result = _req(cfg, {"p": 0.5})
        assert result["p"] == 1.0

    def test_only_max_specified(self) -> None:
        cfg = {"kind": "clamp", "param": "p", "max": 1.0}
        result = _req(cfg, {"p": 99.9})
        assert result["p"] == 1.0

    def test_response_phase(self) -> None:
        cfg = {"kind": "clamp", "param": "temperature", "max": 2.0, "phase": "response"}
        result = _resp(cfg, {"temperature": 5.0})
        assert result["temperature"] == 2.0


# ---------------------------------------------------------------------------
# when-predicate ops
# ---------------------------------------------------------------------------


class TestWhenPredicate:
    def test_op_present_true(self) -> None:
        assert _eval_when([{"field": "tools", "op": "present"}], {"tools": []}) is True

    def test_op_present_false(self) -> None:
        assert _eval_when([{"field": "tools", "op": "present"}], {}) is False

    def test_op_absent_true(self) -> None:
        assert _eval_when([{"field": "tools", "op": "absent"}], {}) is True

    def test_op_absent_false(self) -> None:
        assert _eval_when([{"field": "tools", "op": "absent"}], {"tools": []}) is False

    def test_op_equals_true(self) -> None:
        assert _eval_when([{"field": "stream", "op": "equals", "value": True}], {"stream": True}) is True

    def test_op_equals_false(self) -> None:
        assert _eval_when([{"field": "stream", "op": "equals", "value": True}], {"stream": False}) is False

    def test_op_equals_missing_field(self) -> None:
        assert _eval_when([{"field": "stream", "op": "equals", "value": True}], {}) is False

    def test_op_in_true(self) -> None:
        assert _eval_when([{"field": "role", "op": "in", "value": ["user", "assistant"]}], {"role": "user"}) is True

    def test_op_in_false(self) -> None:
        assert _eval_when([{"field": "role", "op": "in", "value": ["user"]}], {"role": "system"}) is False

    def test_op_non_empty_true(self) -> None:
        assert _eval_when([{"field": "tools", "op": "non_empty"}], {"tools": [1]}) is True

    def test_op_non_empty_false_empty_list(self) -> None:
        assert _eval_when([{"field": "tools", "op": "non_empty"}], {"tools": []}) is False

    def test_op_non_empty_false_absent(self) -> None:
        assert _eval_when([{"field": "tools", "op": "non_empty"}], {}) is False

    def test_op_gt_true(self) -> None:
        assert _eval_when([{"field": "n", "op": "gt", "value": 1}], {"n": 2}) is True

    def test_op_gt_false(self) -> None:
        assert _eval_when([{"field": "n", "op": "gt", "value": 1}], {"n": 1}) is False

    def test_op_lt_true(self) -> None:
        assert _eval_when([{"field": "n", "op": "lt", "value": 5}], {"n": 3}) is True

    def test_op_lt_false(self) -> None:
        assert _eval_when([{"field": "n", "op": "lt", "value": 5}], {"n": 5}) is False

    def test_multiple_clauses_all_must_hold(self) -> None:
        clauses = [
            {"field": "tools", "op": "non_empty"},
            {"field": "stream", "op": "equals", "value": False},
        ]
        assert _eval_when(clauses, {"tools": [1], "stream": False}) is True
        assert _eval_when(clauses, {"tools": [1], "stream": True}) is False

    def test_empty_when_is_always_true(self) -> None:
        assert _eval_when([], {"anything": "here"}) is True
        assert _eval_when(None, {}) is True

    def test_unknown_op_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown when-predicate op"):
            _eval_when([{"field": "x", "op": "magic"}], {"x": 1})

    def test_nested_field_path(self) -> None:
        assert _eval_when(
            [{"field": "response_format.type", "op": "equals", "value": "json_schema"}],
            {"response_format": {"type": "json_schema"}},
        ) is True

    def test_nested_field_absent(self) -> None:
        assert _eval_when(
            [{"field": "response_format.type", "op": "present"}],
            {"response_format": {}},
        ) is False


# ---------------------------------------------------------------------------
# Phase routing — cross-primitive
# ---------------------------------------------------------------------------


class TestPhaseRouting:
    """Primitives default to request phase; response-phase variants must be explicit."""

    def test_default_phase_is_request(self) -> None:
        # A ValueMapTransform with no phase field → request only
        t = ValueMapTransform({"kind": "value-map", "param": "voice", "map": {"a": "b"}, "default": "b"})
        body = {"voice": "a"}
        # on_request should apply
        assert t.on_request(copy.deepcopy(body), CTX)["voice"] == "b"
        # on_response should NOT apply (is a no-op)
        assert t.on_response(copy.deepcopy(body), CTX)["voice"] == "a"

    def test_explicit_response_phase_skips_request(self) -> None:
        t = DropParamsTransform({"kind": "drop-params", "drop": ["n"], "phase": "response"})
        body = {"n": 1, "model": "gpt"}
        # on_request must be a no-op
        result = t.on_request(copy.deepcopy(body), CTX)
        assert "n" in result
        # on_response must drop
        result = t.on_response(copy.deepcopy(body), CTX)
        assert "n" not in result


# ---------------------------------------------------------------------------
# Registry and pipeline
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_and_resolve(self) -> None:
        reg = TransformRegistry()
        reg.register("test-drop", {"kind": "drop-params", "drop": ["x"]})
        primitives = reg.resolve("test-drop")
        assert len(primitives) == 1

    def test_resolve_unknown_raises(self) -> None:
        reg = TransformRegistry()
        with pytest.raises(KeyError):
            reg.resolve("nonexistent")

    def test_register_list_of_primitives(self) -> None:
        reg = TransformRegistry()
        reg.register("multi", [
            {"kind": "drop-params", "drop": ["a"]},
            {"kind": "drop-params", "drop": ["b"]},
        ])
        primitives = reg.resolve("multi")
        assert len(primitives) == 2

    def test_pipeline_applies_in_order(self) -> None:
        reg = TransformRegistry()
        reg.register("set-default", {"kind": "default-params", "set": {"x": 1}})
        reg.register("drop-x", {"kind": "drop-params", "drop": ["x"]})
        # set-default runs first (adds x=1), then drop-x removes it
        result = run_request_pipeline(["set-default", "drop-x"], {}, registry=reg)
        assert "x" not in result

    def test_pipeline_uses_module_registry_by_default(self) -> None:
        # The module-level REGISTRY has the reviewed packs registered
        result = run_request_pipeline(["llamacpp-cache-prompt"], {"prompt": "hi"})
        assert result["cache_prompt"] is True

    def test_unknown_build_primitive_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown transform kind"):
            _build_primitive({"kind": "nonexistent-kind"})
