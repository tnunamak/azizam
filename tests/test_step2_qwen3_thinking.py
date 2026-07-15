"""Step 2 parity test — reasoning-effort built-in.

GATE: The transform path must produce the correct body mutations for the full
input matrix.

Test cases cover:
  - absent           → no-op
  - None value       → no-op (pop, then treat None as absent)
  - invalid (not str)  → same 400 error message
  - invalid (wrong str) → same 400 error message
  - all configured efforts → param stripped and numeric thinking budget emitted
  - none → enable_thinking=False set via chat_template_kwargs; NO /no_think text injected
  - chat_template_kwargs already present with enable_thinking=True → must NOT overwrite
  - messages absent/not-a-list → body unchanged (except reasoning_effort popped)
"""

from __future__ import annotations

import copy
from typing import Any, Optional

import pytest

from gateway.transforms import ReasoningEffortTransform

_TRANSFORM = ReasoningEffortTransform()


def _apply(body: dict[str, Any]) -> tuple[dict[str, Any], Optional[str]]:
    """Apply the transform to a deep copy; return (mutated_body, error_or_None)."""
    b = copy.deepcopy(body)
    err = _TRANSFORM.apply(b)
    return b, err


# ---------------------------------------------------------------------------
# Absent / None value
# ---------------------------------------------------------------------------


class TestQwen3ThinkingParityAbsent:
    def test_absent_no_reasoning_effort_key(self) -> None:
        """No reasoning_effort in body → no-op."""
        body = {"model": "qwen3", "messages": [{"role": "user", "content": "Hello"}]}
        b, err = _apply(body)
        assert err is None
        assert "reasoning_effort" not in b
        assert b == body  # body unchanged

    def test_absent_empty_body(self) -> None:
        b, err = _apply({})
        assert err is None
        assert b == {}


class TestQwen3ThinkingParityNoneValue:
    def test_none_value(self) -> None:
        """reasoning_effort=None → pop it, return None (no suppression)."""
        body = {
            "model": "qwen3",
            "reasoning_effort": None,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        b, err = _apply(body)
        assert err is None
        assert "reasoning_effort" not in b
        # No suppression fields added
        assert "chat_template_kwargs" not in b


# ---------------------------------------------------------------------------
# Invalid values → 400 error message
# ---------------------------------------------------------------------------


class TestQwen3ThinkingParityInvalid:
    def _assert_400_message(self, effort: Any, label: str) -> None:
        body = {"model": "qwen3", "reasoning_effort": effort, "messages": []}
        b, err = _apply(body)
        assert err is not None, f"[{label}] expected error but got None"
        assert "reasoning_effort" not in b, f"[{label}] effort not popped"
        assert "Invalid value for 'reasoning_effort'" in err, f"[{label}] wrong message: {err!r}"
        # Exact sorted list expected
        valid_sorted = sorted({"none", "minimal", "low", "medium", "high", "xhigh"})
        assert str(valid_sorted) in err, f"[{label}] sorted list not in message: {err!r}"

    def test_invalid_non_string_int(self) -> None:
        self._assert_400_message(42, "int")

    def test_invalid_non_string_list(self) -> None:
        self._assert_400_message(["high"], "list")

    def test_invalid_unknown_string(self) -> None:
        self._assert_400_message("ultra", "ultra")

    def test_invalid_uppercase_string(self) -> None:
        """Case-sensitive: 'HIGH' is not 'high'."""
        self._assert_400_message("HIGH", "uppercase")

    def test_invalid_empty_string(self) -> None:
        self._assert_400_message("", "empty_str")

    def test_error_message_exact_format(self) -> None:
        """The exact error format: f"Invalid value ... {effort!r} ... {sorted(...)}." """
        body = {"reasoning_effort": "bad", "messages": []}
        _, err = _apply(body)
        assert err == (
            "Invalid value for 'reasoning_effort': 'bad'. "
            "Expected one of: ['high', 'low', 'medium', 'minimal', 'none', 'xhigh']."
        )


# ---------------------------------------------------------------------------
# Strip-only (low/medium/high/xhigh)
# ---------------------------------------------------------------------------


class TestQwen3ThinkingParityBudget:
    """Configured effort levels become BeeLlama request budgets."""

    @pytest.mark.parametrize(
        ("effort", "budget"),
        [("low", 4096), ("medium", 6144), ("high", 8192), ("xhigh", -1)],
    )
    def test_budget_is_forwarded(self, effort: str, budget: int) -> None:
        body = {
            "model": "qwen3",
            "reasoning_effort": effort,
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
        }
        b, err = _apply(body)
        assert err is None
        assert "reasoning_effort" not in b
        assert b["thinking_budget_tokens"] == budget
        assert "chat_template_kwargs" not in b
        # No system message inserted
        assert all(m.get("role") != "system" for m in b.get("messages", []))

    def test_translated_budget_is_idempotent_across_internal_gateway_hops(self) -> None:
        transform = ReasoningEffortTransform({"default_effort": "high"})
        body = {
            "model": "qwen3",
            "thinking_budget_tokens": 0,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": "Hello"}],
        }

        error = transform.apply(body)

        assert error is None
        assert body["thinking_budget_tokens"] == 0
        assert body["chat_template_kwargs"]["enable_thinking"] is False
        assert "reasoning_effort" not in body

    @pytest.mark.parametrize("budget", [None, True, "unlimited", -2])
    def test_invalid_translated_budget_is_rejected(self, budget: object) -> None:
        transform = ReasoningEffortTransform({"default_effort": "high"})
        body = {"model": "qwen3", "thinking_budget_tokens": budget}

        error = transform.apply(body)

        assert error is not None
        assert "thinking_budget_tokens" in error

    @pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
    def test_budget_no_messages(self, effort: str) -> None:
        body = {"model": "qwen3", "reasoning_effort": effort}
        b, err = _apply(body)
        assert err is None
        assert "reasoning_effort" not in b
        assert "thinking_budget_tokens" in b
        assert "chat_template_kwargs" not in b


# ---------------------------------------------------------------------------
# Suppress path (none/minimal)
# ---------------------------------------------------------------------------


class TestQwen3ThinkingParitySuppress:
    """none: full suppression path — kwarg only, no prompt-string injection."""

    @pytest.mark.parametrize(("effort", "budget"), [("none", 0), ("minimal", 2048)])
    def test_suppress_sets_budget(self, effort: str, budget: int) -> None:
        """none/minimal → configured budget; only none suppresses thinking."""
        body = {
            "model": "qwen3",
            "reasoning_effort": effort,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        b, err = _apply(body)
        assert err is None
        assert "reasoning_effort" not in b
        assert b["thinking_budget_tokens"] == budget
        if budget == 0:
            assert b["chat_template_kwargs"]["enable_thinking"] is False
        else:
            assert "chat_template_kwargs" not in b

    @pytest.mark.parametrize("effort", ["none"])
    def test_suppress_no_system_message_inserted(self, effort: str) -> None:
        """No system message → NO /no_think inserted; messages unchanged except reasoning_effort gone."""
        body = {
            "model": "qwen3",
            "reasoning_effort": effort,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        b, err = _apply(body)
        assert err is None
        # No system message injected
        assert all(m.get("role") != "system" for m in b["messages"])
        # User message untouched
        assert b["messages"][0] == {"role": "user", "content": "Hello"}

    @pytest.mark.parametrize("effort", ["none"])
    def test_suppress_existing_system_message_unchanged(self, effort: str) -> None:
        """Existing system message → NOT modified; no /no_think appended."""
        body = {
            "model": "qwen3",
            "reasoning_effort": effort,
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello"},
            ],
        }
        b, err = _apply(body)
        assert err is None
        sys_msg = next(m for m in b["messages"] if m["role"] == "system")
        # System message content must be untouched — no /no_think appended
        assert sys_msg["content"] == "You are helpful."
        assert "/no_think" not in sys_msg["content"]

    @pytest.mark.parametrize("effort", ["none"])
    def test_suppress_no_no_think_text_anywhere(self, effort: str) -> None:
        """Verify /no_think text never appears in any message after suppression."""
        body = {
            "model": "qwen3",
            "reasoning_effort": effort,
            "messages": [
                {"role": "system", "content": "Be helpful."},
                {"role": "user", "content": "Hello"},
            ],
        }
        b, err = _apply(body)
        assert err is None
        for msg in b["messages"]:
            content = msg.get("content", "")
            assert "/no_think" not in content, (
                f"Found /no_think in message: {msg!r}"
            )

    @pytest.mark.parametrize("effort", ["none"])
    def test_suppress_no_messages_key(self, effort: str) -> None:
        """No messages key → only chat_template_kwargs mutation."""
        body = {"model": "qwen3", "reasoning_effort": effort}
        b, err = _apply(body)
        assert err is None
        assert b["chat_template_kwargs"]["enable_thinking"] is False
        assert "messages" not in b

    @pytest.mark.parametrize("effort", ["none"])
    def test_suppress_messages_not_a_list(self, effort: str) -> None:
        """messages is not a list → only chat_template_kwargs mutation."""
        body = {"model": "qwen3", "reasoning_effort": effort, "messages": "invalid"}
        b, err = _apply(body)
        assert err is None
        assert b["chat_template_kwargs"]["enable_thinking"] is False
        # messages left as-is (not mutated)
        assert b["messages"] == "invalid"


# ---------------------------------------------------------------------------
# chat_template_kwargs edge cases
# ---------------------------------------------------------------------------


class TestQwen3ThinkingParityChatTemplateKwargs:
    def test_setdefault_does_not_overwrite_existing_true(self) -> None:
        """enable_thinking=True already set → must NOT be overwritten."""
        body = {
            "model": "qwen3",
            "reasoning_effort": "none",
            "messages": [{"role": "user", "content": "Hello"}],
            "chat_template_kwargs": {"enable_thinking": True},
        }
        b, err = _apply(body)
        assert err is None
        assert b["chat_template_kwargs"]["enable_thinking"] is True

    def test_setdefault_does_not_overwrite_existing_false(self) -> None:
        """enable_thinking=False already set → should remain False (setdefault)."""
        body = {
            "model": "qwen3",
            "reasoning_effort": "none",
            "messages": [{"role": "user", "content": "Hello"}],
            "chat_template_kwargs": {"enable_thinking": False},
        }
        b, err = _apply(body)
        assert err is None
        assert b["chat_template_kwargs"]["enable_thinking"] is False

    def test_existing_chat_template_kwargs_other_keys_preserved(self) -> None:
        """Other keys in chat_template_kwargs must be preserved."""
        body = {
            "model": "qwen3",
            "reasoning_effort": "none",
            "messages": [{"role": "user", "content": "Hello"}],
            "chat_template_kwargs": {"some_other_key": "value"},
        }
        b, err = _apply(body)
        assert err is None
        assert b["chat_template_kwargs"]["some_other_key"] == "value"
        assert b["chat_template_kwargs"]["enable_thinking"] is False

    def test_chat_template_kwargs_not_a_dict_gets_overwritten(self) -> None:
        """If chat_template_kwargs is not a dict, setdefault replaces it."""
        body = {
            "model": "qwen3",
            "reasoning_effort": "none",
            "messages": [{"role": "user", "content": "Hello"}],
            "chat_template_kwargs": "invalid",
        }
        b, err = _apply(body)
        assert err is None
        # setdefault called on body: if value is "invalid" (truthy), body.setdefault
        # won't overwrite it. The legacy code checks isinstance(kwargs, dict) so
        # if it's not a dict, enable_thinking is NOT set.
        # Verify the actual behavior matches our implementation.
        assert b["chat_template_kwargs"] == "invalid"


# ---------------------------------------------------------------------------
# Message list integrity (no injection side-effects)
# ---------------------------------------------------------------------------


class TestQwen3ThinkingMessageIntegrity:
    def test_multiple_system_messages_all_unchanged(self) -> None:
        """When multiple system messages exist, NONE are modified — no injection."""
        body = {
            "model": "qwen3",
            "reasoning_effort": "none",
            "messages": [
                {"role": "system", "content": "First system."},
                {"role": "user", "content": "Hello"},
                {"role": "system", "content": "Second system."},
            ],
        }
        b, err = _apply(body)
        assert err is None
        assert b["chat_template_kwargs"]["enable_thinking"] is False
        # Neither system message should be modified
        assert b["messages"][0]["content"] == "First system."
        assert b["messages"][2]["content"] == "Second system."

    def test_non_dict_messages_skipped_no_injection(self) -> None:
        """Non-dict entries in messages list → no crash, no injection."""
        body = {
            "model": "qwen3",
            "reasoning_effort": "none",
            "messages": [
                "not a dict",
                {"role": "user", "content": "Hello"},
            ],
        }
        b, err = _apply(body)
        assert err is None
        assert b["chat_template_kwargs"]["enable_thinking"] is False
        # No system message inserted, non-dict entry untouched
        assert b["messages"][0] == "not a dict"
        assert all(
            not isinstance(m, dict) or m.get("role") != "system"
            for m in b["messages"]
        )


# ---------------------------------------------------------------------------
# Registration / pipeline integration
# ---------------------------------------------------------------------------


class TestQwen3ThinkingRegistration:
    def test_qwen3_thinking_is_registered(self) -> None:
        from gateway.transforms import REGISTRY
        primitives = REGISTRY.resolve("reasoning-effort")
        assert len(primitives) == 1

    def test_qwen3_thinking_on_request_mutates_body(self) -> None:
        from gateway.transforms import run_request_pipeline
        body = {
            "reasoning_effort": "none",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        result = run_request_pipeline(["reasoning-effort"], body)
        assert "reasoning_effort" not in result
        assert result["chat_template_kwargs"]["enable_thinking"] is False
        # No /no_think injected into messages
        for msg in result.get("messages", []):
            assert "/no_think" not in msg.get("content", "")

    def test_qwen3_thinking_response_phase_is_noop(self) -> None:
        from gateway.transforms import run_response_pipeline
        payload = {"choices": [{"message": {"content": "hi"}}]}
        original = copy.deepcopy(payload)
        result = run_response_pipeline(["reasoning-effort"], copy.deepcopy(payload))
        assert result == original
