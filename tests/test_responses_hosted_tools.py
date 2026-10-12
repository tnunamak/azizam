"""Optional hosted declarations must not prevent client tool execution."""

import copy

import pytest

import responses_translator as rt
from test_responses_namespace_tools import CUSTOM, FUNCTION, buffered, namespace, streamed


@pytest.mark.parametrize("kind", [
    "web_search", "web_search_preview",
    "web_search_2025_06_24", "web_search_2025_08_25",
    "file_search", "file_search_preview",
    "code_interpreter", "code_interpreter_preview",
    "computer_use", "computer_use_preview",
    "image_generation", "mcp", "shell", "local_shell", "apply_patch",
])
def test_pinned_hosted_tool_contract_does_not_shrink_with_implementation(kind):
    req = {"input": "hi", "tools": [{"type": kind}]}
    mapping = rt.ToolMapping(req)
    assert mapping.omitted_tools == [kind]
    assert "tools" not in rt.to_chat_completions(req, tool_mapping=mapping)
    for choice in ({"type": kind}, {"type": "function", "name": kind}, "required"):
        with pytest.raises(rt.ResponsesTranslationError) as error:
            rt.to_chat_completions({**req, "tool_choice": choice})
        assert error.value.code == "hosted_tool_not_supported"
        assert error.value.param == "tool_choice"


def test_mixed_client_tools_round_trip_with_optional_web_search():
    req = {"input": "hi", "tools": [FUNCTION, CUSTOM, namespace(), {"type": "web_search"}],
           "tool_choice": "required"}
    original = copy.deepcopy(req)
    mapping = rt.ToolMapping(req)
    cc = rt.to_chat_completions(req, tool_mapping=mapping)
    assert mapping.omitted_tools == ["web_search"]
    assert len(cc["tools"]) == 3
    assert cc["tool_choice"] == "required"
    for tool, kind, ns, args in zip(cc["tools"], ["function_call", "custom_tool_call", "function_call"],
                                   [None, None, "azizam"], ['{"text":"hi"}', '{"input":"patch"}', '{"text":"hi"}']):
        name = tool["function"]["name"]
        item = buffered(req, name, args, mapping)["output"][0]
        assert item["type"] == kind and item.get("namespace") == ns
        frames = streamed(req, name, args, mapping)
        streamed_item = frames[-1]["response"]["output"][0]
        assert {k: v for k, v in streamed_item.items() if k != "id"} == {k: v for k, v in item.items() if k != "id"}
        result = {"type": "custom_tool_call_output" if kind == "custom_tool_call" else "function_call_output",
                  "call_id": item["call_id"], "output": "ok"}
        continuation = rt.to_chat_completions({**req, "input": [item, result]})
        assert continuation["messages"][0]["tool_calls"][0]["function"]["name"] == name
        assert continuation["messages"][1]["content"] == "ok"
    assert req == original


def test_forced_client_tool_named_like_a_hosted_tool_is_preserved():
    req = {"tools": [{**FUNCTION, "name": "web_search"}, {"type": "web_search"}],
           "tool_choice": {"type": "function", "name": "web_search"}}
    cc = rt.to_chat_completions(req)
    assert cc["tools"][0]["function"]["name"] == "web_search"
    assert cc["tool_choice"]["function"]["name"] == "web_search"


def test_named_hosted_tool_choice_is_rejected():
    with pytest.raises(rt.ResponsesTranslationError) as error:
        rt.to_chat_completions({"tools": [{"type": "mcp", "name": "remote"}],
                                "tool_choice": {"type": "function", "name": "remote"}})
    assert error.value.code == "hosted_tool_not_supported"
