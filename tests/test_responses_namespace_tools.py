"""Codex namespace and client discovery contracts, pinned to b17c74cf."""

import copy
import json
import re

import pytest

import responses_translator as rt


FUNCTION = {"type": "function", "name": "echo", "description": "Echo text.", "strict": False,
            "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}
CUSTOM = {"type": "custom", "name": "apply_patch", "format": {"type": "text"}}
SEARCH = {"type": "tool_search", "execution": "client", "description": "Discover tools.",
          "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}


def namespace(name="azizam", tools=None):
    return {"type": "namespace", "name": name, "description": "Azizam test tools.",
            "tools": tools if tools is not None else [FUNCTION]}


def call(name, args='{"text":"hello"}'):
    return {"id": "call_1", "type": "function", "function": {"name": name, "arguments": args}}


def buffered(req, name, args='{"text":"hello"}', mapping=None):
    return rt.from_chat_completions(
        {"choices": [{"message": {"tool_calls": [call(name, args)]}, "finish_reason": "tool_calls"}]},
        response_id="resp_ns", created_at=123, original_request=req, tool_mapping=mapping)


def streamed(req, name, args='{"text":"hello"}', mapping=None):
    tr = rt.StreamTranslator(response_id="resp_ns", created_at=123, original_request=req, tool_mapping=mapping)
    frames = tr.start()
    # Names as well as JSON escapes can arrive in fragments.
    for index, fragment in enumerate((name[:5], name[5:])):
        tool = {"index": 0, "function": {"name": fragment, "arguments": args if index else ""}}
        if index == 0:
            tool["id"] = "call_1"
        frames += tr.feed(("data: " + json.dumps({"choices": [{"delta": {"tool_calls": [tool]}}]}) + "\n\n").encode())
    frames += tr.feed(b'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}\n\n') + tr.finish()
    return [json.loads(frame.decode().split("data: ", 1)[1]) for frame in frames]


def test_namespace_round_trip_preserves_schema_description_choice_and_identity():
    req = {"input": "Echo hello.", "tools": [namespace()],
           "tool_choice": {"type": "function", "namespace": "azizam", "name": "echo"}}
    original = copy.deepcopy(req)
    cc = rt.to_chat_completions(req)
    fn = cc["tools"][0]["function"]
    assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", fn["name"])
    assert fn["parameters"] == FUNCTION["parameters"] and fn["strict"] is False
    assert "Azizam test tools." in fn["description"] and "Echo text." in fn["description"]
    assert cc["tool_choice"]["function"]["name"] == fn["name"]
    response = buffered(req, fn["name"])
    item = response["output"][0]
    assert item["type"] == "function_call" and item["namespace"] == "azizam" and item["name"] == "echo"
    assert item["arguments"] == '{"text":"hello"}' and item["call_id"] == "call_1"
    payloads = streamed(req, fn["name"])
    for event in payloads:
        if event["type"] in ("response.output_item.added", "response.output_item.done"):
            assert event["item"]["namespace"] == "azizam" and event["item"]["name"] == "echo"
    assert [p["sequence_number"] for p in payloads] == list(range(1, len(payloads) + 1))
    assert payloads[-1]["response"]["output"][0]["namespace"] == "azizam"
    result = {"type": "function_call_output", "call_id": "call_1", "output": "hello"}
    replay = rt.to_chat_completions({**req, "input": [item, result]})
    assert replay["messages"][0]["tool_calls"][0]["function"] == {"name": fn["name"], "arguments": item["arguments"]}
    assert replay["messages"][1] == {"role": "tool", "tool_call_id": "call_1", "content": "hello"}
    assert req == original  # metadata never mutates or leaks from the caller


def test_collision_with_flat_custom_and_flat_alias_is_resolved():
    ns = namespace(tools=[CUSTOM])
    first_alias = rt.to_chat_completions({"tools": [ns]})["tools"][0]["function"]["name"]
    flat = {**FUNCTION, "name": first_alias}
    req = {"tools": [ns, CUSTOM, flat, namespace("azizam_other", [CUSTOM])]}
    cc = rt.to_chat_completions(req)
    names = [t["function"]["name"] for t in cc["tools"]]
    assert len(set(names)) == len(names)
    assert names[0] != first_alias and names[1] == "apply_patch" and names[2] == first_alias
    for name, expected_namespace in ((names[0], "azizam"), (names[1], None), (names[3], "azizam_other")):
        item = buffered(req, name, '{"input":"*** Begin Patch\\n*** End Patch"}')["output"][0]
        assert item["type"] == "custom_tool_call" and item["name"] == "apply_patch"
        assert item.get("namespace") == expected_namespace
    assert rt.to_chat_completions({"tools": list(reversed(req["tools"]))})["tools"] == list(reversed(cc["tools"]))


def test_long_unicode_and_ambiguous_delimiters_have_distinct_bounded_names():
    tools = [namespace("azizam" * 60, [{**FUNCTION, "name": "سلام" * 70}]),
             namespace("azizam_a", [{**FUNCTION, "name": "b"}]),
             namespace("azizam", [{**FUNCTION, "name": "a_b"}])]
    req = {"tools": tools}
    names = [t["function"]["name"] for t in rt.to_chat_completions(req)["tools"]]
    assert len(set(names)) == 3
    for tool, name in zip(tools, names):
        assert re.fullmatch(r"[a-zA-Z0-9_-]{64}", name)
        item = buffered(req, name)["output"][0]
        assert item["namespace"] == tool["name"] and item["name"] == tool["tools"][0]["name"]


def test_stored_history_remaps_when_an_alias_is_later_taken_by_a_flat_tool():
    req = {"input": "Echo", "tools": [namespace()]}
    alias = rt.to_chat_completions(req)["tools"][0]["function"]["name"]
    response = buffered(req, alias)
    prior = rt.stored_chain_to_messages([{"request_json": req, "response_json": response}])
    continuation = {"tools": [namespace(), {**FUNCTION, "name": alias}],
                    "input": [{"type": "function_call_output", "call_id": "call_1", "output": "hello"}]}
    mapping = rt.ToolMapping(continuation, prior)
    cc = rt.to_chat_completions(continuation, prior_messages=prior, tool_mapping=mapping)
    new_alias = cc["tools"][0]["function"]["name"]
    assert new_alias != alias
    assert cc["messages"][1]["tool_calls"][0]["function"]["name"] == new_alias
    assert buffered(continuation, new_alias, mapping=mapping)["output"][0]["namespace"] == "azizam"


def test_client_tool_search_round_trip_loads_discovered_namespaces():
    req = {"tools": [SEARCH], "tool_choice": {"type": "tool_search"}}
    cc = rt.to_chat_completions(req)
    assert cc["tools"][0]["function"]["parameters"] == SEARCH["parameters"]
    assert cc["tool_choice"] == {"type": "function", "function": {"name": "tool_search"}}
    item = buffered(req, "tool_search", '{"query":"echo","limit":1}')["output"][0]
    assert item["type"] == "tool_search_call" and item["execution"] == "client"
    assert item["arguments"] == {"query": "echo", "limit": 1}
    payloads = streamed(req, "tool_search", '{"query":"echo","limit":1}')
    assert payloads[-1]["response"]["output"][0]["arguments"] == item["arguments"]
    assert not any(p["type"].startswith("response.function_call_arguments") for p in payloads)
    discovered = {"type": "tool_search_output", "call_id": "call_1", "execution": "client", "status": "completed", "tools": [namespace()]}
    continuation = {"tools": [SEARCH], "input": [item, discovered]}
    cc = rt.to_chat_completions(continuation)
    assert len(cc["tools"]) == 2
    assert cc["messages"][0]["tool_calls"][0]["function"]["arguments"] == json.dumps(item["arguments"])
    assert cc["messages"][1]["tool_call_id"] == "call_1"
    alias = cc["tools"][1]["function"]["name"]
    assert buffered(continuation, alias)["output"][0]["namespace"] == "azizam"


@pytest.mark.parametrize("execution", ["server", None, "hosted"])
def test_hosted_search_is_rejected_with_a_clear_error(execution):
    with pytest.raises(rt.ResponsesTranslationError, match="hosted search") as error:
        rt.to_chat_completions({"tools": [{**SEARCH, "execution": execution}]})
    assert error.value.code == "hosted_tool_not_supported" and error.value.param == "tools[0].execution"


@pytest.mark.parametrize("tool", [namespace(tools=[FUNCTION, FUNCTION]), namespace(tools=[{"type": "namespace"}]),
                                  {"type": "namespace", "name": "", "tools": []},
                                  {"type": "namespace", "name": "azizam", "tools": {}}])
def test_invalid_namespaces_fail_before_backend_dispatch(tool):
    with pytest.raises(rt.ResponsesTranslationError):
        rt.to_chat_completions({"tools": [tool]})


@pytest.mark.parametrize("arguments", ['[]', 'not json'])
def test_invalid_client_search_arguments_never_emit_an_executable_item(arguments):
    req = {"tools": [SEARCH]}
    with pytest.raises(ValueError, match="tool_search arguments"):
        buffered(req, "tool_search", arguments)
    payloads = streamed(req, "tool_search", arguments)
    assert payloads[-1]["type"] == "response.failed"
    assert payloads[-1]["response"]["output"] == []


def test_discovered_tools_survive_previous_response_id_and_internal_metadata_is_removed():
    first = {"input": "Find echo", "tools": [SEARCH]}
    search_response = buffered(first, "tool_search", '{"query":"echo"}')
    discovery = {"type": "tool_search_output", "call_id": "call_1", "execution": "client", "status": "completed", "tools": [namespace()]}
    second = {"input": [discovery], "tools": [SEARCH]}
    alias = rt.to_chat_completions(second)["tools"][1]["function"]["name"]
    tool_response = buffered(second, alias)
    history = rt.stored_chain_to_messages([
        {"request_json": first, "response_json": search_response},
        {"request_json": second, "response_json": tool_response},
    ])
    req = {"input": [{"type": "function_call_output", "call_id": "call_1", "output": "hello"}], "tools": [SEARCH]}
    cc = rt.to_chat_completions(req, prior_messages=history)
    assert len(cc["tools"]) == 2
    assert cc["tools"][1]["function"]["name"] == alias
    assert "_azizam_" not in json.dumps(cc)
    assert "namespace" not in cc["messages"][-2]["tool_calls"][0]["function"]


def test_namespace_custom_stream_and_replay_without_an_active_declaration():
    req = {"tools": [namespace(tools=[CUSTOM])]}
    alias = rt.to_chat_completions(req)["tools"][0]["function"]["name"]
    payloads = streamed(req, alias, '{"input":"patch"}')
    item = payloads[-1]["response"]["output"][0]
    assert item["type"] == "custom_tool_call" and item["namespace"] == "azizam" and item["input"] == "patch"
    req = {"input": [item]}
    cc = rt.to_chat_completions(req)
    assert cc["messages"][0]["tool_calls"][0]["function"] == {"name": alias, "arguments": '{"input": "patch"}'}
    assert buffered(req, alias, '{"input":"next"}')["output"][0]["type"] == "function_call"


def test_namespace_declaration_errors_keep_the_nested_parameter_path():
    req = {"tools": [namespace(tools=[{**CUSTOM, "format": None}])]}
    with pytest.raises(rt.ResponsesTranslationError) as error:
        rt.to_chat_completions(req)
    assert error.value.param == "tools[0].tools[0].format"


def test_namespaced_choice_requires_a_real_declared_identity():
    with pytest.raises(rt.ResponsesTranslationError, match="declared tool"):
        rt.to_chat_completions({"tools": [namespace()], "tool_choice": {"type": "function", "namespace": "azizam", "name": "missing"}})


@pytest.mark.parametrize("choice,history", [
    ({"type": "function", "namespace": "azizam", "name": "echo"},
     {"type": "function_call", "namespace": "azizam", "name": "echo", "arguments": "{}", "call_id": "call_old"}),
    ({"type": "tool_search"},
     {"type": "tool_search_call", "execution": "client", "arguments": {"query": "echo"}, "call_id": "call_old"}),
])
def test_replayed_identity_is_not_an_active_tool_choice(choice, history):
    with pytest.raises(rt.ResponsesTranslationError):
        rt.to_chat_completions({"input": [history], "tool_choice": choice})


@pytest.mark.parametrize("tool,param", [
    (namespace(tools=[{**FUNCTION, "parameters": []}]), "tools[0].tools[0].parameters"),
    ({**SEARCH, "parameters": []}, "tools[0].parameters"),
    ({**namespace(), "description": []}, "tools[0].description"),
])
def test_invalid_namespace_and_search_schemas_are_rejected(tool, param):
    with pytest.raises(rt.ResponsesTranslationError) as error:
        rt.to_chat_completions({"tools": [tool]})
    assert error.value.param == param


def test_historical_search_without_a_declaration_is_not_new_client_search():
    history = {"type": "tool_search_call", "call_id": "call_old", "execution": "client", "arguments": {"query": "echo"}}
    item = buffered({"input": [history]}, "tool_search", '{"query":"echo"}')["output"][0]
    assert item["type"] == "function_call"
