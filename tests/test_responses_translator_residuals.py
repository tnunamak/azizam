import json
import unittest

import responses_translator as rt


class ResponsesTranslatorResidualTests(unittest.TestCase):
    def test_rsp01_structured_tool_outputs_reject_unsupported_attachments(self):
        for part in (
            {"type": "input_file", "file_id": "file_test"},
            {"type": "input_file", "file_url": "https://example.test/test.pdf"},
            {"type": "input_file", "file_data": "data:application/pdf;base64,AA=="},
            *({"type": kind, "file_id": "file_test"} for kind in ("input_image", "image_url", "image")),
        ):
            with self.subTest(part=part):
                request = {"input": [{"type": "function_call_output", "call_id": "call_test",
                                      "output": [{"type": "input_text", "text": "ok"}, part]}]}
                with self.assertRaises(rt.ResponsesTranslationError) as raised:
                    rt.to_chat_completions(request)
                self.assertEqual(raised.exception.code, "unsupported_operation")
                self.assertEqual(raised.exception.param, "input[0].output[1]")
        output = [{"type": "input_text", "text": "ok"}, {"result": 42}]
        translated = rt.to_chat_completions({"input": [
            {"type": "function_call_output", "call_id": "call_test", "output": output}]})
        self.assertEqual(json.loads(translated["messages"][0]["content"]), output)

    def test_rsp02_input_ids_reject_duplicates_empty_and_non_strings(self):
        for bad_id in ("item_first", "", None, 7, [], {}):
            with self.subTest(bad_id=bad_id):
                request = {"input": [
                    {"id": "item_first", "role": "user", "content": "one"},
                    {"id": bad_id, "role": "user", "content": "two"}]}
                with self.assertRaises(rt.ResponsesTranslationError) as raised:
                    rt.to_chat_completions(request)
                self.assertEqual(raised.exception.code, "invalid_request_error")
                self.assertEqual(raised.exception.param, "input[1].id")
        request = {"input": [{"id": "item_first", "role": "user", "content": "one"},
                             {"role": "user", "content": "two"}]}
        rt.to_chat_completions(request)
        items = rt.input_items_from_request(request, response_id="resp_test")
        self.assertEqual(items, rt.input_items_from_request(request, response_id="resp_test"))
        self.assertEqual(items[0]["id"], "item_first")
        self.assertEqual(len({item["id"] for item in items}), 2)

    def test_rsp03_malformed_coalesced_frame_preserves_events_and_lifecycles(self):
        def wire(frame):
            return ("data: " + json.dumps(frame) + "\n\n").encode()

        malformed = [
            {"choices": [{"delta": "bad"}]},
            {"choices": {}},
            {"choices": [None]},
            {"choices": [{"delta": {"content": "discard", "tool_calls": [
                {"index": 0, "function": {"name": "echo", "arguments": "{}"}},
                {"index": 1, "function": "bad"}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": [], "function": {}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"function": {"arguments": {}}}]}}]},
            {"usage": {"prompt_tokens": "bad"}, "choices": []},
        ]
        valid = {"choices": [{"delta": {"reasoning_content": "think", "content": "kept",
                 "tool_calls": [{"index": 0, "id": "call_test",
                                 "function": {"name": "echo", "arguments": "{}"}}]}}]}
        trailing = {"choices": [{"delta": {"content": "ignored"}, "finish_reason": "stop"}]}
        for bad in malformed:
            with self.subTest(frame=bad):
                tr = rt.StreamTranslator(response_id="resp_test", created_at=123, original_request={})
                frames = tr.start()
                frames += tr.feed(wire(valid) + wire(bad) + wire(trailing))
                frames += tr.finish()
                payloads = [json.loads(line[6:]) for frame in frames
                            for line in frame.decode().splitlines() if line.startswith("data: ")]
                types = [event["type"] for event in payloads]
                self.assertEqual([event["sequence_number"] for event in payloads],
                                 list(range(1, len(payloads) + 1)))
                self.assertEqual(types.count("error"), 1)
                self.assertEqual(types.count("response.failed"), 1)
                self.assertNotIn("response.completed", types)
                self.assertEqual(payloads[-1]["response"]["output_text"], "kept")
                self.assertEqual([event["delta"] for event in payloads
                                  if event["type"] == "response.output_text.delta"], ["kept"])
                added, done = [], []
                for event in payloads:
                    if event["type"] == "response.output_item.added":
                        added.append(event["item"]["id"])
                    elif event["type"] == "response.output_item.done":
                        self.assertIn(event["item"]["id"], added)
                        done.append(event["item"]["id"])
                self.assertCountEqual(added, done)
                self.assertEqual(len(added), 3)
                self.assertEqual(tr.feed(wire(valid)), [])
                self.assertEqual(tr.finish(), [])
                self.assertEqual(tr.terminal_event(), [])
