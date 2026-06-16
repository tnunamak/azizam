"""Step 6 tests — tool-call family EXTRACTORS (response-phase built-ins).

Each extractor lifts plain-TEXT tool calls (from a backend started WITHOUT a
--tool-call-parser flag) into proper OpenAI tool_calls.  For EACH of the six
families we assert:
  1. POSITIVE: a real-format response → correct tool_calls + finish_reason
     "tool_calls" + the lifted text stripped from content.
  2. NEGATIVE / no-op: plain text with no markers → untouched.
  3. MALFORMED: broken markup → PASS THROUGH (no broken extract).  False-extract
     is the dangerous failure mode, so malformed input must be left as content.

Plus a non-alteration guard: none of these auto-attach to existing services,
and a clean (already-proper) response is untouched.

Formats verified 2026-06-16 against vLLM tool parsers + official chat templates
(see docs/design/TOOLCALL_EXTRACTORS_REPORT.md).  Unit tests against REGISTRY.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from gateway.transforms import (
    REGISTRY,
    run_request_pipeline,
    run_response_pipeline,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resp(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run the named extractor's response phase on a deep copy of payload."""
    return run_response_pipeline([name], copy.deepcopy(payload))


def _wrap(content: str) -> dict[str, Any]:
    """A minimal chat-completion payload with one assistant message."""
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ]
    }


def _calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result["choices"][0]["message"].get("tool_calls") or []


def _content(result: dict[str, Any]) -> Any:
    return result["choices"][0]["message"].get("content")


def _finish(result: dict[str, Any]) -> Any:
    return result["choices"][0]["finish_reason"]


def _args(call: dict[str, Any]) -> dict[str, Any]:
    return json.loads(call["function"]["arguments"])


# All six extractor ids, for shared guards.
ALL_EXTRACTORS = [
    "hermes2pro-toolcall-extract",
    "mistral-toolcall-extract",
    "commandr-toolcall-extract",
    "qwen-xml-toolcall-extract",
    "gemma-toolcall-extract",
    "llama3-toolcall-extract",
]


# ===========================================================================
# Hermes — <tool_call>{"name":...,"arguments":{...}}</tool_call>
# ===========================================================================


class TestHermesExtractor:
    NAME = "hermes2pro-toolcall-extract"

    def test_registered(self) -> None:
        assert len(REGISTRY.resolve(self.NAME)) == 1

    def test_positive(self) -> None:
        content = (
            'Let me check the weather.\n'
            '<tool_call>\n'
            '{"name": "get_weather", "arguments": {"city": "Paris"}}\n'
            '</tool_call>'
        )
        result = _resp(self.NAME, _wrap(content))
        calls = _calls(result)
        assert len(calls) == 1
        assert calls[0]["type"] == "function"
        assert calls[0]["function"]["name"] == "get_weather"
        assert _args(calls[0]) == {"city": "Paris"}
        assert _finish(result) == "tool_calls"
        # Lifted text stripped; the preamble prose remains.
        assert "<tool_call>" not in (_content(result) or "")
        assert "check the weather" in _content(result)

    def test_arguments_first_key_order(self) -> None:
        # The Hermes card shows arguments-first ordering — must still parse.
        content = '<tool_call>\n{"arguments": {"x": 1}, "name": "f"}\n</tool_call>'
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result)[0]["function"]["name"] == "f"
        assert _content(result) is None  # nothing but the tool call

    def test_multiple_calls(self) -> None:
        content = (
            '<tool_call>\n{"name": "a", "arguments": {}}\n</tool_call>'
            '<tool_call>\n{"name": "b", "arguments": {"k": "v"}}\n</tool_call>'
        )
        result = _resp(self.NAME, _wrap(content))
        names = [c["function"]["name"] for c in _calls(result)]
        assert names == ["a", "b"]

    def test_negative_plain_text(self) -> None:
        payload = _wrap("Just a normal answer with no tool calls.")
        result = _resp(self.NAME, payload)
        assert _calls(result) == []
        assert _content(result) == "Just a normal answer with no tool calls."
        assert _finish(result) == "stop"

    def test_malformed_unterminated_passthrough(self) -> None:
        content = '<tool_call>\n{"name": "f", "arguments": {}}'  # no closing tag
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content  # untouched
        assert _finish(result) == "stop"

    def test_malformed_bad_json_passthrough(self) -> None:
        content = '<tool_call>\n{not valid json}\n</tool_call>'
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content

    def test_malformed_no_name_passthrough(self) -> None:
        content = '<tool_call>\n{"arguments": {}}\n</tool_call>'
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content


# ===========================================================================
# Mistral — [TOOL_CALLS][{"name":...,"arguments":{...}}]
# ===========================================================================


class TestMistralExtractor:
    NAME = "mistral-toolcall-extract"

    def test_registered(self) -> None:
        assert len(REGISTRY.resolve(self.NAME)) == 1

    def test_positive(self) -> None:
        content = '[TOOL_CALLS][{"name": "get_weather", "arguments": {"city": "Paris"}}]'
        result = _resp(self.NAME, _wrap(content))
        calls = _calls(result)
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "get_weather"
        assert _args(calls[0]) == {"city": "Paris"}
        assert _finish(result) == "tool_calls"
        assert _content(result) is None

    def test_positive_with_leading_text(self) -> None:
        content = 'Sure!\n[TOOL_CALLS][{"name": "f", "arguments": {}}]'
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result)[0]["function"]["name"] == "f"
        assert _content(result) == "Sure!"

    def test_parallel_calls(self) -> None:
        content = (
            '[TOOL_CALLS]['
            '{"name": "a", "arguments": {}},'
            '{"name": "b", "arguments": {"k": 1}}]'
        )
        result = _resp(self.NAME, _wrap(content))
        assert [c["function"]["name"] for c in _calls(result)] == ["a", "b"]

    def test_negative_plain_text(self) -> None:
        result = _resp(self.NAME, _wrap("No tools here."))
        assert _calls(result) == []
        assert _content(result) == "No tools here."

    def test_malformed_bad_array_passthrough(self) -> None:
        content = '[TOOL_CALLS][{"name": "f", "arguments":'  # truncated
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content

    def test_malformed_not_array_passthrough(self) -> None:
        content = '[TOOL_CALLS]{"name": "f"}'  # object, not array
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content


# ===========================================================================
# Command-R — Action: ```json [ {"tool_name":...,"parameters":{...}} ] ```
# ===========================================================================


class TestCommandRExtractor:
    NAME = "commandr-toolcall-extract"

    def test_registered(self) -> None:
        assert len(REGISTRY.resolve(self.NAME)) == 1

    def test_positive(self) -> None:
        content = (
            "Action: ```json\n"
            "[\n"
            '    {\n'
            '        "tool_name": "internet_search",\n'
            '        "parameters": {"query": "biggest penguin"}\n'
            "    }\n"
            "]\n"
            "```"
        )
        result = _resp(self.NAME, _wrap(content))
        calls = _calls(result)
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "internet_search"
        assert _args(calls[0]) == {"query": "biggest penguin"}
        assert _finish(result) == "tool_calls"
        assert _content(result) is None

    def test_uses_tool_name_and_parameters_keys(self) -> None:
        # Command-R uses tool_name/parameters, NOT name/arguments.
        content = 'Action: ```json\n[{"name": "f", "arguments": {}}]\n```'
        # name/arguments instead of tool_name/parameters → no tool_name → pass through.
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content

    def test_negative_plain_text(self) -> None:
        result = _resp(self.NAME, _wrap("Here is my plain answer."))
        assert _calls(result) == []
        assert _content(result) == "Here is my plain answer."

    def test_negative_action_word_in_prose(self) -> None:
        # "Action:" appears but no json fence — must not trigger.
        result = _resp(self.NAME, _wrap("Action: think carefully before answering."))
        assert _calls(result) == []

    def test_malformed_bad_json_passthrough(self) -> None:
        content = 'Action: ```json\n[{"tool_name": "f", "parameters":}]\n```'
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content


# ===========================================================================
# Qwen-XML — <tool_call><function=NAME><parameter=K>V</parameter></function></tool_call>
# ===========================================================================


class TestQwenXmlExtractor:
    NAME = "qwen-xml-toolcall-extract"

    def test_registered(self) -> None:
        assert len(REGISTRY.resolve(self.NAME)) == 1

    def test_positive(self) -> None:
        content = (
            "<tool_call>\n"
            "<function=get_weather>\n"
            "<parameter=city>\nParis\n</parameter>\n"
            "<parameter=days>\n3\n</parameter>\n"
            "</function>\n"
            "</tool_call>"
        )
        result = _resp(self.NAME, _wrap(content))
        calls = _calls(result)
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "get_weather"
        args = _args(calls[0])
        assert args["city"] == "Paris"
        assert args["days"] == 3  # JSON-coerced to int
        assert _finish(result) == "tool_calls"
        assert _content(result) is None

    def test_positive_with_preamble(self) -> None:
        content = (
            "Let me look that up.\n"
            "<tool_call>\n<function=f>\n<parameter=x>\nhello\n</parameter>\n"
            "</function>\n</tool_call>"
        )
        result = _resp(self.NAME, _wrap(content))
        assert _args(_calls(result)[0]) == {"x": "hello"}
        assert "look that up" in _content(result)

    def test_negative_hermes_json_form_passthrough(self) -> None:
        # Hermes JSON inside <tool_call> is NOT the Qwen XML form — pass through.
        content = '<tool_call>\n{"name": "f", "arguments": {}}\n</tool_call>'
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content

    def test_negative_plain_text(self) -> None:
        result = _resp(self.NAME, _wrap("No XML tool calls here."))
        assert _calls(result) == []
        assert _content(result) == "No XML tool calls here."

    def test_malformed_no_function_passthrough(self) -> None:
        content = "<tool_call>\n<function=>\n</function>\n</tool_call>"  # empty name
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content


# ===========================================================================
# Gemma — ```tool_code\n func(arg=val) \n```
# ===========================================================================


class TestGemmaExtractor:
    NAME = "gemma-toolcall-extract"

    def test_registered(self) -> None:
        assert len(REGISTRY.resolve(self.NAME)) == 1

    def test_positive(self) -> None:
        content = (
            "```tool_code\n"
            'convert(amount=200000.0, currency="USD", new_currency="EUR")\n'
            "```"
        )
        result = _resp(self.NAME, _wrap(content))
        calls = _calls(result)
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "convert"
        args = _args(calls[0])
        assert args == {"amount": 200000.0, "currency": "USD", "new_currency": "EUR"}
        assert _finish(result) == "tool_calls"
        assert _content(result) is None

    def test_positive_print_wrapper(self) -> None:
        content = '```tool_code\nprint(get_weather(city="Paris"))\n```'
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result)[0]["function"]["name"] == "get_weather"
        assert _args(_calls(result)[0]) == {"city": "Paris"}

    def test_negative_plain_text(self) -> None:
        result = _resp(self.NAME, _wrap("Plain answer, no tool_code block."))
        assert _calls(result) == []
        assert _content(result) == "Plain answer, no tool_code block."

    def test_negative_other_code_fence(self) -> None:
        # A normal python fence is NOT a tool call.
        content = "```python\nprint('hello')\n```"
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content

    def test_malformed_positional_args_passthrough(self) -> None:
        # Positional args have no OpenAI mapping — refuse rather than guess.
        content = '```tool_code\nget_weather("Paris")\n```'
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content

    def test_malformed_non_call_passthrough(self) -> None:
        content = "```tool_code\nx = 1 + 2\n```"
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content


# ===========================================================================
# Llama-3 — <|python_tag|>{"name":...,"parameters":{...}}; {...}
# ===========================================================================


class TestLlama3Extractor:
    NAME = "llama3-toolcall-extract"

    def test_registered(self) -> None:
        assert len(REGISTRY.resolve(self.NAME)) == 1

    def test_positive_parameters_key(self) -> None:
        content = '<|python_tag|>{"name": "get_weather", "parameters": {"city": "Paris"}}'
        result = _resp(self.NAME, _wrap(content))
        calls = _calls(result)
        assert len(calls) == 1
        assert calls[0]["function"]["name"] == "get_weather"
        assert _args(calls[0]) == {"city": "Paris"}
        assert _finish(result) == "tool_calls"
        assert _content(result) is None

    def test_positive_arguments_key(self) -> None:
        # Llama can use either "arguments" or "parameters".
        content = '<|python_tag|>{"name": "f", "arguments": {"k": 1}}'
        result = _resp(self.NAME, _wrap(content))
        assert _args(_calls(result)[0]) == {"k": 1}

    def test_positive_multiple_semicolon_separated(self) -> None:
        content = (
            '<|python_tag|>{"name": "a", "parameters": {}}; '
            '{"name": "b", "parameters": {"x": 2}}'
        )
        result = _resp(self.NAME, _wrap(content))
        assert [c["function"]["name"] for c in _calls(result)] == ["a", "b"]

    def test_negative_plain_text(self) -> None:
        result = _resp(self.NAME, _wrap("A normal reply."))
        assert _calls(result) == []
        assert _content(result) == "A normal reply."

    def test_negative_bare_json_without_tag(self) -> None:
        # A bare JSON object without <|python_tag|> is too risky to claim.
        content = '{"name": "f", "parameters": {}}'
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content

    def test_malformed_bad_json_passthrough(self) -> None:
        content = '<|python_tag|>{"name": "f", "parameters":'  # truncated
        result = _resp(self.NAME, _wrap(content))
        assert _calls(result) == []
        assert _content(result) == content


# ===========================================================================
# Shared guards — non-alteration & shape safety
# ===========================================================================


class TestExtractorsNonAlteration:
    """None of the extractors auto-attach; none corrupt a clean response."""

    @pytest.mark.parametrize("name", ALL_EXTRACTORS)
    def test_clean_response_untouched(self, name: str) -> None:
        # A normal answer with none of the family markers must pass through.
        payload = _wrap("The capital of France is Paris.")
        result = _resp(name, payload)
        assert _calls(result) == []
        assert _content(result) == "The capital of France is Paris."
        assert _finish(result) == "stop"

    @pytest.mark.parametrize("name", ALL_EXTRACTORS)
    def test_existing_tool_calls_not_overridden(self, name: str) -> None:
        # If the engine already produced tool_calls, never touch them.
        payload = {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_existing",
                                "type": "function",
                                "function": {"name": "real", "arguments": "{}"},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
        result = _resp(name, payload)
        assert _calls(result)[0]["id"] == "call_existing"

    @pytest.mark.parametrize("name", ALL_EXTRACTORS)
    def test_request_phase_is_noop(self, name: str) -> None:
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        result = run_request_pipeline([name], copy.deepcopy(body))
        assert result == body

    @pytest.mark.parametrize("name", ALL_EXTRACTORS)
    def test_non_chat_payload_untouched(self, name: str) -> None:
        # No "choices" key (e.g. an embeddings response) → untouched.
        payload = {"data": [{"embedding": [0.1, 0.2]}]}
        result = _resp(name, payload)
        assert result == payload

    def test_extractors_not_in_default_pipeline(self) -> None:
        # Sanity: registered, but registration alone does not attach them.
        # Running with an EMPTY id list must never extract anything.
        content = '<tool_call>\n{"name": "f", "arguments": {}}\n</tool_call>'
        result = run_response_pipeline([], copy.deepcopy(_wrap(content)))
        assert _calls(result) == []
        assert _content(result) == content
