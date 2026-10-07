from __future__ import annotations

import json
import unittest

import responses_translator as rt


def _event_names(frames: list[bytes]) -> list[str]:
    names: list[str] = []
    for frame in frames:
        for line in frame.decode("utf-8").splitlines():
            if line.startswith("event: "):
                names.append(line.removeprefix("event: "))
    return names


def _event_payloads(frames: list[bytes]) -> list[dict]:
    payloads: list[dict] = []
    for frame in frames:
        for line in frame.decode("utf-8").splitlines():
            if line.startswith("data: "):
                payloads.append(json.loads(line.removeprefix("data: ")))
    return payloads


class ResponsesTranslatorTests(unittest.TestCase):
    def translator(self):
        return rt.StreamTranslator(response_id="resp_test", created_at=123, original_request={"input": "hi"})

    def wire(self, delta, finish=None, newline="\n"):
        choice = {"delta": delta, "finish_reason": finish}
        return ("data: " + json.dumps({"choices": [choice]}, ensure_ascii=False) + newline * 2).encode()

    def test_upstream_error_fails_instead_of_completing(self):
        tr = self.translator()
        frames = tr.start() + tr.feed(self.wire({"content": "partial"}))
        frames += tr.feed(b'data: {"error":{"message":"backend failed","code":"backend_error"}}\n\n')
        frames += tr.feed(self.wire({"content": "ignored"}, "stop")) + tr.finish()
        payloads = _event_payloads(frames)
        self.assertEqual(payloads[-1]["type"], "response.failed")
        self.assertEqual(payloads[-1]["response"]["status"], "failed")
        self.assertEqual(payloads[-1]["response"]["error"]["message"], "backend failed")
        self.assertEqual(payloads[-1]["response"]["output_text"], "partial")
        self.assertNotIn("response.completed", _event_names(frames))

    def test_eof_and_done_without_finish_reason_fail(self):
        for trailer in (b"", b"data: [DONE]\n\n"):
            with self.subTest(trailer=trailer):
                tr = self.translator()
                frames = tr.start() + tr.feed(self.wire({"content": "partial"}) + trailer) + tr.finish()
                final = _event_payloads(frames)[-1]
                self.assertEqual(final["type"], "response.failed")
                self.assertIn("finish_reason", final["response"]["error"]["message"])

    def test_utf8_text_and_tool_arguments_survive_every_byte_split(self):
        text = "سلام دنیا 🌍🙂"
        args = json.dumps({"text": text}, ensure_ascii=False)
        payload = self.wire({"content": text, "tool_calls": [{"index": 0, "id": "call_test",
                             "function": {"name": "echo", "arguments": args}}]}) + self.wire({}, "tool_calls")
        for boundary in range(1, len(payload)):
            with self.subTest(boundary=boundary):
                tr = self.translator()
                frames = tr.start() + tr.feed(payload[:boundary]) + tr.feed(payload[boundary:]) + tr.finish()
                final = _event_payloads(frames)[-1]["response"]
                self.assertEqual(final["output_text"], text)
                self.assertEqual(final["output"][1]["arguments"], args)
        tr = self.translator()
        for byte in payload:
            tr.feed(bytes([byte]))
        tr.finish()
        self.assertEqual(tr.build_final_response()["output_text"], text)

    def test_sse_newlines_survive_every_byte_split(self):
        for newline in ("\n", "\r", "\r\n"):
            payload = self.wire({"content": "hello"}, newline=newline) + self.wire({}, "stop", newline)
            for boundary in range(1, len(payload)):
                with self.subTest(newline=repr(newline), boundary=boundary):
                    tr = self.translator()
                    frames = tr.start() + tr.feed(payload[:boundary]) + tr.feed(payload[boundary:]) + tr.finish()
                    final = _event_payloads(frames)[-1]["response"]
                    self.assertEqual(final["status"], "completed")
                    self.assertEqual(final["output_text"], "hello")

    def test_allowed_tools_is_rejected_without_widening_access(self):
        for mode in ("auto", "required"):
            with self.subTest(mode=mode), self.assertRaises(rt.ResponsesTranslationError) as raised:
                rt.to_chat_completions({"input": "hi", "tools": [
                    {"type": "function", "name": "allowed"}, {"type": "function", "name": "excluded"}],
                    "tool_choice": {"type": "allowed_tools", "mode": mode,
                                    "tools": [{"type": "function", "name": "allowed"}]}})
            self.assertEqual(raised.exception.param, "tool_choice")
            self.assertEqual(raised.exception.code, "unsupported_operation")

    def test_unsupported_attachments_are_rejected_at_the_content_parameter(self):
        for part in ({"type": "input_file", "file_id": "file_test"},
                     {"type": "input_file", "file_data": "data:application/pdf;base64,AA==", "filename": "test.pdf"},
                     {"type": "input_file", "file_url": "https://example.test/test.pdf"},
                     {"type": "input_image", "file_id": "file_test"}):
            with self.subTest(part=part), self.assertRaises(rt.ResponsesTranslationError) as raised:
                rt.to_chat_completions({"input": [{"role": "user", "content": [
                    {"type": "input_text", "text": "describe"}, part]}]})
            self.assertEqual(raised.exception.param, "input[0].content[1]")
            self.assertEqual(raised.exception.code, "unsupported_operation")

    def test_custom_tools_and_custom_outputs_are_rejected(self):
        for tool_format in ({"type": "text"}, {"type": "grammar", "syntax": "lark", "definition": 'start: "hi"'}):
            with self.subTest(format=tool_format), self.assertRaises(rt.ResponsesTranslationError) as raised:
                rt.to_chat_completions({"input": "hi", "tools": [
                    {"type": "custom", "name": "custom", "format": tool_format}]})
            self.assertEqual(raised.exception.param, "tools[0].type")
        with self.assertRaises(rt.ResponsesTranslationError) as raised:
            rt.to_chat_completions({"input": [{"type": "custom_tool_call_output", "call_id": "call_test", "output": "hi"}]})
        self.assertEqual(raised.exception.param, "input[0].type")

    def test_buffered_refusal_is_response_content(self):
        out = rt.from_chat_completions({"choices": [{"message": {"content": None, "refusal": "Cannot help."},
                                                     "finish_reason": "stop"}]},
                                      response_id="resp_test", created_at=123, original_request={})
        self.assertEqual(out["output"][0]["content"], [{"type": "refusal", "refusal": "Cannot help."}])
        self.assertEqual(out["output_text"], "")

    def test_stream_refusal_content_indexes_and_lifecycle(self):
        for text_first in (False, True):
            with self.subTest(text_first=text_first):
                tr = self.translator()
                frames = tr.start()
                if text_first:
                    frames += tr.feed(self.wire({"content": "Intro."}))
                frames += tr.feed(self.wire({"refusal": "Cannot "}))
                frames += tr.feed(self.wire({"refusal": "help."}, "stop")) + tr.finish()
                payloads = _event_payloads(frames)
                deltas = [p for p in payloads if p["type"] == "response.refusal.delta"]
                self.assertEqual("".join(p["delta"] for p in deltas), "Cannot help.")
                self.assertEqual([p["content_index"] for p in deltas], [int(text_first)] * 2)
                done = next(p for p in payloads if p["type"] == "response.refusal.done")
                self.assertEqual(done["refusal"], "Cannot help.")
                content = payloads[-1]["response"]["output"][0]["content"]
                self.assertEqual(content[int(text_first)], {"type": "refusal", "refusal": "Cannot help."})

    def test_input_item_ids_are_stable_across_reads(self):
        request = {"input": [{"role": "user", "content": "one"}, {"role": "user", "content": "two"}]}
        self.assertEqual(rt.input_items_from_request(request), rt.input_items_from_request(request))

    def test_tools_and_json_schema_map_to_chat_completions_shape(self) -> None:
        out = rt.to_chat_completions(
            {
                "model": "local",
                "input": "extract weather",
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "description": "Fetch weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                        "strict": True,
                    }
                ],
                "tool_choice": {"type": "function", "name": "get_weather"},
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "weather_answer",
                        "schema": {"type": "object", "properties": {"answer": {"type": "string"}}},
                        "strict": True,
                    }
                },
            }
        )

        self.assertEqual(
            out["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Fetch weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                        "strict": True,
                    },
                }
            ],
        )
        self.assertEqual(out["tool_choice"], {"type": "function", "function": {"name": "get_weather"}})
        self.assertEqual(out["response_format"]["type"], "json_schema")
        self.assertEqual(out["response_format"]["json_schema"]["name"], "weather_answer")
        self.assertTrue(out["response_format"]["json_schema"]["strict"])

    def test_function_call_output_shape_from_chat_response(self) -> None:
        out = rt.from_chat_completions(
            {
                "model": "local",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_123",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
            response_id="resp_test",
            created_at=123,
            original_request={"model": "local", "input": "x"},
        )

        self.assertEqual(out["output_text"], "")
        self.assertEqual(out["output"][0]["type"], "function_call")
        self.assertEqual(out["output"][0]["call_id"], "call_123")
        self.assertEqual(out["output"][0]["name"], "lookup")
        self.assertEqual(out["output"][0]["arguments"], "{\"q\":\"x\"}")

    def test_streaming_text_event_sequence_and_final_response(self) -> None:
        tr = rt.StreamTranslator(
            response_id="resp_stream",
            created_at=123,
            original_request={"model": "local", "input": "hi", "stream": True},
        )
        frames = tr.start()
        frames.extend(
            tr.feed(
                b'data: {"id":"chatcmpl","model":"local","choices":[{"index":0,"delta":{"content":"he"}}]}\n\n'
                b'data: {"id":"chatcmpl","model":"local","choices":[{"index":0,"delta":{"content":"llo"},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
                b"data: [DONE]\n\n"
            )
        )
        frames.extend(tr.finish())

        self.assertEqual(
            _event_names(frames),
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.content_part.added",
                "response.output_text.delta",
                "response.output_text.delta",
                "response.output_text.done",
                "response.content_part.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual([p["sequence_number"] for p in _event_payloads(frames)], list(range(1, 11)))
        self.assertEqual(_event_payloads(frames)[-1]["response"]["output_text"], "hello")

    def test_previous_response_chain_replays_input_assistant_call_and_tool_output(self) -> None:
        messages = rt.stored_chain_to_messages(
            [
                {
                    "request_json": {"input": "what should I call?"},
                    "response_json": {
                        "output": [
                            {
                                "type": "function_call",
                                "call_id": "call_lookup",
                                "name": "lookup",
                                "arguments": "{\"q\":\"abc\"}",
                            }
                        ]
                    },
                },
                {
                    "request_json": {
                        "input": [
                            {
                                "type": "function_call_output",
                                "call_id": "call_lookup",
                                "output": {"answer": 42},
                            }
                        ]
                    },
                    "response_json": {
                        "output": [
                            {
                                "type": "message",
                                "content": [{"type": "output_text", "text": "The answer is 42."}],
                            }
                        ]
                    },
                },
            ]
        )

        self.assertEqual(messages[0], {"role": "user", "content": "what should I call?"})
        self.assertEqual(messages[1]["role"], "assistant")
        self.assertEqual(messages[1]["tool_calls"][0]["id"], "call_lookup")
        self.assertEqual(messages[2], {"role": "tool", "tool_call_id": "call_lookup", "content": "{\"answer\": 42}"})
        self.assertEqual(messages[3], {"role": "assistant", "content": "The answer is 42."})

    def test_multimodal_input_image_maps_to_chat_content_parts(self) -> None:
        out = rt.to_chat_completions(
            {
                "model": "vision-local",
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "describe"},
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64,aGVsbG8=",
                                "detail": "low",
                            },
                        ],
                    }
                ],
            }
        )

        self.assertEqual(
            out["messages"],
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,aGVsbG8=",
                                "detail": "low",
                            },
                        },
                    ],
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
