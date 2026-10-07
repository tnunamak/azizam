"""Offline round trips for Responses freeform tools over chat function calls."""

import json

import pytest

import responses_translator as rt


PATCH_INPUT = '*** Begin Patch\n*** Add File: hello.txt\n+"سلام 🌍" \\ path\n*** End Patch\n'
CUSTOM_TOOL = {"type": "custom", "name": "apply_patch", "description": "Edit files.", "format": {"type": "text"}}
FUNCTION_TOOL = {"type": "function", "name": "shell", "parameters": {"type": "object"}}


def call(name, arguments, index=0):
    return {"index": index, "id": f"call_{index}", "type": "function",
            "function": {"name": name, "arguments": arguments}}


def wire(calls=None, finish=None):
    return ("data: " + json.dumps({"choices": [{"delta": {"tool_calls": calls or []},
                                                "finish_reason": finish}]}, ensure_ascii=False) + "\n\n").encode()


def events(frames):
    return [json.loads(frame.decode().split("data: ", 1)[1]) for frame in frames]


def buffered(req, calls):
    return rt.from_chat_completions({"choices": [{"message": {"tool_calls": calls}, "finish_reason": "tool_calls"}]},
                                    response_id="resp_test", created_at=123, original_request=req)


@pytest.mark.parametrize("ensure_ascii", [False, True])
def test_multiline_patch_round_trip_buffered_and_streaming(ensure_ascii):
    req = {"input": "Create hello.txt", "tools": [CUSTOM_TOOL],
           "tool_choice": {"type": "custom", "name": "apply_patch"}}
    cc = rt.to_chat_completions(req)
    assert cc["tool_choice"] == {"type": "function", "function": {"name": "apply_patch"}}
    assert cc["tools"][0]["function"]["parameters"] == {
        "type": "object", "properties": {"input": {"type": "string"}},
        "required": ["input"], "additionalProperties": False,
    }
    arguments = json.dumps({"input": PATCH_INPUT}, ensure_ascii=ensure_ascii)
    expected = buffered(req, [call("apply_patch", arguments)])["output"][0]
    assert expected["type"] == "custom_tool_call"
    assert expected["input"] == PATCH_INPUT
    assert "arguments" not in expected
    # Split the actual chat arguments at every character, including JSON escapes.
    tr = rt.StreamTranslator(response_id="resp_test", created_at=123, original_request=req)
    frames = tr.start() + tr.feed(wire([call("apply_patch", "")]))
    for character in arguments:
        frames += tr.feed(wire([{"index": 0, "function": {"arguments": character}}]))
    frames += tr.feed(wire(finish="tool_calls")) + tr.finish()
    payloads = events(frames)
    assert [p["sequence_number"] for p in payloads] == list(range(1, len(payloads) + 1))
    assert "response.function_call_arguments.delta" not in [p["type"] for p in payloads]
    delta = next(p for p in payloads if p["type"] == "response.custom_tool_call_input.delta")
    done = next(p for p in payloads if p["type"] == "response.custom_tool_call_input.done")
    added = next(p for p in payloads if p["type"] == "response.output_item.added")
    assert delta["delta"] == done["input"] == PATCH_INPUT
    assert delta["item_id"] == done["item_id"] == added["item"]["id"]
    assert delta["output_index"] == done["output_index"] == added["output_index"] == 0
    final = payloads[-1]["response"]["output"][0]
    assert {k: v for k, v in final.items() if k != "id"} == {k: v for k, v in expected.items() if k != "id"}
    replayed = rt.to_chat_completions({"input": [final]})["messages"][0]["tool_calls"][0]
    assert json.loads(replayed["function"]["arguments"])["input"] == PATCH_INPUT


def test_mixed_tools_and_fragmented_names_remain_distinct():
    req = {"input": "Edit and run", "tools": [FUNCTION_TOOL, CUSTOM_TOOL]}
    cc = rt.to_chat_completions(req)
    assert len(cc["tools"]) == 2
    assert cc["tools"][0]["function"]["parameters"] == FUNCTION_TOOL["parameters"]
    args = json.dumps({"input": PATCH_INPUT})
    tr = rt.StreamTranslator(response_id="resp_test", created_at=123, original_request=req)
    frames = tr.start() + tr.feed(wire([call("apply_", args[:8]), call("shell", '{"command":', 1)]))
    frames += tr.feed(wire([{"index": 0, "function": {"name": "patch", "arguments": args[8:]}},
                            {"index": 1, "function": {"arguments": '"ls"}'}}], "tool_calls")) + tr.finish()
    payloads = events(frames)
    output = payloads[-1]["response"]["output"]
    assert [o["type"] for o in output] == ["custom_tool_call", "function_call"]
    assert output[0]["input"] == PATCH_INPUT
    assert output[1]["arguments"] == '{"command":"ls"}'
    buf = buffered(req, [call("apply_patch", args), call("shell", '{"command":"ls"}', 1)])
    assert [{k: v for k, v in i.items() if k != "id"} for i in buf["output"]] == [
        {k: v for k, v in i.items() if k != "id"} for i in output]
    assert [p["output_index"] for p in payloads if p["type"].endswith(".delta")] == [0, 1]
    other = buffered({"tools": [FUNCTION_TOOL]}, [call("apply_patch", args)])["output"][0]
    assert other["type"] == "function_call"  # identity cannot leak between requests


def test_continuation_replays_custom_call_and_output_from_stored_chain():
    req = {"input": "Edit", "tools": [CUSTOM_TOOL]}
    response = buffered(req, [call("apply_patch", json.dumps({"input": PATCH_INPUT}))])
    result = {"type": "custom_tool_call_output", "call_id": "call_0", "output": "Success. سلام"}
    chain = rt.stored_chain_to_messages([{"request_json": req, "response_json": response}])
    cc = rt.to_chat_completions({"input": [result]}, prior_messages=chain)
    assert [m["role"] for m in cc["messages"]] == ["user", "assistant", "tool"]
    assert json.loads(cc["messages"][1]["tool_calls"][0]["function"]["arguments"])["input"] == PATCH_INPUT
    assert cc["messages"][-1] == {"role": "tool", "tool_call_id": "call_0", "content": "Success. سلام"}


@pytest.mark.parametrize("syntax,definition", [("lark", 'start: "hello"'), ("regex", "^hello$")])
def test_grammar_is_advisory_description_and_echoed_without_mutation(syntax, definition):
    tool = {**CUSTOM_TOOL, "format": {"type": "grammar", "syntax": syntax, "definition": definition}}
    req = {"input": "Edit", "tools": [tool]}
    cc = rt.to_chat_completions(req)
    description = cc["tools"][0]["function"]["description"]
    assert syntax in description and definition in description and "advisory" in description
    assert "grammar" not in cc  # no global constrained decoding for mixed tool arguments
    result = buffered(req, [call("apply_patch", json.dumps({"input": PATCH_INPUT}))])
    assert result["tools"] == [tool]
    assert result["output"][0]["input"] == PATCH_INPUT


@pytest.mark.parametrize("arguments", ["not json", '{}', '{"input":42}', '{"input":"ok","extra":true}'])
def test_malformed_custom_wrapper_is_not_delivered_as_a_successful_call(arguments):
    req = {"input": "Edit", "tools": [CUSTOM_TOOL]}
    with pytest.raises(ValueError, match="Upstream custom tool arguments"):
        buffered(req, [call("apply_patch", arguments)])
    tr = rt.StreamTranslator(response_id="resp_test", created_at=123, original_request=req)
    payloads = events(tr.start() + tr.feed(wire([call("apply_patch", arguments)], "tool_calls")) + tr.finish())
    assert payloads[-1]["type"] == "response.failed"
    assert payloads[-1]["response"]["output"] == []
    assert not any(p["type"].startswith("response.custom_tool_call_input") for p in payloads)


def test_empty_custom_input_is_preserved():
    req = {"input": "Edit", "tools": [CUSTOM_TOOL]}
    assert buffered(req, [call("apply_patch", '{"input":""}')])["output"][0]["input"] == ""
    tr = rt.StreamTranslator(response_id="resp_test", created_at=123, original_request=req)
    payloads = events(tr.start() + tr.feed(wire([call("apply_patch", '{"input":""}')], "tool_calls")) + tr.finish())
    assert next(p for p in payloads if p["type"] == "response.custom_tool_call_input.done")["input"] == ""
    assert payloads[-1]["response"]["output"][0]["input"] == ""


def test_persistence_failure_retains_already_emitted_custom_calls():
    req = {"input": "Edit", "tools": [CUSTOM_TOOL]}
    tr = rt.StreamTranslator(response_id="resp_test", created_at=123, original_request=req)
    frames = tr.start() + tr.feed(wire([call("apply_patch", json.dumps({"input": PATCH_INPUT}))], "tool_calls"))
    frames += tr.finish(emit_terminal=False)
    frames += tr.fail("Azizam could not persist the response.") + tr.terminal_event()
    payloads = events(frames)
    assert payloads[-1]["type"] == "response.failed"
    assert payloads[-1]["response"]["output"][0]["input"] == PATCH_INPUT
    assert next(p for p in payloads if p["type"] == "response.output_item.done")["item"] == payloads[-1]["response"]["output"][0]


def test_text_after_deferred_custom_call_has_no_missing_output_slot():
    req = {"input": "Edit", "tools": [CUSTOM_TOOL]}
    tr = rt.StreamTranslator(response_id="resp_test", created_at=123, original_request=req)
    frames = tr.start() + tr.feed(wire([call("apply_patch", json.dumps({"input": PATCH_INPUT}))]))
    frames += tr.feed(('data: ' + json.dumps({"choices": [{"delta": {"content": "Edited."},
                                                          "finish_reason": "tool_calls"}]}) + '\n\n').encode())
    frames += tr.finish()
    # Mirror the SDK accumulator: append added items, then directly index deltas.
    output = []
    for payload in events(frames):
        if payload["type"] == "response.output_item.added":
            assert payload["output_index"] == len(output)
            output.append(payload["item"])
        elif "output_index" in payload:
            assert payload["output_index"] < len(output)
    assert [i["type"] for i in output] == ["message", "custom_tool_call"]
    assert tr.build_final_response()["output"][1]["input"] == PATCH_INPUT


@pytest.mark.parametrize("fields,param", [
    ({"name": ""}, "tools[0].name"),
    ({"format": None}, "tools[0].format"),
    ({"format": {"type": "grammar", "syntax": "unknown", "definition": "text"}}, "tools[0].format"),
    ({"format": {"type": "grammar", "syntax": "lark"}}, "tools[0].format"),
])
def test_invalid_custom_declarations_have_parameter_errors(fields, param):
    with pytest.raises(rt.ResponsesTranslationError) as raised:
        rt.to_chat_completions({"input": "Edit", "tools": [{**CUSTOM_TOOL, **fields}]})
    assert raised.value.param == param


def test_duplicate_tool_names_are_rejected_before_losing_type_identity():
    with pytest.raises(rt.ResponsesTranslationError, match="unique"):
        rt.to_chat_completions({"input": "Edit", "tools": [CUSTOM_TOOL, {**FUNCTION_TOOL, "name": "apply_patch"}]})


def test_custom_output_retains_file_input_rejection():
    with pytest.raises(rt.ResponsesTranslationError) as raised:
        rt.to_chat_completions({"input": [{"type": "custom_tool_call_output", "call_id": "call_0",
                                          "output": [{"type": "input_file", "file_id": "file_test"}]}]})
    assert raised.value.param == "input[0].output[0]"
