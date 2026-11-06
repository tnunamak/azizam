import copy
import json

import pytest

import responses_translator as rt


def wire(delta=None, finish=None, usage=None):
    body = {"choices": [{"index": 0, "delta": delta or {},
                         "finish_reason": finish}]}
    if usage is not None:
        body["usage"] = usage
    return ("data: " + json.dumps(body, ensure_ascii=False) + "\n\n").encode()


def events(frames):
    return [json.loads(line[6:])
            for frame in frames
            for line in frame.decode().splitlines()
            if line.startswith("data: ")]


def translator(request=None):
    return rt.StreamTranslator(
        response_id="resp_gap", created_at=123,
        original_request=request or {"input": "hello"},
    )


# T1: currently source-predicted RED.
# Kills retaining only the first nonempty plain-function name fragment.
def test_plain_function_fragmented_name_matches_buffered_result():
    request = {"input": "weather", "tools": [
        {"type": "function", "name": "get_weather",
         "parameters": {"type": "object"}}
    ]}
    tr = translator(request)
    frames = tr.start()
    frames += tr.feed(wire({"tool_calls": [{
        "index": 0, "id": "call_weather",
        "function": {"name": "get_", "arguments": '{"city":'}
    }]}))
    frames += tr.feed(wire({"tool_calls": [{
        "index": 0,
        "function": {"name": "weather", "arguments": '"Oslo"}'}
    }]}, "tool_calls"))
    frames += tr.finish()
    payloads = events(frames)
    streamed = payloads[-1]["response"]["output"]
    buffered = rt.from_chat_completions(
        {"choices": [{"message": {"tool_calls": [{
            "id": "call_weather", "type": "function",
            "function": {"name": "get_weather",
                         "arguments": '{"city":"Oslo"}'}
        }]}, "finish_reason": "tool_calls"}]},
        response_id="resp_gap", created_at=123, original_request=request,
    )["output"]
    strip_id = lambda item: {k: v for k, v in item.items() if k != "id"}
    assert [strip_id(x) for x in streamed] == [strip_id(x) for x in buffered]
    done = [p["item"] for p in payloads
            if p["type"] == "response.output_item.done"]
    assert done == streamed
    assert streamed[0]["name"] == "get_weather"


# T2: currently source-predicted RED.
# Requires all accepted reasoning bytes, no deltas after closure, and matching
# done/final snapshots, without prescribing buffer-versus-new-item strategy.
def test_interleaved_reasoning_does_not_reopen_a_closed_item():
    tr = translator()
    frames = tr.start()
    frames += tr.feed(wire({"reasoning_content": "first"}))
    frames += tr.feed(wire({"content": "answer"}))
    frames += tr.feed(wire({"reasoning_content": "second"}, "stop"))
    frames += tr.finish()
    payloads = events(frames)
    closed = set()
    done = {}
    for event in payloads:
        if event["type"].endswith(".delta") and "item_id" in event:
            assert event["item_id"] not in closed
        if event["type"] == "response.output_item.done":
            assert event["item"]["id"] not in closed
            closed.add(event["item"]["id"])
            done[event["item"]["id"]] = event["item"]
    final = payloads[-1]["response"]
    assert final["status"] == "completed"
    assert final["output_text"] == "answer"
    assert "".join(part["text"] for item in final["output"]
                   if item["type"] == "reasoning"
                   for part in item["content"]) == "firstsecond"
    assert {item["id"]: item for item in final["output"]} == done


