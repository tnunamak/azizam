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


# T3: strengthens status/usage parity, not an additional routing test.
# Kills dropping post-finish usage, dropping details, and mapping content_filter
# to completed or max_output_tokens.
def test_content_filter_and_late_usage_match_buffered_translation():
    usage = {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12,
             "prompt_tokens_details": {"cached_tokens": 3},
             "completion_tokens_details": {"reasoning_tokens": 4}}
    tr = translator()
    frames = tr.start() + tr.feed(wire({"content": "partial"}, "content_filter"))
    frames += tr.feed(("data: " + json.dumps(
        {"choices": [], "usage": usage}) + "\n\n").encode())
    frames += tr.feed(b"data: [DONE]\n\n") + tr.finish()
    payloads = events(frames)
    final = payloads[-1]["response"]
    other = rt.from_chat_completions(
        {"choices": [{"message": {"content": "partial"},
                      "finish_reason": "content_filter"}], "usage": usage},
        response_id="resp_gap", created_at=123,
        original_request={"input": "hello"},
    )
    assert payloads[-1]["type"] == "response.incomplete"
    for key in ("status", "incomplete_details", "usage", "output_text"):
        assert final[key] == other[key]
    assert final["incomplete_details"] == {"reason": "content_filter"}
    assert final["usage"] == {
        "input_tokens": 5,
        "input_tokens_details": {"cached_tokens": 3, "cache_write_tokens": 0},
        "output_tokens": 7,
        "output_tokens_details": {"reasoning_tokens": 4},
        "total_tokens": 12,
    }


# T4: kills suppressing the incremental decoder's EOF failure and duplicate
# terminal/error emissions; a valid finish_reason must not hide corrupt bytes.
def test_incomplete_utf8_at_eof_fails_once_and_keeps_complete_prefix():
    tr = translator()
    frames = tr.start() + tr.feed(wire({"content": "kept"}, "stop"))
    frames += tr.feed(b"\xe2\x82")
    frames += tr.finish()
    payloads = events(frames)
    assert [p["type"] for p in payloads].count("error") == 1
    assert [p["type"] for p in payloads].count("response.failed") == 1
    assert not any(p["type"] == "response.completed" for p in payloads)
    final = payloads[-1]["response"]
    assert final["output_text"] == "kept"
    assert "UTF-8" in final["error"]["message"]
    assert tr.finish() == tr.terminal_event() == []
    assert tr.feed(wire({"content": "ignored"}, "stop")) == []


# T5: kills stale instruction replay, shallow-copy mutation, token-precedence
# reversal, dropping zero-valued sampling, and losing no-think hints.
@pytest.mark.parametrize("effort", ["none", "minimal"])
def test_request_and_history_are_preserved_while_new_controls_take_effect(effort):
    previous = [{"role": "system", "content": "stale"},
                {"role": "user", "content": "earlier"}]
    request = {"input": "next", "instructions": "current",
               "reasoning": {"effort": effort},
               "max_output_tokens": 9, "max_completion_tokens": 99,
               "temperature": 0, "top_p": 0, "parallel_tool_calls": False,
               "stream": True}
    saved = copy.deepcopy((request, previous))
    result = rt.to_chat_completions(request, prior_messages=previous)
    assert result["messages"] == [
        {"role": "system", "content": "current\n/no_think"},
        {"role": "user", "content": "earlier"},
        {"role": "user", "content": "next"},
    ]
    assert result["chat_template_kwargs"]["enable_thinking"] is False
    assert result["max_tokens"] == 9
    assert result["temperature"] == result["top_p"] == 0
    assert result["parallel_tool_calls"] is False
    assert result["stream_options"] == {"include_usage": True}
    assert (request, previous) == saved
    replay = rt.stored_chain_to_messages([{
        "request_json": {"instructions": "must not return", "input": "old"},
        "response_json": {"output": [{"type": "message",
            "content": [{"type": "output_text", "text": "reply"}]}]},
    }])
    assert replay == [{"role": "user", "content": "old"},
                      {"role": "assistant", "content": "reply"}]
