"""Tests for Part B — toolcall detect-and-advise + structural normalization.

SLVP design: docs/design/TOOLCALL_SLVP_DESIGN.md

Gate:
  - Detection fires ONLY when: tools were requested + marker present + tool_calls empty.
  - Detection does NOT fire when: tools absent, or tool_calls already present, or
    no marker in content (no false positives on normal responses).
  - Advisory header maps correctly to each backend preset.
  - args-stringify converts a dict arguments to a JSON string; leaves strings alone.
  - finish-reason repair flips stop→tool_calls only when tool_calls present.
  - Streaming detector: detects at close, passes chunks unchanged, no false positive
    when tool_calls present in stream.
"""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import pytest

from gateway.toolcall_detection import (
    TOOLCALL_MARKERS,
    ToolcallStreamDetector,
    advisory_for_backend,
    detect_unlifted_toolcalls,
    normalize_toolcall_args,
    normalize_toolcall_finish_reason,
    request_had_tools,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _payload(*choices: dict) -> dict[str, Any]:
    return {"id": "cmpl-1", "object": "chat.completion", "choices": list(choices)}


def _message_choice(
    content: str | None = None,
    tool_calls: list | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {"index": 0, "message": msg, "finish_reason": finish_reason}


def _delta_choice(
    content: str | None = None,
    tool_calls: list | None = None,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {"index": 0, "delta": delta, "finish_reason": finish_reason}


def _req_with_tools(**kwargs: Any) -> dict[str, Any]:
    return {
        "model": "test",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
        **kwargs,
    }


def _sse_chunk(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


async def _collect_advisory(detector: ToolcallStreamDetector, chunks: list[bytes]) -> str | None:
    """Run the detector over synthetic chunks and return its advisory."""
    collected = []
    async def _gen():
        for c in chunks:
            yield c

    async for chunk in detector.wrap(_gen()):
        collected.append(chunk)

    # Chunks must be passed through unchanged.
    assert collected == chunks, "Stream detector must not modify chunks"
    return detector.advisory()


# ---------------------------------------------------------------------------
# §5.2 — request_had_tools predicate
# ---------------------------------------------------------------------------

class TestRequestHadTools:
    def test_with_tools(self) -> None:
        assert request_had_tools(_req_with_tools()) is True

    def test_empty_tools(self) -> None:
        assert request_had_tools({"model": "x", "messages": [], "tools": []}) is False

    def test_no_tools_key(self) -> None:
        assert request_had_tools({"model": "x", "messages": []}) is False

    def test_tool_choice_none_suppresses(self) -> None:
        assert request_had_tools(_req_with_tools(tool_choice="none")) is False

    def test_tool_choice_auto_passes(self) -> None:
        assert request_had_tools(_req_with_tools(tool_choice="auto")) is True

    def test_tool_choice_absent_passes(self) -> None:
        req = _req_with_tools()
        req.pop("tool_choice", None)
        assert request_had_tools(req) is True


# ---------------------------------------------------------------------------
# §5.2 — detect_unlifted_toolcalls
# ---------------------------------------------------------------------------

class TestDetectUnliftedToolcalls:
    """Detection fires only when all three conditions hold simultaneously."""

    def test_hermes_marker_detected(self) -> None:
        content = "<tool_call>{\"name\": \"f\", \"arguments\": {}}</tool_call>"
        p = _payload(_message_choice(content=content))
        assert detect_unlifted_toolcalls(p, request_had_tools=True) == "hermes_or_qwen"

    def test_mistral_marker_detected(self) -> None:
        content = "[TOOL_CALLS][{\"name\": \"f\", \"arguments\": {}}]"
        p = _payload(_message_choice(content=content))
        assert detect_unlifted_toolcalls(p, request_had_tools=True) == "mistral"

    def test_llama3_marker_detected(self) -> None:
        content = "<|python_tag|>{\"name\": \"f\", \"parameters\": {}}"
        p = _payload(_message_choice(content=content))
        assert detect_unlifted_toolcalls(p, request_had_tools=True) == "llama3"

    def test_gemma_marker_detected(self) -> None:
        content = "```tool_code\nfunc(a=1)\n```"
        p = _payload(_message_choice(content=content))
        assert detect_unlifted_toolcalls(p, request_had_tools=True) == "gemma"

    def test_command_r_marker_detected(self) -> None:
        content = 'Action: ```json\n[{"tool_name": "f", "parameters": {}}]\n```'
        p = _payload(_message_choice(content=content))
        assert detect_unlifted_toolcalls(p, request_had_tools=True) == "command_r"

    # --- No false positive: tools NOT requested ---

    def test_no_fire_when_tools_not_requested(self) -> None:
        """request_had_tools=False → no detection even when marker present."""
        content = "<tool_call>{\"name\": \"f\"}</tool_call>"
        p = _payload(_message_choice(content=content))
        assert detect_unlifted_toolcalls(p, request_had_tools=False) is None

    # --- No false positive: tool_calls already present ---

    def test_no_fire_when_tool_calls_already_present(self) -> None:
        """tool_calls non-empty on the message → skip detection for that choice."""
        tc = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
        content = "<tool_call>{\"name\": \"f\"}</tool_call>"
        p = _payload(_message_choice(content=content, tool_calls=tc))
        assert detect_unlifted_toolcalls(p, request_had_tools=True) is None

    # --- No false positive: normal response, no marker ---

    def test_no_fire_on_plain_prose(self) -> None:
        """Ordinary assistant response with no marker → None."""
        p = _payload(_message_choice(content="Sure, I can help with that!"))
        assert detect_unlifted_toolcalls(p, request_had_tools=True) is None

    def test_no_fire_on_empty_content(self) -> None:
        p = _payload(_message_choice(content=""))
        assert detect_unlifted_toolcalls(p, request_had_tools=True) is None

    def test_no_fire_on_none_content(self) -> None:
        p = _payload(_message_choice(content=None))
        assert detect_unlifted_toolcalls(p, request_had_tools=True) is None

    def test_non_dict_choices_skipped(self) -> None:
        p: dict[str, Any] = {"choices": ["not a dict"]}
        assert detect_unlifted_toolcalls(p, request_had_tools=True) is None

    def test_no_choices(self) -> None:
        assert detect_unlifted_toolcalls({}, request_had_tools=True) is None

    def test_payload_not_mutated(self) -> None:
        """Detection must never modify the payload."""
        content = "<tool_call>{\"name\": \"f\"}</tool_call>"
        p = _payload(_message_choice(content=content))
        original = copy.deepcopy(p)
        detect_unlifted_toolcalls(p, request_had_tools=True)
        assert p == original


# ---------------------------------------------------------------------------
# §5.3 — advisory_for_backend
# ---------------------------------------------------------------------------

class TestAdvisoryForBackend:
    """The advisory is ENGINE-AGNOSTIC (ENGINE_KNOWLEDGE_BOUNDARY.md): it carries
    the detected family (data, from the marker table) + a generic instruction,
    and NEVER per-engine CLI flags (which churn and are the maintenance treadmill).
    Exact backend flags, if wanted, come from an operator-authored data field."""

    # Engine CLI flags that must NEVER appear in the gateway's own advisory text.
    _FORBIDDEN_FLAGS = (
        "--tool-call-parser",
        "--enable-auto-tool-choice",
        "--chat_format",
        "--jinja",
        "chatml-function-calling",
    )

    def test_advisory_states_detected_family_and_generic_fix(self) -> None:
        msg = advisory_for_backend("vllm", "hermes_or_qwen")
        assert "hermes_or_qwen" in msg          # detected family (data) is surfaced
        assert "tool_calls" in msg              # names the missing structured field
        assert "configuration" in msg.lower() or "config" in msg.lower()
        assert "vllm" in msg.lower()            # backend label included for context

    def test_advisory_carries_no_engine_cli_flags(self) -> None:
        # Across every backend hint and every detected family, the gateway's own
        # advisory must contain ZERO engine-specific CLI flags.
        for hint in ("vllm", "sglang", "llamacpp", "llama-server", "llama-cpp-python",
                     "ollama", "tabbyapi", "koboldcpp", "my-custom-server", ""):
            for _, family in TOOLCALL_MARKERS:
                msg = advisory_for_backend(hint, family)
                for flag in self._FORBIDDEN_FLAGS:
                    assert flag not in msg, (
                        f"engine CLI flag {flag!r} leaked into advisory for "
                        f"{hint!r}/{family!r}: {msg!r}"
                    )

    def test_operator_authored_advisory_is_appended(self) -> None:
        # Exact per-backend remediation, when wanted, is supplied as DATA by the
        # operator (a service-record field), never maintained as code.
        custom = "Restart vLLM with --tool-call-parser hermes."
        msg = advisory_for_backend("vllm", "hermes_or_qwen", operator_advisory=custom)
        assert custom in msg
        # ...and the generic base is still present.
        assert "tool_calls" in msg

    def test_no_operator_advisory_omits_specifics(self) -> None:
        msg = advisory_for_backend("vllm", "hermes_or_qwen", operator_advisory=None)
        assert "Restart vLLM" not in msg

    def test_generic_unknown_backend(self) -> None:
        msg = advisory_for_backend("my-custom-server", "llama3")
        assert "llama3" in msg.lower()
        assert len(msg) > 20

    def test_header_value_is_single_line(self) -> None:
        """Advisory must not contain newlines (used as a response header value)."""
        for hint in ("vllm", "sglang", "llamacpp", "ollama", "unknown-backend"):
            for _, family in TOOLCALL_MARKERS:
                msg = advisory_for_backend(hint, family)
                assert "\n" not in msg, f"newline in advisory for {hint}/{family}: {msg!r}"


# ---------------------------------------------------------------------------
# §6.1 — normalize_toolcall_args (args-stringify)
# ---------------------------------------------------------------------------

class TestNormalizeToolcallArgs:
    def _tc(self, arguments: Any) -> dict[str, Any]:
        return {
            "id": "c1", "type": "function",
            "function": {"name": "f", "arguments": arguments},
        }

    def test_dict_args_stringified(self) -> None:
        tc = self._tc({"city": "Paris"})
        p = _payload(_message_choice(tool_calls=[tc]))
        changed = normalize_toolcall_args(p)
        assert changed is True
        result_args = p["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(result_args, str)
        assert json.loads(result_args) == {"city": "Paris"}

    def test_string_args_untouched(self) -> None:
        tc = self._tc('{"city": "Paris"}')
        p = _payload(_message_choice(tool_calls=[tc]))
        original = copy.deepcopy(p)
        changed = normalize_toolcall_args(p)
        assert changed is False
        assert p == original

    def test_no_tool_calls_noop(self) -> None:
        p = _payload(_message_choice(content="Hello"))
        original = copy.deepcopy(p)
        changed = normalize_toolcall_args(p)
        assert changed is False
        assert p == original

    def test_empty_choices_noop(self) -> None:
        assert normalize_toolcall_args({}) is False

    def test_multiple_tool_calls_all_stringified(self) -> None:
        tcs = [self._tc({"a": 1}), self._tc({"b": 2})]
        p = _payload(_message_choice(tool_calls=tcs))
        changed = normalize_toolcall_args(p)
        assert changed is True
        for tc in p["choices"][0]["message"]["tool_calls"]:
            assert isinstance(tc["function"]["arguments"], str)

    def test_dict_args_in_delta_shape(self) -> None:
        """Delta (streaming) shape is also handled."""
        tc = self._tc({"x": 42})
        choice = {"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}
        p = {"choices": [choice]}
        changed = normalize_toolcall_args(p)
        assert changed is True
        result = p["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(result, str)
        assert json.loads(result) == {"x": 42}

    def test_already_correct_string_args_unchanged(self) -> None:
        """Idempotent: running twice must not double-encode."""
        tc = self._tc({"city": "Paris"})
        p = _payload(_message_choice(tool_calls=[tc]))
        normalize_toolcall_args(p)
        first_result = copy.deepcopy(p)
        normalize_toolcall_args(p)
        assert p == first_result


# ---------------------------------------------------------------------------
# §6.2 — normalize_toolcall_finish_reason (finish-reason repair)
# ---------------------------------------------------------------------------

class TestNormalizeToolcallFinishReason:
    def _tc_item(self) -> dict[str, Any]:
        return {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}

    def test_stop_repaired_when_tool_calls_present(self) -> None:
        p = _payload(_message_choice(tool_calls=[self._tc_item()], finish_reason="stop"))
        changed = normalize_toolcall_finish_reason(p)
        assert changed is True
        assert p["choices"][0]["finish_reason"] == "tool_calls"

    def test_already_tool_calls_noop(self) -> None:
        p = _payload(_message_choice(tool_calls=[self._tc_item()], finish_reason="tool_calls"))
        original = copy.deepcopy(p)
        changed = normalize_toolcall_finish_reason(p)
        assert changed is False
        assert p == original

    def test_plain_stop_without_tool_calls_untouched(self) -> None:
        """Never touch finish_reason=stop when no tool_calls present."""
        p = _payload(_message_choice(content="Sure!", finish_reason="stop"))
        original = copy.deepcopy(p)
        changed = normalize_toolcall_finish_reason(p)
        assert changed is False
        assert p == original

    def test_none_finish_reason_repaired(self) -> None:
        """finish_reason=None (mid-stream) is also repaired when tool_calls present."""
        p = _payload(_message_choice(tool_calls=[self._tc_item()], finish_reason=None))
        changed = normalize_toolcall_finish_reason(p)
        assert changed is True
        assert p["choices"][0]["finish_reason"] == "tool_calls"

    def test_no_tool_calls_noop(self) -> None:
        p = _payload(_message_choice(content="Hello", finish_reason="stop"))
        changed = normalize_toolcall_finish_reason(p)
        assert changed is False

    def test_empty_choices_noop(self) -> None:
        assert normalize_toolcall_finish_reason({}) is False

    def test_delta_shape_repaired(self) -> None:
        """finish_reason repair works on streaming delta shape too."""
        tc = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
        choice = {"index": 0, "delta": {"tool_calls": tc}, "finish_reason": "stop"}
        p = {"choices": [choice]}
        changed = normalize_toolcall_finish_reason(p)
        assert changed is True
        assert p["choices"][0]["finish_reason"] == "tool_calls"

    def test_idempotent(self) -> None:
        p = _payload(_message_choice(tool_calls=[self._tc_item()], finish_reason="stop"))
        normalize_toolcall_finish_reason(p)
        first = copy.deepcopy(p)
        normalize_toolcall_finish_reason(p)
        assert p == first


# ---------------------------------------------------------------------------
# §5.5 — ToolcallStreamDetector
# ---------------------------------------------------------------------------

class TestToolcallStreamDetector:
    def _run(self, chunks: list[bytes], had_tools: bool = True, backend: str = "vllm") -> str | None:
        detector = ToolcallStreamDetector(had_tools=had_tools, backend_hint=backend)
        return asyncio.run(
            _collect_advisory(detector, chunks)
        )

    def test_detects_hermes_marker_at_stream_close(self) -> None:
        chunks = [
            _sse_chunk(_payload(_delta_choice(content="<tool_call>"))),
            _sse_chunk(_payload(_delta_choice(content="{\"name\": \"f\"}"))),
            b"data: [DONE]\n\n",
        ]
        advisory = self._run(chunks, had_tools=True, backend="vllm")
        assert advisory is not None
        # The detected family (data) is surfaced; no engine CLI flag.
        assert "hermes_or_qwen" in advisory
        assert "--tool-call-parser" not in advisory

    def test_detects_mistral_marker(self) -> None:
        chunks = [
            _sse_chunk(_payload(_delta_choice(content="[TOOL_CALLS]"))),
            b"data: [DONE]\n\n",
        ]
        advisory = self._run(chunks, had_tools=True, backend="vllm")
        assert advisory is not None
        assert "mistral" in advisory

    def test_no_advisory_when_tool_calls_in_stream(self) -> None:
        """If delta.tool_calls is present in any chunk, detection is suppressed."""
        tc = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
        chunks = [
            _sse_chunk(_payload(_delta_choice(tool_calls=tc))),
            b"data: [DONE]\n\n",
        ]
        advisory = self._run(chunks, had_tools=True, backend="vllm")
        assert advisory is None

    def test_no_advisory_when_tools_not_requested(self) -> None:
        """had_tools=False → always None even when marker present."""
        chunks = [
            _sse_chunk(_payload(_delta_choice(content="<tool_call>{\"name\": \"f\"}</tool_call>"))),
            b"data: [DONE]\n\n",
        ]
        advisory = self._run(chunks, had_tools=False, backend="vllm")
        assert advisory is None

    def test_no_advisory_on_plain_prose(self) -> None:
        """Ordinary streaming response with no marker → None."""
        chunks = [
            _sse_chunk(_payload(_delta_choice(content="Sure, "))),
            _sse_chunk(_payload(_delta_choice(content="I can help!"))),
            b"data: [DONE]\n\n",
        ]
        advisory = self._run(chunks, had_tools=True, backend="vllm")
        assert advisory is None

    def test_chunks_passed_through_unchanged(self) -> None:
        """The detector must not modify any chunk byte."""
        chunks = [
            _sse_chunk(_payload(_delta_choice(content="<tool_call>hi</tool_call>"))),
            b"data: [DONE]\n\n",
        ]
        detector = ToolcallStreamDetector(had_tools=True, backend_hint="vllm")
        collected: list[bytes] = []

        async def run() -> None:
            async def _gen():
                for c in chunks:
                    yield c
            async for chunk in detector.wrap(_gen()):
                collected.append(chunk)

        asyncio.run(run())
        assert collected == chunks

    def test_marker_split_across_chunks_detected(self) -> None:
        """Marker split across two chunks is caught via rolling tail."""
        # "<|python_tag|>" split into two chunks
        chunks = [
            _sse_chunk(_payload(_delta_choice(content="<|python_"))),
            _sse_chunk(_payload(_delta_choice(content="tag|>{\"name\": \"f\"}"))),
            b"data: [DONE]\n\n",
        ]
        advisory = self._run(chunks, had_tools=True, backend="llamacpp")
        assert advisory is not None
        # Split marker was reassembled and the family detected; engine-agnostic.
        assert "llama3" in advisory
        assert "--jinja" not in advisory

    def test_stream_advisory_is_engine_agnostic(self) -> None:
        """Stream advisory surfaces the detected family + backend label, never an
        engine CLI flag (ENGINE_KNOWLEDGE_BOUNDARY.md)."""
        chunks = [
            _sse_chunk(_payload(_delta_choice(content="<tool_call>{}</tool_call>"))),
            b"data: [DONE]\n\n",
        ]
        advisory = self._run(chunks, had_tools=True, backend="llamacpp")
        assert advisory is not None
        assert "llamacpp" in advisory.lower()
        assert "--jinja" not in advisory
        assert "--tool-call-parser" not in advisory

    def test_detected_family_accessible(self) -> None:
        chunks = [
            _sse_chunk(_payload(_delta_choice(content="[TOOL_CALLS][{\"name\":\"f\"}]"))),
            b"data: [DONE]\n\n",
        ]
        detector = ToolcallStreamDetector(had_tools=True, backend_hint="vllm")
        asyncio.run(_collect_advisory(detector, chunks))
        assert detector.detected_family == "mistral"


@pytest.mark.parametrize("structured_call", [False, True])
@pytest.mark.parametrize("padding", ["", "x" * 256])
def test_stream_advisory_survives_transport_splits_and_long_deltas(structured_call, padding):
    marker = _sse_chunk(_payload(_delta_choice(content="<tool_call>" + padding)))
    calls = [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    tool_frame = _sse_chunk(_payload(_delta_choice(tool_calls=calls)))
    wire = marker + (tool_frame if structured_call else b"") + b"data: [DONE]\n\n"

    async def exercise():
        partitions = [[wire]]
        partitions.extend([wire[:i], wire[i:]] for i in range(1, len(wire)))
        partitions.append([wire[i : i + 1] for i in range(len(wire))])
        for index, chunks in enumerate(partitions):
            detector = ToolcallStreamDetector(had_tools=True, backend_hint="fixture")
            advisory = await _collect_advisory(detector, chunks)
            if structured_call:
                assert advisory is None, (index, advisory)
            else:
                assert advisory is not None, index
                assert "hermes_or_qwen" in advisory, (index, advisory)

    asyncio.run(exercise())


def test_oversized_unterminated_line_is_not_retained():
    detector = ToolcallStreamDetector(had_tools=True, backend_hint="fixture")
    detector._process_chunk(b": " + b"x" * ((1 << 20) + 1))
    assert detector._line_carry == b""
