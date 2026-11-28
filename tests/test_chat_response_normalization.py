from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import proxy
from gateway.transforms import normalize_chat_completion_payload


class ChatResponseNormalizationTests(unittest.TestCase):
    def test_message_reasoning_content_does_not_fill_empty_content(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "visible reply",
                    }
                }
            ]
        }

        changed = normalize_chat_completion_payload(payload)

        self.assertFalse(changed)
        message = payload["choices"][0]["message"]
        self.assertEqual(message["content"], "")
        self.assertEqual(message["reasoning_content"], "visible reply")

    def test_think_tags_split_reasoning_from_visible_content(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "<think>private reasoning</think>\nvisible reply",
                    }
                }
            ]
        }

        changed = normalize_chat_completion_payload(payload)

        self.assertTrue(changed)
        message = payload["choices"][0]["message"]
        self.assertEqual(message["content"], "visible reply")
        self.assertEqual(message["reasoning_content"], "private reasoning")

    def test_existing_content_is_preserved(self) -> None:
        payload = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "already visible",
                        "reasoning_content": "internal",
                    }
                }
            ]
        }

        changed = normalize_chat_completion_payload(payload)

        self.assertFalse(changed)
        self.assertEqual(payload["choices"][0]["message"]["content"], "already visible")

    def test_streaming_delta_reasoning_content_stays_private(self) -> None:
        event = {
            "choices": [
                {
                    "delta": {
                        "reasoning_content": "visible token",
                    }
                }
            ]
        }
        chunk = f"data: {json.dumps(event)}\n\n".encode("utf-8")

        normalized = proxy._normalize_chat_completion_sse_chunk(chunk)

        line = normalized.decode("utf-8").splitlines()[0]
        payload = json.loads(line.removeprefix("data: "))
        self.assertEqual(payload["choices"][0]["delta"], {"reasoning_content": "visible token"})


if __name__ == "__main__":
    unittest.main()
