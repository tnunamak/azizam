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
