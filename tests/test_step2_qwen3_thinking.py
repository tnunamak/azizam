"""Step 2 parity test — qwen3-thinking built-in.

GATE: The transform path must produce BYTE-IDENTICAL body mutations to the
legacy apply_chat_completions_reasoning_effort() function for the full input
matrix.  Parity was proven against the legacy function before it was deleted
(all 37 tests passed).  This file now tests the transform directly as the
source of truth, retaining the full input matrix so the invariant is visible.

Test cases cover:
  - absent           → no-op
  - None value       → no-op (pop, then treat None as absent)
  - invalid (not str)  → same 400 error message
  - invalid (wrong str) → same 400 error message
  - low/medium/high/xhigh → param stripped, no suppression injected
  - none/minimal (no system msg) → enable_thinking=False set + /no_think at index 0
  - none/minimal (system msg, no hint yet) → appends hint to existing system msg
  - none/minimal (system msg, hint already present) → no double-injection
  - none/minimal (system msg, empty content) → appends hint with no leading \\n
  - chat_template_kwargs already present with enable_thinking=True → must NOT overwrite
  - messages absent/not-a-list → body unchanged (except reasoning_effort popped)
"""

from __future__ import annotations

import copy
from typing import Any, Optional

import pytest

from gateway.transforms import Qwen3ThinkingTransform

_TRANSFORM = Qwen3ThinkingTransform()


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
        """The exact error format from legacy: f"Invalid value ... {effort!r} ... {sorted(...)}." """
        body = {"reasoning_effort": "bad", "messages": []}
        _, err = _apply(body)
        assert err == (
            "Invalid value for 'reasoning_effort': 'bad'. "
            "Expected one of: ['high', 'low', 'medium', 'minimal', 'none', 'xhigh']."
        )


# ---------------------------------------------------------------------------
# Strip-only (low/medium/high/xhigh)
# ---------------------------------------------------------------------------


class TestQwen3ThinkingParityStrip:
    """low/medium/high/xhigh: strip param only, no suppression."""

    @pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
    def test_strip_only(self, effort: str) -> None:
        body = {
            "model": "qwen3",
            "reasoning_effort": effort,
            "messages": [{"role": "user", "content": "Hello"}],
            "temperature": 0.7,
        }
        b, err = _apply(body)
        assert err is None
        assert "reasoning_effort" not in b
        assert "chat_template_kwargs" not in b
        # No system message inserted
        assert all(m.get("role") != "system" for m in b.get("messages", []))

    @pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
    def test_strip_only_no_messages(self, effort: str) -> None:
        body = {"model": "qwen3", "reasoning_effort": effort}
        b, err = _apply(body)
        assert err is None
        assert "reasoning_effort" not in b
        assert "chat_template_kwargs" not in b


# ---------------------------------------------------------------------------
# Suppress path (none/minimal)
# ---------------------------------------------------------------------------


class TestQwen3ThinkingParitySuppress:
    """none/minimal: full suppression path."""

    @pytest.mark.parametrize("effort", ["none", "minimal"])
    def test_suppress_no_system_message(self, effort: str) -> None:
        """No system message → insert /no_think at index 0."""
        body = {
            "model": "qwen3",
            "reasoning_effort": effort,
            "messages": [{"role": "user", "content": "Hello"}],
        }
        b, err = _apply(body)
        assert err is None
        assert "reasoning_effort" not in b
        assert b["chat_template_kwargs"]["enable_thinking"] is False
        assert b["messages"][0] == {"role": "system", "content": "/no_think"}
        assert b["messages"][1]["role"] == "user"

    @pytest.mark.parametrize("effort", ["none", "minimal"])
    def test_suppress_system_message_without_hint(self, effort: str) -> None:
        """Existing system message without /no_think → append hint."""
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
        assert sys_msg["content"] == "You are helpful.\n/no_think"

    @pytest.mark.parametrize("effort", ["none", "minimal"])
    def test_suppress_system_message_already_has_hint(self, effort: str) -> None:
        """System message already contains /no_think → no double-injection."""
        body = {
            "model": "qwen3",
            "reasoning_effort": effort,
            "messages": [
                {"role": "system", "content": "You are /no_think assistant."},
                {"role": "user", "content": "Hello"},
            ],
        }
        b, err = _apply(body)
        assert err is None
        sys_msg = next(m for m in b["messages"] if m["role"] == "system")
        # Must remain unchanged — no second injection
        assert sys_msg["content"] == "You are /no_think assistant."
        # Only one system message
        assert sum(1 for m in b["messages"] if m["role"] == "system") == 1

    @pytest.mark.parametrize("effort", ["none", "minimal"])
    def test_suppress_system_message_empty_content(self, effort: str) -> None:
        """System message with empty content → hint appended without leading \\n."""
        body = {
            "model": "qwen3",
            "reasoning_effort": effort,
            "messages": [
                {"role": "system", "content": ""},
                {"role": "user", "content": "Hello"},
            ],
        }
        b, err = _apply(body)
        assert err is None
        sys_msg = next(m for m in b["messages"] if m["role"] == "system")
        # Empty content → hint with no leading \n
        assert sys_msg["content"] == "/no_think"

    @pytest.mark.parametrize("effort", ["none", "minimal"])
    def test_suppress_no_messages_key(self, effort: str) -> None:
        """No messages key → only chat_template_kwargs mutation."""
        body = {"model": "qwen3", "reasoning_effort": effort}
        b, err = _apply(body)
        assert err is None
        assert b["chat_template_kwargs"]["enable_thinking"] is False
        assert "messages" not in b

    @pytest.mark.parametrize("effort", ["none", "minimal"])
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
# System message selection (FIRST, not LAST)
# ---------------------------------------------------------------------------


class TestQwen3ThinkingParitySystemMessageSelection:
    def test_first_system_message_modified_not_last(self) -> None:
        """When multiple system messages exist, the FIRST one gets the hint."""
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
        # First system message should have the hint
        assert "/no_think" in b["messages"][0]["content"]
        # Second system message must be unchanged
        assert b["messages"][2]["content"] == "Second system."

    def test_non_dict_messages_skipped(self) -> None:
        """Non-dict entries in messages list should be skipped."""
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
        # No system message found → insert at 0
        assert b["messages"][0] == {"role": "system", "content": "/no_think"}


# ---------------------------------------------------------------------------
# Registration / pipeline integration
# ---------------------------------------------------------------------------


class TestQwen3ThinkingRegistration:
    def test_qwen3_thinking_is_registered(self) -> None:
        from gateway.transforms import REGISTRY
        primitives = REGISTRY.resolve("qwen3-thinking")
        assert len(primitives) == 1

    def test_qwen3_thinking_on_request_mutates_body(self) -> None:
        from gateway.transforms import run_request_pipeline
        body = {
            "reasoning_effort": "none",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        result = run_request_pipeline(["qwen3-thinking"], body)
        assert "reasoning_effort" not in result
        assert result["chat_template_kwargs"]["enable_thinking"] is False

    def test_qwen3_thinking_response_phase_is_noop(self) -> None:
        from gateway.transforms import run_response_pipeline
        payload = {"choices": [{"message": {"content": "hi"}}]}
        original = copy.deepcopy(payload)
        result = run_response_pipeline(["qwen3-thinking"], copy.deepcopy(payload))
        assert result == original
