"""Tests for the pure protocol-frontend translators.

These tests cover request translation, response translation, unsupported
feature errors, and that helper metadata never carries raw prompt content.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from gateway.protocols import anthropic as anthro  # noqa: E402
from gateway.protocols import ollama as oll  # noqa: E402


# ---------------------------------------------------------------------------
# Anthropic — requests
# ---------------------------------------------------------------------------


class AnthropicRequestTranslationTests(unittest.TestCase):
    def test_minimal_text_message_round_trips_to_canonical(self) -> None:
        body = {
            "model": "claude-3-haiku",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hello"}],
        }
        out = anthro.messages_request_to_canonical(body)
        self.assertEqual(out["model"], "claude-3-haiku")
        self.assertEqual(out["max_tokens"], 64)
        self.assertEqual(out["messages"], [{"role": "user", "content": "hello"}])
        self.assertFalse(out["stream"])

    def test_system_prompt_string_is_prepended(self) -> None:
        body = {
            "model": "claude-3-haiku",
            "max_tokens": 32,
            "system": "be terse",
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = anthro.messages_request_to_canonical(body)
        self.assertEqual(out["messages"][0], {"role": "system", "content": "be terse"})
        self.assertEqual(out["messages"][1]["role"], "user")

    def test_system_prompt_text_blocks_are_concatenated(self) -> None:
        body = {
            "model": "claude-3-haiku",
            "max_tokens": 32,
            "system": [
                {"type": "text", "text": "rule one"},
                {"type": "text", "text": "rule two"},
            ],
            "messages": [{"role": "user", "content": "hi"}],
        }
        out = anthro.messages_request_to_canonical(body)
        self.assertEqual(
            out["messages"][0],
            {"role": "system", "content": "rule one\nrule two"},
        )

    def test_text_content_blocks_are_concatenated(self) -> None:
        body = {
            "model": "claude-3-haiku",
            "max_tokens": 32,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "part 1"},
                        {"type": "text", "text": "part 2"},
                    ],
                }
            ],
        }
        out = anthro.messages_request_to_canonical(body)
        self.assertEqual(out["messages"][0]["content"], "part 1\npart 2")

    def test_tools_and_tool_choice_translate(self) -> None:
        body = {
            "model": "claude-3-haiku",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Look up weather",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
            "tool_choice": {"type": "tool", "name": "get_weather"},
        }
        out = anthro.messages_request_to_canonical(body)
        self.assertEqual(out["tools"][0]["type"], "function")
        self.assertEqual(out["tools"][0]["function"]["name"], "get_weather")
        self.assertEqual(
            out["tool_choice"],
            {"type": "function", "function": {"name": "get_weather"}},
        )

    def test_assistant_tool_use_becomes_openai_tool_call(self) -> None:
        body = {
            "model": "claude-3-haiku",
            "max_tokens": 32,
            "messages": [
                {"role": "user", "content": "weather?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Looking up."},
                        {
                            "type": "tool_use",
                            "id": "toolu_abc",
                            "name": "get_weather",
                            "input": {"city": "SF"},
                        },
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_abc",
                            "content": "72F",
                        }
                    ],
                },
            ],
        }
        out = anthro.messages_request_to_canonical(body)
        # user, assistant w/ tool_call, tool result
        roles = [m["role"] for m in out["messages"]]
        self.assertEqual(roles, ["user", "assistant", "tool"])
        assistant = out["messages"][1]
        self.assertEqual(assistant["content"], "Looking up.")
        self.assertEqual(assistant["tool_calls"][0]["id"], "toolu_abc")
        self.assertEqual(
            assistant["tool_calls"][0]["function"]["name"], "get_weather"
        )
        self.assertEqual(
            json.loads(assistant["tool_calls"][0]["function"]["arguments"]),
            {"city": "SF"},
        )
        self.assertEqual(out["messages"][2]["tool_call_id"], "toolu_abc")
        self.assertEqual(out["messages"][2]["content"], "72F")

    def test_image_block_is_rejected(self) -> None:
        body = {
            "model": "claude-3-haiku",
            "max_tokens": 32,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "iVBORw0KG",
                            },
                        }
                    ],
                }
            ],
        }
        with self.assertRaises(anthro.AnthropicTranslationError) as ctx:
            anthro.messages_request_to_canonical(body)
        self.assertIn("image", str(ctx.exception))
        self.assertEqual(
            ctx.exception.error_body["error"]["type"], "invalid_request_error"
        )

    def test_missing_max_tokens_is_rejected(self) -> None:
        body = {
            "model": "claude-3-haiku",
            "messages": [{"role": "user", "content": "hi"}],
        }
        with self.assertRaises(anthro.AnthropicTranslationError):
            anthro.messages_request_to_canonical(body)

    def test_metadata_user_id_maps_to_canonical_user(self) -> None:
        body = {
            "model": "claude-3-haiku",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hi"}],
            "metadata": {"user_id": "user_42"},
        }
        out = anthro.messages_request_to_canonical(body)
        self.assertEqual(out["user"], "user_42")


# ---------------------------------------------------------------------------
# Anthropic — responses + errors
# ---------------------------------------------------------------------------


class AnthropicResponseTranslationTests(unittest.TestCase):
    def _parse_sse_frames(self, frames: list[str]) -> list[tuple[str, dict]]:
        parsed: list[tuple[str, dict]] = []
        for frame in frames:
            event = None
            data = None
            for line in frame.strip().splitlines():
                if line.startswith("event: "):
                    event = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    data = json.loads(line.removeprefix("data: "))
            self.assertIsNotNone(event)
            self.assertIsNotNone(data)
            parsed.append((event, data))
        return parsed

    def test_canonical_text_response_to_messages(self) -> None:
        canonical = {
            "id": "chatcmpl-1",
            "model": "claude-3-haiku",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello back"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        }
        out = anthro.canonical_response_to_messages(canonical)
        self.assertEqual(out["type"], "message")
        self.assertEqual(out["role"], "assistant")
        self.assertEqual(out["model"], "claude-3-haiku")
        self.assertEqual(out["content"], [{"type": "text", "text": "hello back"}])
        self.assertEqual(out["stop_reason"], "end_turn")
        self.assertEqual(out["usage"], {"input_tokens": 4, "output_tokens": 2})
        self.assertTrue(out["id"].startswith("msg_"))

    def test_canonical_stream_to_messages_sse_text_events(self) -> None:
        chunks = [
            {
                "id": "chatcmpl-1",
                "model": "claude-3-haiku",
                "choices": [{"index": 0, "delta": {"role": "assistant"}}],
            },
            {
                "id": "chatcmpl-1",
                "model": "claude-3-haiku",
                "choices": [{"index": 0, "delta": {"content": "hel"}}],
            },
            {
                "id": "chatcmpl-1",
                "model": "claude-3-haiku",
                "choices": [{"index": 0, "delta": {"content": "lo"}}],
            },
            {
                "id": "chatcmpl-1",
                "model": "claude-3-haiku",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]

        events = self._parse_sse_frames(anthro.canonical_stream_to_messages_sse(chunks))

        self.assertEqual(
            [event for event, _ in events],
            [
                "message_start",
                "content_block_start",
                "content_block_delta",
                "content_block_delta",
                "content_block_stop",
                "message_delta",
                "message_stop",
            ],
        )
        self.assertEqual(events[0][1]["message"]["id"], "msg_chatcmpl-1")
        self.assertEqual(events[0][1]["message"]["model"], "claude-3-haiku")
        self.assertEqual(events[2][1]["delta"], {"type": "text_delta", "text": "hel"})
        self.assertEqual(events[3][1]["delta"], {"type": "text_delta", "text": "lo"})
        self.assertEqual(events[5][1]["delta"]["stop_reason"], "end_turn")

    def test_canonical_stream_to_messages_sse_accepts_raw_openai_sse(self) -> None:
        raw_events = [
            "",
            'data: {"id":"chatcmpl-2","model":"claude-3-haiku","choices":[{"index":0,"delta":{"content":"hi"}}]}',
            "data: [DONE]",
            'data: {"id":"chatcmpl-2","model":"claude-3-haiku","choices":[{"index":0,"delta":{},"finish_reason":"length"}]}',
        ]

        events = self._parse_sse_frames(
            anthro.canonical_stream_to_messages_sse(raw_events)
        )

        self.assertEqual(events[0][0], "message_start")
        self.assertEqual(events[2][1]["delta"]["text"], "hi")
        self.assertEqual(events[-2][1]["delta"]["stop_reason"], "max_tokens")

    def test_reasoning_content_fallback_produces_text_block(self) -> None:
        canonical = {
            "id": "chatcmpl-1",
            "model": "claude-3-haiku",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "visible reply",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        out = anthro.canonical_response_to_messages(canonical)
        self.assertEqual(out["content"], [{"type": "text", "text": "visible reply"}])

    def test_reasoning_content_think_tags_use_final_text(self) -> None:
        canonical = {
            "id": "chatcmpl-1",
            "model": "claude-3-haiku",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "<think>private reasoning</think>\nvisible reply",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        out = anthro.canonical_response_to_messages(canonical)
        self.assertEqual(out["content"], [{"type": "text", "text": "visible reply"}])

    def test_tool_calls_produce_tool_use_blocks(self) -> None:
        canonical = {
            "id": "chatcmpl-2",
            "model": "claude-3-haiku",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Let me check.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": json.dumps({"city": "SF"}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }
        out = anthro.canonical_response_to_messages(canonical)
        self.assertEqual(out["stop_reason"], "tool_use")
        types = [b["type"] for b in out["content"]]
        self.assertIn("text", types)
        self.assertIn("tool_use", types)
        tu = next(b for b in out["content"] if b["type"] == "tool_use")
        self.assertEqual(tu["id"], "call_1")
        self.assertEqual(tu["name"], "get_weather")
        self.assertEqual(tu["input"], {"city": "SF"})

    def test_length_finish_maps_to_max_tokens(self) -> None:
        canonical = {
            "model": "claude-3-haiku",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "...."},
                    "finish_reason": "length",
                }
            ],
        }
        out = anthro.canonical_response_to_messages(canonical)
        self.assertEqual(out["stop_reason"], "max_tokens")

    def test_unsupported_feature_error_shape(self) -> None:
        err = anthro.unsupported_feature_error("streaming", detail="not wired yet")
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["error"]["type"], "invalid_request_error")
        self.assertIn("streaming", err["error"]["message"])
        self.assertIn("not wired yet", err["error"]["message"])

    def test_safe_request_summary_omits_prompt_content(self) -> None:
        body = {
            "model": "claude-3-haiku",
            "max_tokens": 64,
            "system": "secret system prompt content",
            "messages": [
                {"role": "user", "content": "sensitive user content"},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "also sensitive"}],
                },
            ],
            "tools": [
                {"name": "f", "input_schema": {"type": "object", "properties": {}}}
            ],
        }
        summary = anthro.safe_request_summary(body)
        as_text = json.dumps(summary)
        self.assertNotIn("sensitive", as_text)
        self.assertNotIn("secret system prompt", as_text)
        self.assertEqual(summary["message_count"], 2)
        self.assertEqual(summary["tool_count"], 1)
        self.assertTrue(summary["has_system"])


# ---------------------------------------------------------------------------
# Ollama — /api/chat
# ---------------------------------------------------------------------------


class OllamaChatTranslationTests(unittest.TestCase):
    def test_chat_request_translates(self) -> None:
        body = {
            "model": "llama3",
            "messages": [
                {"role": "system", "content": "be terse"},
                {"role": "user", "content": "hi"},
            ],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 128, "top_k": 40},
        }
        out = oll.chat_request_to_canonical(body)
        self.assertEqual(out["model"], "llama3")
        self.assertEqual(len(out["messages"]), 2)
        self.assertFalse(out["stream"])
        self.assertAlmostEqual(out["temperature"], 0.2)
        self.assertEqual(out["max_tokens"], 128)
        # native-only options ride along in a namespaced bucket
        self.assertEqual(out["__ollama_options__"], {"top_k": 40})

    def test_chat_format_json_maps_to_response_format(self) -> None:
        body = {
            "model": "llama3",
            "messages": [{"role": "user", "content": "hi"}],
            "format": "json",
        }
        out = oll.chat_request_to_canonical(body)
        self.assertEqual(out["response_format"], {"type": "json_object"})

    def test_chat_images_translate_to_openai_multimodal_content(self) -> None:
        body = {
            "model": "llava",
            "messages": [
                {
                    "role": "user",
                    "content": "what is this",
                    "images": ["iVBORw0KGgo="],
                }
            ],
        }
        out = oll.chat_request_to_canonical(body)
        self.assertEqual(
            out["messages"][0]["content"],
            [
                {"type": "text", "text": "what is this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                },
            ],
        )

    def test_chat_image_validation_fails_clearly(self) -> None:
        body = {
            "model": "llava",
            "messages": [{"role": "user", "content": "x", "images": "b64..."}],
        }
        with self.assertRaises(oll.OllamaTranslationError) as ctx:
            oll.chat_request_to_canonical(body)
        self.assertIn("images must be a list of base64 strings", str(ctx.exception))

    def test_chat_tools_translate(self) -> None:
        body = {
            "model": "llama3",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Look up weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            ],
        }
        out = oll.chat_request_to_canonical(body)
        self.assertEqual(out["tools"][0]["function"]["name"], "get_weather")

    def test_canonical_response_to_chat(self) -> None:
        canonical = {
            "model": "llama3",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "hello",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "get_weather",
                                    "arguments": json.dumps({"city": "SF"}),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3},
        }
        out = oll.canonical_response_to_chat(canonical)
        self.assertEqual(out["model"], "llama3")
        self.assertTrue(out["done"])
        self.assertEqual(out["done_reason"], "tool_calls")
        self.assertEqual(out["message"]["content"], "hello")
        self.assertEqual(
            out["message"]["tool_calls"][0]["function"]["name"], "get_weather"
        )
        self.assertEqual(
            out["message"]["tool_calls"][0]["function"]["arguments"],
            {"city": "SF"},
        )
        self.assertEqual(out["prompt_eval_count"], 10)
        self.assertEqual(out["eval_count"], 3)

    def test_canonical_stream_chunk_to_chat_delta(self) -> None:
        chunk = {
            "model": "llama3",
            "choices": [
                {"delta": {"role": "assistant", "content": "hel"}, "index": 0}
            ],
        }
        out = oll.canonical_stream_chunk_to_chat(chunk)
        self.assertEqual(out["model"], "llama3")
        self.assertFalse(out["done"])
        self.assertEqual(out["message"], {"role": "assistant", "content": "hel"})
        self.assertNotIn("done_reason", out)

    def test_canonical_stream_chunk_to_chat_done_with_usage(self) -> None:
        chunk = {
            "model": "llama3",
            "choices": [{"delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        }
        out = oll.canonical_stream_chunk_to_chat(chunk)
        self.assertTrue(out["done"])
        self.assertEqual(out["done_reason"], "stop")
        self.assertEqual(out["prompt_eval_count"], 7)
        self.assertEqual(out["eval_count"], 2)


# ---------------------------------------------------------------------------
# Ollama — /api/generate
# ---------------------------------------------------------------------------


class OllamaGenerateTranslationTests(unittest.TestCase):
    def test_generate_request_translates_prompt_and_system(self) -> None:
        body = {
            "model": "llama3",
            "prompt": "tell me a joke",
            "system": "be funny",
            "options": {"temperature": 0.9},
        }
        out = oll.generate_request_to_canonical(body)
        self.assertEqual(out["model"], "llama3")
        self.assertEqual(out["messages"][0], {"role": "system", "content": "be funny"})
        self.assertEqual(
            out["messages"][1], {"role": "user", "content": "tell me a joke"}
        )
        self.assertAlmostEqual(out["temperature"], 0.9)

    def test_generate_raw_flag_carried_via_namespace(self) -> None:
        out = oll.generate_request_to_canonical(
            {"model": "llama3", "prompt": "x", "raw": True}
        )
        self.assertTrue(out["__ollama_raw__"])

    def test_generate_images_translate_to_openai_multimodal_content(self) -> None:
        out = oll.generate_request_to_canonical(
            {
                "model": "llava",
                "prompt": "describe",
                "images": ["data:image/webp;base64,abc123"],
            }
        )
        self.assertEqual(
            out["messages"][0]["content"],
            [
                {"type": "text", "text": "describe"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/webp;base64,abc123"},
                },
            ],
        )

    def test_canonical_response_to_generate(self) -> None:
        canonical = {
            "model": "llama3",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "ha"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1},
        }
        out = oll.canonical_response_to_generate(canonical)
        self.assertEqual(out["response"], "ha")
        self.assertTrue(out["done"])
        self.assertEqual(out["done_reason"], "stop")
        self.assertEqual(out["prompt_eval_count"], 4)
        self.assertEqual(out["eval_count"], 1)

    def test_canonical_stream_chunk_to_generate_delta(self) -> None:
        chunk = {
            "model": "llama3",
            "choices": [{"delta": {"content": "ha"}, "index": 0}],
        }
        out = oll.canonical_stream_chunk_to_generate(chunk)
        self.assertEqual(out["response"], "ha")
        self.assertFalse(out["done"])
        self.assertNotIn("done_reason", out)

    def test_canonical_stream_chunk_to_generate_done_with_usage(self) -> None:
        chunk = {
            "model": "llama3",
            "choices": [{"delta": {}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 8},
        }
        out = oll.canonical_stream_chunk_to_generate(chunk)
        self.assertEqual(out["response"], "")
        self.assertTrue(out["done"])
        self.assertEqual(out["done_reason"], "length")
        self.assertEqual(out["prompt_eval_count"], 4)
        self.assertEqual(out["eval_count"], 8)


# ---------------------------------------------------------------------------
# Ollama — /api/embed and /api/embeddings
# ---------------------------------------------------------------------------


class OllamaEmbeddingTranslationTests(unittest.TestCase):
    def test_embed_request_translates_string_input(self) -> None:
        out = oll.embed_request_to_canonical(
            {"model": "nomic-embed-text-v1.5", "input": "search_query: hello"}
        )
        self.assertEqual(out["model"], "nomic-embed-text-v1.5")
        self.assertEqual(out["input"], "search_query: hello")

    def test_embed_request_translates_batch_input(self) -> None:
        out = oll.embed_request_to_canonical(
            {"model": "nomic-embed-text-v1.5", "input": ["a", "b"]}
        )
        self.assertEqual(out["input"], ["a", "b"])

    def test_embed_request_rejects_non_text_input(self) -> None:
        with self.assertRaises(oll.OllamaTranslationError):
            oll.embed_request_to_canonical(
                {"model": "nomic-embed-text-v1.5", "input": [1, 2]}
            )

    def test_legacy_embeddings_request_translates_prompt(self) -> None:
        out = oll.embeddings_request_to_canonical(
            {"model": "nomic-embed-text-v1.5", "prompt": "hello"}
        )
        self.assertEqual(out, {"model": "nomic-embed-text-v1.5", "input": "hello"})

    def test_canonical_response_to_embed(self) -> None:
        canonical = {
            "model": "nomic-embed-text-v1.5",
            "data": [
                {"object": "embedding", "index": 1, "embedding": [0.3, 0.4]},
                {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
            ],
            "usage": {"prompt_tokens": 7},
        }
        out = oll.canonical_response_to_embed(canonical)
        self.assertEqual(out["model"], "nomic-embed-text-v1.5")
        self.assertEqual(out["embeddings"], [[0.1, 0.2], [0.3, 0.4]])
        self.assertEqual(out["prompt_eval_count"], 7)

    def test_canonical_response_to_legacy_embeddings(self) -> None:
        out = oll.canonical_response_to_embeddings(
            {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}
        )
        self.assertEqual(out, {"embedding": [0.1, 0.2]})


# ---------------------------------------------------------------------------
# Ollama — model-management and error helpers
# ---------------------------------------------------------------------------


class OllamaTagsAndErrorTests(unittest.TestCase):
    def test_tags_response_from_summaries(self) -> None:
        summaries = [
            {
                "id": "llama3:8b",
                "family": "llama",
                "parameter_size": "8B",
                "quantization_level": "Q4_K_M",
                "size": 4_000_000_000,
                "digest": "sha256:abc",
                "modified_at": "2026-01-01T00:00:00Z",
            },
            {"id": "qwen3:7b"},
            "not a mapping",
            {"id": ""},  # rejected
        ]
        out = oll.tags_response_from_model_summaries(summaries)
        names = [m["name"] for m in out["models"]]
        self.assertEqual(names, ["llama3:8b", "qwen3:7b"])
        first = out["models"][0]
        self.assertEqual(first["details"]["family"], "llama")
        self.assertEqual(first["details"]["families"], ["llama"])
        self.assertEqual(first["size"], 4_000_000_000)

    def test_show_response_from_summary(self) -> None:
        out = oll.show_response_from_model_summary(
            {
                "id": "llama3:8b",
                "family": "llama",
                "parameter_size": "8B",
                "quantization_level": "Q4_K_M",
                "template": "{{ .Prompt }}",
                "capabilities": ["completion", "tools"],
            }
        )
        self.assertEqual(out["modelfile"], "FROM llama3:8b")
        self.assertEqual(out["template"], "{{ .Prompt }}")
        self.assertEqual(out["details"]["family"], "llama")
        self.assertEqual(out["capabilities"], ["completion", "tools"])
        self.assertEqual(out["model_info"]["general.architecture"], "llama")

    def test_ps_response_from_summaries(self) -> None:
        out = oll.ps_response_from_model_summaries(
            [
                {
                    "id": "llama3:8b",
                    "size": 4_000_000_000,
                    "size_vram": 2_000_000_000,
                    "digest": "sha256:abc",
                    "expires_at": "2026-01-01T01:00:00Z",
                },
                {"name": ""},
            ]
        )
        self.assertEqual(len(out["models"]), 1)
        model = out["models"][0]
        self.assertEqual(model["name"], "llama3:8b")
        self.assertEqual(model["size_vram"], 2_000_000_000)
        self.assertEqual(model["expires_at"], "2026-01-01T01:00:00Z")

    def test_version_response(self) -> None:
        self.assertEqual(oll.version_response("0.9.0"), {"version": "0.9.0"})

    def test_unsupported_operation_error_shape(self) -> None:
        err = oll.unsupported_operation_error("/api/pull", detail="no daemon")
        self.assertIn("error", err)
        self.assertIn("/api/pull", err["error"])
        self.assertIn("no daemon", err["error"])

    def test_safe_request_summary_chat_omits_messages(self) -> None:
        body = {
            "model": "llama3",
            "messages": [
                {"role": "user", "content": "very secret prompt here"},
            ],
            "tools": [{"type": "function", "function": {"name": "f"}}],
        }
        s = oll.safe_request_summary(body, endpoint="chat")
        self.assertNotIn("secret", json.dumps(s))
        self.assertEqual(s["model"], "llama3")
        self.assertEqual(s["message_count"], 1)
        self.assertTrue(s["has_tools"])

    def test_safe_request_summary_generate_omits_prompt(self) -> None:
        body = {
            "model": "llama3",
            "prompt": "secret prompt",
            "system": "secret system",
        }
        s = oll.safe_request_summary(body, endpoint="generate")
        as_text = json.dumps(s)
        self.assertNotIn("secret prompt", as_text)
        self.assertNotIn("secret system", as_text)
        self.assertTrue(s["has_prompt"])
        self.assertTrue(s["has_system"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
