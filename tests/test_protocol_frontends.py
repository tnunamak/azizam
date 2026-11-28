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
        self.assertEqual(out["reasoning_effort"], "minimal")

    def test_thinking_enabled_maps_budget_to_reasoning_effort(self) -> None:
        def effort(thinking):
            return anthro.messages_request_to_canonical(
                {
                    "model": "m",
                    "max_tokens": 4096,
                    "thinking": thinking,
                    "messages": [{"role": "user", "content": "hello"}],
                }
            )["reasoning_effort"]

        self.assertEqual(effort({"type": "enabled", "budget_tokens": 1024}), "low")
        self.assertEqual(effort({"type": "enabled", "budget_tokens": 4000}), "medium")
        self.assertEqual(effort({"type": "enabled", "budget_tokens": 20000}), "high")
        self.assertEqual(effort({"type": "adaptive"}), "medium")
        self.assertEqual(effort({"type": "disabled"}), "minimal")

    def test_invalid_thinking_is_rejected(self) -> None:
        for thinking in (
            "yes",
            {"type": "enabled"},
            {"type": "enabled", "budget_tokens": 0},
            {"type": "sideways"},
        ):
            with self.assertRaises(anthro.AnthropicTranslationError, msg=thinking):
                anthro.messages_request_to_canonical(
                    {
                        "model": "m",
                        "max_tokens": 64,
                        "thinking": thinking,
                        "messages": [{"role": "user", "content": "hello"}],
                    }
                )

    def test_system_role_messages_join_the_system_prompt(self) -> None:
        # Claude Code sends a system-role entry inside ``messages``.
        out = anthro.messages_request_to_canonical(
            {
                "model": "m",
                "max_tokens": 64,
                "system": [{"type": "text", "text": "base"}],
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "q"}]},
                    {"role": "system", "content": [{"type": "text", "text": "env info"}]},
                ],
            }
        )
        self.assertEqual(
            out["messages"],
            [
                {"role": "system", "content": "base\n\nenv info"},
                {"role": "user", "content": "q"},
            ],
        )
        with self.assertRaises(anthro.AnthropicTranslationError):
            anthro.messages_request_to_canonical(
                {
                    "model": "m",
                    "max_tokens": 64,
                    "messages": [{"role": "system", "content": "only"}],
                }
            )

    def test_adaptive_thinking_uses_output_config_effort_and_display(self) -> None:
        body = {
            "model": "m",
            "max_tokens": 64,
            "thinking": {"type": "adaptive", "display": "omitted"},
            "output_config": {"effort": "high"},
            "messages": [{"role": "user", "content": "q"}],
        }
        self.assertEqual(anthro.messages_request_to_canonical(body)["reasoning_effort"], "high")
        self.assertFalse(anthro.thinking_requested(body))
        body["thinking"] = {"type": "adaptive"}
        self.assertTrue(anthro.thinking_requested(body))

    def test_adaptive_thinking_preserves_effort_and_maps_max(self) -> None:
        body = {
            "model": "m",
            "max_tokens": 64,
            "thinking": {"type": "adaptive"},
            "messages": [{"role": "user", "content": "q"}],
        }
        self.assertEqual(anthro.messages_request_to_canonical(body)["reasoning_effort"], "medium")
        for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
            with self.subTest(effort=effort):
                request = {**body, "output_config": {"effort": effort}}
                expected = "xhigh" if effort == "max" else effort
                self.assertEqual(anthro.messages_request_to_canonical(request)["reasoning_effort"], expected)
        for effort in ("unknown", None, 42, ["high"]):
            with self.subTest(effort=effort):
                request = {**body, "output_config": {"effort": effort}}
                with self.assertRaisesRegex(anthro.AnthropicTranslationError, r"output_config\.effort"):
                    anthro.messages_request_to_canonical(request)

    def test_prior_thinking_blocks_are_dropped_from_history(self) -> None:
        out = anthro.messages_request_to_canonical(
            {
                "model": "m",
                "max_tokens": 64,
                "messages": [
                    {"role": "user", "content": "q"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "hm", "signature": "sig"},
                            {"type": "redacted_thinking", "data": "xx"},
                            {"type": "text", "text": "answer"},
                        ],
                    },
                    {"role": "user", "content": "next"},
                ],
            }
        )
        self.assertEqual(out["messages"][1], {"role": "assistant", "content": "answer"})

    def test_tool_results_precede_user_text_in_the_same_turn(self) -> None:
        out = anthro.messages_request_to_canonical(
            {
                "model": "m",
                "max_tokens": 64,
                "messages": [
                    {"role": "user", "content": "q"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "t1", "name": "f", "input": {}}
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "also this"},
                            {"type": "tool_result", "tool_use_id": "t1", "content": "r"},
                        ],
                    },
                ],
            }
        )
        self.assertEqual(
            [m["role"] for m in out["messages"]],
            ["user", "assistant", "tool", "user"],
        )

    def _tool_message(self, result: dict) -> dict:
        out = anthro.messages_request_to_canonical({"model": "m", "max_tokens": 64, "messages": [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "shot", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", **result}]},
        ]})
        return out["messages"][2]

    def test_tool_result_image_becomes_the_chat_image_part(self) -> None:
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YQ=="}}
        url_image = {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}}
        msg = self._tool_message({"content": [{"type": "text", "text": "the page"}, image, url_image]})
        self.assertEqual(msg, {"role": "tool", "tool_call_id": "t1", "content": [
            {"type": "text", "text": "the page"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}},
            {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
        ]})
        failed = self._tool_message({"is_error": True, "content": [image]})
        self.assertEqual(failed["content"], [{"type": "text", "text": "Error"},
                                             {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}}])

    def test_tool_result_with_an_image_names_the_bad_block(self) -> None:
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YQ=="}}
        with self.assertRaises(anthro.AnthropicTranslationError) as caught:
            self._tool_message({"content": [{"type": "text", "text": "a"}, {"type": "audio"}, image]})
        self.assertIn("at messages[2].content[0].content[1]", str(caught.exception))
        self.assertNotIn("[1][0]", str(caught.exception))

    def test_tool_result_text_blocks_stay_one_string(self) -> None:
        msg = self._tool_message({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]})
        self.assertEqual(msg["content"], "a\nb")
        with self.assertRaises(anthro.AnthropicTranslationError):
            self._tool_message({"content": [{"type": "document", "source": {}}]})

    def _user_message(self, content: list) -> dict:
        out = anthro.messages_request_to_canonical({"model": "m", "max_tokens": 64, "messages": [
            {"role": "user", "content": content}]})
        return out["messages"][0]

    TEXT_DOC = {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "the notes"}}

    def test_a_text_document_in_a_user_message_becomes_its_text(self) -> None:
        msg = self._user_message([self.TEXT_DOC, {"type": "text", "text": "summarize"}])
        self.assertEqual(msg, {"role": "user", "content": "the notes\nsummarize"})

    def test_a_text_document_in_a_tool_result_becomes_its_text(self) -> None:
        msg = self._tool_message({"content": [self.TEXT_DOC]})
        self.assertEqual(msg, {"role": "tool", "tool_call_id": "t1", "content": "the notes"})

    def test_a_titled_document_puts_its_title_first(self) -> None:
        titled = {**self.TEXT_DOC, "title": "Meeting"}
        self.assertEqual(self._user_message([titled])["content"], "Title: Meeting\nthe notes")
        self.assertEqual(self._tool_message({"content": [titled]})["content"], "Title: Meeting\nthe notes")

    def test_a_content_document_keeps_its_text_and_images(self) -> None:
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YQ=="}}
        doc = {"type": "document", "source": {"type": "content", "content": [
            {"type": "text", "text": "page one"}, image, {"type": "text", "text": "page two"}]}}
        expected = [{"type": "text", "text": "page one"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}},
                    {"type": "text", "text": "page two"}]
        self.assertEqual(self._user_message([doc])["content"], expected)
        self.assertEqual(self._tool_message({"content": [doc]})["content"], expected)
        text_only = {"type": "document", "source": {"type": "content", "content": [{"type": "text", "text": "only"}]}}
        self.assertEqual(self._tool_message({"content": [text_only]})["content"], "only")

    def test_a_pdf_url_or_file_document_is_still_refused_with_its_path(self) -> None:
        for source in ({"type": "base64", "media_type": "application/pdf", "data": "JVBE"},
                       {"type": "url", "url": "https://example.test/a.pdf"}, {"type": "file", "file_id": "f1"}):
            with self.subTest(source=source["type"]):
                with self.assertRaises(anthro.AnthropicTranslationError) as caught:
                    self._user_message([{"type": "text", "text": "a"}, {"type": "document", "source": source}])
                self.assertIn("unsupported content block type 'document' at messages[0].content[1]", str(caught.exception))
                with self.assertRaises(anthro.AnthropicTranslationError) as caught:
                    self._tool_message({"content": [{"type": "document", "source": source}]})
                self.assertIn("'document' at messages[2].content[0].content[0]", str(caught.exception))

    SEARCH = {"type": "search_result", "source": "https://example.test/a", "title": "A page",
              "content": [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}],
              "citations": {"enabled": True}}

    def test_a_search_result_in_a_tool_result_keeps_its_place_among_the_parts(self) -> None:
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YQ=="}}
        msg = self._tool_message({"content": [{"type": "text", "text": "found"}, self.SEARCH, image]})
        self.assertEqual(msg["content"], [
            {"type": "text", "text": "found"},
            {"type": "text", "text": "A page\nhttps://example.test/a\nfirst\nsecond"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}},
        ])
        self.assertEqual(self._tool_message({"content": [self.SEARCH]})["content"],
                         "A page\nhttps://example.test/a\nfirst\nsecond")

    def test_a_search_result_in_a_user_message_becomes_its_text(self) -> None:
        msg = self._user_message([self.SEARCH])
        self.assertEqual(msg, {"role": "user", "content": "A page\nhttps://example.test/a\nfirst\nsecond"})

    def test_a_malformed_search_result_is_a_named_400(self) -> None:
        cases = {
            "source": {**self.SEARCH, "source": 3},
            "title": {**self.SEARCH, "title": None},
            "content": {**self.SEARCH, "content": []},
            "content[0]": {**self.SEARCH, "content": [{"type": "image"}]},
            "content[1].text": {**self.SEARCH, "content": [{"type": "text", "text": "a"}, {"type": "text", "text": 5}]},
        }
        for field, block in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(anthro.AnthropicTranslationError) as caught:
                    self._tool_message({"content": [{"type": "text", "text": "a"}, block]})
                self.assertIn(f"messages[2].content[0].content[1].{field}", str(caught.exception))
                with self.assertRaises(anthro.AnthropicTranslationError) as caught:
                    self._user_message([block])
                self.assertIn(f"messages[0].content[0].{field}", str(caught.exception))

    def test_a_malformed_document_title_or_empty_content_names_its_path(self) -> None:
        with self.assertRaises(anthro.AnthropicTranslationError) as caught:
            self._user_message([{**self.TEXT_DOC, "title": 5}])
        self.assertIn("messages[0].content[0].title must be a string", str(caught.exception))
        with self.assertRaises(anthro.AnthropicTranslationError) as caught:
            self._tool_message({"content": [{"type": "document", "source": {"type": "content", "content": []}}]})
        self.assertIn("messages[2].content[0].content[0].source.content must be a non-empty list", str(caught.exception))

    def test_an_assistant_document_image_names_its_nested_path(self) -> None:
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "YQ=="}}
        doc = {"type": "document", "source": {"type": "content", "content": [{"type": "text", "text": "a"}, image]}}
        with self.assertRaises(anthro.AnthropicTranslationError) as caught:
            anthro.messages_request_to_canonical({"model": "m", "max_tokens": 8, "messages": [
                {"role": "user", "content": "q"}, {"role": "assistant", "content": [doc]}]})
        self.assertIn("at messages[1].content[0].source.content[1]", str(caught.exception))

    def test_a_document_in_the_system_prompt_is_still_refused(self) -> None:
        with self.assertRaises(anthro.AnthropicTranslationError):
            anthro.messages_request_to_canonical({"model": "m", "max_tokens": 8, "system": [self.TEXT_DOC],
                                                  "messages": [{"role": "user", "content": "q"}]})

    def test_tool_choice_none_and_parallel_flag_and_top_k(self) -> None:
        out = anthro.messages_request_to_canonical(
            {
                "model": "m",
                "max_tokens": 64,
                "top_k": 40,
                "tools": [{"name": "f", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "none"},
                "messages": [{"role": "user", "content": "q"}],
            }
        )
        self.assertEqual(out["tool_choice"], "none")
        self.assertEqual(out["top_k"], 40)
        out = anthro.messages_request_to_canonical(
            {
                "model": "m",
                "max_tokens": 64,
                "tools": [{"name": "f", "input_schema": {"type": "object"}}],
                "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
                "messages": [{"role": "user", "content": "q"}],
            }
        )
        self.assertIs(out["parallel_tool_calls"], False)

    def test_server_tool_types_are_rejected_not_faked(self) -> None:
        with self.assertRaises(anthro.AnthropicTranslationError):
            anthro.messages_request_to_canonical(
                {
                    "model": "m",
                    "max_tokens": 64,
                    "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                    "messages": [{"role": "user", "content": "q"}],
                }
            )

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

    def test_a_block_type_that_is_not_a_string_is_a_named_refusal(self) -> None:
        for bad in ({"x": 1}, ["text"], 3):
            for body in (
                {"model": "m", "max_tokens": 8, "messages": [{"role": "user", "content": [{"type": bad, "text": "x"}]}]},
                {"model": "m", "max_tokens": 8, "system": [{"type": bad, "text": "x"}],
                 "messages": [{"role": "user", "content": "x"}]},
            ):
                with self.subTest(bad=bad, where="system" if "system" in body else "messages"):
                    with self.assertRaisesRegex(anthro.AnthropicTranslationError, r"\.type must be a string"):
                        anthro.messages_request_to_canonical(body)

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
        failed = json.loads(json.dumps(body))
        failed["messages"][2]["content"][0]["is_error"] = True
        self.assertNotEqual(
            anthro.messages_request_to_canonical(failed)["messages"][2]["content"],
            anthro.messages_request_to_canonical(body)["messages"][2]["content"],
            "a failed tool result must not read like a successful one",
        )
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

    def test_base64_image_block_becomes_openai_image_url(self) -> None:
        body = {
            "model": "claude-3-haiku",
            "max_tokens": 32,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
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
        out = anthro.messages_request_to_canonical(body)
        self.assertEqual(
            out["messages"][0]["content"],
            [
                {"type": "text", "text": "describe this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBORw0KG"},
                },
            ],
        )

    def test_url_image_block_becomes_openai_image_url(self) -> None:
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
                                "type": "url",
                                "url": "https://example.test/cat.png",
                            },
                        }
                    ],
                }
            ],
        }
        out = anthro.messages_request_to_canonical(body)
        self.assertEqual(
            out["messages"][0]["content"],
            [
                {
                    "type": "image_url",
                    "image_url": {"url": "https://example.test/cat.png"},
                }
            ],
        )

    def test_document_thinking_blocks_are_rejected(self) -> None:
        for btype in ("document",):
            with self.subTest(btype=btype):
                body = {
                    "model": "claude-3-haiku",
                    "max_tokens": 32,
                    "messages": [
                        {"role": "user", "content": [{"type": btype}]}
                    ],
                }
                with self.assertRaises(anthro.AnthropicTranslationError) as ctx:
                    anthro.messages_request_to_canonical(body)
                self.assertIn(btype, str(ctx.exception))
                self.assertEqual(
                    ctx.exception.error_body["error"]["type"],
                    "invalid_request_error",
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

    def _assert_usage_on_both_messages_surfaces(self, usage, expected) -> None:
        non_streaming = anthro.canonical_response_to_messages(
            {"model": "fixture", "usage": usage}
        )
        with self.subTest(surface="non-streaming"):
            self.assertEqual(non_streaming["usage"], expected)
        frames = anthro.canonical_stream_to_messages_sse(
            [
                {"model": "fixture", "choices": [{"delta": {"content": "fixture"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": usage},
            ]
        )
        final_usage = [
            payload["usage"]
            for event, payload in self._parse_sse_frames(frames)
            if event == "message_delta"
        ]
        with self.subTest(surface="streaming"):
            self.assertEqual(final_usage, [expected])

    def test_count_tokens_counts_tool_use_input(self) -> None:
        def body(command: str) -> dict:
            return {
                "model": "m",
                "messages": [
                    {"role": "user", "content": "run it"},
                    {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"command": command}}],
                    },
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}]},
                ],
            }

        short = anthro.estimate_count_tokens(body("ls"))["input_tokens"]
        long = anthro.estimate_count_tokens(body("x" * 4000))["input_tokens"]
        self.assertGreaterEqual(long - short, 999)

    def test_count_tokens_counts_an_image_at_its_billed_size(self) -> None:
        # Anthropic bills an image at width*height/750 tokens, about 1,600 at most; its base64 length says nothing.
        image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 400_000}}
        in_user = {"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": "look"}, image]}]}
        in_tool_result = {
            "model": "m",
            "messages": [
                {"role": "user", "content": "shoot"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "shot", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": [image]}]},
            ],
        }
        for name, body in (("user image", in_user), ("tool_result image", in_tool_result)):
            with self.subTest(name):
                tokens = anthro.estimate_count_tokens(body)["input_tokens"]
                self.assertGreaterEqual(tokens, 1600)
                self.assertLess(tokens, 2000)

    def test_count_tokens_of_text_blocks_matches_the_same_text_as_a_string(self) -> None:
        blocks = [{"type": "text", "text": "x" * 400}, {"type": "text", "text": "y" * 400}]
        as_blocks = {"model": "m", "messages": [{"role": "user", "content": blocks}]}
        as_string = {"model": "m", "messages": [{"role": "user", "content": "x" * 400 + "\n" + "y" * 400}]}
        self.assertEqual(anthro.estimate_count_tokens(as_blocks), anthro.estimate_count_tokens(as_string))

    def test_messages_usage_keeps_wire_key_order(self) -> None:
        usage = {"prompt_tokens": 11, "completion_tokens": 7}
        reply = anthro.canonical_response_to_messages({"model": "fixture", "usage": usage})
        self.assertEqual(list(reply["usage"]), ["input_tokens", "output_tokens"])
        frames = anthro.canonical_stream_to_messages_sse(
            [
                {"model": "fixture", "choices": [{"delta": {"content": "hi"}}]},
                {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": usage},
            ]
        )
        delta = next(p["usage"] for e, p in self._parse_sse_frames(frames) if e == "message_delta")
        self.assertEqual(list(delta), ["output_tokens", "input_tokens"])

    def test_messages_stream_start_emits_only_zero_usage_counts(self) -> None:
        frames = anthro.canonical_stream_to_messages_sse(
            [
                {"model": "fixture", "choices": [{"delta": {"content": "hi"}}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
            ]
        )
        events = self._parse_sse_frames(frames)
        self.assertEqual(events[0][0], "message_start")
        self.assertEqual(events[0][1]["message"]["usage"], {"input_tokens": 0, "output_tokens": 0})

    def test_messages_stream_keeps_usage_fields_across_chunks(self) -> None:
        frames = anthro.canonical_stream_to_messages_sse(
            [
                {"model": "fixture", "choices": [{"delta": {"content": "hi"}}]},
                {"choices": [], "usage": {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 8}}},
                {"choices": [], "usage": {"completion_tokens": 2}},
            ]
        )
        delta = next(p["usage"] for e, p in self._parse_sse_frames(frames) if e == "message_delta")
        self.assertEqual(
            delta,
            {"output_tokens": 2, "input_tokens": 2, "cache_read_input_tokens": 8, "cache_creation_input_tokens": 0},
        )

    def test_messages_usage_reports_cached_prompt_tokens(self) -> None:
        self._assert_usage_on_both_messages_surfaces(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 800},
            },
            {
                "input_tokens": 200,
                "output_tokens": 2,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 0,
            },
        )

    def test_messages_usage_clamps_input_count_when_cache_exceeds_prompt(self) -> None:
        self._assert_usage_on_both_messages_surfaces(
            {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cached_tokens": 20},
            },
            {
                "input_tokens": 0,
                "output_tokens": 2,
                "cache_read_input_tokens": 20,
                "cache_creation_input_tokens": 0,
            },
        )

    def test_messages_usage_separates_cache_creation_from_fresh_input(self) -> None:
        for creation, fresh, written in ((300, 100, 300), (500, 0, 500), (True, 400, 0), (-1, 400, 0)):
            with self.subTest(creation=creation):
                self._assert_usage_on_both_messages_surfaces(
                    {
                        "prompt_tokens": 1000,
                        "completion_tokens": 2,
                        "prompt_tokens_details": {
                            "cached_tokens": 600,
                            "cache_creation_tokens": creation,
                        },
                    },
                    {
                        "input_tokens": fresh,
                        "output_tokens": 2,
                        "cache_read_input_tokens": 600,
                        "cache_creation_input_tokens": written,
                    },
                )

    def test_messages_usage_counts_cache_creation_without_cache_reads(self) -> None:
        self._assert_usage_on_both_messages_surfaces(
            {
                "prompt_tokens": 1000,
                "completion_tokens": 2,
                "prompt_tokens_details": {"cache_creation_tokens": 300},
            },
            {"input_tokens": 700, "output_tokens": 2, "cache_creation_input_tokens": 300},
        )

    def test_messages_usage_without_valid_cache_count_keeps_legacy_shape(self) -> None:
        base = {"prompt_tokens": 1000, "completion_tokens": 2}
        for extra in (
            {},
            {"prompt_tokens_details": {}},
            {"prompt_tokens_details": {"cached_tokens": True}},
            {"prompt_tokens_details": {"cached_tokens": -1}},
        ):
            with self.subTest(extra=extra):
                self._assert_usage_on_both_messages_surfaces(
                    {**base, **extra}, {"input_tokens": 1000, "output_tokens": 2}
                )

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

    def test_reasoning_content_pure_think_tags_are_suppressed(self) -> None:
        canonical = {
            "id": "chatcmpl-1",
            "model": "claude-3-haiku",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "<think>private reasoning</think>",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        out = anthro.canonical_response_to_messages(canonical)
        self.assertEqual(out["content"], [])

    def test_content_think_tags_use_final_text(self) -> None:
        canonical = {
            "id": "chatcmpl-1",
            "model": "claude-3-haiku",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "<think>private reasoning</think>\nvisible reply",
                    },
                    "finish_reason": "stop",
                }
            ],
        }
        out = anthro.canonical_response_to_messages(canonical)
        self.assertEqual(out["content"], [{"type": "text", "text": "visible reply"}])

    def test_stream_reasoning_content_pure_think_tags_are_suppressed(self) -> None:
        chunks = [
            {
                "id": "chatcmpl-1",
                "model": "claude-3-haiku",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "reasoning_content": "<think>private reasoning</think>"
                        },
                    }
                ],
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
            ["message_start", "message_delta", "message_stop"],
        )

    def test_stream_content_split_think_tags_are_suppressed(self) -> None:
        chunks = [
            {
                "id": "chatcmpl-1",
                "model": "claude-3-haiku",
                "choices": [{"index": 0, "delta": {"content": "<thi"}}],
            },
            {
                "id": "chatcmpl-1",
                "model": "claude-3-haiku",
                "choices": [{"index": 0, "delta": {"content": "nk>priv"}}],
            },
            {
                "id": "chatcmpl-1",
                "model": "claude-3-haiku",
                "choices": [{"index": 0, "delta": {"content": "ate</think>\npong"}}],
            },
            {
                "id": "chatcmpl-1",
                "model": "claude-3-haiku",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]

        events = self._parse_sse_frames(anthro.canonical_stream_to_messages_sse(chunks))

        deltas = [
            payload["delta"]["text"]
            for event, payload in events
            if event == "content_block_delta"
        ]
        self.assertEqual(deltas, ["\npong"])

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

    def _tool_chunk(self, index: int, args: str, *, call_id: str | None = None, name: str | None = None) -> dict:
        function: dict = {"arguments": args}
        call: dict = {"index": index, "function": function}
        if call_id is not None:
            call["id"] = call_id
            call["type"] = "function"
            function["name"] = name
        return {
            "id": "chatcmpl-t",
            "model": "m",
            "choices": [{"index": 0, "delta": {"tool_calls": [call]}}],
        }

    def _tool_blocks_from_stream(self, events: list[tuple[str, dict]]) -> list[dict]:
        blocks: dict[int, dict] = {}
        starts: list[int] = []
        stops: list[int] = []
        for event, payload in events:
            if event == "content_block_start":
                starts.append(payload["index"])
                blocks[payload["index"]] = {**payload["content_block"], "json": ""}
            elif event == "content_block_delta" and payload["delta"]["type"] == "input_json_delta":
                self.assertIn(payload["index"], blocks)
                self.assertNotIn(payload["index"], stops, "a delta arrived after its block stopped")
                blocks[payload["index"]]["json"] += payload["delta"]["partial_json"]
            elif event == "content_block_stop":
                stops.append(payload["index"])
        self.assertEqual(sorted(starts), sorted(stops))
        self.assertEqual(len(starts), len(set(starts)), "each block starts once")
        self.assertEqual(starts, list(range(len(starts))), "block indexes are contiguous")
        return [blocks[i] for i in starts]

    def test_stream_interleaved_parallel_tool_arguments_stay_with_their_call(self) -> None:
        chunks = [
            self._tool_chunk(0, '{"n":', call_id="call_A", name="count"),
            self._tool_chunk(1, "{}", call_id="call_B", name="ping"),
            self._tool_chunk(0, "1}"),
            {"id": "chatcmpl-t", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = self._parse_sse_frames(anthro.canonical_stream_to_messages_sse(chunks))
        blocks = self._tool_blocks_from_stream(events)
        self.assertEqual([(b["id"], b["name"]) for b in blocks], [("call_A", "count"), ("call_B", "ping")])
        self.assertEqual(json.loads(blocks[0]["json"]), {"n": 1})
        self.assertEqual(json.loads(blocks[1]["json"]), {})
        stop = next(p for e, p in events if e == "message_delta")
        self.assertEqual(stop["delta"]["stop_reason"], "tool_use")

    def test_stream_sequential_parallel_tool_calls_keep_order_and_arguments(self) -> None:
        chunks = [
            self._tool_chunk(0, '{"city":', call_id="call_A", name="weather"),
            self._tool_chunk(0, '"SF"}'),
            self._tool_chunk(1, '{"city":"LA"}', call_id="call_B", name="weather"),
            {"id": "chatcmpl-t", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = self._parse_sse_frames(anthro.canonical_stream_to_messages_sse(chunks))
        blocks = self._tool_blocks_from_stream(events)
        self.assertEqual([b["id"] for b in blocks], ["call_A", "call_B"])
        self.assertEqual([json.loads(b["json"]) for b in blocks], [{"city": "SF"}, {"city": "LA"}])

    def test_stream_buffered_tool_call_takes_id_and_name_from_a_later_fragment(self) -> None:
        late = self._tool_chunk(1, '"x"}')
        late["choices"][0]["delta"]["tool_calls"][0].update({"id": "call_B", "type": "function"})
        late["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] = "echo"
        chunks = [
            self._tool_chunk(0, '{"n":', call_id="call_A", name="count"),
            self._tool_chunk(1, '{"s":'),
            late,
            self._tool_chunk(0, "1}"),
            {"id": "chatcmpl-t", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = self._parse_sse_frames(anthro.canonical_stream_to_messages_sse(chunks))
        blocks = self._tool_blocks_from_stream(events)
        self.assertEqual([(b["id"], b["name"]) for b in blocks], [("call_A", "count"), ("call_B", "echo")])
        self.assertEqual([json.loads(b["json"]) for b in blocks], [{"n": 1}, {"s": "x"}])

    def _content_between_tool_fragments(self, delta: dict, *, thinking: bool) -> list[tuple[str, dict]]:
        chunks = [
            self._tool_chunk(0, '{"n":', call_id="call_A", name="count"),
            self._tool_chunk(1, '{"s":', call_id="call_B", name="echo"),
            {"id": "chatcmpl-t", "model": "m", "choices": [{"index": 0, "delta": delta}]},
            self._tool_chunk(0, "1}"),
            self._tool_chunk(1, '"x"}'),
            {"id": "chatcmpl-t", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        ]
        events = self._parse_sse_frames(anthro.canonical_stream_to_messages_sse(chunks, thinking=thinking))
        tools = [p["content_block"]["id"] for e, p in events if e == "content_block_start" and p["content_block"]["type"] == "tool_use"]
        self.assertEqual(tools, ["call_A", "call_B"])
        blocks = {}
        for event, payload in events:
            if event == "content_block_start":
                blocks[payload["index"]] = {**payload["content_block"], "json": "", "said": ""}
            elif event == "content_block_delta":
                d = payload["delta"]
                blocks[payload["index"]]["json"] += d.get("partial_json", "")
                blocks[payload["index"]]["said"] += d.get("text", "") + d.get("thinking", "")
        starts = [p["index"] for e, p in events if e == "content_block_start"]
        stops = [p["index"] for e, p in events if e == "content_block_stop"]
        self.assertEqual(starts, list(range(len(starts))))
        self.assertEqual(stops, starts, "each block stops before the next starts")
        ordered = [blocks[i] for i in starts]
        self.assertEqual([json.loads(b["json"]) for b in ordered if b["type"] == "tool_use"], [{"n": 1}, {"s": "x"}])
        return [(b["type"], b["said"]) for b in ordered if b["type"] != "tool_use"]

    def test_stream_text_between_tool_fragments_keeps_every_argument(self) -> None:
        said = self._content_between_tool_fragments({"content": "checking"}, thinking=False)
        self.assertEqual(said, [("text", "checking")])

    def test_stream_thinking_between_tool_fragments_keeps_every_argument(self) -> None:
        said = self._content_between_tool_fragments({"reasoning_content": "hmm"}, thinking=True)
        self.assertEqual(said, [("thinking", "hmm")])

    def test_length_stop_after_a_tool_call_reports_max_tokens(self) -> None:
        canonical = {
            "model": "m",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}
                        ],
                    },
                    "finish_reason": "length",
                }
            ],
        }
        self.assertEqual(anthro.canonical_response_to_messages(canonical)["stop_reason"], "max_tokens")
        chunks = [
            self._tool_chunk(0, '{"a":', call_id="call_1", name="f"),
            {"id": "chatcmpl-t", "model": "m", "choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]},
        ]
        events = self._parse_sse_frames(anthro.canonical_stream_to_messages_sse(chunks))
        stop = next(p for e, p in events if e == "message_delta")
        self.assertEqual(stop["delta"]["stop_reason"], "max_tokens")

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
    def test_think_levels_match_the_messages_surface(self) -> None:
        # "max" means the top level on every surface: Messages maps it to xhigh, so Ollama does too.
        cases = {"max": "xhigh", "MAX": "xhigh", "high": "high", "low": "low", False: "minimal"}
        for think, expected in cases.items():
            with self.subTest(think=think):
                chat = oll.chat_request_to_canonical(
                    {"model": "m", "messages": [{"role": "user", "content": "hi"}], "think": think}
                )
                generate = oll.generate_request_to_canonical({"model": "m", "prompt": "hi", "think": think})
                self.assertEqual(chat.get("reasoning_effort"), expected)
                self.assertEqual(generate.get("reasoning_effort"), expected)
        self.assertTrue(oll.thinking_requested({"think": "max"}))

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
        # sampling options the backend understands are forwarded
        self.assertEqual(out["top_k"], 40)
        self.assertNotIn("__ollama_options__", out)

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
        self.assertEqual(out["done_reason"], "stop")
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

    def test_stream_translator_chat_text_then_final_with_late_usage(self) -> None:
        tr = oll.OllamaStreamTranslator(endpoint="chat", request_model="llama3")
        objs = []
        for chunk in (
            {"model": "llama3", "choices": [{"delta": {"role": "assistant"}}]},
            {"model": "llama3", "choices": [{"delta": {"content": "hel"}}]},
            {"model": "llama3", "choices": [{"delta": {}, "finish_reason": "stop"}]},
            {
                "model": "llama3",
                "choices": [{"delta": {}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
                "timings": {"prompt_ms": 2.0, "predicted_ms": 3.0},
            },
        ):
            objs.extend(tr.feed(chunk))
        objs.extend(tr.finish())
        self.assertEqual([o["done"] for o in objs], [False, True])
        self.assertEqual(objs[0]["message"], {"role": "assistant", "content": "hel"})
        final = objs[-1]
        self.assertEqual(final["done_reason"], "stop")
        self.assertEqual(final["prompt_eval_count"], 7)
        self.assertEqual(final["eval_count"], 2)
        self.assertEqual(final["prompt_eval_duration"], 2_000_000)
        self.assertEqual(final["eval_duration"], 3_000_000)
        self.assertIsInstance(final["total_duration"], int)

    def test_stream_translator_sends_tool_call_whole(self) -> None:
        tr = oll.OllamaStreamTranslator(endpoint="chat")
        objs = []
        parts = [
            {"index": 0, "id": "c1", "function": {"name": "get_weather", "arguments": "{"}},
            {"index": 0, "function": {"arguments": '"city":"'}},
            {"index": 0, "function": {"arguments": 'Paris"}'}},
        ]
        for part in parts:
            objs.extend(
                tr.feed({"model": "m", "choices": [{"delta": {"tool_calls": [part]}}]})
            )
        objs.extend(
            tr.feed({"model": "m", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]})
        )
        objs.extend(tr.finish())
        self.assertEqual(len(objs), 2)
        self.assertEqual(
            objs[0]["message"]["tool_calls"],
            [{"id": "c1", "function": {"name": "get_weather", "arguments": {"city": "Paris"}}}],
        )
        self.assertTrue(objs[1]["done"])

    def test_stream_translator_keeps_parallel_tool_calls_in_index_order(self) -> None:
        tr = oll.OllamaStreamTranslator(endpoint="chat")
        objs = []
        for i in [11, 0, 10, 2, 1, 9, 3, 8, 4, 7, 5, 6]:  # arrival order differs from index order
            part = {"index": i, "id": f"c{i}", "function": {"name": f"t{i}", "arguments": f'{{"n":{i}}}'}}
            objs.extend(tr.feed({"model": "m", "choices": [{"delta": {"tool_calls": [part]}}]}))
        objs.extend(tr.feed({"model": "m", "choices": [{"delta": {}, "finish_reason": "tool_calls"}]}))
        objs.extend(tr.finish())
        calls = objs[0]["message"]["tool_calls"]
        self.assertEqual([c["function"]["name"] for c in calls], [f"t{i}" for i in range(12)])
        self.assertEqual([c["function"]["arguments"] for c in calls], [{"n": i} for i in range(12)])

    def test_stream_translator_think_off_does_not_leak_reasoning(self) -> None:
        def run(finish_reason, thinking):
            tr = oll.OllamaStreamTranslator(endpoint="chat", thinking=thinking)
            objs = tr.feed(
                {"model": "m", "choices": [{"delta": {"reasoning_content": "hm"}}]}
            )
            objs += tr.feed(
                {"model": "m", "choices": [{"delta": {}, "finish_reason": finish_reason}]}
            )
            return objs + tr.finish()

        cut = run("length", False)
        self.assertEqual([o["message"]["content"] for o in cut], [""])
        self.assertNotIn("thinking", cut[0]["message"])
        shown = run("stop", True)
        self.assertEqual(shown[0]["message"]["thinking"], "hm")
        self.assertEqual(shown[0]["message"]["content"], "")


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

    def test_stream_translator_generate_text_and_final(self) -> None:
        tr = oll.OllamaStreamTranslator(endpoint="generate", request_model="llama3")
        objs = tr.feed({"model": "llama3", "choices": [{"delta": {"content": "ha"}}]})
        objs += tr.feed(
            {"model": "llama3", "choices": [{"delta": {}, "finish_reason": "length"}]}
        )
        objs += tr.feed(
            {
                "model": "llama3",
                "choices": [],
                "usage": {"prompt_tokens": 4, "completion_tokens": 8},
            }
        )
        objs += tr.finish()
        self.assertEqual(objs[0]["response"], "ha")
        self.assertFalse(objs[0]["done"])
        self.assertEqual(objs[1]["response"], "")
        self.assertTrue(objs[1]["done"])
        self.assertEqual(objs[1]["done_reason"], "length")
        self.assertEqual(objs[1]["prompt_eval_count"], 4)
        self.assertEqual(objs[1]["eval_count"], 8)


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
        # A summary names no file format, so none is claimed; a summary that names one keeps it.
        self.assertEqual(first["details"]["format"], "")
        self.assertEqual(oll.tags_response_from_model_summaries([{"id": "m", "format": "safetensors"}])["models"][0]["details"]["format"], "safetensors")

    def test_show_response_from_summary(self) -> None:
        out = oll.show_response_from_model_summary(
            {
                "id": "llama3:8b",
                "family": "llama",
                "parameter_size": "8B",
                "quantization_level": "Q4_K_M",
                "template": "{{ .Prompt }}",
                "capabilities": ["completion", "tools"],
                "context_window": 102400,
            }
        )
        self.assertEqual(out["modelfile"], "FROM llama3:8b")
        self.assertEqual(out["template"], "{{ .Prompt }}")
        self.assertEqual(out["details"]["family"], "llama")
        self.assertEqual(out["capabilities"], ["completion", "tools"])
        self.assertEqual(out["model_info"]["general.architecture"], "llama")
        self.assertEqual(out["model_info"]["llama.context_length"], 102400)
        self.assertIn("num_ctx 102400", out["parameters"])

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
