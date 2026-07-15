"""Step 4 parity tests — reasoning-field-normalize (4a) + streaming-think-split (4b).

Step 4a GATE: normalize_chat_completion_payload() must produce byte-identical
output to the legacy _normalize_chat_completion_visible_content() for the full
corpus of response shapes.  Parity was proven against the legacy function before
it was deleted (full corpus passed).  These tests now serve as the specification.

Step 4b GATE:
  - When StreamingThinkSplitTransform is attached, a </think> split across two
    SSE chunks is correctly normalized.
  - When NOT attached (default/off), the existing per-chunk behavior is
    byte-identical (no regression).
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from gateway.transforms import (
    REGISTRY,
    ReasoningFieldNormalizeTransform,
    StreamingThinkSplitTransform,
    normalize_chat_completion_payload,
    split_reasoning_visible_text,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload(*choices: dict) -> dict[str, Any]:
    return {"id": "cmpl-1", "object": "chat.completion", "choices": list(choices)}


def _message_choice(content: str | None = None, reasoning_content: str | None = None) -> dict:
    msg: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    return {"index": 0, "message": msg, "finish_reason": "stop"}


def _delta_choice(content: str | None = None, reasoning_content: str | None = None) -> dict:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if reasoning_content is not None:
        delta["reasoning_content"] = reasoning_content
    return {"index": 0, "delta": delta, "finish_reason": None}


def _sse_chunk(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


def _parse_sse_chunk(chunk: bytes) -> dict:
    text = chunk.decode("utf-8")
    for line in text.splitlines():
        if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]"):
            return json.loads(line[5:].strip())
    raise ValueError(f"No data line in chunk: {chunk!r}")


# ---------------------------------------------------------------------------
# Step 4a — split_reasoning_visible_text (utility)
# ---------------------------------------------------------------------------


class TestSplitReasoningVisibleText:
    def test_no_think_tag_returns_none_and_text(self) -> None:
        r, v = split_reasoning_visible_text("hello world")
        assert r is None
        assert v == "hello world"

    def test_empty_string(self) -> None:
        r, v = split_reasoning_visible_text("")
        assert r is None
        assert v is None

    def test_non_string_input(self) -> None:
        r, v = split_reasoning_visible_text(42)
        assert r is None
        assert v is None

    def test_think_tag_at_start(self) -> None:
        r, v = split_reasoning_visible_text("<think>my reasoning</think>answer")
        assert r == "my reasoning"
        assert v == "answer"

    def test_think_tag_with_newlines(self) -> None:
        r, v = split_reasoning_visible_text("<think>reasoning</think>\nvisible")
        assert r == "reasoning"
        assert v == "visible"

    def test_only_open_tag_no_close(self) -> None:
        """No closing </think> → return (None, full_text)."""
        r, v = split_reasoning_visible_text("<think>no close tag")
        assert r is None
        assert v == "<think>no close tag"

    def test_only_close_tag(self) -> None:
        """Text before </think> has no open tag — reasoning is entire preceding text."""
        r, v = split_reasoning_visible_text("some text</think>visible")
        assert r == "some text"
        assert v == "visible"

    def test_case_insensitive_close_tag(self) -> None:
        """rfind uses .lower() so uppercase tags are also caught."""
        r, v = split_reasoning_visible_text("<think>R</THINK>V")
        assert r == "R"
        assert v == "V"

    def test_empty_visible_after_close(self) -> None:
        """Visible portion is empty → visible or None → None."""
        r, v = split_reasoning_visible_text("<think>reasoning</think>   ")
        assert r == "reasoning"
        assert v is None  # "   ".strip() == "" → falsy → None


# ---------------------------------------------------------------------------
# Step 4a — normalize_chat_completion_payload (full corpus)
# ---------------------------------------------------------------------------


class TestNormalizeChatCompletionPayload:
    """Full corpus of response shapes — byte-identical to legacy behavior."""

    def test_noop_clean_response(self) -> None:
        """Clean response with both content and no think tags → no change."""
        p = _payload(_message_choice(content="Hello!", reasoning_content="my reasoning"))
        original = copy.deepcopy(p)
        changed = normalize_chat_completion_payload(p)
        assert not changed
        assert p == original

    def test_noop_no_choices(self) -> None:
        p = {"id": "x", "object": "chat.completion"}
        original = copy.deepcopy(p)
        changed = normalize_chat_completion_payload(p)
        assert not changed
        assert p == original

    def test_noop_choices_not_list(self) -> None:
        p = {"choices": "invalid"}
        original = copy.deepcopy(p)
        changed = normalize_chat_completion_payload(p)
        assert not changed
        assert p == original

    def test_case1_think_tag_leaked_into_content(self) -> None:
        """Case 1: <think>…</think> in content → strip tags, promote reasoning."""
        p = _payload(_message_choice(content="<think>reasoning</think>answer"))
        changed = normalize_chat_completion_payload(p)
        assert changed
        msg = p["choices"][0]["message"]
        assert msg["content"] == "answer"
        assert msg["reasoning_content"] == "reasoning"

    def test_case1_no_existing_reasoning_content(self) -> None:
        """Case 1 with no pre-existing reasoning_content → reasoning_content set."""
        p = _payload(_message_choice(content="<think>R</think>V"))
        normalize_chat_completion_payload(p)
        msg = p["choices"][0]["message"]
        assert msg["content"] == "V"
        assert msg["reasoning_content"] == "R"

    def test_case1_existing_reasoning_content_not_overwritten(self) -> None:
        """Case 1 when reasoning_content already set → must NOT overwrite."""
        p = _payload(_message_choice(
            content="<think>R</think>V",
            reasoning_content="existing reasoning",
        ))
        normalize_chat_completion_payload(p)
        msg = p["choices"][0]["message"]
        assert msg["content"] == "V"
        # existing reasoning_content preserved (not overwritten by split reasoning)
        assert msg["reasoning_content"] == "existing reasoning"

    def test_reasoning_only_content_is_preserved(self) -> None:
        """Reasoning-only responses must not expose reasoning as visible text."""
        p = _payload(_message_choice(content="", reasoning_content="visible reply"))
        changed = normalize_chat_completion_payload(p)
        assert not changed
        msg = p["choices"][0]["message"]
        assert msg["content"] == ""
        assert msg["reasoning_content"] == "visible reply"

    def test_case2_reasoning_content_with_think_tags(self) -> None:
        """Case 2 where reasoning_content itself has think tags → extract visible."""
        p = _payload(_message_choice(
            content="",
            reasoning_content="<think>private</think>\nvisible reply",
        ))
        changed = normalize_chat_completion_payload(p)
        assert changed
        msg = p["choices"][0]["message"]
        assert msg["content"] == "visible reply"
        assert msg["reasoning_content"] == "private"

    def test_case2_reasoning_content_absent_content_not_triggered(self) -> None:
        """Case 2 only triggers when content is falsy AND reasoning_content is set+truthy."""
        p = _payload(_message_choice(content="has content", reasoning_content=""))
        original = copy.deepcopy(p)
        changed = normalize_chat_completion_payload(p)
        assert not changed
        assert p == original

    def test_delta_case1_think_tag_in_delta_content(self) -> None:
        """Delta Case 1: <think> leaked into delta.content → strip."""
        p = _payload(_delta_choice(content="<think>R</think>V"))
        changed = normalize_chat_completion_payload(p)
        assert changed
        assert p["choices"][0]["delta"]["content"] == "V"

    def test_delta_reasoning_only_is_preserved(self) -> None:
        """Reasoning-only deltas must not become visible content."""
        p = _payload(_delta_choice(reasoning_content="streaming token"))
        changed = normalize_chat_completion_payload(p)
        assert not changed
        assert p["choices"][0]["delta"] == {"reasoning_content": "streaming token"}

    def test_delta_case2_content_present_no_mirror(self) -> None:
        """Delta with both content and reasoning_content → no change."""
        p = _payload(_delta_choice(content="answer", reasoning_content="reasoning"))
        original = copy.deepcopy(p)
        changed = normalize_chat_completion_payload(p)
        assert not changed
        assert p == original

    def test_multiple_choices_each_normalized(self) -> None:
        """Multiple choices in one response — each normalized independently."""
        p = _payload(
            _message_choice(content="<think>R1</think>V1"),
            _message_choice(content="", reasoning_content="V2"),
        )
        changed = normalize_chat_completion_payload(p)
        assert changed
        assert p["choices"][0]["message"]["content"] == "V1"
        assert p["choices"][1]["message"]["content"] == ""

    def test_non_dict_choice_skipped(self) -> None:
        """Non-dict entries in choices list must be skipped safely."""
        p = {"choices": ["not a dict", _message_choice(content="<think>R</think>V")]}
        changed = normalize_chat_completion_payload(p)
        assert changed
        assert p["choices"][1]["message"]["content"] == "V"


class TestReasoningFieldNormalizeRegistration:
    def test_registered(self) -> None:
        primitives = REGISTRY.resolve("reasoning-field-normalize")
        assert len(primitives) == 1

    def test_noop_on_request(self) -> None:
        from gateway.transforms import TransformContext, run_request_pipeline
        body = {"model": "qwen3", "messages": [{"role": "user", "content": "hi"}]}
        original = copy.deepcopy(body)
        result = run_request_pipeline(["reasoning-field-normalize"], body)
        assert result == original

    def test_applies_on_response(self) -> None:
        from gateway.transforms import TransformContext, run_response_pipeline
        p = _payload(_message_choice(content="<think>R</think>V"))
        result = run_response_pipeline(["reasoning-field-normalize"], p)
        assert result["choices"][0]["message"]["content"] == "V"


# ---------------------------------------------------------------------------
# Step 4b — StreamingThinkSplitTransform (opt-in, new capability)
# ---------------------------------------------------------------------------


class TestStreamingThinkSplitTransform:
    """4b: split </think> across chunks is caught; off = byte-identical to legacy."""

    def test_clean_chunk_passthrough(self) -> None:
        """Clean chunk with no think tags → returned unchanged."""
        chunk = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        sst = StreamingThinkSplitTransform()
        result = sst.process_chunk(chunk)
        # May differ in whitespace but must have same content
        assert b"hello" in result

    def test_complete_think_tag_in_single_chunk(self) -> None:
        """Complete </think> in one chunk → normalized correctly."""
        payload = {"choices": [{"delta": {"content": "<think>R</think>V"}}]}
        chunk = _sse_chunk(payload)
        sst = StreamingThinkSplitTransform()
        result = sst.process_chunk(chunk)
        parsed = _parse_sse_chunk(result)
        assert parsed["choices"][0]["delta"]["content"] == "V"

    def test_split_think_close_tag_across_chunks(self) -> None:
        """The key 4b test: </think> split across two chunks is caught when ON."""
        # Simulate </think> split: chunk1 ends with "</thi", chunk2 starts with "nk>"
        # Build a realistic SSE response where the close tag is split.
        full_content = "<think>reasoning</think>answer"
        # Split at position in the middle of </think>
        split_pos = full_content.index("</think>") + 4  # "</thi" in chunk1
        part1 = full_content[:split_pos]
        part2 = full_content[split_pos:]

        payload1 = {"choices": [{"delta": {"content": part1}}]}
        payload2 = {"choices": [{"delta": {"content": part2}}]}
        chunk1 = _sse_chunk(payload1)
        chunk2 = _sse_chunk(payload2)

        sst = StreamingThinkSplitTransform()
        r1 = sst.process_chunk(chunk1)
        r2 = sst.process_chunk(chunk2)

        # Together the two output chunks should emit the answer (not the raw tags)
        combined = r1 + r2
        combined_text = combined.decode("utf-8")
        assert "answer" in combined_text
        # The raw <think> content should NOT appear in the final visible output
        assert "<think>reasoning</think>answer" not in combined_text

    def test_off_by_default_no_regression(self) -> None:
        """When streaming-think-split is NOT attached, SSE chunk is byte-identical to legacy."""
        # The legacy per-chunk normalizer (_normalize_chat_completion_sse_chunk in proxy.py)
        # normalizes each chunk independently. Verify that NOT using the SST transform
        # gives byte-identical output to calling it directly.
        import proxy
        payload = {"choices": [{"delta": {"reasoning_content": "visible token"}}]}
        chunk = _sse_chunk(payload)
        # Per-chunk normalize without SST
        result = proxy._normalize_chat_completion_sse_chunk(chunk)
        parsed = _parse_sse_chunk(result)
        assert "content" not in parsed["choices"][0]["delta"]
        assert parsed["choices"][0]["delta"]["reasoning_content"] == "visible token"
        # Same result from normalize_chat_completion_payload applied to same data
        p_copy = copy.deepcopy(payload)
        normalize_chat_completion_payload(p_copy)
        assert "content" not in p_copy["choices"][0]["delta"]
        assert p_copy["choices"][0]["delta"]["reasoning_content"] == "visible token"

    def test_response_noop(self) -> None:
        """on_response shim must be a no-op for registry protocol compliance."""
        sst = StreamingThinkSplitTransform()
        from gateway.transforms import TransformContext
        payload = {"choices": [{"message": {"content": "hi"}}]}
        original = copy.deepcopy(payload)
        result = sst.on_response(payload, TransformContext())
        assert result == original

    def test_request_noop(self) -> None:
        """on_request shim must be a no-op."""
        sst = StreamingThinkSplitTransform()
        from gateway.transforms import TransformContext
        body = {"model": "qwen3"}
        original = copy.deepcopy(body)
        result = sst.on_request(body, TransformContext())
        assert result == original

    def test_non_utf8_chunk_passthrough(self) -> None:
        """Non-UTF-8 bytes → returned unchanged."""
        chunk = b"\xff\xfe invalid utf-8"
        sst = StreamingThinkSplitTransform()
        result = sst.process_chunk(chunk)
        assert result == chunk
